import pytest
from cryptography.fernet import Fernet

from butler.credentials import CredentialStore
from butler.llm.provider import LLMAuthError, LLMBadRequest, LLMRateLimit, LLMUnavailable
from butler.llm.router import AllModelsFailed, ModelRouter, classify


class Clock:
    t = 1000.0
    def __call__(self): return self.t


def setup(behaviors, providers=("anthropic", "openai", "nebius", "qwen")):
    creds = CredentialStore(":memory:", Fernet.generate_key().decode())
    for p in providers:
        creds.add("1", p, f"{p}-model", f"key-for-{p}-123456")
    calls = []

    class Fake:
        def __init__(self, p, key, model): self.p, self.key = p, key
        def complete(self, system, messages, max_tokens=1000):
            calls.append(self.p)
            b = behaviors.get(self.p, "ok")
            if isinstance(b, Exception): raise b
            return f"answer from {self.p}"

    clock = Clock()
    return creds, ModelRouter(creds, lambda p, k, m: Fake(p, k, m), clock, cooldown=60), calls, clock


def test_uses_first_model_when_healthy():
    _, r, calls, _ = setup({})
    res = r.complete("1", "chat", "sys", [{"role": "user", "content": "hi"}])
    assert (res.provider, calls) == ("anthropic", ["anthropic"])


def test_falls_back_through_chain_in_order():
    _, r, calls, _ = setup({"anthropic": LLMUnavailable("HTTP 503"), "openai": LLMRateLimit("HTTP 429"),
                            "nebius": LLMBadRequest("HTTP 400")})
    res = r.complete("1", "chat", "s", [])
    assert res.provider == "qwen" and calls == ["anthropic", "openai", "nebius", "qwen"]
    assert [a.ok for a in res.attempts] == [False, False, False, True]


def test_auth_failure_disables_that_key():
    creds, r, calls, _ = setup({"anthropic": LLMAuthError("HTTP 401")})
    assert r.complete("1", "chat", "s", []).provider == "openai"
    assert [c["status"] for c in creds.list_creds("1")][0] == "invalid"
    calls.clear()
    r.complete("1", "chat", "s", [])
    assert "anthropic" not in calls          # invalid keys are not retried


def test_cooldown_skips_recently_failed_model_then_recovers():
    _, r, calls, clock = setup({"anthropic": LLMRateLimit("HTTP 429")})
    r.complete("1", "chat", "s", []); calls.clear()
    r.complete("1", "chat", "s", [])
    assert calls == ["openai"]               # anthropic cooling down
    clock.t += 61; calls.clear()
    r.complete("1", "chat", "s", [])
    assert calls[0] == "anthropic"           # tried again after cooldown


def test_per_task_kind_order_and_default_order():
    creds, r, calls, _ = setup({})
    creds.set_order("1", "code", ["qwen", "anthropic"])
    assert r.complete("1", "code", "s", []).provider == "qwen"
    assert r.complete("1", "chat", "s", []).provider == "anthropic"
    creds.set_order("1", "default", ["nebius"])
    assert r.complete("1", "chat", "s", []).provider == "nebius"


def test_all_fail_and_no_models():
    _, r, _, _ = setup({p: LLMUnavailable("down") for p in ("anthropic", "openai")}, providers=("anthropic", "openai"))
    with pytest.raises(AllModelsFailed) as e:
        r.complete("1", "chat", "s", [])
    assert len(e.value.attempts) == 2 and not e.value.no_models
    with pytest.raises(AllModelsFailed) as e2:
        r.complete("2", "chat", "s", [])      # user 2 has no keys: can't use user 1's
    assert e2.value.no_models


def test_secret_redacted_from_errors_and_never_leaks_across_users():
    creds, r, _, _ = setup({"anthropic": RuntimeError("boom key-for-anthropic-123456")},
                           providers=("anthropic",))
    with pytest.raises(AllModelsFailed) as e:
        r.complete("1", "chat", "s", [])
    assert "key-for-anthropic-123456" not in str(e.value)
    from butler.credentials import CredentialError
    with pytest.raises(CredentialError):
        creds.secret("2", creds.list_creds("1")[0]["id"])


def test_classify():
    assert classify("fix this python bug") == "code"
    assert classify("draft an email to Sam") == "drafting"
    assert classify("hello") == "chat"
