"""Encrypted API-key + service-secret storage, single-use setup tokens, model ordering, usage and budgets.
Raw secrets exist only (a) in the web form POST, (b) encrypted in the DB, (c) briefly in memory when used.
The graph only gets non-secret metadata."""
import hashlib
import json
import re
import secrets
import time

from cryptography.fernet import Fernet, MultiFernet

from .db import Db
from .llm.provider import PROVIDERS

MODEL_RE = re.compile(r"^[\w./:+\-]{1,100}$")
TASK_KINDS = ("default", "chat", "research", "drafting", "code", "extraction", "planning")


class CredentialError(Exception):
    pass


class Vault:
    """Encrypts with the first key, decrypts with any: rotation = add new key first, run rotate, drop old."""

    def __init__(self, primary: str, old: tuple[str, ...] | list[str] = ()):
        mk = lambda k: Fernet(k.encode() if isinstance(k, str) else k)
        self.mf = MultiFernet([mk(primary), *[mk(k) for k in old]])

    def encrypt(self, data: bytes) -> bytes:
        return self.mf.encrypt(data)

    def decrypt(self, token) -> bytes:
        return self.mf.decrypt(bytes(token))

    def rotate(self, token) -> bytes:
        return self.mf.rotate(bytes(token))


class CredentialStore:
    def __init__(self, path, master_key, old_keys=()):
        self.vault = master_key if isinstance(master_key, Vault) else Vault(master_key, old_keys)
        self.db = Db.coerce(path)
        for ddl in (
            """CREATE TABLE IF NOT EXISTS credentials(id INTEGER PRIMARY KEY AUTOINCREMENT,
               user_id TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, enc BLOB NOT NULL,
               last4 TEXT, priority INTEGER, status TEXT NOT NULL DEFAULT 'active', created REAL,
               price_in REAL, price_out REAL, UNIQUE(user_id, provider, model))""",
            """CREATE TABLE IF NOT EXISTS service_secrets(user_id TEXT NOT NULL, service TEXT NOT NULL,
               enc BLOB NOT NULL, updated REAL, PRIMARY KEY(user_id, service))""",
            """CREATE TABLE IF NOT EXISTS task_order(user_id TEXT, task_kind TEXT, providers TEXT,
               PRIMARY KEY(user_id, task_kind))""",
            """CREATE TABLE IF NOT EXISTS setup_tokens(hash TEXT PRIMARY KEY, user_id TEXT,
               expires REAL, used INTEGER DEFAULT 0, purpose TEXT DEFAULT 'llm')""",
            """CREATE TABLE IF NOT EXISTS llm_calls(ts REAL, user_id TEXT, cred_id INTEGER, task_kind TEXT,
               ok INTEGER, error TEXT, input_tokens INTEGER, output_tokens INTEGER, cost REAL)""",
            """CREATE TABLE IF NOT EXISTS budgets(user_id TEXT PRIMARY KEY, daily_tokens INTEGER, daily_cost REAL)""",
        ):
            self.db.ddl(ddl)

    # ---- model credentials ----
    def add(self, user_id: str, provider: str, model: str, api_key: str,
            price_in: float | None = None, price_out: float | None = None) -> int:
        api_key, model = api_key.strip(), model.strip()
        if provider not in PROVIDERS:
            raise CredentialError("unknown provider")
        if not MODEL_RE.match(model):
            raise CredentialError("invalid model id")
        if not (8 <= len(api_key) <= 512) or re.search(r"\s", api_key):
            raise CredentialError("invalid API key format")
        for p in (price_in, price_out):
            if p is not None and not (0 <= p < 10_000):
                raise CredentialError("invalid price")
        n = self.db.execute("SELECT COUNT(*) AS n FROM credentials WHERE user_id=?", (str(user_id),)).fetchone()["n"]
        self.db.execute(
            "INSERT INTO credentials(user_id,provider,model,enc,last4,priority,status,created,price_in,price_out) "
            "VALUES(?,?,?,?,?,?,'active',?,?,?) ON CONFLICT(user_id,provider,model) DO UPDATE SET "
            "enc=excluded.enc, last4=excluded.last4, status='active', price_in=excluded.price_in, "
            "price_out=excluded.price_out",
            (str(user_id), provider, model, self.vault.encrypt(api_key.encode()), api_key[-4:], n,
             time.time(), price_in, price_out))
        return self.db.execute("SELECT id FROM credentials WHERE user_id=? AND provider=? AND model=?",
                               (str(user_id), provider, model)).fetchone()["id"]

    def list_creds(self, user_id: str) -> "list[dict]":
        rows = self.db.execute("SELECT id,provider,model,last4,priority,status,price_in,price_out FROM credentials "
                               "WHERE user_id=? ORDER BY priority, id", (str(user_id),)).fetchall()
        return [dict(r) for r in rows]

    def set_price(self, user_id: str, cred_id: int, price_in: float, price_out: float) -> bool:
        if not (0 <= price_in < 10_000 and 0 <= price_out < 10_000):
            raise CredentialError("invalid price")
        return self.db.execute("UPDATE credentials SET price_in=?, price_out=? WHERE id=? AND user_id=?",
                               (price_in, price_out, cred_id, str(user_id))).rowcount == 1

    def has_active_model(self, user_id: str) -> bool:
        return any(r["status"] == "active" for r in self.list_creds(user_id))

    def secret(self, user_id: str, cred_id: int) -> str:
        r = self.db.execute("SELECT enc FROM credentials WHERE id=? AND user_id=?",
                            (cred_id, str(user_id))).fetchone()
        if not r:
            raise CredentialError("no such credential")  # also the tenant check
        return self.vault.decrypt(r["enc"]).decode()

    def remove(self, user_id: str, cred_id: int) -> dict | None:
        r = self.db.execute("SELECT id,provider,model FROM credentials WHERE id=? AND user_id=?",
                            (cred_id, str(user_id))).fetchone()
        if r:
            self.db.execute("DELETE FROM credentials WHERE id=?", (cred_id,))
        return dict(r) if r else None

    def set_status(self, cred_id: int, status: str) -> None:
        self.db.execute("UPDATE credentials SET status=? WHERE id=?", (status, cred_id))

    # ---- other service secrets (e.g. PayPal client credentials) ----
    def set_service(self, user_id: str, service: str, data: dict) -> None:
        self.db.execute(
            "INSERT INTO service_secrets(user_id,service,enc,updated) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id,service) DO UPDATE SET enc=excluded.enc, updated=excluded.updated",
            (str(user_id), service, self.vault.encrypt(json.dumps(data).encode()), time.time()))

    def get_service(self, user_id: str, service: str) -> dict | None:
        r = self.db.execute("SELECT enc FROM service_secrets WHERE user_id=? AND service=?",
                            (str(user_id), service)).fetchone()
        return json.loads(self.vault.decrypt(r["enc"])) if r else None

    def remove_service(self, user_id: str, service: str) -> bool:
        return self.db.execute("DELETE FROM service_secrets WHERE user_id=? AND service=?",
                               (str(user_id), service)).rowcount == 1

    def rotate_all(self) -> int:
        """Re-encrypt every stored secret under the primary key. Safe to re-run."""
        n = 0
        for table, keys in (("credentials", ("id",)), ("service_secrets", ("user_id", "service"))):
            for r in self.db.execute(f"SELECT {','.join(keys)}, enc FROM {table}").fetchall():
                where = " AND ".join(f"{k}=?" for k in keys)
                self.db.execute(f"UPDATE {table} SET enc=? WHERE {where}",
                                (self.vault.rotate(r["enc"]), *[r[k] for k in keys]))
                n += 1
        return n

    # ---- usage, prices, budgets ----
    def record_call(self, user_id, cred_id, task_kind, ok: bool, error: str = "",
                    usage: tuple[int, int] | None = None, cost: float | None = None) -> None:
        i, o = usage if usage else (None, None)
        self.db.execute("INSERT INTO llm_calls VALUES(?,?,?,?,?,?,?,?,?)",
                        (time.time(), str(user_id), cred_id, task_kind, int(ok), error[:200], i, o, cost))

    def usage_since(self, user_id: str, since: float) -> dict:
        r = self.db.execute(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens),0) AS i, COALESCE(SUM(output_tokens),0) AS o, "
            "COALESCE(SUM(cost),0) AS cost, SUM(CASE WHEN ok=1 AND cost IS NULL THEN 1 ELSE 0 END) AS unpriced "
            "FROM llm_calls WHERE user_id=? AND ts>=? AND ok=1", (str(user_id), since)).fetchone()
        return dict(calls=r["calls"], tokens=int(r["i"]) + int(r["o"]), input=int(r["i"]), output=int(r["o"]),
                    cost=float(r["cost"]), unpriced=int(r["unpriced"] or 0))

    def usage_by_model(self, user_id: str, since: float) -> list[dict]:
        rows = self.db.execute(
            "SELECT c.provider AS provider, c.model AS model, COUNT(*) AS calls, "
            "COALESCE(SUM(l.input_tokens),0) AS i, COALESCE(SUM(l.output_tokens),0) AS o, "
            "COALESCE(SUM(l.cost),0) AS cost FROM llm_calls l JOIN credentials c ON c.id=l.cred_id "
            "WHERE l.user_id=? AND l.ts>=? AND l.ok=1 GROUP BY c.provider, c.model ORDER BY calls DESC",
            (str(user_id), since)).fetchall()
        return [dict(provider=r["provider"], model=r["model"], calls=r["calls"],
                     tokens=int(r["i"]) + int(r["o"]), cost=float(r["cost"])) for r in rows]

    def set_budget(self, user_id: str, daily_tokens: int | None, daily_cost: float | None) -> None:
        self.db.execute("INSERT INTO budgets VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                        "daily_tokens=excluded.daily_tokens, daily_cost=excluded.daily_cost",
                        (str(user_id), daily_tokens, daily_cost))

    def get_budget(self, user_id: str) -> dict:
        r = self.db.execute("SELECT daily_tokens, daily_cost FROM budgets WHERE user_id=?", (str(user_id),)).fetchone()
        return dict(r) if r else {"daily_tokens": None, "daily_cost": None}

    # ---- per-task-kind provider order ----
    def set_order(self, user_id: str, task_kind: str, providers: "list[str]") -> None:
        if task_kind not in TASK_KINDS:
            raise CredentialError(f"task kind must be one of {', '.join(TASK_KINDS)}")
        bad = [p for p in providers if p not in PROVIDERS]
        if bad:
            raise CredentialError(f"unknown provider: {bad[0]}")
        self.db.execute("INSERT INTO task_order VALUES(?,?,?) ON CONFLICT(user_id,task_kind) "
                        "DO UPDATE SET providers=excluded.providers", (str(user_id), task_kind, ",".join(providers)))

    def get_order(self, user_id: str, task_kind: str) -> "list[str]":
        r = self.db.execute("SELECT providers FROM task_order WHERE user_id=? AND task_kind=?",
                            (str(user_id), task_kind)).fetchone()
        return [p for p in r["providers"].split(",") if p] if r else []

    # ---- single-use, short-lived setup tokens ----
    @staticmethod
    def _h(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def issue_token(self, user_id: str, ttl: int = 600, purpose: str = "llm") -> str:
        token = secrets.token_urlsafe(32)
        self.db.execute("INSERT INTO setup_tokens(hash,user_id,expires,used,purpose) VALUES(?,?,?,0,?)",
                        (self._h(token), str(user_id), time.time() + ttl, purpose))
        return token

    def peek(self, token: str) -> tuple[str, str] | None:
        r = self.db.execute("SELECT user_id, purpose FROM setup_tokens WHERE hash=? AND used=0 AND expires>?",
                            (self._h(token), time.time())).fetchone()
        return (r["user_id"], r["purpose"]) if r else None

    def peek_token(self, token: str) -> str | None:
        p = self.peek(token)
        return p[0] if p else None

    def consume_token(self, token: str) -> str | None:
        """Atomic: two simultaneous submits can't both win."""
        uid = self.peek_token(token)
        if not uid:
            return None
        cur = self.db.execute("UPDATE setup_tokens SET used=1 WHERE hash=? AND used=0", (self._h(token),))
        return uid if cur.rowcount == 1 else None
