"""Direct PayPal Payouts API client (Sandbox by default).
Docs: https://developer.paypal.com/docs/payouts/standard/  (treat docs as source of truth;
verify endpoint/field names against them before relying on this)."""
import base64

import httpx

BASES = {"sandbox": "https://api-m.sandbox.paypal.com", "live": "https://api-m.paypal.com"}


class PayPalUncertain(Exception):
    """Request may or may not have reached PayPal (timeout / lost response). Never blind-retry
    with a NEW key; reconcile instead."""


class PayPalRejected(Exception):
    """PayPal definitively refused the request."""


class PayPalClient:
    def __init__(self, client_id: str, secret: str, env: str = "sandbox",
                 transport: httpx.BaseTransport | None = None):
        self.base = BASES[env]
        self._auth = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
        self.http = httpx.Client(base_url=self.base, timeout=20, transport=transport)
        self._token: str | None = None

    def _bearer(self) -> str:
        if not self._token:
            r = self.http.post("/v1/oauth2/token", data={"grant_type": "client_credentials"},
                               headers={"Authorization": f"Basic {self._auth}"})
            r.raise_for_status()
            self._token = r.json()["access_token"]
        return self._token

    def access_token(self) -> str:
        """OAuth bearer token (also what the PayPal MCP server needs as PAYPAL_ACCESS_TOKEN)."""
        self._token = None
        return self._bearer()

    def create_payout(self, *, idempotency_key: str, recipient: str, amount: str,
                      currency: str, note: str) -> dict:
        body = {
            "sender_batch_header": {"sender_batch_id": idempotency_key,
                                    "email_subject": "You have a payment"},
            "items": [{"recipient_type": "EMAIL", "receiver": recipient, "note": note[:200],
                       "sender_item_id": idempotency_key,
                       "amount": {"value": amount, "currency": currency}}],
        }
        try:
            r = self.http.post("/v1/payments/payouts", json=body, headers={
                "Authorization": f"Bearer {self._bearer()}", "PayPal-Request-Id": idempotency_key})
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise PayPalUncertain(str(e)) from e
        if r.status_code >= 500:
            raise PayPalUncertain(f"HTTP {r.status_code}")
        if r.status_code >= 400:
            raise PayPalRejected(f"HTTP {r.status_code}: {r.text[:300]}")
        h = r.json()["batch_header"]
        return {"batch_id": h["payout_batch_id"], "status": h["batch_status"]}

    def get_batch_status(self, batch_id: str) -> str:
        r = self.http.get(f"/v1/payments/payouts/{batch_id}",
                          headers={"Authorization": f"Bearer {self._bearer()}"})
        r.raise_for_status()
        return r.json()["batch_header"]["batch_status"]


def map_status(provider_status: str) -> str:
    s = provider_status.upper()
    if s == "SUCCESS":
        return "succeeded"
    if s in ("DENIED", "CANCELED", "CANCELLED", "FAILED"):
        return "failed"
    return "processing"  # PENDING / PROCESSING / NEW: not money-moved yet
