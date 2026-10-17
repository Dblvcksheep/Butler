"""Builds the whole object graph once; used by the bot, the Celery app and the CLI."""
import json
import logging
from dataclasses import dataclass

from .config import Settings
from .credentials import CredentialStore, Vault
from .db import Db
from .graph.store import GraphStore
from .integrations import DIRECT_SERVICES
from .integrations.email import EmailTool
from .integrations.search import WebSearch
from .integrations.web_fetch import WebFetch
from .llm.router import ModelRouter
from .mcp_client import McpRunner, load_services
from .notify import TelegramNotifier
from .orchestrator import Orchestrator
from .payments.intents import IntentStore, PaymentError
from .payments.paypal import PayPalClient
from .payments.service import PaymentService
from .tasks.store import TaskError, TaskStore
from .tasks.worker import TaskWorker
from .tools.executor import BUILTIN_TOOLS, PAYOUT_TOOL, ToolExecutor
from .tools.store import ToolStore


log = logging.getLogger('butler.runtime')


@dataclass
class Runtime:
    settings: Settings
    db: Db
    creds: CredentialStore
    router: ModelRouter
    orch: Orchestrator
    tasks: TaskStore
    tools: ToolStore
    mcp: McpRunner
    payments: PaymentService
    executor: ToolExecutor
    worker: TaskWorker
    notifier: object
    direct: dict

    def connect_service(self, user_id: str, service: str, force: bool = False) -> dict:
        """Discover (once), grant the service to this user, mirror tools into their graph."""
        if service in DIRECT_SERVICES:                       # direct tools: no discovery, just register + grant + mirror
            for d in self.direct.values():
                if d.spec["service"] == service:
                    self.tools.upsert_tool(**d.spec)
            self.tools.grant(user_id, service)
            tools = self.tools.tools_for_service(service)
            self.orch.memory(user_id).sync_tools(service, tools)
            return {"tools": tools, "hidden": []}
        if service not in self.mcp.services:
            raise ValueError("unknown service. Available: " + ", ".join(self.connectables()))
        hidden: list[str] = []
        if force or not any(t["tool_id"].startswith("mcp.") for t in self.tools.tools_for_service(service)):
            exposed, hidden = self.mcp.discover(user_id, service)
            for t in exposed:
                self.tools.upsert_tool(**t)
            self.tools.drop_service_tools(service, {t["tool_id"] for t in exposed})
        if service == "paypal":
            self.tools.upsert_tool(**PAYOUT_TOOL)
        self.tools.grant(user_id, service)
        tools = self.tools.tools_for_service(service)
        self.orch.memory(user_id).sync_tools(service, tools)
        return {"tools": tools, "hidden": hidden}

    def ensure_builtins(self, user_id: str) -> None:
        """Always-on tools that need no credentials: contacts.* (graph-backed) and web.fetch. Registered, granted,
        and mirrored into the user's graph."""
        for t in list(BUILTIN_TOOLS) + [self.direct["web.fetch"].spec]:
            self.tools.upsert_tool(**t)
        mem = self.orch.memory(user_id)
        for svc in ("contacts", "web"):
            self.tools.grant(user_id, svc)
            mem.sync_tools(svc, self.tools.tools_for_service(svc))

    def connectables(self) -> dict[str, tuple[str, str | None]]:
        """Everything /connect accepts: name -> (label, secret name or None)."""
        out = {n: (s.label, s.secret_service) for n, s in self.mcp.services.items()}
        out.update({n: (v["label"], v["secret"]) for n, v in DIRECT_SERVICES.items()})
        return out

    def create_task(self, user_id: str, steps: list, schedule: str, tz: str = "UTC",
                    objective: str | None = None, scope: str | None = None) -> dict:
        """Create the task (SQL) and register it in the graph: ownership, plan, tool usage, dependencies."""
        t = self.tasks.create(user_id, steps, schedule, tz, objective=objective, scope=scope)
        mem = self.orch.memory(user_id)
        try:
            mem.record_task(t["id"], t["objective"], f"{t['sched_kind']} {t['sched_spec']} {tz}")
            mem.record_plan(t["id"], json.loads(t["steps"]))
            if t["sched_kind"] == "after":
                for parent in t["sched_spec"].split(","):
                    mem.record_dependency(t["id"], parent)
        except Exception:
            if t["sched_kind"] == "after" and mem.enabled:       # dependencies unreadable => don't run it blind
                self.tasks.set_status(t["id"], user_id, "cancelled")
                raise TaskError("couldn't register this task's dependencies in the graph; not scheduled")
            log.exception("graph registration failed for task %s", t["id"])
        return t

    def disconnect_service(self, user_id: str, service: str) -> None:
        self.tools.revoke(user_id, service)
        self.orch.memory(user_id).revoke_service(service)
        spec = self.mcp.services.get(service)
        if spec and spec.secret_service:
            self.creds.remove_service(user_id, spec.secret_service)
        if service in DIRECT_SERVICES:
            self.creds.remove_service(user_id, DIRECT_SERVICES[service]["secret"])


def build_runtime(settings: Settings, graph: GraphStore | None = None, notifier=None, db: Db | None = None,
                  services: dict | None = None, llm_factory=None) -> Runtime:
    db = db or Db(settings.database_url or settings.db_path)
    creds = CredentialStore(db, Vault(settings.master_key, settings.old_master_keys))
    if not settings.graph_enabled:
        graph = None                                  # BUTLER_GRAPH=off: no FalkorDB at all
    else:
        graph = graph or GraphStore.connect(settings.falkor_host, settings.falkor_port)
    router = ModelRouter(creds, **({"factory": llm_factory} if llm_factory else {}))
    orch = Orchestrator(graph, router)
    if notifier is None and settings.telegram_token:
        notifier = TelegramNotifier(settings.telegram_token)

    def paypal_for(user_id: str) -> PayPalClient:
        sec = creds.get_service(user_id, "paypal")
        if not sec:
            raise PaymentError("PayPal isn't connected. Use /connect paypal.")
        env = sec.get("env", "sandbox")
        if env != "sandbox" and not settings.allow_live:
            raise PaymentError("live PayPal is disabled")
        return PayPalClient(sec["client_id"], sec["client_secret"], env)

    payments = PaymentService(IntentStore(db), paypal_for, settings)
    tools = ToolStore(db)
    mcp = McpRunner(creds, services or load_services(), allow_live=settings.allow_live)
    direct = {d.spec["tool_id"]: d for d in (WebSearch(creds).tool(), WebFetch().tool(),
                                             EmailTool(creds, db, orch.memory, settings).tool())}
    executor = ToolExecutor(tools, mcp, payments, notifier,
                            graph_hook=lambda uid, tid, ok: orch.memory(uid).tool_outcome(tid, ok),
                            memory_for=orch.memory, direct=direct)
    tasks = TaskStore(db)
    executor.on_resolved = lambda ap: tasks.resume_run(ap["run_id"]) if ap.get("run_id") is not None else None

    def recorder(user_id, tid, rid, status, provider, model, steps):
        m = orch.memory(user_id)
        m.record_run(tid, rid, status, provider, model)
        if steps:
            m.record_steps(tid, rid, steps)
        # the dependency graph reads these: a dependent task is ready only when its parents' last_status is 'completed'
        m.set_task_status(tid, status=tasks.get_task(tid)["status"], last=status)

    worker = TaskWorker(tasks, orch, notifier, recorder, executor=executor,
                        max_attempts=settings.max_attempts, stale_after=settings.run_stale_seconds)
    return Runtime(settings, db, creds, router, orch, tasks, tools, mcp, payments, executor, worker, notifier, direct)
