import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _ids(raw: str) -> frozenset[str]:
    return frozenset(x.strip() for x in raw.split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    telegram_token: str = ""
    # allowed_users: frozenset[str] = frozenset()
    falkor_host: str = "localhost"
    falkor_port: int = 6379
    db_path: str = "butler.sqlite3"
    anthropic_key: str = ""
    model: str = "claude-sonnet-5-5"
    paypal_env: str = "sandbox"
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    max_per_tx: str = "50"
    max_per_day: str = "100"
    database_url: str = ""        # postgresql://... ; empty = SQLite at db_path
    old_master_keys: tuple = ()   # previous Fernet keys, only needed during rotation
    allow_live: bool = False
    email_daily_cap: int = 20
    email_max_batch: int = 10
    graph_enabled: bool = True    # BUTLER_GRAPH=off runs without FalkorDB (ablation / degraded mode)
    worker_mode: str = "inline"   # inline (polling thread in the bot) | celery
    redis_url: str = "redis://localhost:6379/1"
    master_key: str = ""          # Fernet key; encrypts stored API keys
    public_url: str = ""          # https://butler.example.com  (used for /addkey links)
    web_port: int = 8080
    task_poll_seconds: int = 30
    max_attempts: int = 3
    run_stale_seconds: int = 300
    allowed_currencies: frozenset[str] = field(default_factory=lambda: frozenset({"USD", "EUR", "GBP"}))

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        s = cls(
            telegram_token=e("TELEGRAM_BOT_TOKEN", ""),
            # allowed_users=_ids(e("BUTLER_ALLOWED_USERS", "")),
            falkor_host=e("FALKORDB_HOST", "localhost"),
            falkor_port=int(e("FALKORDB_PORT", "6379")),
            db_path=e("BUTLER_DB_PATH", "butler.sqlite3"),
            anthropic_key=e("ANTHROPIC_API_KEY", ""),
            model=e("BUTLER_MODEL", "claude-sonnet-5-5"),
            paypal_env=e("PAYPAL_ENV", "sandbox"),
            paypal_client_id=e("PAYPAL_CLIENT_ID", ""),
            paypal_client_secret=e("PAYPAL_CLIENT_SECRET", ""),
            max_per_tx=e("PAYMENT_MAX_PER_TX", "50"),
            max_per_day=e("PAYMENT_MAX_PER_DAY", "100"),
            database_url=e("DATABASE_URL", ""),
            old_master_keys=tuple(k.strip() for k in e("BUTLER_OLD_MASTER_KEYS", "").split(",") if k.strip()),
            allow_live=e("BUTLER_ALLOW_LIVE") == "1",
            graph_enabled=e("BUTLER_GRAPH", "on").lower() != "off",
            email_daily_cap=int(e("EMAIL_DAILY_CAP", "20")),
            email_max_batch=int(e("EMAIL_MAX_BATCH", "10")),
            worker_mode=e("BUTLER_WORKER_MODE", "inline"),
            redis_url=e("REDIS_URL", "redis://localhost:6379/1"),
            master_key=e("BUTLER_MASTER_KEY", ""),
            public_url=e("BUTLER_PUBLIC_URL", "").rstrip("/"),
            web_port=int(e("BUTLER_WEB_PORT", "8080")),
        )
        if not s.master_key:
            raise RuntimeError("BUTLER_MASTER_KEY is required (generate one, see README)")
        if s.paypal_env != "sandbox" and e("BUTLER_ALLOW_LIVE") != "1":
            raise RuntimeError("PAYPAL_ENV is not 'sandbox'; set BUTLER_ALLOW_LIVE=1 to allow live mode")
        return s
