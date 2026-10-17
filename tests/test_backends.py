"""The same store logic must behave identically on SQLite and on a real PostgreSQL server (embedded via pgserver)."""
import json
import threading
import uuid

import httpx
import pytest
from cryptography.fernet import Fernet

from butler.config import Settings
from butler.credentials import CredentialStore, Vault
from butler.db import Db
from butler.llm.router import RouterResult
from butler.payments.intents import IntentStore, PaymentError
from butler.payments.paypal import PayPalClient
from butler.payments.service import PaymentService
from butler.tasks.store import TaskStore
from butler.tasks.worker import TaskWorker
from butler.tools.store import ApprovalError, ToolStore


@pytest.fixture(scope="session")
def pg_uri(tmp_path_factory):
    try:
        import pgserver
        srv = pgserver.get_server(str(tmp_path_factory.mktemp("pg")))
    except Exception as e:                       # pragma: no cover
        pytest.skip(f"embedded postgres unavailable: {e}")
    yield srv.get_uri()
    srv.cleanup()


@pytest.fixture(params=["sqlite", "postgres"])
def dbf(request, tmp_path):
    """Factory returning NEW connections to the same logical database (to test cross-connection behaviour)."""
    if request.param == "sqlite":
        path = str(tmp_path / "t.db")
        return lambda: Db(path)
    uri = request.getfixturevalue("pg_uri")
    schema = "t" + uuid.uuid4().hex[:12]
    return lambda: Db(uri, schema=schema)


class FakeOrch:
    def __init__(self): self.calls = []
    def run_step(self, user_id, objective, prompt, scope, kind, role=""):
        self.calls.append(prompt)
        return RouterResult(f"out:{prompt[:10]}", "anthropic", "m")


def test_payments_safety_properties(dbf):
    calls = []

    def h(req):
        if req.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "t"})
        calls.append(req.headers["PayPal-Request-Id"])
        return httpx.Response(201, json={"batch_header": {"payout_batch_id": "B1", "batch_status": "SUCCESS"}})

    store = IntentStore(dbf())
    svc = PaymentService(store, PayPalClient("i", "s", transport=httpx.MockTransport(h)), Settings(max_per_tx="50", max_per_day="100"))
    i = svc.prepare("1", "a@b.co", "20", "usd", "x")
    with pytest.raises(PaymentError):
        svc.execute(i["id"], "1")                               # no approval
    store.approve(i["id"], "1")
    assert svc.execute(i["id"], "1")["status"] == "succeeded"
    with pytest.raises(PaymentError):
        svc.execute(i["id"], "1")                               # no second payout
    assert calls == [i["id"]]
    j = svc.prepare("1", "a@b.co", "20", "usd", "x")
    store.approve(j["id"], "1")
    store.amend(j["id"], "1", svc.s.allowed_currencies, amount="45")   # amount change voids approval
    with pytest.raises(PaymentError):
        svc.execute(j["id"], "1")
    assert store.spent_today("1") == 20


def test_task_lifecycle_checkpoints_retry_and_stale_recovery(dbf):
    s = TaskStore(dbf())
    t = s.create("1", ["step one", "step two"], "every 5m")
    now = t["next_run"] + 1
    assert len(s.claim_due(now)) == 1 and s.claim_due(now) == []          # claimed once only
    run = s.db.execute("SELECT * FROM runs").fetchone()
    assert s.begin_execution(run["id"], "w1", now, 300) and not s.begin_execution(run["id"], "w2", now, 300)
    s.save_checkpoint(run["id"], "s1", "first")
    s.save_checkpoint(run["id"], "s1", "first-again")                     # upsert, not duplicate
    assert s.checkpoints(run["id"]) == {"s1": "first-again"}
    assert s.recover_stale(now + 1000, 300, 3) == 1                       # worker died: heartbeat stale
    assert s.get_run(run["id"])["status"] == "retrying"
    assert len(s.claim_retries(now + 1000 + 31)) == 1
    assert s.get_run(run["id"])["attempt"] == 2 and s.checkpoints(run["id"])      # checkpoints survive


def test_worker_resumes_after_approval_wait_and_after_trigger(dbf):
    db = dbf()
    s, orch = TaskStore(db), FakeOrch()
    a = s.create("1", ["do a"], "now")
    b = s.create("1", ["do b"], f"after {a['id']}")
    w = TaskWorker(s, orch)
    w.tick(); w.tick()
    rb = s.db.execute("SELECT * FROM runs WHERE task_id=?", (b["id"],)).fetchone()
    assert rb["status"] == "completed" and "out:do a" in rb["input"]
    c = s.create("1", ["x"], "now")
    w.tick()
    run = s.db.execute("SELECT * FROM runs WHERE task_id=?", (c["id"],)).fetchone()
    s.db.execute("UPDATE runs SET status='running', worker='w' WHERE id=?", (run["id"],))
    s.set_waiting(run["id"], "ap1")
    assert s.get_run(run["id"])["status"] == "waiting" and s.resume_run(run["id"])
    assert len(s.claim_resumes(0)) == 1 and s.get_run(run["id"])["attempt"] == 1


def test_concurrent_claims_never_duplicate_a_run(dbf):
    s0 = TaskStore(dbf())
    ids = [s0.create("1", [f"t{i}"], "every 5m")["id"] for i in range(15)]
    s0.db.execute("UPDATE tasks SET next_run=1.0")
    claimed, errors = [], []

    def worker():
        try:
            st = TaskStore(dbf())                       # own connection = like another worker process
            claimed.extend(r["task_id"] for r in st.claim_due(10_000.0))
        except Exception as e:                           # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errors
    assert sorted(claimed) == sorted(ids)               # every task exactly once, across 4 workers
    assert s0.db.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 15


def test_credentials_tokens_usage_and_rotation(dbf):
    k1, k2 = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    db = dbf()
    c = CredentialStore(db, Vault(k1))
    cid = c.add("1", "openai", "m", "sk-secret-value-123456", 2.0, 8.0)
    c.record_call("1", cid, "chat", True, usage=(1000, 500), cost=0.006)
    c.record_call("1", cid, "chat", True, usage=(10, 5))
    u = c.usage_since("1", 0)
    assert (u["calls"], u["tokens"], u["unpriced"]) == (2, 1515, 1) and u["cost"] == pytest.approx(0.006)
    assert c.usage_by_model("1", 0)[0]["tokens"] == 1515
    c.set_budget("1", 100, 1.5)
    assert c.get_budget("1") == {"daily_tokens": 100, "daily_cost": 1.5}
    rot = CredentialStore(db, Vault(k2, [k1]))
    assert rot.rotate_all() == 1 and CredentialStore(db, Vault(k2)).secret("1", cid) == "sk-secret-value-123456"
    tok = c.issue_token("1")
    wins = []
    ts = [threading.Thread(target=lambda: wins.append(CredentialStore(dbf(), Vault(k1)).consume_token(tok))) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert [w for w in wins if w] == ["1"]               # single-use even under a race


def test_tool_approval_executes_at_most_once_under_a_race(dbf):
    ts = ToolStore(dbf())
    ap = ts.request_approval("1", "mcp.x.y", {"a": 1}, run_id=5, step_id="s1")
    assert ts.request_approval("1", "mcp.x.y", {"a": 1}, run_id=5, step_id="s1")["id"] == ap["id"]   # idempotent per step
    ts.approve(ap["id"], "1")
    with pytest.raises(ApprovalError):
        ts.approve(ap["id"], "2")
    got = []
    threads = [threading.Thread(target=lambda: got.append(ToolStore(dbf()).claim_execute(ap["id"]))) for _ in range(4)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert len([g for g in got if g]) == 1
    assert ts.db.execute("SELECT COUNT(*) AS n FROM approvals").fetchone()["n"] == 1
