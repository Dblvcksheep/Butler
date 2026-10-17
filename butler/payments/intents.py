"""Durable payment-intent records (SQLite for the MVP; swap for Postgres later).

Safety properties enforced here, in plain code, never by the LLM:
  * approval is bound to a fingerprint of (user, recipient, amount, currency, note)
  * changing any of those clears the approval
  * execution is claimed atomically (approved -> submitting) so double dispatch can't pay twice
  * the intent id is the provider idempotency key and is never regenerated
"""
import hashlib
import json
import re
import time
import uuid

from butler.db import Db
from decimal import Decimal, InvalidOperation

STATES = ("awaiting_approval", "approved", "submitting", "processing",
          "succeeded", "failed", "unresolved", "cancelled")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class PaymentError(Exception):
    pass


def validate(recipient: str, amount: str, currency: str, allowed: frozenset[str]) -> tuple[str, str, str]:
    if not _EMAIL.match(recipient or ""):
        raise PaymentError("invalid recipient email")
    try:
        amt = Decimal(str(amount)).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise PaymentError("invalid amount")
    if amt <= 0:
        raise PaymentError("amount must be positive")
    cur = (currency or "").upper()
    if cur not in allowed:
        raise PaymentError(f"currency {cur or '?'} not allowed")
    return recipient.strip().lower(), str(amt), cur


def fingerprint(user_id: str, recipient: str, amount: str, currency: str, note: str) -> str:
    blob = json.dumps([str(user_id), recipient, amount, currency, note], separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


class IntentStore:
    def __init__(self, path=":memory:"):
        self.db = Db.coerce(path)
        self.db.ddl("""CREATE TABLE IF NOT EXISTS intents(
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, recipient TEXT NOT NULL,
            amount TEXT NOT NULL, currency TEXT NOT NULL, note TEXT NOT NULL,
            status TEXT NOT NULL, approved_fp TEXT, batch_id TEXT, provider_status TEXT,
            error TEXT, created REAL, updated REAL)""")
        self.db.ddl("""CREATE TABLE IF NOT EXISTS audit(
            ts REAL, intent_id TEXT, event TEXT, detail TEXT)""")

    def log(self, iid: str, event: str, detail: str = "") -> None:
        self.db.execute("INSERT INTO audit VALUES(?,?,?,?)", (time.time(), iid, event, detail))

    def create(self, user_id, recipient, amount, currency, note) -> dict:
        iid = uuid.uuid4().hex
        now = time.time()
        self.db.execute(
            "INSERT INTO intents(id,user_id,recipient,amount,currency,note,status,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (iid, str(user_id), recipient, amount, currency, note, "awaiting_approval", now, now))
        self.log(iid, "created", f"{amount} {currency} -> {recipient}")
        return self.get(iid)

    def get(self, iid: str) -> dict:
        row = self.db.execute("SELECT * FROM intents WHERE id=?", (iid,)).fetchone()
        if not row:
            raise PaymentError("unknown intent")
        return dict(row)

    def owned(self, iid: str, user_id: str) -> dict:
        i = self.get(iid)
        if i["user_id"] != str(user_id):
            raise PaymentError("not your payment")  # tenant check
        return i

    def transition(self, iid: str, from_states: tuple[str, ...], to: str, **fields) -> bool:
        """Atomic compare-and-set on status. Returns False if the state wasn't as expected."""
        sets = ", ".join(f"{k}=?" for k in fields)
        q = f"UPDATE intents SET status=?, updated=?{', ' + sets if sets else ''} " \
            f"WHERE id=? AND status IN ({','.join('?' * len(from_states))})"
        cur = self.db.execute(q, (to, time.time(), *fields.values(), iid, *from_states))
        ok = cur.rowcount == 1
        if ok:
            self.log(iid, f"-> {to}", json.dumps({k: v for k, v in fields.items() if k != "approved_fp"}))
        return ok

    def approve(self, iid: str, user_id: str) -> dict:
        i = self.owned(iid, user_id)
        fp = fingerprint(i["user_id"], i["recipient"], i["amount"], i["currency"], i["note"])
        if not self.transition(iid, ("awaiting_approval",), "approved", approved_fp=fp):
            raise PaymentError(f"cannot approve from state {i['status']}")
        return self.get(iid)

    def amend(self, iid: str, user_id: str, allowed: frozenset[str], **changes) -> dict:
        """Any change to material fields voids the approval and re-requests it."""
        i = self.owned(iid, user_id)
        if i["status"] not in ("awaiting_approval", "approved"):
            raise PaymentError(f"cannot amend from state {i['status']}")
        merged = {k: changes.get(k, i[k]) for k in ("recipient", "amount", "currency", "note")}
        r, a, c = validate(merged["recipient"], merged["amount"], merged["currency"], allowed)
        self.transition(iid, ("awaiting_approval", "approved"), "awaiting_approval",
                        recipient=r, amount=a, currency=c, note=merged["note"], approved_fp=None)
        return self.get(iid)

    def spent_today(self, user_id: str) -> "Decimal":
        since = time.time() - 86400
        rows = self.db.execute(
            "SELECT amount FROM intents WHERE user_id=? AND created>? AND status IN "
            "('submitting','processing','succeeded','unresolved')", (str(user_id), since)).fetchall()
        return sum((Decimal(r["amount"]) for r in rows), Decimal("0"))
