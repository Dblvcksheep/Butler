import pytest

from butler.tasks.store import TaskError


def steps2():
    return [{"id": "s1", "agent": "researcher", "kind": "llm", "prompt": "collect", "depends_on": []},
            {"id": "s2", "agent": "writer", "kind": "llm", "prompt": "write", "depends_on": ["s1"]}]


def register(rt, t, uid="1"):
    rt.orch.memory(uid).record_task(t["id"], t["objective"], "x")


def run_of(rt, tid):
    return rt.db.execute("SELECT * FROM runs WHERE task_id=?", (tid,)).fetchone()


def test_downstream_task_runs_after_upstream_with_its_output_and_graph_records_it(rt):
    a = rt.tasks.create("1", steps2(), "now", objective="A")
    register(rt, a)
    b = rt.tasks.create("1", ["use upstream"], f"after {a['id']}", objective="B")
    register(rt, b)
    rt.orch.memory("1").record_dependency(b["id"], a["id"])
    assert b["next_run"] is None                                   # not time-driven
    rt.worker.tick()                                               # A runs, finishes, triggers B
    ra = run_of(rt, a["id"])
    assert ra["status"] == "completed"
    assert rt.tasks.get_task(b["id"])["next_run"] is not None
    rt.worker.tick()                                               # B runs with A's output as data
    rb = run_of(rt, b["id"])
    assert rb["status"] == "completed" and ra["output"] in rb["input"] and "[from task" in rb["input"]
    assert "upstream task" in rb["output"] and ra["output"][:20] in rb["output"]
    rows = rt.orch.memory("1").tg.query(
        "MATCH (c:Task {id:$c})-[:DEPENDS_ON]->(p:Task {id:$p}) RETURN count(*)", {"c": b["id"], "p": a["id"]})
    assert rows[0][0] == 1                                         # (Task)-[:DEPENDS_ON]->(Task)


def test_agent_handoffs_recorded_in_graph(rt):
    a = rt.tasks.create("1", steps2(), "now", objective="A")
    register(rt, a)
    rt.worker.tick()
    ra = run_of(rt, a["id"])
    assert rt.orch.memory("1").handoffs(ra["id"]) == [("researcher", "s1", "writer", "s2")]
    agents = rt.orch.memory("1").tg.query("MATCH (g:Agent)-[:EXECUTED]->(:Step) RETURN g.name ORDER BY g.name")
    assert [r[0] for r in agents] == ["researcher", "writer"]


def test_cannot_depend_on_another_users_or_unknown_task(rt):
    a = rt.tasks.create("1", ["x"], "now")
    with pytest.raises(TaskError):
        rt.tasks.create("2", ["y"], f"after {a['id']}")
    with pytest.raises(TaskError):
        rt.tasks.create("1", ["y"], "after deadbeef")


def test_paused_downstream_task_does_not_fire(rt):
    a = rt.tasks.create("1", ["x"], "now")
    b = rt.tasks.create("1", ["y"], f"after {a['id']}")
    rt.tasks.set_status(b["id"], "1", "paused")
    rt.worker.tick()
    rt.worker.tick()
    assert run_of(rt, b["id"]) is None


def test_recurring_upstream_triggers_downstream_every_time(rt):
    a = rt.tasks.create("1", ["x"], "every 5m")
    b = rt.tasks.create("1", ["y"], f"after {a['id']}")
    for n in (1, 2):
        rt.db.execute("UPDATE tasks SET next_run=? WHERE id=?", (0.0 + n, a["id"]))   # make A due again
        rt.worker.tick(); rt.worker.tick()
    assert rt.db.execute("SELECT COUNT(*) AS n FROM runs WHERE task_id=?", (b["id"],)).fetchone()["n"] == 2
