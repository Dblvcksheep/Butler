"""Durable task + run ledger (SQLite or PostgreSQL). Operational state lives here; the graph holds context/relations."""
import json
import time
import uuid

from ..db import Db
from .schedule import next_run, parse_schedule

TASK_TRANSITIONS = {
    "scheduled": {"paused", "cancelled", "completed", "failed"},
    "paused": {"scheduled", "cancelled"},
    "completed": set(), "cancelled": set(), "failed": set(),
}
ONE_SHOT = ("once", "now")
MAX_STEPS = 8


class TaskError(Exception):
    pass


def normalize_steps(steps: list) -> list[dict]:
    """Accept plain strings (sequential LLM steps) or full step dicts (model-written plans)."""
    out: list[dict] = []
    for i, s in enumerate(steps):
        if isinstance(s, str):
            s = s.strip()
            if s:
                out.append({"id": f"s{i + 1}", "agent": "assistant", "kind": "llm", "prompt": s,
                            "depends_on": [out[-1]["id"]] if out else []})
        else:
            d = dict(s)
            d.setdefault("agent", "assistant")
            d.setdefault("depends_on", [])
            out.append(d)
    if len(out) > MAX_STEPS:
        raise TaskError(f"at most {MAX_STEPS} steps")
    return out


def topo_order(steps: list[dict]) -> list[dict]:
    ids = {s["id"] for s in steps}
    if len(ids) != len(steps):
        raise ValueError("duplicate step ids")
    for s in steps:
        for d in s["depends_on"]:
            if d not in ids or d == s["id"]:
                raise ValueError(f"step {s['id']} depends on unknown step {d}")
    done: list[dict] = []
    seen: set[str] = set()
    remaining = list(steps)
    while remaining:
        ready = [s for s in remaining if all(d in seen for d in s["depends_on"])]
        if not ready:
            raise ValueError("dependency cycle in plan")
        for s in ready:
            done.append(s)
            seen.add(s["id"])
            remaining.remove(s)
    return done


class TaskStore:
    def __init__(self, path=":memory:"):
        self.db = Db.coerce(path)
        self.db.ddl("""CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            objective TEXT, steps TEXT, kind TEXT, sched_kind TEXT, sched_spec TEXT, tz TEXT,
            next_run REAL, status TEXT NOT NULL, scope TEXT, created REAL, pending_input TEXT)""")
        self.db.ddl("""CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT, scheduled_for REAL, status TEXT, attempt INTEGER, started REAL, heartbeat REAL,
            retry_at REAL, ended REAL, provider TEXT, model TEXT, output TEXT, error TEXT,
            input TEXT, worker TEXT, waiting_on TEXT, UNIQUE(task_id, scheduled_for))""")
        self.db.ddl("""CREATE TABLE IF NOT EXISTS run_steps(run_id INTEGER, step_id TEXT, output TEXT,
            PRIMARY KEY(run_id, step_id))""")
        self.db.ddl("""CREATE TABLE IF NOT EXISTS plan_drafts(id TEXT PRIMARY KEY, user_id TEXT, goal TEXT,
            schedule TEXT, tz TEXT, plan TEXT, created REAL)""")

    # ---- tasks ----
    def create(self, user_id, steps: list, schedule: str, tz="UTC", kind="chat", scope=None,
               objective: str | None = None) -> dict:
        now = time.time()
        sk, spec = parse_schedule(schedule, tz, now)
        if sk == "after":
            for parent in spec.split(","):          # every parent must exist and be yours
                self.owned(parent, user_id)
        norm = normalize_steps(steps)
        if not norm:
            raise TaskError("a task needs an objective")
        topo_order(norm)                            # validates deps/cycles
        obj = (objective or norm[0].get("prompt") or norm[0].get("capability") or "task")[:200]
        tid = uuid.uuid4().hex[:8]
        self.db.execute(
            "INSERT INTO tasks(id,user_id,objective,steps,kind,sched_kind,sched_spec,tz,next_run,status,scope,created) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, str(user_id), obj, json.dumps(norm), kind, sk, spec, tz,
             next_run(sk, spec, tz, now), "scheduled", scope, now))
        return self.get_task(tid)

    def get_task(self, tid: str) -> dict:
        r = self.db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if not r:
            raise TaskError("unknown task")
        return dict(r)

    def owned(self, tid: str, user_id: str) -> dict:
        t = self.get_task(tid)
        if t["user_id"] != str(user_id):
            raise TaskError("not your task")
        return t

    def list_tasks(self, user_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM tasks WHERE user_id=? ORDER BY created", (str(user_id),))]

    def set_status(self, tid: str, user_id: str, to: str) -> dict:
        t = self.owned(tid, user_id)
        if to not in TASK_TRANSITIONS[t["status"]]:
            raise TaskError(f"cannot go from {t['status']} to {to}")
        nxt = t["next_run"]
        if to == "scheduled":                       # resume: skip anything missed while paused
            nxt = next_run(t["sched_kind"], t["sched_spec"], t["tz"], time.time())
            if nxt is None and t["sched_kind"] in ("once", "now"):
                raise TaskError("this one-time task's time has passed")
        self.db.execute("UPDATE tasks SET status=?, next_run=? WHERE id=?", (to, nxt, tid))
        return self.get_task(tid)

    def _finish_task(self, tid: str, status: str) -> None:
        self.db.execute("UPDATE tasks SET status=? WHERE id=? AND status='scheduled'", (status, tid))

    # ---- plan drafts (a model-written plan waits here for the user's confirmation) ----
    def save_draft(self, user_id, goal, schedule, tz, plan: list[dict]) -> str:
        did = uuid.uuid4().hex[:12]
        self.db.execute("INSERT INTO plan_drafts VALUES(?,?,?,?,?,?,?)",
                        (did, str(user_id), goal, schedule, tz, json.dumps(plan), time.time()))
        return did

    def take_draft(self, did: str, user_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM plan_drafts WHERE id=? AND user_id=?", (did, str(user_id))).fetchone()
        if not r or time.time() - r["created"] > 3600:
            return None
        if self.db.execute("DELETE FROM plan_drafts WHERE id=?", (did,)).rowcount != 1:
            return None
        d = dict(r)
        d["plan"] = json.loads(d["plan"])
        return d

    # ---- runs ----
    def get_run(self, rid: int) -> dict:
        return dict(self.db.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone())

    def list_runs(self, tid: str, limit: int = 5) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT id,status,attempt,provider,model,error,started,ended FROM runs WHERE task_id=? "
            "ORDER BY id DESC LIMIT ?", (tid, limit))]

    def due_after(self, now: float) -> list[dict]:
        """Dependent tasks that have been triggered and are waiting for the scheduler's readiness decision."""
        return [dict(r) for r in self.db.execute(
            "SELECT id, user_id FROM tasks WHERE status='scheduled' AND sched_kind='after' "
            "AND next_run IS NOT NULL AND next_run<=?", (now,))]

    def claim_due(self, now: float, skip=()) -> list[dict]:
        """Atomically turn due tasks into runs. UNIQUE(task_id, scheduled_for) + SKIP LOCKED (Postgres)
        make duplicate dispatch impossible, including across several workers."""
        claimed: list[int] = []
        with self.db.transaction():
            due = self.db.execute("SELECT * FROM tasks WHERE status='scheduled' AND next_run IS NOT NULL "
                                  "AND next_run<=?" + self.db.skip_locked, (now,)).fetchall()
            for t in due:
                if t["id"] in skip:                 # blocked by the dependency graph: stays due, re-checked next tick
                    continue
                sched_for, nxt = t["next_run"], None
                if t["sched_kind"] not in ONE_SHOT + ("after",):
                    nxt = next_run(t["sched_kind"], t["sched_spec"], t["tz"], sched_for)
                    if nxt is not None and nxt <= now:      # missed several: skip ahead, don't burst
                        nxt = next_run(t["sched_kind"], t["sched_spec"], t["tz"], now)
                ins = self.db.execute(
                    "INSERT INTO runs(task_id,scheduled_for,status,attempt,started,heartbeat,input) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(task_id, scheduled_for) DO NOTHING",
                    (t["id"], sched_for, "running", 1, now, now, t["pending_input"]))
                if ins.rowcount == 1:
                    claimed.append(self.db.execute("SELECT id FROM runs WHERE task_id=? AND scheduled_for=?",
                                                   (t["id"], sched_for)).fetchone()["id"])
                self.db.execute("UPDATE tasks SET next_run=?, pending_input=NULL WHERE id=?", (nxt, t["id"]))
        return [self.get_run(i) for i in claimed]

    def claim_retries(self, now: float) -> list[dict]:
        out = []
        for r in self.db.execute("SELECT id FROM runs WHERE status='retrying' AND retry_at<=?", (now,)).fetchall():
            cur = self.db.execute("UPDATE runs SET status='running', attempt=attempt+1, heartbeat=?, worker=NULL "
                                  "WHERE id=? AND status='retrying'", (now, r["id"]))
            if cur.rowcount == 1:
                out.append(self.get_run(r["id"]))
        return out

    def claim_resumes(self, now: float) -> list[dict]:
        """Runs that were waiting for a human approval and have been unblocked. Not a retry: attempt unchanged."""
        out = []
        for r in self.db.execute("SELECT id FROM runs WHERE status='resume'").fetchall():
            cur = self.db.execute("UPDATE runs SET status='running', heartbeat=?, worker=NULL, waiting_on=NULL "
                                  "WHERE id=? AND status='resume'", (now, r["id"]))
            if cur.rowcount == 1:
                out.append(self.get_run(r["id"]))
        return out

    def begin_execution(self, rid: int, token: str, now: float, stale_after: float) -> bool:
        """Only one worker may execute a run, even if the queue delivers the message twice."""
        cur = self.db.execute("UPDATE runs SET worker=?, heartbeat=? WHERE id=? AND status='running' "
                              "AND (worker IS NULL OR heartbeat<?)", (token, now, rid, now - stale_after))
        return cur.rowcount == 1

    def heartbeat(self, rid: int, now: float) -> None:
        self.db.execute("UPDATE runs SET heartbeat=? WHERE id=?", (now, rid))

    def save_checkpoint(self, rid: int, step_id: str, output: str) -> None:
        self.db.execute("INSERT INTO run_steps(run_id,step_id,output) VALUES(?,?,?) "
                        "ON CONFLICT(run_id,step_id) DO UPDATE SET output=excluded.output", (rid, step_id, output))

    def checkpoints(self, rid: int) -> dict[str, str]:
        return {r["step_id"]: r["output"] for r in self.db.execute(
            "SELECT step_id,output FROM run_steps WHERE run_id=?", (rid,))}

    def set_waiting(self, rid: int, approval_id: str) -> None:
        self.db.execute("UPDATE runs SET status='waiting', waiting_on=?, worker=NULL WHERE id=? AND status='running'",
                        (approval_id, rid))

    def resume_run(self, rid: int) -> bool:
        return self.db.execute("UPDATE runs SET status='resume' WHERE id=? AND status='waiting'", (rid,)).rowcount == 1

    def complete_run(self, rid, now, provider, model, output) -> None:
        self.db.execute("UPDATE runs SET status='completed', ended=?, provider=?, model=?, output=? WHERE id=?",
                        (now, provider, model, output, rid))
        tid = self.get_run(rid)["task_id"]
        if self.get_task(tid)["sched_kind"] in ONE_SHOT:
            self._finish_task(tid, "completed")
        # task dependencies: downstream 'after' tasks become DUE and accumulate this output as (data) input.
        # Whether they actually run is decided by the dependency graph (all parents completed?).
        for r in self.db.execute("SELECT id, sched_spec, pending_input FROM tasks "
                                 "WHERE sched_kind='after' AND status='scheduled'").fetchall():
            if tid in r["sched_spec"].split(","):
                merged = ((r["pending_input"] or "") + f"\n\n[from task {tid}]\n{(output or '')[:2000]}").strip()[-4000:]
                self.db.execute("UPDATE tasks SET next_run=?, pending_input=? WHERE id=?", (now, merged, r["id"]))

    def fail_run(self, rid: int, error: str, now: float) -> None:
        """Permanent failure (denied/rejected/bad plan): retrying can't help."""
        self.db.execute("UPDATE runs SET status='failed', ended=?, error=?, worker=NULL WHERE id=?",
                        (now, error[:300], rid))
        r = self.get_run(rid)
        if self.get_task(r["task_id"])["sched_kind"] in ONE_SHOT:
            self._finish_task(r["task_id"], "failed")

    def fail_or_retry(self, rid: int, error: str, now: float, max_attempts: int) -> str:
        r = self.get_run(rid)
        if r["attempt"] >= max_attempts:
            self.fail_run(rid, error, now)
            return "failed"
        backoff = 30 * 2 ** (r["attempt"] - 1)
        self.db.execute("UPDATE runs SET status='retrying', retry_at=?, error=?, worker=NULL WHERE id=?",
                        (now + backoff, error[:300], rid))
        return "retrying"

    def recover_stale(self, now: float, stale_after: float, max_attempts: int) -> int:
        """A worker died mid-run: its heartbeat stops. Re-queue (checkpoints are kept) or fail it."""
        n = 0
        for r in self.db.execute("SELECT id FROM runs WHERE status='running' AND heartbeat<?",
                                 (now - stale_after,)).fetchall():
            self.fail_or_retry(r["id"], "worker stopped responding", now, max_attempts)
            n += 1
        return n
