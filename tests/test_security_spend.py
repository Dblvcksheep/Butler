import json
import threading
import time

import httpx
import pytest
from cryptography.fernet import Fernet, InvalidToken

from butler.credentials import CredentialStore, Vault
from butler.db import Db
from butler.llm.provider import (LLMAuthError, LLMBadRequest, LLMRateLimit, LLMUnavailable, build_provider)
from butler.llm.router import BudgetExceeded, ModelRouter
from butler.notify import TelegramNotifier
from butler.web import create_app


# ---------- master-key rotation ----------
def test_master_key_rotation_reencrypts_everything():
    k1, k2 = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    db = Db(":memory:")
    old = CredentialStore(db, Vault(k1))
    cid = old.add("1", "openai", "m", "sk-secret-value-123456")
    old.set_service("1", "paypal", {"client_id": "a", "client_secret": "b", "env": "sandbox"})
    new = CredentialStore(db, Vault(k2, [k1]))                    # during rotation both keys are configured
    assert new.secret("1", cid) == "sk-secret-value-123456"       # old data still readable
    assert new.rotate_all() == 2
    only_new = CredentialStore(db, Vault(k2))
    assert only_new.secret("1", cid) == "sk-secret-value-123456"
    assert only_new.get_service("1", "paypal")["client_secret"] == "b"
    with pytest.raises(InvalidToken):
        CredentialStore(db, Vault(k1)).secret("1", cid)           # old key alone no longer decrypts
    new.add("1", "qwen", "q", "fresh-key-1234567")                # new writes use the primary key
    assert only_new.secret("1", new.list_creds("1")[-1]["id"]) == "fresh-key-1234567"


# ---------- "at least one model, all providers optional" ----------
def test_any_single_provider_is_enough_and_none_means_no_models():
    c = CredentialStore(":memory:", Fernet.generate_key().decode())
    assert not c.has_active_model("1")
    c.add("1", "qwen", "q", "only-qwen-key-123456")                # e.g. no Claude, no GPT
    assert c.has_active_model("1")
    c.set_status(c.list_creds("1")[0]["id"], "invalid")
    assert not c.has_active_model("1")


# ---------- spend tracking and budgets ----------
class UsageProv:
    def __init__(self, usage): self.last_usage = usage
    def complete(self, system, messages, max_tokens=1000): return "ok"


def spend_setup(prices=(2.0, 10.0)):
    c = CredentialStore(":memory:", Fernet.generate_key().decode())
    c.add("1", "anthropic", "a", "key-anthropic-123456", *prices)
    return c, ModelRouter(c, lambda p, k, m: UsageProv((1000, 500)))


def test_cost_is_computed_from_user_supplied_prices_and_summed():
    c, r = spend_setup()
    for _ in range(3):
        r.complete("1", "chat", "s", [])
    u = c.usage_since("1", 0)
    assert u["calls"] == 3 and u["tokens"] == 4500
    assert u["cost"] == pytest.approx(3 * (1000 * 2.0 + 500 * 10.0) / 1_000_000)
    assert c.usage_by_model("1", 0)[0]["calls"] == 3


def test_unpriced_models_count_tokens_but_not_cost():
    c, r = spend_setup(prices=(None, None))
    r.complete("1", "chat", "s", [])
    u = c.usage_since("1", 0)
    assert u["tokens"] == 1500 and u["cost"] == 0 and u["unpriced"] == 1


def test_token_budget_blocks_further_calls_and_is_per_user():
    c, r = spend_setup()
    c.add("2", "anthropic", "a", "key-user-two-123456")
    c.set_budget("1", 2500, None)
    r.complete("1", "chat", "s", []); r.complete("1", "chat", "s", [])      # 3000 tokens used
    with pytest.raises(BudgetExceeded):
        r.complete("1", "chat", "s", [])
    assert r.complete("2", "chat", "s", []).text == "ok"                    # user 2 unaffected
    c.set_budget("1", None, None)
    assert r.complete("1", "chat", "s", []).text == "ok"                    # budget off


def test_cost_budget():
    c, r = spend_setup()
    c.set_budget("1", None, 0.007)                                          # one call costs 0.007
    r.complete("1", "chat", "s", [])
    with pytest.raises(BudgetExceeded):
        r.complete("1", "chat", "s", [])


def test_budget_exceeded_stops_a_scheduled_task_without_retry(rt):
    rt.creds.set_budget("1", 1, None)
    rt.router.complete("1", "chat", "s", [{"role": "user", "content": "hi"}])    # uses 15 tokens: over budget now
    t = rt.tasks.create("1", ["x"], "now")
    rt.worker.tick()
    run = rt.db.execute("SELECT * FROM runs").fetchone()
    assert run["status"] == "failed" and run["attempt"] == 1 and "budget" in run["error"].lower()


# ---------- providers: usage capture, request shape, typed errors ----------
def transport(status=200, body=None, capture=None):
    def h(req: httpx.Request):
        if capture is not None:
            capture.append((str(req.url), dict(req.headers), json.loads(req.content)))
        return httpx.Response(status, json=body or {})
    return httpx.MockTransport(h)


def test_anthropic_request_and_usage():
    cap = []
    p = build_provider("anthropic", "k-anthropic-1", "m", transport(
        body={"content": [{"type": "text", "text": "hi"}], "usage": {"input_tokens": 7, "output_tokens": 3}}, capture=cap))
    assert p.complete("sys", [{"role": "user", "content": "x"}]) == "hi" and p.last_usage == (7, 3)
    assert cap[0][0] == "https://api.anthropic.com/v1/messages" and cap[0][1]["x-api-key"] == "k-anthropic-1"


@pytest.mark.parametrize("provider,url,param", [
    ("openai", "https://api.openai.com/v1/chat/completions", "max_completion_tokens"),
    ("nebius", "https://api.tokenfactory.nebius.com/v1/chat/completions", "max_tokens"),
    ("qwen", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions", "max_tokens"),
])
def test_openai_compatible_providers(provider, url, param):
    cap = []
    p = build_provider(provider, "k-123456789", "some-model", transport(
        body={"choices": [{"message": {"content": "yo"}}], "usage": {"prompt_tokens": 4, "completion_tokens": 2}}, capture=cap))
    assert p.complete("sys", [{"role": "user", "content": "x"}], 50) == "yo" and p.last_usage == (4, 2)
    assert cap[0][0] == url and cap[0][1]["authorization"] == "Bearer k-123456789"
    assert cap[0][2][param] == 50 and cap[0][2]["messages"][0] == {"role": "system", "content": "sys"}


def test_base_url_override(monkeypatch):
    monkeypatch.setenv("NEBIUS_BASE_URL", "https://example.test/v1/")
    cap = []
    p = build_provider("nebius", "k-123456789", "m", transport(body={"choices": [{"message": {"content": ""}}]}, capture=cap))
    p.complete("", [])
    assert cap[0][0] == "https://example.test/v1/chat/completions"


@pytest.mark.parametrize("status,exc", [(401, LLMAuthError), (403, LLMAuthError), (429, LLMRateLimit),
                                        (500, LLMUnavailable), (529, LLMUnavailable), (400, LLMBadRequest)])
def test_http_errors_map_to_typed_errors_and_never_echo_the_key(status, exc):
    p = build_provider("openai", "sk-leaky-key-123456", "m", transport(status, {"error": "bad key sk-leaky-key-123456"}))
    with pytest.raises(exc) as e:
        p.complete("", [])
    assert "sk-leaky-key-123456" not in str(e.value)


def test_network_timeout_is_unavailable():
    def boom(req): raise httpx.ReadTimeout("slow", request=req)
    with pytest.raises(LLMUnavailable):
        build_provider("qwen", "k-123456789", "m", httpx.MockTransport(boom)).complete("", [])


# ---------- notifier ----------
def test_telegram_notifier_sends_inline_buttons():
    cap = []
    n = TelegramNotifier("TOKEN", transport(body={"ok": True}, capture=cap))
    n(42, "hello", [("Approve", "tap:1"), ("Reject", "tnr:1")])
    url, _, body = cap[0]
    assert url.endswith("/botTOKEN/sendMessage") and body["chat_id"] == 42
    assert body["reply_markup"]["inline_keyboard"][0][0] == {"text": "Approve", "callback_data": "tap:1"}


# ---------- web: PayPal connect page ----------
def paypal_app(token_fn, allow_live=False, seen=None):
    creds = CredentialStore(":memory:", Fernet.generate_key().decode())
    ev = threading.Event()
    def connected(uid, svc):
        if seen is not None: seen.append((uid, svc))
        ev.set()
    app = create_app(creds, None, on_connected=connected, allow_live=allow_live, paypal_token=token_fn)
    return creds, app.test_client(), ev


def test_paypal_page_stores_encrypted_creds_and_triggers_discovery():
    seen = []
    creds, c, ev = paypal_app(lambda i, s, e: "tok", seen=seen)
    t = creds.issue_token("7", purpose="paypal")
    assert b"Connect PayPal" in c.get(f"/setup/{t}").data
    r = c.post(f"/setup/{t}", data={"env": "sandbox", "client_id": "cid", "client_secret": "sec-123"})
    assert r.status_code == 200 and b"sec-123" not in r.data
    assert creds.get_service("7", "paypal") == {"client_id": "cid", "client_secret": "sec-123", "env": "sandbox"}
    assert ev.wait(3) and seen == [("7", "paypal")]
    assert c.post(f"/setup/{t}", data={"env": "sandbox", "client_id": "x", "client_secret": "y"}).status_code == 410


def test_paypal_page_rejects_bad_credentials_and_live_mode_when_disabled():
    def bad(i, s, e): raise httpx.HTTPStatusError("401", request=httpx.Request("POST", "http://x"),
                                                   response=httpx.Response(401))
    creds, c, _ = paypal_app(bad)
    t = creds.issue_token("7", purpose="paypal")
    assert c.post(f"/setup/{t}", data={"env": "sandbox", "client_id": "a", "client_secret": "b"}).status_code == 400
    assert creds.get_service("7", "paypal") is None
    creds2, c2, _ = paypal_app(lambda i, s, e: "tok", allow_live=False)
    t2 = creds2.issue_token("7", purpose="paypal")
    assert c2.post(f"/setup/{t2}", data={"env": "live", "client_id": "a", "client_secret": "b"}).status_code == 400


def test_llm_page_accepts_prices_for_spend_tracking():
    creds = CredentialStore(":memory:", Fernet.generate_key().decode())
    class F:
        def __init__(self, p, k, m): pass
        def validate(self): pass
    c = create_app(creds, None, factory=F).test_client()
    t = creds.issue_token("9")
    c.post(f"/setup/{t}", data={"provider": "nebius", "model": "nvidia/nemotron-x", "api_key": "nb-key-123456789",
                                "price_in": "0.5", "price_out": "1.5"})
    row = creds.list_creds("9")[0]
    assert (row["price_in"], row["price_out"]) == (0.5, 1.5)


# ---------- hackathon evidence ----------
def test_hackathon_report_requires_a_real_nebius_call_to_an_nvidia_model():
    from butler.cli import hackathon_report
    creds = CredentialStore(":memory:", Fernet.generate_key().decode())
    cid = creds.add("1", "nebius", "nvidia/some-nemotron-model", "nb-key-123456789")
    assert "FAIL" in hackathon_report(creds)                      # configured but never actually called
    creds.record_call("1", cid, "chat", True, usage=(5, 5))
    rep = hackathon_report(creds)
    assert "PASS" in rep and "nvidia/some-nemotron-model" in rep
    other = creds.add("1", "nebius", "some-other-model", "nb-key-987654321")
    creds.record_call("1", other, "chat", True)
    assert rep.count("PASS") == 1 and "FAIL" not in hackathon_report(creds)
