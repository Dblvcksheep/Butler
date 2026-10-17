"""Celery deployment of the task runtime (BUTLER_WORKER_MODE=celery).

  celery -A butler.celery_app worker -l info          # executes runs
  celery -A butler.celery_app beat   -l info          # dispatches due work every TASK_POLL_SECONDS

Safe with many workers: claiming is atomic in the database and begin_execution() makes a run single-owner
even if the broker redelivers it. Crashed workers are detected by stale heartbeats and re-queued."""
import os

from celery import Celery

from .config import Settings

app = Celery("butler", broker=os.environ.get("REDIS_URL", "redis://localhost:6379/1"))
app.conf.update(task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1,
                timezone="UTC", beat_schedule={"dispatch": {
                    "task": "butler.dispatch", "schedule": float(os.environ.get("TASK_POLL_SECONDS", "30"))}})
_rt = None


def runtime():
    global _rt
    if _rt is None:
        from .runtime import build_runtime
        _rt = build_runtime(Settings.from_env())
    return _rt


@app.task(name="butler.dispatch")
def dispatch() -> int:
    return runtime().worker.dispatch(lambda rid: execute_run.delay(rid))


@app.task(name="butler.execute_run")
def execute_run(run_id: int) -> None:
    runtime().worker.run_one(run_id)
