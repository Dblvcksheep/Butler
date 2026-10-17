import json

import httpx
import pytest

from butler.payments.intents import IntentStore, PaymentError
from butler.payments.paypal import PayPalClient
from butler.payments.service import PaymentService


def make(settings, handler):
    store = IntentStore()
    pp = PayPalClient("id", "secret", transport=httpx.MockTransport(handler))
    return PaymentService(store, pp, settings), store


def ok_handler(calls):
    def h(req: httpx.Request):
        if req.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "t"})
        if req.method == "POST":
            calls.append(json.loads(req.content))
            assert req.headers["PayPal-Request-Id"]
            return httpx.Response(201, json={"batch_header": {"payout_batch_id": "B1", "batch_status": "PENDING"}})
        return httpx.Response(200, json={"batch_header": {"batch_status": "SUCCESS"}})
    return h


def test_requires_approval(settings):
    svc, _ = make(settings, ok_handler([]))
    i = svc.prepare("1", "a@b.co", "20", "usd", "design")
    with pytest.raises(PaymentError):
        svc.execute(i["id"], "1")


def test_happy_path_pending_is_not_paid_until_reconciled(settings):
    calls = []
    svc, store = make(settings, ok_handler(calls))
    i = svc.prepare("1", "a@b.co", "20", "usd", "design")
    store.approve(i["id"], "1")
    assert svc.execute(i["id"], "1")["status"] == "processing"   # accepted != paid
    assert svc.reconcile(i["id"], "1")["status"] == "succeeded"
    assert len(calls) == 1 and calls[0]["sender_batch_header"]["sender_batch_id"] == i["id"]


def test_amount_change_voids_approval(settings):
    svc, store = make(settings, ok_handler([]))
    i = svc.prepare("1", "a@b.co", "20", "usd", "design")
    store.approve(i["id"], "1")
    store.amend(i["id"], "1", settings.allowed_currencies, amount="45")
    with pytest.raises(PaymentError):
        svc.execute(i["id"], "1")


def test_double_execute_pays_once(settings):
    calls = []
    svc, store = make(settings, ok_handler(calls))
    i = svc.prepare("1", "a@b.co", "20", "usd", "x")
    store.approve(i["id"], "1")
    svc.execute(i["id"], "1")
    with pytest.raises(PaymentError):
        svc.execute(i["id"], "1")
    assert len(calls) == 1


def test_other_user_cannot_approve(settings):
    svc, store = make(settings, ok_handler([]))
    i = svc.prepare("1", "a@b.co", "20", "usd", "x")
    with pytest.raises(PaymentError):
        store.approve(i["id"], "2")


def test_limits_and_validation(settings):
    svc, _ = make(settings, ok_handler([]))
    with pytest.raises(PaymentError):
        svc.prepare("1", "a@b.co", "51", "usd", "x")        # per-tx
    with pytest.raises(PaymentError):
        svc.prepare("1", "not-an-email", "5", "usd", "x")
    with pytest.raises(PaymentError):
        svc.prepare("1", "a@b.co", "5", "xyz", "x")
    with pytest.raises(PaymentError):
        svc.prepare("1", "a@b.co", "-5", "usd", "x")


def test_daily_limit(settings):
    svc, store = make(settings, ok_handler([]))
    for _ in range(2):
        i = svc.prepare("1", "a@b.co", "50", "usd", "x")
        store.approve(i["id"], "1"); svc.execute(i["id"], "1")
    i = svc.prepare("1", "a@b.co", "1", "usd", "x")
    store.approve(i["id"], "1")
    with pytest.raises(PaymentError):
        svc.execute(i["id"], "1")


def test_timeout_is_unresolved_and_reconcile_reuses_same_key(settings):
    state = {"n": 0, "keys": []}

    def h(req):
        if req.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "t"})
        state["keys"].append(req.headers["PayPal-Request-Id"])
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ReadTimeout("lost response", request=req)
        return httpx.Response(201, json={"batch_header": {"payout_batch_id": "B9", "batch_status": "SUCCESS"}})

    svc, store = make(settings, h)
    i = svc.prepare("1", "a@b.co", "20", "usd", "x")
    store.approve(i["id"], "1")
    assert svc.execute(i["id"], "1")["status"] == "unresolved"
    with pytest.raises(PaymentError):                       # no blind second submit via execute()
        svc.execute(i["id"], "1")
    assert svc.reconcile(i["id"], "1")["status"] == "succeeded"
    assert state["keys"][0] == state["keys"][1] == i["id"]


def test_provider_rejection_marks_failed(settings):
    def h(req):
        if req.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(400, json={"name": "VALIDATION_ERROR"})
    svc, store = make(settings, h)
    i = svc.prepare("1", "a@b.co", "20", "usd", "x")
    store.approve(i["id"], "1")
    assert svc.execute(i["id"], "1")["status"] == "failed"
