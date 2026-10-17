"""Single gate for every tool call, interactive or scheduled.

Order of checks: tool exists -> user granted the service -> args valid against the tool's JSON schema
-> approval policy -> invoke. Payments NEVER execute here: they create a PaymentIntent that a human
approves through the normal /pay flow."""
import json
import re
import time
from dataclasses import dataclass

import jsonschema

from ..mcp_client import McpError, McpRunner, McpTransient
from .store import ApprovalError, ToolStore

PAYOUT_TOOL = dict(
    tool_id="paypal.create_payout", service="paypal", name="create_payout", capability="paypal.payout",
    risk="high", approval=True, financial=True,
    description="Prepare a PayPal payout to an email recipient (always needs your approval).",
    input_schema={"type": "object", "additionalProperties": False,
                  "required": ["recipient", "amount", "currency"],
                  "properties": {"recipient": {"type": "string"}, "amount": {"type": "string"},
                                 "currency": {"type": "string"}, "note": {"type": "string"}}})


_COMPANIES = {"type": "string", "description": "company names: one per line, comma-separated, or a JSON list/object with 'new'"}
BUILTIN_TOOLS = [
    dict(tool_id="contacts.filter_new", service="contacts", name="filter_new", capability="contacts.filter",
         risk="low", approval=False, financial=False,
         description="Remove companies you've already contacted (checked against your graph).",
         input_schema={"type": "object", "additionalProperties": False, "required": ["companies"],
                       "properties": {"companies": _COMPANIES}}),
    dict(tool_id="contacts.record", service="contacts", name="record", capability="contacts.record",
         risk="low", approval=False, financial=False,
         description="Record companies as contacted in your graph (does not send anything).",
         input_schema={"type": "object", "additionalProperties": False, "required": ["companies"],
                       "properties": {"companies": _COMPANIES, "channel": {"type": "string"}}}),
]


def parse_names(value) -> list[str]:
    """Accepts a list, a JSON list, a JSON object with a 'new' list (output of contacts.filter_new), or free text."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    if isinstance(value, dict):
        value = value.get("new", [])
    if isinstance(value, str):
        value = re.split(r"[\n,]+", value)
    out, seen = [], set()
    for v in value if isinstance(value, list) else []:
        n = re.sub(r"^[\s\-*\u2022\d.)]+", "", str(v)).strip()
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n[:120])
    return out[:200]


class ToolDenied(Exception):
    pass


class ToolArgError(Exception):
    pass


class ToolFailed(Exception):
    pass


class ToolTransient(Exception):
    pass


@dataclass
class ExecCtx:
    user_id: str
    run_id: int | None = None
    step_id: str | None = None
    evidence: str = ""          # the task goal + this run's web.* tool outputs (where recipient addresses must come from)


@dataclass
class ToolOutcome:
    status: str                 # ok | needs_approval | pending_user
    text: str = ""
    approval_id: str | None = None


class ToolExecutor:
    def __init__(self, tools: ToolStore, mcp: McpRunner, payments=None, notify=None, graph_hook=None,
                 memory_for=None, direct=None):
        self.tools, self.mcp, self.payments, self.notify, self.graph_hook = tools, mcp, payments, notify, graph_hook
        self.direct = direct or {}                    # tool_id -> integrations.DirectTool
        self.irreversible = {t for t, d in self.direct.items() if d.irreversible}
        self.memory_for = memory_for                  # user_id -> Memory | NullMemory
        self.on_resolved = None       # callable(approval dict): unblock the waiting run

    def _say(self, user_id, text, buttons=None):
        if self.notify:
            try:
                self.notify(user_id, text, buttons)
            except Exception:
                pass

    @staticmethod
    def _summary(args: dict) -> str:
        return json.dumps(args, ensure_ascii=False)[:500]

    def run(self, ctx: ExecCtx, tool_id: str, args: dict) -> ToolOutcome:
        tool = self.tools.get_tool(tool_id)
        if tool is None:
            raise ToolDenied(f"unknown tool {tool_id}")
        if not self.tools.is_granted(ctx.user_id, tool):
            raise ToolDenied(f"{tool['service']} is not connected for you (/connect {tool['service']})")
        try:
            jsonschema.validate(args, tool["input_schema"])
        except jsonschema.ValidationError as e:
            raise ToolArgError(f"bad arguments for {tool_id}: {e.message[:200]}")

        if ctx.run_id is not None:   # resuming after an approval decision
            ap = self.tools.find_approval(ctx.run_id, ctx.step_id)
            if ap:
                if ap["status"] == "executed":
                    return ToolOutcome("ok", ap["result"] or "")
                if ap["status"] in ("pending", "approved", "executing"):
                    return ToolOutcome("needs_approval", approval_id=ap["id"])
                raise ToolDenied(f"action was {ap['status']}" + (f": {ap['error']}" if ap["error"] else ""))

        if tool_id in ("contacts.filter_new", "contacts.record"):
            return ToolOutcome("ok", self._contacts(ctx.user_id, tool_id, args))

        prep = None
        direct = self.direct.get(tool_id)
        if direct is not None and direct.prepare is not None:
            prep = direct.prepare(ctx, args)              # validate + filter + preview BEFORE asking the human
            if prep.empty:
                return ToolOutcome("ok", prep.empty)
            args = prep.args                              # the approval covers the filtered arguments

        if tool_id == PAYOUT_TOOL["tool_id"]:
            if self.payments is None:
                raise ToolDenied("payments are not configured")
            i = self.payments.prepare(ctx.user_id, args["recipient"], args["amount"], args["currency"],
                                      args.get("note", "Payment"))
            self._say(ctx.user_id, f"Payment prepared (NOT sent):\n{i['amount']} {i['currency']} -> {i['recipient']}\n"
                                   f"Note: {i['note']}", [("Approve", f"ok:{i['id']}"), ("Cancel", f"no:{i['id']}")])
            return ToolOutcome("pending_user", f"Payment {i['id']} prepared and awaiting your approval (not sent).")

        if tool["approval"]:
            ap = self.tools.request_approval(ctx.user_id, tool_id, args, ctx.run_id, ctx.step_id)
            self._say(ctx.user_id, f"Approval needed to run {tool_id}:\n{prep.preview if prep and prep.preview else self._summary(args)}",
                      [("Approve", f"tap:{ap['id']}"), ("Reject", f"tnr:{ap['id']}")])
            return ToolOutcome("needs_approval", approval_id=ap["id"])

        return ToolOutcome("ok", self._invoke(ctx.user_id, tool, args, ctx.run_id))

    def _contacts(self, user_id: str, tool_id: str, args: dict) -> str:
        """Graph-backed built-ins. They are only as good as the graph: with BUTLER_GRAPH=off the filter can't
        tell what you've already contacted and says so."""
        mem = self.memory_for(user_id)
        names = parse_names(args["companies"])
        if tool_id == "contacts.record":
            if not mem.enabled:
                return "graph is off: nothing was recorded"
            return f"recorded {mem.record_contacts(names, args.get('channel', 'email'))} compan(ies) as contacted"
        if not mem.enabled:
            return json.dumps({"new": names, "already_contacted": [],
                               "warning": "graph is off: duplicate check unavailable, nothing was filtered"})
        from ..graph.memory import norm
        found = mem.contacted_batch(names)
        new = [n for n in names if norm(n) not in found]
        dup = [{"company": found[norm(n)]["name"],
                "contacted_on": time.strftime("%Y-%m-%d", time.gmtime(found[norm(n)]["at"]))}
               for n in names if norm(n) in found]
        return json.dumps({"new": new, "already_contacted": dup})

    def _invoke(self, user_id, tool, args, run_id) -> str:
        d = self.direct.get(tool["tool_id"])
        if d is not None:
            try:
                text = d.execute(user_id, args, run_id)
            except (ToolTransient, ToolFailed, ToolDenied, ToolArgError) as e:
                self._outcome(user_id, tool, run_id, False, str(e), args)
                raise
            self._outcome(user_id, tool, run_id, True, "", args)
            return text
        try:
            text = self.mcp.call(user_id, tool["service"], tool["name"], args)
        except McpTransient as e:
            self._outcome(user_id, tool, run_id, False, str(e), args)
            raise ToolTransient(str(e))
        except McpError as e:
            self._outcome(user_id, tool, run_id, False, str(e), args)
            raise ToolFailed(str(e))
        self._outcome(user_id, tool, run_id, True, "", args)
        return text

    def _outcome(self, user_id, tool, run_id, ok, err, args):
        self.tools.log_call(user_id, tool["tool_id"], run_id, ok, err, args)
        if self.graph_hook:
            try:
                self.graph_hook(user_id, tool["tool_id"], ok)
            except Exception:
                pass

    # ---- human decisions ----
    def approve_and_execute(self, approval_id: str, user_id: str) -> ToolOutcome:
        ap = self.tools.approve(approval_id, user_id)          # owner, expiry, fingerprint checks
        claimed = self.tools.claim_execute(ap["id"])
        if claimed is None:
            raise ApprovalError("already executing")
        tool = self.tools.get_tool(claimed["tool_id"])
        try:
            text = self._invoke(user_id, tool, claimed["args"], claimed["run_id"])
            self.tools.finish_approval(claimed["id"], "executed", text)
            out = ToolOutcome("ok", text)
        except ToolTransient as e:
            # outcome unknown: for money-moving tools never auto-retry
            unknown = tool["financial"] or tool["tool_id"] in self.irreversible
            self.tools.finish_approval(claimed["id"], "unresolved" if unknown else "failed", error=str(e))
            out = ToolOutcome("error", f"Outcome unknown ({e}); not retried." if unknown else str(e))
        except (ToolFailed, ToolDenied, ToolArgError) as e:   # an approved action must never be left 'executing'
            self.tools.finish_approval(claimed["id"], "failed", error=str(e))
            out = ToolOutcome("error", str(e))
        self._resolved(claimed["id"])
        return out

    def reject(self, approval_id: str, user_id: str) -> None:
        self.tools.reject(approval_id, user_id)
        self._resolved(approval_id)

    def _resolved(self, approval_id: str) -> None:
        if self.on_resolved:
            self.on_resolved(self.tools.get_approval(approval_id))
