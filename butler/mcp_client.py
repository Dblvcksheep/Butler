"""MCP client: connects to MCP servers over stdio, discovers tools, calls them.

Security model (all in code, not in the LLM):
  * deny by default: only tools matching a rule in the service spec are exposed at all
  * risk/approval/financial flags come from OUR rules; a server annotation can only make things stricter
  * server env = minimal default env + per-user secrets for that service only (never our master key)
  * tool output is untrusted data: truncated here, wrapped as data wherever it reaches an LLM
"""
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from fnmatch import fnmatch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MAX_OUTPUT = 8000


class McpError(Exception):
    """Tool/server returned an error (not retryable as-is)."""


class McpTransient(McpError):
    """Timeout / connection problem: the call may or may not have happened."""


@dataclass(frozen=True)
class Rule:
    pattern: str            # fnmatch on the MCP tool name
    capability: str
    risk: str               # low | medium | high
    approval: bool
    financial: bool = False


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    label: str
    command: str
    args: tuple
    rules: tuple
    secret_service: str | None = None          # needs per-user secrets stored under this name
    static_env: dict = field(default_factory=dict)

    def policy_for(self, tool_name: str) -> Rule | None:
        for r in self.rules:
            if fnmatch(tool_name, r.pattern):
                return r
        return None


# Checked against the real @paypal/mcp server (npx, --tools=all) on 2026-10-14. Re-verify after upgrades with
# `python -m butler.cli discover USER paypal`:
# anything not matched by a rule is hidden, so a wrong name fails closed.
PAYPAL = ServiceSpec(
    "paypal", "PayPal", "npx", ("-y", "@paypal/mcp", "--tools=all"),
    rules=(
        Rule("list_*", "paypal.read", "low", False),
        Rule("get_*", "paypal.read", "low", False),
        Rule("show_*", "paypal.read", "low", False),
        Rule("create_order", "paypal.orders.create", "high", True, True),
        Rule("pay_order", "paypal.orders.pay", "high", True, True),
        Rule("create_refund", "paypal.refunds.create", "high", True, True),
        Rule("send_invoice", "paypal.invoices.send", "high", True, True),
        Rule("create_invoice", "paypal.invoices.create", "medium", True, False),
    ),
    secret_service="paypal")

FETCH = ServiceSpec(   # example web-research server (needs `uv`); untested here
    "fetch", "Web fetch", "uvx", ("mcp-server-fetch",),
    rules=(Rule("fetch", "web.fetch", "medium", False),))

BUILTIN = {s.name: s for s in (PAYPAL, FETCH)}


def load_services(path: str | None = None) -> dict[str, ServiceSpec]:
    """Built-ins, optionally overridden/extended by a JSON file (BUTLER_MCP_CONFIG)."""
    out = dict(BUILTIN)
    path = path or os.environ.get("BUTLER_MCP_CONFIG")
    if path and os.path.exists(path):
        for name, c in json.load(open(path)).items():
            out[name] = ServiceSpec(name, c.get("label", name), c["command"], tuple(c.get("args", ())),
                                    tuple(Rule(**r) for r in c["rules"]), c.get("secret_service"),
                                    c.get("static_env", {}))
    return out


def paypal_env(secrets: dict, allow_live: bool, token_minter=None) -> dict:
    env = "SANDBOX" if secrets.get("env", "sandbox") == "sandbox" else "PRODUCTION"
    if env == "PRODUCTION" and not allow_live:
        raise McpError("live PayPal is disabled (BUTLER_ALLOW_LIVE=1 required)")
    if token_minter is None:
        from .payments.paypal import PayPalClient
        token_minter = lambda: PayPalClient(secrets["client_id"], secrets["client_secret"],
                                            "sandbox" if env == "SANDBOX" else "live").access_token()
    return {"PAYPAL_ACCESS_TOKEN": token_minter(), "PAYPAL_ENVIRONMENT": env}


class McpRunner:
    def __init__(self, creds, services: dict[str, ServiceSpec], allow_live=False, timeout=60.0,
                 env_builders: dict | None = None):
        self.creds, self.services, self.allow_live, self.timeout = creds, services, allow_live, timeout
        self.env_builders = env_builders or {"paypal": lambda sec: paypal_env(sec, allow_live)}

    def _params(self, user_id: str, spec: ServiceSpec) -> StdioServerParameters:
        env = dict(spec.static_env)
        if spec.secret_service:
            sec = self.creds.get_service(user_id, spec.secret_service)
            if not sec:
                raise McpError(f"{spec.label} is not connected. Use /connect {spec.name}.")
            builder = self.env_builders.get(spec.name)
            if builder:
                env.update(builder(sec))
        return StdioServerParameters(command=spec.command, args=list(spec.args), env=env)

    async def _session(self, params, fn):
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await fn(s)

    def _run(self, params, fn):
        try:
            return asyncio.run(asyncio.wait_for(self._session(params, fn), self.timeout))
        except (asyncio.TimeoutError, TimeoutError) as e:
            raise McpTransient("MCP server timed out") from e
        except (OSError, ConnectionError) as e:
            raise McpTransient(f"MCP server unavailable: {type(e).__name__}") from e

    def discover(self, user_id: str, service: str) -> tuple[list[dict], list[str]]:
        """Return (exposed, hidden_names). Exposed entries carry OUR policy merged with the server's schema."""
        spec = self.services[service]

        async def go(s):
            return (await s.list_tools()).tools

        exposed, hidden = [], []
        for t in self._run(self._params(user_id, spec), go):
            rule = spec.policy_for(t.name)
            if rule is None:
                hidden.append(t.name)
                continue
            ann = t.annotations
            approval = rule.approval
            if ann is not None and (ann.destructiveHint is True or ann.readOnlyHint is False):
                approval = True                 # server says it writes: never less strict than that
            exposed.append(dict(tool_id=f"mcp.{service}.{t.name}", service=service, name=t.name,
                                capability=rule.capability, risk=rule.risk, approval=approval,
                                financial=rule.financial, description=(t.description or "")[:300],
                                input_schema=t.inputSchema or {"type": "object"}))
        return exposed, hidden

    def call(self, user_id: str, service: str, tool_name: str, args: dict) -> str:
        spec = self.services[service]

        async def go(s):
            return await s.call_tool(tool_name, args)

        res = self._run(self._params(user_id, spec), go)
        text = "\n".join(getattr(c, "text", "") for c in res.content if getattr(c, "text", None))
        if res.isError:
            raise McpError(text[:300] or "tool error")
        return text[:MAX_OUTPUT]
