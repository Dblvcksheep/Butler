import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from butler.llm.router import AllModelsFailed, RouterResult
from butler.tasks.schedule import next_run, parse_schedule
from butler.tasks.store import TaskError, TaskStore
from butler.tasks.worker import TaskWorker
from butler.tools.executor import ToolArgError, ToolOutcome


class FakeOrch:
    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def run_step(self, user_id, objective, prompt, scope, kind, role=""):
        self.calls.append(prompt)
        if self.fail_on and self.fail_on(prompt):
            raise AllModelsFailed("all down", [])
        return RouterResult(f"out({prompt[:12]})", "anthropic", "m")


def test_schedule_parsing_and_next_run_in_timezone():
    lagos = ZoneInfo("Africa/Lagos")
    after = datetime(2026, 10, 14, 10, 0, tzinfo=lagos).timestamp()
    n = datetime.fromtimestamp(next_run("daily", "09:00", "Africa/Lagos", after), lagos)
    assert (n.day, n.hour) == (15, 9)
    w = datetime.fromtimestamp(next_run("weekly", "fri 08:30", "Africa/Lagos", after), lagos)
    assert w.weekday() == 4 and (w.hour, w.minute) == (8, 30)
    assert next_run("every", "600", "UTC", 100.0) == 700.0
    for bad in ["daily 25:00", "every 1m", "weekly xyz 09:00", "sometimes", "once 2001-01-01T00:00"]:
        with pytest.raises(ValueError):
            parse_schedule(bad, "UTC", time.time())
    with pytest.raises(ValueError):
        parse_schedule("daily 09:00", "Mars/Base", time.time())


def due_store(sched="every 5m"):
    s = TaskStore()
    t = s.create("1", ["step one", "step two"], sched)
    return s, t


def test_due_task_runs_once_even_if_ticked_twice():
    s, t = due_store()
    now = t["next_run"] + 1
    assert len(s.claim_due(now)) == 1
    assert s.claim_due(now) == []                       # no duplicate dispatch


def test_runs_after_conversation_ends_and_notifies():
    s, t = due_store()
    msgs = []
    clock = [t["next_run"] + 1]
    w = TaskWorker(s, FakeOrch(), lambda u, m: msgs.append((u, m)), now=lambda: clock[0])
    assert w.tick() == 1
    assert msgs and msgs[0][0] == "1" and "anthropic" in msgs[0][1]
    assert s.get_task(t["id"])["next_run"] > clock[0]    # recurring: rescheduled


def test_checkpoint_resume_does_not_redo_completed_steps():
    s, t = due_store()
    clock = [t["next_run"] + 1]
    orch = FakeOrch(fail_on=lambda p: p.startswith("step two") and len(orch.calls) < 3)
    w = TaskWorker(s, orch, now=lambda: clock[0])
    w.tick()                                             # step one ok, step two fails -> retrying
    run = s.db.execute("SELECT * FROM runs").fetchone()
    assert run["status"] == "retrying" and len(s.checkpoints(run["id"])) == 1
    clock[0] += 31
    w.tick()                                             # retry resumes at step two
    run = s.db.execute("SELECT * FROM runs").fetchone()
    assert run["status"] == "completed" and run["attempt"] == 2
    assert sum(1 for c in orch.calls if c.startswith("step one")) == 1


def test_gives_up_after_max_attempts_and_fails_once_task():
    s = TaskStore()
    t = s.create("1", ["x"], "once 2099-01-01T00:00")
    msgs = []
    clock = [t["next_run"] + 1]
    w = TaskWorker(s, FakeOrch(fail_on=lambda p: True), lambda u, m: msgs.append(m),
                   now=lambda: clock[0], max_attempts=2)
    for _ in range(4):
        w.tick(); clock[0] += 100
    assert s.db.execute("SELECT status FROM runs").fetchone()[0] == "failed"
    assert s.get_task(t["id"])["status"] == "failed" and msgs and "failed" in msgs[-1]


def test_worker_crash_recovered_via_stale_heartbeat():
    s, t = due_store()
    run = s.claim_due(t["next_run"] + 1)[0]              # worker claims then "dies"
    later = t["next_run"] + 1000
    assert s.recover_stale(later, 300, 3) == 1
    assert s.get_run(run["id"])["status"] == "retrying"
    w = TaskWorker(s, FakeOrch(), now=lambda: later + 60)
    w.tick()
    assert s.get_run(run["id"])["status"] == "completed"


def test_invalid_email_json_is_reformatted_and_tool_is_retried():
    email_json = '[{"to":"hello@example.com","subject":"Backend role","body":"Hello","company":"Acme"}]'

    class RepairOrch(FakeOrch):
        def run_step(self, user_id, objective, prompt, scope, kind, role=""):
            self.calls.append(prompt)
            return RouterResult(email_json, "anthropic", "m")

    class FakeExecutor:
        def __init__(self):
            self.calls = []

        def run(self, ctx, tool_id, args):
            self.calls.append(args)
            if len(self.calls) == 1:
                raise ToolArgError("messages is not valid JSON")
            return ToolOutcome("ok", "draft accepted")

    class FakeStore:
        def __init__(self):
            self.checkpoints, self.waiting = {}, None

        def save_checkpoint(self, run_id, step_id, output):
            self.checkpoints[step_id] = output

        def heartbeat(self, run_id, now):
            pass

    store, orch, executor = FakeStore(), RepairOrch(), FakeExecutor()
    worker = TaskWorker(store, orch, executor=executor)
    steps = [
        {"id": "s1", "agent": "writer", "kind": "llm", "prompt": "Draft application emails", "depends_on": []},
        {"id": "s2", "agent": "operator", "kind": "tool", "capability": "email.send",
         "tool_id": "email.send_batch",
         "args": {"messages": "{{s1}}"}, "depends_on": ["s1"]},
    ]
    outputs = {"s1": "Here are the application emails."}
    result, _ = worker._step({"user_id": "1", "objective": "Apply for backend roles", "scope": None, "kind": "chat"},
                             {"id": 1, "input": None}, steps[1], {s["id"]: s for s in steps}, outputs)

    assert result == "draft accepted"
    assert len(executor.calls) == 2 and json.loads(executor.calls[1]["messages"]) == json.loads(email_json)
    assert outputs["s1"] == email_json and store.checkpoints["s1"] == email_json


def test_pause_resume_cancel_and_ownership():
    s, t = due_store()
    s.set_status(t["id"], "1", "paused")
    assert s.claim_due(t["next_run"] + 10_000) == []     # paused tasks never run
    s.set_status(t["id"], "1", "scheduled")
    with pytest.raises(TaskError):
        s.set_status(t["id"], "2", "cancelled")          # other user
    s.set_status(t["id"], "1", "cancelled")
    with pytest.raises(TaskError):
        s.set_status(t["id"], "1", "scheduled")          # cancelled is terminal
