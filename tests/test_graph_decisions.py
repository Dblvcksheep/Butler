"""The graph must DECIDE things. Each test here would fail if FalkorDB were removed or ignored."""
import inspect
import json
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from butler import ablation
from butler.config import Settings
from butler.docs_graph import render
from butler.graph.memory import GraphDisabled, Memory, NullMemory
from butler.planner import make_plan
from butler.runtime import build_runtime
from butler.tools.executor import parse_names


def llm(txt, agent="assistant"):
    return [{"id": "s1", "agent": agent, "kind": "llm", "prompt": txt, "depends_on": []}]


def runs_of(rt, tid):
    return [dict(r) for r in rt.db.execute("SELECT * FROM runs WHERE task_id=? ORDER BY id", (tid,))]


def make_abc(rt):
    a = rt.create_task("1", llm("A work"), "now", objective="A")
    b = rt.create_task("1", llm("B work"), "daily 09:00", objective="B")
    c = rt.create_task("1", llm("C uses both"), f"after {a['id']},{b['id']}", objective="C")
    return a, b, c


# ---------- scheduler readiness is decided by the graph ----------
def test_dependent_task_waits_for_all_parents_and_receives_every_parents_output(rt):
    a, b, c = make_abc(rt)
    rt.worker.tick(); rt.worker.tick()                          # A runs; C triggered; graph says B isn't done
    assert runs_of(rt, c["id"]) == []
    assert [x["id"] for x in rt.orch.memory("1").blocking_parents(c["id"])] == [b["id"]]
    rt.db.execute("UPDATE tasks SET next_run=? WHERE id=?", (time.time() - 1, b["id"]))
    for _ in range(3):
        rt.worker.tick()
    cr = runs_of(rt, c["id"])
    assert len(cr) == 1 and cr[0]["status"] == "completed"
    assert f"[from task {a['id']}]" in cr[0]["input"] and f"[from task {b['id']}]" in cr[0]["input"]


def test_scheduler_fails_closed_when_the_graph_is_unreachable(rt):
    a, b, c = make_abc(rt)
    rt.worker.tick(); rt.worker.tick()
    rt.db.execute("UPDATE tasks SET next_run=? WHERE id=?", (time.time() - 1, b["id"]))
    rt.worker.tick()                                            # B runs and completes; graph now says all done
    real = rt.orch.memory

    def boom(uid): raise RuntimeError("falkordb down")
    rt.orch.memory = boom
    rt.worker.tick(); rt.worker.tick()
    assert runs_of(rt, c["id"]) == []                           # unknown readiness => do NOT run blind
    rt.orch.memory = real
    rt.worker.tick()
    assert len(runs_of(rt, c["id"])) == 1                       # graph back: it runs


def test_without_the_graph_the_same_scenario_runs_early_on_partial_input(graph):
    rt = ablation._runtime(None, enabled=False)
    a, b, c = make_abc(rt)
    rt.worker.tick(); rt.worker.tick()
    early = runs_of(rt, c["id"])
    assert len(early) == 1 and f"[from task {b['id']}]" not in early[0]["input"]    # degraded behaviour, by design


def test_blocked_tasks_follow_chains_and_name_root_causes(rt):
    mem = rt.orch.memory("1")
    a = rt.create_task("1", llm("a"), "daily 09:00", objective="A")
    b = rt.create_task("1", llm("b"), f"after {a['id']}", objective="B")
    c = rt.create_task("1", llm("c"), f"after {b['id']}", objective="C")
    mem.set_task_status(a["id"], last="failed")
    blocked = {e["id"]: e for e in mem.blocked_tasks()}
    assert set(blocked) == {b["id"], c["id"]}
    assert [r["id"] for r in blocked[c["id"]]["root_causes"]] == [a["id"]]
    assert max(x["depth"] for x in blocked[c["id"]]["blockers"]) == 2          # transitive: 2 hops
    assert [d["id"] for d in mem.downstream(a["id"])] == [b["id"], c["id"]] or \
           {d["id"] for d in mem.downstream(a["id"])} == {b["id"], c["id"]}


def test_failure_notice_lists_the_dependents_that_will_not_run(rt):
    bad = [{"id": "s1", "agent": "researcher", "kind": "tool", "capability": "data.search",
            "tool_id": "mcp.fake.list_items", "args": {"query": "x"}, "depends_on": []}]   # fake not connected
    a = rt.create_task("1", bad, "now", objective="A")
    b = rt.create_task("1", llm("b"), f"after {a['id']}", objective="B depends on A")
    rt.worker.tick()
    msg = [n[1] for n in rt.notes if "stopped" in n[1]][0]
    assert "will not run" in msg and b["id"] in msg


# ---------- impact analysis ----------
def test_impact_lists_tasks_and_their_dependents_and_ignores_cancelled_ones(rt):
    rt.connect_service("1", "fake")
    mem = rt.orch.memory("1")
    step = [{"id": "s1", "agent": "researcher", "kind": "tool", "capability": "data.search",
             "tool_id": "mcp.fake.list_items", "args": {"query": "x"}, "depends_on": []}]
    t = rt.create_task("1", step, "daily 09:00", objective="check items")
    d = rt.create_task("1", llm("report"), f"after {t['id']}", objective="report")
    imp = mem.impact("fake")
    assert [(x["id"], x["downstream"]) for x in imp] == [(t["id"], [d["id"]])]
    assert mem.impact("paypal") == []                           # unrelated service: nothing breaks
    mem.set_task_status(t["id"], status="cancelled")
    assert mem.impact("fake") == []
    assert rt.orch.memory("2").impact("fake") == []             # tenant isolation


# ---------- /why: explanation is read from graph paths ----------
def test_explain_task_reads_plan_ranking_preferences_and_handoffs_from_the_graph(rt):
    rt.connect_service("1", "fake")
    mem = rt.orch.memory("1")
    mem.remember("tone", "blunt")
    steps = [{"id": "s1", "agent": "researcher", "kind": "tool", "capability": "data.search",
              "tool_id": "mcp.fake.list_items", "args": {"query": "x"}, "depends_on": []},
             {"id": "s2", "agent": "writer", "kind": "llm", "prompt": "summarise", "depends_on": ["s1"]}]
    t = rt.create_task("1", steps, "now", objective="items digest")
    rt.worker.tick()
    ex = mem.explain_task(t["id"])
    s1 = ex["steps"][0]
    assert s1["tool"] == "mcp.fake.list_items" and s1["candidates"][0]["tool_id"] == "mcp.fake.list_items"
    assert s1["candidates"][0]["ok"] == 1                       # the real call's success was written back
    assert ex["steps"][1]["after"] == ["s1"]
    assert ex["last_run"]["status"] == "completed"
    assert ex["handoffs"] == [("researcher", "s1", "writer", "s2")]
    assert ex["preferences"][0]["key"] == "tone"
    assert mem.explain_task("nope") is None
    assert rt.orch.memory("2").explain_task(t["id"]) is None


# ---------- duplicate prevention inside a plan ----------
def test_prospecting_plan_dedupes_against_the_graph_across_runs(rt):
    rt.ensure_builtins("1")
    plan = json.dumps({"steps": [
        {"id": "s1", "agent": "researcher", "kind": "llm", "prompt": "list companies"},
        {"id": "s2", "agent": "analyst", "kind": "tool", "capability": "contacts.filter",
         "args": {"companies": "{{s1}}"}, "depends_on": ["s1"]},
        {"id": "s3", "agent": "analyst", "kind": "tool", "capability": "contacts.record",
         "args": {"companies": "{{s2}}"}, "depends_on": ["s2"]}]})
    rt.script.append(plan)
    steps, _ = make_plan(rt.router, rt.orch.memory("1"), rt.tools, "1", "find prospects")
    assert [s.get("tool_id") for s in steps] == [None, "contacts.filter_new", "contacts.record"]

    def run_once(companies):
        rt.script.append(companies)
        t = rt.create_task("1", steps, "now", objective="prospects")
        rt.worker.tick()
        rid = runs_of(rt, t["id"])[0]["id"]
        cp = rt.tasks.checkpoints(rid)
        return json.loads(cp["s2"]), cp["s3"]

    first, rec = run_once("Acme Ltd\nGlobex")
    assert first["new"] == ["Acme Ltd", "Globex"] and first["already_contacted"] == [] and "recorded 2" in rec
    second, _ = run_once("- Globex\n- Initech\n- acme ltd")
    assert second["new"] == ["Initech"]
    assert {d["company"] for d in second["already_contacted"]} == {"Globex", "Acme Ltd"}


def test_agent_permissions_apply_to_the_contacts_tools(rt):
    rt.ensure_builtins("1")
    plan = json.dumps({"steps": [{"id": "s1", "agent": "writer", "kind": "tool", "capability": "contacts.filter",
                                  "args": {"companies": "x"}}]})
    rt.script += [plan, plan]
    from butler.planner import PlanError
    with pytest.raises(PlanError):
        make_plan(rt.router, rt.orch.memory("1"), rt.tools, "1", "goal")       # the writer has no tools


@pytest.mark.parametrize("raw,expected", [
    ("Acme Ltd\nGlobex", ["Acme Ltd", "Globex"]),
    ("- Acme Ltd\n* Globex\n3. Initech", ["Acme Ltd", "Globex", "Initech"]),
    ('["A Co", "B Co", "a co"]', ["A Co", "B Co"]),
    ('{"new": ["X", "Y"], "already_contacted": []}', ["X", "Y"]),
    ("a, b,, c", ["a", "b", "c"]),
    ("", []),
])
def test_parse_names(raw, expected):
    assert parse_names(raw) == expected


# ---------- graph off + ablation ----------
def test_graph_off_runtime_has_no_graph_and_memory_degrades_visibly():
    rt = ablation._runtime(None, enabled=False)
    assert rt.orch.graph is None and isinstance(rt.orch.memory("1"), NullMemory)
    with pytest.raises(GraphDisabled):
        rt.orch.memory("1").remember("a", "b")
    assert rt.orch.memory("1").blocked_tasks() is None and rt.orch.memory("1").impact("x") is None
    assert rt.orch.chat("1", "hi").text.startswith("OK:")        # chat itself still works


def test_ablation_graph_on_vs_off_differs_on_every_scenario(graph):
    on, off = ablation.run(True, graph), ablation.run(False)
    S = ablation.SCENARIOS
    assert set(on) == set(off) == set(S)
    assert all(on[k] != off[k] for k in S)
    assert "applied" in on[S[0]] and "UNAVAILABLE" in off[S[0]]
    assert "valid" in on[S[1]] and "REJECTED" in off[S[1]]
    assert "dropped already-contacted ['Acme Ltd']" in on[S[2]] and "NOT caught" in off[S[2]]
    assert "C ran 1x" in on[S[3]] and "waited for B" in on[S[3]]
    assert "C ran 2x" in off[S[3]] and "BEFORE B finished" in off[S[3]]
    assert "breaks 1 task" in on[S[4]] and "UNAVAILABLE" in off[S[4]]


# ---------- documentation cannot drift from the code ----------
def test_graph_doc_is_generated_from_the_code_and_covers_every_query():
    doc = (Path(__file__).parent.parent / "docs" / "GRAPH.md").read_text()
    assert doc == render(), "docs/GRAPH.md is stale: run `python -m butler.cli graphdoc`"
    for name, fn in inspect.getmembers(Memory, inspect.isfunction):
        if "tg.query(" in inspect.getsource(fn):
            assert f"### `{name}`" in doc, f"{name} has Cypher but is missing from docs/GRAPH.md"
