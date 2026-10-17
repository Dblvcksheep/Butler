from decimal import Decimal

from .intents import IntentStore, PaymentError, validate
from .paypal import PayPalClient, PayPalRejected, PayPalUncertain, map_status


class PaymentService:
    def __init__(self, store: IntentStore, paypal, settings):
        """`paypal` is a PayPalClient, or a callable user_id -> PayPalClient (per-user credentials)."""
        self.store, self._pp, self.s = store, paypal, settings

    def _client(self, user_id: str) -> PayPalClient:
        return self._pp if hasattr(self._pp, "create_payout") else self._pp(user_id)

    def prepare(self, user_id: str, recipient: str, amount: str, currency: str, note: str) -> dict:
        r, a, c = validate(recipient, amount, currency, self.s.allowed_currencies)
        if Decimal(a) > Decimal(self.s.max_per_tx):
            raise PaymentError(f"over per-transaction limit ({self.s.max_per_tx})")
        return self.store.create(user_id, r, a, c, note.strip()[:200])

    def execute(self, iid: str, user_id: str) -> dict:
        i = self.store.owned(iid, user_id)
        # re-check everything at execution time; don't trust earlier checks
        from .intents import fingerprint
        fp = fingerprint(i["user_id"], i["recipient"], i["amount"], i["currency"], i["note"])
        if i["status"] != "approved" or i["approved_fp"] != fp:
            raise PaymentError("no valid approval for these exact details")
        if Decimal(i["amount"]) > Decimal(self.s.max_per_tx):
            raise PaymentError("over per-transaction limit")
        if self.store.spent_today(user_id) + Decimal(i["amount"]) > Decimal(self.s.max_per_day):
            raise PaymentError("over daily limit")
        # atomic claim: only one caller can move approved -> submitting
        if not self.store.transition(iid, ("approved",), "submitting"):
            raise PaymentError("already being executed")
        try:
            res = self._client(user_id).create_payout(idempotency_key=iid, recipient=i["recipient"],
                                        amount=i["amount"], currency=i["currency"], note=i["note"])
        except PayPalUncertain as e:
            self.store.transition(iid, ("submitting",), "unresolved", error=str(e)[:300])
            return self.store.get(iid)
        except PayPalRejected as e:
            self.store.transition(iid, ("submitting",), "failed", error=str(e)[:300])
            return self.store.get(iid)
        self.store.transition(iid, ("submitting",), map_status(res["status"]),
                              batch_id=res["batch_id"], provider_status=res["status"])
        return self.store.get(iid)

    def reconcile(self, iid: str, user_id: str) -> dict:
        """Resolve processing/unresolved intents against PayPal. Re-submits (if needed) only
        with the SAME idempotency key, so a lost response can't become a second payout."""
        i = self.store.owned(iid, user_id)
        if i["status"] == "processing" and i["batch_id"]:
            ps = self._client(user_id).get_batch_status(i["batch_id"])
            self.store.transition(iid, ("processing",), map_status(ps), provider_status=ps)
        elif i["status"] == "unresolved":
            try:
                res = self._client(user_id).create_payout(idempotency_key=iid, recipient=i["recipient"],
                                            amount=i["amount"], currency=i["currency"], note=i["note"])
                self.store.transition(iid, ("unresolved",), map_status(res["status"]),
                                      batch_id=res["batch_id"], provider_status=res["status"])
            except PayPalRejected as e:
                self.store.transition(iid, ("unresolved",), "failed", error=str(e)[:300])
            except PayPalUncertain:
                pass  # still unresolved; stay put
        return self.store.get(iid)
