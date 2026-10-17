from cryptography.fernet import Fernet

from butler.credentials import CredentialStore
from butler.llm.provider import LLMUnavailable
from butler.llm.router import ModelRouter
from butler.orchestrator import Orchestrator


def build(graph, fail=()):
    creds = CredentialStore(":memory:", Fernet.generate_key().decode())
    creds.add("1", "anthropic", "a", "key-anthropic-123456")
    creds.add("1", "openai", "o", "key-openai-1234567")
    seen = {}

    class Fake:
        def __init__(self, p, k, m): self.p = p
        def complete(self, system, messages, max_tokens=1000):
            if self.p in fail: raise LLMUnavailable("503")
            seen["system"] = system
            return f"hi from {self.p}"
    return Orchestrator(graph, ModelRouter(creds, lambda p, k, m: Fake(p, k, m))), seen


def test_graph_preference_reaches_prompt_and_trace_records_fallback(graph):
    orch, seen = build(graph, fail={"anthropic"})
    orch.memory("1").remember("email tone", "natural, never 'I hope this finds you well'")
    r = orch.chat("1", "hello")
    assert r.provider == "openai" and not r.attempts[0].ok
    assert "never 'I hope this finds you well'" in seen["system"]
    assert orch.last_trace["1"].prefs[0]["key"] == "email tone"


def test_correction_changes_next_prompt(graph):
    orch, seen = build(graph)
    m = orch.memory("1")
    m.remember("tone", "formal"); orch.chat("1", "hi"); assert "formal" in seen["system"]
    m.remember("tone", "casual"); orch.chat("1", "hi again")
    assert "casual" in seen["system"] and "formal" not in seen["system"]


def test_credential_ref_and_task_history_in_graph_hold_no_secrets(graph):
    m = graph and __import__("butler.graph.memory", fromlist=["Memory"]).Memory(graph.tenant("1"), "1")
    m.set_credential_ref(7, "openai", "o", "4567")
    m.record_task("t1", "daily news", "daily 09:00 UTC")
    m.record_run("t1", 1, "completed", "anthropic", "a")
    assert m.task_history("t1")[0]["provider"] == "anthropic"
    rows = m.tg.query("MATCH (c:CredentialReference) RETURN properties(c)")
    assert set(rows[0][0]) == {"id", "provider", "model", "last4"}
