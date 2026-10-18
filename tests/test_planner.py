import json

import pytest

from butler.planner import PlanError, describe_plan, extract_json, make_plan, validate_plan


def js(steps):
    return json.dumps({"steps": steps})


def test_extract_json_accepts_raw_newlines_in_model_strings():
    text = 'Plan follows:\n{"steps":[{"id":"s1","agent":"writer","kind":"llm",'
    text += '"prompt":"first line\nsecond line"}]}\n'

    parsed = extract_json(text)

    assert parsed["steps"][0]["prompt"] == "first line\nsecond line"


def test_email_plan_requires_llm_draft_and_no_premature_contact_record():
    class Mem:
        def resolve_capability(self, cap):
            return [{"tool_id": {"contacts.record": "contacts.record",
                                 "email.send": "email.send_batch"}[cap]}]

    class Tools:
        schemas = {
            "contacts.record": {"type": "object", "properties": {"companies": {"type": "string"}},
                                "required": ["companies"]},
            "email.send_batch": {"type": "object", "properties": {"messages": {"type": "string"}},
                                 "required": ["messages"]},
        }

        def get_tool(self, tool_id):
            return {"input_schema": self.schemas[tool_id]}

    plan = {"steps": [
        {"id": "s1", "agent": "operator", "capability": "contacts.record",
         "args": {"companies": "Acme"}},
        {"id": "s2", "agent": "writer", "prompt": "draft emails", "depends_on": ["s1"]},
        {"id": "s3", "agent": "operator", "capability": "email.send",
         "args": {"messages": "{{s1}}"}, "depends_on": ["s2"]},
    ]}
    with pytest.raises(PlanError) as e:
        validate_plan(plan, Mem(), Tools())
    assert "email messages must reference an llm step" in str(e.value)
    assert "do not run contacts.record before email.send_batch" in str(e.value)
    assert "kind must be" not in str(e.value)


S1 = {"id": "s1", "agent": "researcher", "kind": "tool", "capability": "data.search", "args": {"query": "AI news"}}
S2 = {"id": "s2", "agent": "writer", "kind": "llm", "prompt": "write a briefing", "depends_on": ["s1"]}


def plan(rt, *scripted):
    rt.connect_service("1", "fake")
    rt.script.extend(scripted)
    return make_plan(rt.router, rt.orch.memory("1"), rt.tools, "1", "daily AI briefing")


def test_valid_plan_resolves_tools_through_the_graph(rt):
    steps, res = plan(rt, js([S1, S2]))
    assert [s["id"] for s in steps] == ["s1", "s2"]
    assert steps[0]["tool_id"] == "mcp.fake.list_items"          # chosen by graph query, not by the model
    assert "needs" not in describe_plan(steps, rt.tools)


def test_model_supplied_tool_id_is_ignored(rt):
    evil = dict(S1, tool_id="mcp.fake.create_order")
    steps, _ = plan(rt, js([evil, S2]))
    assert steps[0]["tool_id"] == "mcp.fake.list_items"


def test_invented_capability_rejected_after_one_repair_attempt(rt):
    bad = dict(S1, agent="operator", capability="shell.exec")
    with pytest.raises(PlanError) as e:
        plan(rt, js([bad]), js([bad]))
    assert "no authorized tool for capability 'shell.exec'" in str(e.value)
    assert rt.script == []                                       # exactly two model calls consumed


def test_repair_attempt_can_fix_a_bad_plan(rt):
    steps, _ = plan(rt, "not json at all", js([S1, S2]))
    assert len(steps) == 2


def test_fenced_json_is_accepted(rt):
    steps, _ = plan(rt, "```json\n" + js([dict(S2, depends_on=[])]) + "\n```")
    assert steps[0]["id"] == "s2" and steps[0]["kind"] == "llm"


@pytest.mark.parametrize("steps,msg", [
    ([dict(S1, agent="writer")], "may not use capability"),                       # writer has no tools
    ([dict(S1, agent="ghost")], "unknown agent"),
    ([dict(S1, args={"nope": 1})], "unknown args"),
    ([{"id": "s1", "agent": "operator", "kind": "tool", "capability": "fake.orders.create", "args": {"amount": "1"}}],
     "missing required args"),
    ([dict(S2, id="a", depends_on=["b"]), dict(S2, id="b", depends_on=["a"])], "cycle"),
    ([dict(S2, depends_on=["zzz"])], "unknown step"),
    ([dict(S2, id=f"s{i}", depends_on=[]) for i in range(9)], "too many steps"),
    ([dict(S2, prompt="")], "needs a prompt"),
    ([{"id": "s1", "kind": "shell", "agent": "operator"}], "kind must be"),
])
def test_invalid_plans_rejected_in_code(rt, steps, msg):
    with pytest.raises(PlanError) as e:
        plan(rt, js(steps), js(steps))
    assert msg in str(e.value)


def test_plan_only_uses_tools_the_user_connected(rt):
    rt.script.append(js([S1]))
    rt.script.append(js([S1]))                                   # fake NOT connected
    with pytest.raises(PlanError):
        make_plan(rt.router, rt.orch.memory("1"), rt.tools, "1", "goal")


def test_llm_only_plan_needs_no_tools(rt):
    rt.script.append(js([{"id": "s1", "agent": "writer", "kind": "llm", "prompt": "hi"}]))
    steps, _ = make_plan(rt.router, rt.orch.memory("1"), rt.tools, "1", "say hi")
    assert steps[0]["kind"] == "llm"


def test_model_written_plan_executes_end_to_end_with_a_real_mcp_tool(rt):
    steps, _ = plan(rt, js([S1, S2]))
    rt.tasks.create("1", steps, "now", objective="daily AI briefing")
    rt.script.append("BRIEFING DONE")                            # s2 (llm) output
    rt.worker.tick()
    run = rt.db.execute("SELECT * FROM runs").fetchone()
    assert run["status"] == "completed" and run["output"] == "BRIEFING DONE"
    assert "widget" in rt.tasks.checkpoints(run["id"])["s1"]     # the real MCP tool ran
