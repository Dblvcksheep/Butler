import time

import pytest

from butler import celery_app


def due_task(rt):
    return rt.tasks.create("1", ["do the thing"], "now")


def test_dispatch_claims_and_executes_eagerly(rt, monkeypatch):
    monkeypatch.setattr(celery_app, "_rt", rt)
    celery_app.app.conf.task_always_eager = True
    try:
        t = due_task(rt)
        assert celery_app.dispatch() == 1
        run = rt.db.execute("SELECT * FROM runs WHERE task_id=?", (t["id"],)).fetchone()
        assert run["status"] == "completed"
        assert celery_app.dispatch() == 0                       # nothing left; no duplicate dispatch
    finally:
        celery_app.app.conf.task_always_eager = False


def test_redelivered_message_does_not_execute_a_run_twice(rt, monkeypatch):
    monkeypatch.setattr(celery_app, "_rt", rt)
    t = due_task(rt)
    run = rt.tasks.claim_due(time.time() + 1)[0]
    calls = []
    orig = rt.orch.run_step
    rt.orch.run_step = lambda *a, **k: (calls.append(1), orig(*a, **k))[1]
    celery_app.execute_run(run["id"])                           # first delivery runs it
    celery_app.execute_run(run["id"])                           # broker redelivers: no-op
    assert len(calls) == 1


def test_real_broker_and_worker_end_to_end(rt, monkeypatch, tmp_path):
    """Celery worker + Redis broker (embedded redis-server via redislite): beat-style dispatch, worker executes."""
    try:
        import redislite
        from celery.contrib.testing.worker import start_worker
        r = redislite.Redis(str(tmp_path / "redis.db"))
        sock = r.socket_file
    except Exception as e:
        pytest.skip(f"embedded redis unavailable: {e}")
    monkeypatch.setattr(celery_app, "_rt", rt)
    app = celery_app.app
    app.conf.update(broker_url=f"redis+socket://{sock}", task_always_eager=False,
                    broker_connection_retry_on_startup=True)       # no result backend: we poll the database
    app._pool = None                                           # earlier tests may have cached a pool on the default broker
    if "amqp" in app.__dict__:
        app.amqp._producer_pool = None
    t = due_task(rt)
    try:
        with start_worker(app, perform_ping_check=False, pool="solo", loglevel="WARNING", shutdown_timeout=20):
            celery_app.dispatch.delay()                                  # dispatcher runs ON the worker, which then
                                                                         # enqueues and executes execute_run
            for _ in range(60):
                row = rt.db.execute("SELECT status FROM runs WHERE task_id=?", (t["id"],)).fetchone()
                if row and row["status"] == "completed":
                    break
                time.sleep(0.5)
        assert row["status"] == "completed"
    finally:                                                        # don't leak the embedded redis-server
        app._pool = None
        from conftest import stop_embedded
        stop_embedded(r)
