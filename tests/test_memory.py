import pytest

from butler.graph.memory import Memory


def mem(graph, uid):
    return Memory(graph.tenant(uid), uid)


def test_preference_persists_and_scopes(graph):
    m = mem(graph, "1")
    m.remember("email tone", "concise")
    m.remember("report format", "bullets", scope="acme")
    assert [p["key"] for p in m.active(("global",))] == ["email tone"]
    assert {p["key"] for p in m.active(("global", "acme"))} == {"email tone", "report format"}


def test_correction_supersedes_and_keeps_provenance(graph):
    m = mem(graph, "1")
    m.remember("email tone", "formal")
    m.remember("email tone", "natural, no 'I hope this finds you well'")
    assert [p["value"] for p in m.active()] == ["natural, no 'I hope this finds you well'"]
    trace = m.why("email tone")
    assert trace[0]["replaced"] == "formal"


def test_forget_deletes_including_history(graph):
    m = mem(graph, "1")
    m.remember("tone", "a"); m.remember("tone", "b")
    assert m.forget("tone") == 2
    assert m.active() == [] and m.why("tone") == []


def test_tenant_isolation(graph):
    mem(graph, "1").remember("secret pref", "mine")
    mem(graph, "1").record_contact("Acme Ltd")
    other = mem(graph, "2")
    assert other.active() == []
    assert other.contact_trace("Acme Ltd") is None


def test_duplicate_outreach_detected_via_graph(graph):
    m = mem(graph, "1")
    assert m.contact_trace("Acme, Ltd.") is None
    m.record_contact("Acme Ltd")
    assert m.contact_trace("acme ltd")["company"] == "Acme Ltd"  # normalized key match


def test_injection_in_values_is_inert(graph):
    m = mem(graph, "1")
    m.remember("x", "'}) DETACH DELETE (u) //")
    assert m.active()[0]["value"].startswith("'})")


def test_bad_user_id_rejected(graph):
    with pytest.raises(ValueError):
        graph.tenant("1; DROP")
