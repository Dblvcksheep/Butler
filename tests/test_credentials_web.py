import sqlite3

import pytest
from cryptography.fernet import Fernet

from butler.credentials import CredentialError, CredentialStore
from butler.llm.provider import LLMAuthError, LLMUnavailable
from butler.web import create_app


def store(tmp_path=None):
    return CredentialStore(str(tmp_path / "c.db") if tmp_path else ":memory:", Fernet.generate_key().decode())


def test_key_encrypted_at_rest(tmp_path):
    s = store(tmp_path)
    cid = s.add("1", "openai", "m1", "sk-super-secret-value-9999")
    raw = open(tmp_path / "c.db", "rb").read()
    assert b"sk-super-secret-value-9999" not in raw
    assert s.secret("1", cid) == "sk-super-secret-value-9999"
    assert s.list_creds("1")[0]["last4"] == "9999" and "enc" not in s.list_creds("1")[0]


def test_rotation_replaces_key_and_reactivates():
    s = store()
    cid = s.add("1", "openai", "m1", "old-key-12345678")
    s.set_status(cid, "invalid")
    assert s.add("1", "openai", "m1", "new-key-12345678") == cid
    assert s.secret("1", cid) == "new-key-12345678" and s.list_creds("1")[0]["status"] == "active"


def test_input_validation():
    s = store()
    for args in [("nope", "m", "k" * 12), ("openai", "bad model!", "k" * 12),
                 ("openai", "m", "short"), ("openai", "m", "has space in key")]:
        with pytest.raises(CredentialError):
            s.add("1", *args)


def test_token_single_use_and_expiry(monkeypatch):
    s = store()
    t = s.issue_token("1")
    assert s.peek_token(t) == "1"
    assert s.consume_token(t) == "1"
    assert s.consume_token(t) is None and s.peek_token(t) is None
    t2 = s.issue_token("1", ttl=-1)
    assert s.consume_token(t2) is None


def make_app(validate=None):
    s = store()

    class F:
        def __init__(self, p, k, m): pass
        def validate(self):
            if validate: raise validate
    return s, create_app(s, None, factory=F).test_client()


def test_web_flow_stores_key_once_and_never_echoes_it():
    s, c = make_app()
    t = s.issue_token("42")
    assert c.get(f"/setup/{t}").status_code == 200
    r = c.post(f"/setup/{t}", data={"provider": "qwen", "model": "m-1", "api_key": "sk-web-secret-123456"})
    assert r.status_code == 200 and b"sk-web-secret-123456" not in r.data
    assert r.headers["Referrer-Policy"] == "no-referrer" and r.headers["Cache-Control"] == "no-store"
    assert s.list_creds("42")[0]["provider"] == "qwen"
    again = c.post(f"/setup/{t}", data={"provider": "qwen", "model": "m-2", "api_key": "sk-other-123456789"})
    assert again.status_code == 410 and len(s.list_creds("42")) == 1


def test_web_rejects_bad_token_and_rejected_key():
    s, c = make_app()
    assert c.get("/setup/not-a-token").status_code == 410
    s2, c2 = make_app(validate=LLMAuthError("HTTP 401"))
    t = s2.issue_token("1")
    r = c2.post(f"/setup/{t}", data={"provider": "openai", "model": "m", "api_key": "bad-key-123456789"})
    assert r.status_code == 400 and s2.list_creds("1") == []


def test_web_saves_when_provider_temporarily_unavailable():
    s, c = make_app(validate=LLMUnavailable("HTTP 503"))
    t = s.issue_token("1")
    r = c.post(f"/setup/{t}", data={"provider": "nebius", "model": "m", "api_key": "good-key-123456789"})
    assert r.status_code == 200 and b"couldn't verify" in r.data and len(s.list_creds("1")) == 1
