"""Answers the hackathon's graph test directly: run the SAME scenarios with FalkorDB on and with it off
(BUTLER_GRAPH=off) and report what changes. Self-contained: scripted model, no network, no real tools."""
import json
import re
import time

from cryptography.fernet import Fernet

from .config import Settings
from .graph.memory import GraphDisabled
from .planner import PlanError, make_plan
from .runtime import build_runtime

SCENARIOS = ("1. stored preference reaches the model", "2. tool plan is validated and a tool chosen",
             "3. duplicate outreach is filtered", "4. dependent task waits for ALL parents",
             "5. impact of revoking a service is known")


def _runtime(graph_store, enabled: bool):
    seen = {"system": ""}
    script: list[str] = []

    class Prov:
        def __init__(self, model): self.last_usage = (1, 1)
        def complete(self, system, messages, max_tokens=1000):
            seen["system"] = system
            if script:
                return script.pop(0)
            p = messages[-1]["content"]
            return "Acme Ltd\nGlobex\nInitech" if "list companies" in p else "OK:" + p[:60]

    rt = build_runtime(Settings(master_key=Fernet.generate_key().decode(), db_path=":memory:", graph_enabled=enabled),
                       graph=graph_store if enabled else None, llm_factory=lambda p, k, m: Prov(m))
    rt.creds.add("1", "anthropic", "m", "ablation-key-123456")
    rt.ablation_seen, rt.ablation_script = seen, script
    return rt


def _llm(i, prompt, agent="assistant", deps=()):
    return {"id": i, "agent": agent, "kind": "llm", "prompt": prompt, "depends_on": list(deps)}


def run(enabled: bool, graph_store=None) -> dict[str, str]:
    rt = _runtime(graph_store, enabled)
    mem, out = rt.orch.memory("1"), {}

    # 1. preferences
    try:
        mem.remember("tone", "blunt, no filler")
        rt.orch.chat("1", "hello")
        out[SCENARIOS[0]] = ("applied: the stored preference is in the model's prompt"
                             if "blunt" in rt.ablation_seen["system"] else "NOT applied")
    except GraphDisabled:
        rt.orch.chat("1", "hello")
        out[SCENARIOS[0]] = "UNAVAILABLE: nothing can be remembered, so the model never sees it"

    # 2. planner: capability -> tool resolution happens in the graph
    rt.ensure_builtins("1")
    plan = json.dumps({"steps": [
        {"id": "s1", "agent": "researcher", "kind": "llm", "prompt": "list companies"},
        {"id": "s2", "agent": "analyst", "kind": "tool", "capability": "contacts.filter",
         "args": {"companies": "{{s1}}"}, "depends_on": ["s1"]}]})
    rt.ablation_script[:] = [plan, plan]
    try:
        steps, _ = make_plan(rt.router, mem, rt.tools, "1", "find new prospects")
        out[SCENARIOS[1]] = f"valid: graph resolved capability contacts.filter -> {steps[1]['tool_id']}"
    except PlanError as e:
        out[SCENARIOS[1]] = "REJECTED: " + str(e)[:80]
    rt.ablation_script.clear()

    # 3. duplicate outreach, inside a real task run
    mem.record_contacts(["Acme Ltd"])
    steps3 = [_llm("s1", "list companies", "researcher"),
              {"id": "s2", "agent": "analyst", "kind": "tool", "capability": "contacts.filter",
               "tool_id": "contacts.filter_new", "args": {"companies": "{{s1}}"}, "depends_on": ["s1"]}]
    t3 = rt.create_task("1", steps3, "now", objective="prospect")
    rt.worker.tick()
    run3 = rt.db.execute("SELECT id FROM runs WHERE task_id=?", (t3["id"],)).fetchone()
    res = json.loads(rt.tasks.checkpoints(run3["id"])["s2"])
    out[SCENARIOS[2]] = (f"kept {res['new']}; dropped already-contacted {[d['company'] for d in res['already_contacted']]}"
                         if enabled else f"kept {res['new']} (duplicate of Acme Ltd NOT caught). {res.get('warning', '')}")

    # 4. dependencies: C runs after A and B. B is slower.
    a = rt.create_task("1", [_llm("s1", "A work")], "now", objective="A")
    b = rt.create_task("1", [_llm("s1", "B work")], "daily 09:00", objective="B")
    c = rt.create_task("1", [_llm("s1", "C uses both")], f"after {a['id']},{b['id']}", objective="C")
    rt.worker.tick()                                      # A runs and completes, which triggers C
    rt.worker.tick()                                      # scheduler decides about C while B has NOT finished
    ran_early = rt.db.execute("SELECT COUNT(*) AS n FROM runs WHERE task_id=?", (c["id"],)).fetchone()["n"] > 0
    rt.db.execute("UPDATE tasks SET next_run=? WHERE id=?", (time.time() - 1, b["id"]))
    for _ in range(3):
        rt.worker.tick()
    runs = [r["input"] or "" for r in rt.db.execute("SELECT input FROM runs WHERE task_id=? ORDER BY id", (c["id"],))]
    names = {a["id"]: "A", b["id"]: "B"}
    src = lambda txt: sorted(names[x] for x in re.findall(r"\[from task (\w+)\]", txt) if x in names)
    out[SCENARIOS[3]] = (f"C ran {len(runs)}x; "
                         + ("it ran BEFORE B finished, " if ran_early else "it waited for B, ")
                         + "inputs per run: " + " | ".join("+".join(src(r)) or "none" for r in runs))

    # 5. impact analysis
    live = rt.create_task("1", [{"id": "s1", "agent": "analyst", "kind": "tool", "capability": "contacts.filter",
                                 "tool_id": "contacts.filter_new", "args": {"companies": "x"}, "depends_on": []}],
                          "daily 09:00", objective="weekly prospect check")
    rt.create_task("1", [_llm("s1", "report")], f"after {live['id']}", objective="report")
    imp = mem.impact("contacts")
    out[SCENARIOS[4]] = ("UNAVAILABLE: a disconnect would proceed blind" if imp is None else
                         f"revoking 'contacts' breaks {len(imp)} task(s) + {sum(len(x['downstream']) for x in imp)} dependent(s)")
    return out


def report(graph_store) -> str:
    on, off = run(True, graph_store), run(False)
    w = max(len(k) for k in SCENARIOS)
    lines = ["SAME scenarios, same code: FalkorDB on vs BUTLER_GRAPH=off", ""]
    for k in SCENARIOS:
        lines += [k, f"    graph ON : {on[k]}", f"    graph OFF: {off[k]}", ""]
    return "\n".join(lines)
