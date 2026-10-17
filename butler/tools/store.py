"""Tool catalog, per-user grants, human approvals and a (no-payload) tool-call ledger."""
import hashlib
import json
import time
import uuid

from ..db import Db


class ApprovalError(Exception):
    pass


def args_fingerprint(user_id: str, tool_id: str, args: dict) -> str:
    return hashlib.sha256(json.dumps([str(user_id), tool_id, args], sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


class ToolStore:
    def __init__(self, path=":memory:"):
        self.db = Db.coerce(path)
        for ddl in (
            """CREATE TABLE IF NOT EXISTS tool_catalog(tool_id TEXT PRIMARY KEY, service TEXT, name TEXT,
               capability TEXT, risk TEXT, approval INTEGER, financial INTEGER, description TEXT,
               input_schema TEXT, updated REAL)""",
            """CREATE TABLE IF NOT EXISTS grants(user_id TEXT, service TEXT, granted REAL,
               PRIMARY KEY(user_id, service))""",
            """CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY, user_id TEXT, tool_id TEXT, args TEXT,
               fingerprint TEXT, status TEXT, run_id INTEGER, step_id TEXT, created REAL, expires REAL,
               result TEXT, error TEXT, UNIQUE(run_id, step_id))""",
            """CREATE TABLE IF NOT EXISTS tool_calls(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, user_id TEXT,
               tool_id TEXT, run_id INTEGER, ok INTEGER, error TEXT, args_hash TEXT)""",
        ):
            self.db.ddl(ddl)

    # ---- catalog ----
    def upsert_tool(self, *, tool_id, service, name, capability, risk, approval, financial,
                    description, input_schema) -> None:
        self.db.execute(
            "INSERT INTO tool_catalog VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(tool_id) DO UPDATE SET "
            "capability=excluded.capability, risk=excluded.risk, approval=excluded.approval, "
            "financial=excluded.financial, description=excluded.description, "
            "input_schema=excluded.input_schema, updated=excluded.updated",
            (tool_id, service, name, capability, risk, int(approval), int(financial),
             description[:300], json.dumps(input_schema), time.time()))

    @staticmethod
    def _tool(r) -> dict:
        d = dict(r)
        d["approval"], d["financial"] = bool(d["approval"]), bool(d["financial"])
        d["input_schema"] = json.loads(d["input_schema"])
        return d

    def get_tool(self, tool_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM tool_catalog WHERE tool_id=?", (tool_id,)).fetchone()
        return self._tool(r) if r else None

    def tools_for_service(self, service: str) -> list[dict]:
        return [self._tool(r) for r in self.db.execute(
            "SELECT * FROM tool_catalog WHERE service=? ORDER BY tool_id", (service,))]

    def drop_service_tools(self, service: str, keep: set[str]) -> None:
        for r in self.tools_for_service(service):
            if r["tool_id"].startswith(f"mcp.{service}.") and r["tool_id"] not in keep:
                self.db.execute("DELETE FROM tool_catalog WHERE tool_id=?", (r["tool_id"],))

    # ---- grants (authoritative; the graph only mirrors these) ----
    def grant(self, user_id: str, service: str) -> None:
        self.db.execute("INSERT INTO grants VALUES(?,?,?) ON CONFLICT(user_id,service) DO NOTHING",
                        (str(user_id), service, time.time()))

    def revoke(self, user_id: str, service: str) -> bool:
        return self.db.execute("DELETE FROM grants WHERE user_id=? AND service=?", (str(user_id), service)).rowcount == 1

    def granted_services(self, user_id: str) -> list[str]:
        return [r["service"] for r in self.db.execute("SELECT service FROM grants WHERE user_id=?", (str(user_id),))]

    def is_granted(self, user_id: str, tool: dict) -> bool:
        return tool["service"] in self.granted_services(user_id)

    # ---- approvals ----
    def request_approval(self, user_id, tool_id, args: dict, run_id=None, step_id=None, ttl=86400) -> dict:
        if run_id is not None:
            ex = self.find_approval(run_id, step_id)
            if ex:
                return ex
        aid, now = uuid.uuid4().hex, time.time()
        self.db.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
                        (aid, str(user_id), tool_id, json.dumps(args, sort_keys=True),
                         args_fingerprint(user_id, tool_id, args), "pending", run_id, step_id, now, now + ttl))
        return self.get_approval(aid)

    def get_approval(self, aid: str) -> dict:
        r = self.db.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
        if not r:
            raise ApprovalError("unknown approval")
        d = dict(r)
        d["args"] = json.loads(d["args"])
        return d

    def find_approval(self, run_id: int, step_id: str) -> dict | None:
        r = self.db.execute("SELECT id FROM approvals WHERE run_id=? AND step_id=?", (run_id, step_id)).fetchone()
        return self.get_approval(r["id"]) if r else None

    def _owned_pending(self, aid: str, user_id: str) -> dict:
        ap = self.get_approval(aid)
        if ap["user_id"] != str(user_id):
            raise ApprovalError("not your approval")
        return ap

    def approve(self, aid: str, user_id: str, now: float | None = None) -> dict:
        now = now or time.time()
        ap = self._owned_pending(aid, user_id)
        if ap["expires"] < now:
            self.db.execute("UPDATE approvals SET status='expired' WHERE id=? AND status='pending'", (aid,))
            raise ApprovalError("approval expired")
        if args_fingerprint(ap["user_id"], ap["tool_id"], ap["args"]) != ap["fingerprint"]:
            raise ApprovalError("approval no longer matches the requested action")
        if self.db.execute("UPDATE approvals SET status='approved' WHERE id=? AND status='pending'",
                           (aid,)).rowcount != 1:
            raise ApprovalError(f"cannot approve from state {ap['status']}")
        return self.get_approval(aid)

    def reject(self, aid: str, user_id: str) -> dict:
        self._owned_pending(aid, user_id)
        self.db.execute("UPDATE approvals SET status='rejected' WHERE id=? AND status='pending'", (aid,))
        return self.get_approval(aid)

    def claim_execute(self, aid: str) -> dict | None:
        """approved -> executing, atomically: a double tap can never run the action twice."""
        if self.db.execute("UPDATE approvals SET status='executing' WHERE id=? AND status='approved'",
                           (aid,)).rowcount != 1:
            return None
        return self.get_approval(aid)

    def finish_approval(self, aid: str, status: str, result: str = "", error: str = "") -> None:
        self.db.execute("UPDATE approvals SET status=?, result=?, error=? WHERE id=?",
                        (status, result[:8000], error[:300], aid))

    def expire_due(self, now: float) -> list[dict]:
        rows = self.db.execute("SELECT id FROM approvals WHERE status='pending' AND expires<?", (now,)).fetchall()
        out = []
        for r in rows:
            if self.db.execute("UPDATE approvals SET status='expired' WHERE id=? AND status='pending'",
                               (r["id"],)).rowcount == 1:
                out.append(self.get_approval(r["id"]))
        return out

    # ---- ledger (hash of args only: no payloads/PII/secrets) ----
    def log_call(self, user_id, tool_id, run_id, ok: bool, error: str, args: dict) -> None:
        self.db.execute("INSERT INTO tool_calls(ts,user_id,tool_id,run_id,ok,error,args_hash) VALUES(?,?,?,?,?,?,?)",
                        (time.time(), str(user_id), tool_id, run_id, int(ok), error[:200],
                         hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()[:16]))
