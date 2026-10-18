"""Tiny DB adapter so the same SQL (a common SQLite/PostgreSQL subset) runs on both.

Write queries with `?` placeholders, `ON CONFLICT ... DO UPDATE/NOTHING`, no `INSERT OR REPLACE`,
no `lastrowid` (use insert_id). Postgres: autocommit connection, explicit transactions."""
import re
import sqlite3
import threading
from contextlib import contextmanager


class Row(dict):
    def __getitem__(self, k):
        if isinstance(k, int):
            return list(self.values())[k]
        return super().__getitem__(k)


class Result:
    def __init__(self, rows, rowcount):
        self._rows, self.rowcount = rows, rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class Db:
    def __init__(self, url: str = ":memory:", schema: str | None = None):
        self.lock = threading.RLock()
        if url.startswith(("postgres://", "postgresql://")):
            import psycopg
            from psycopg.rows import dict_row
            self.dialect = "postgres"
            self.conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
            if schema:
                if not re.fullmatch(r"[A-Za-z0-9_]{1,63}", schema):
                    raise ValueError("bad schema name")
                self.conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                self.conn.execute(f'SET search_path TO "{schema}"')
        else:
            self.dialect = "sqlite"
            self.conn = sqlite3.connect(url.replace("sqlite:///", ""), check_same_thread=False,
                                        isolation_level=None)
            self.conn.row_factory = sqlite3.Row

    @classmethod
    def coerce(cls, x) -> "Db":
        return x if isinstance(x, Db) else cls(x)

    @property
    def skip_locked(self) -> str:
        return " FOR UPDATE SKIP LOCKED" if self.dialect == "postgres" else ""

    def _pg(self, sql: str) -> str:
        return sql.replace("?", "%s")

    def execute(self, sql: str, params=()) -> Result:
        with self.lock:
            if self.dialect == "sqlite":
                cur = self.conn.execute(sql, tuple(params))
                rows = [Row(dict(r)) for r in cur.fetchall()] if cur.description else []
                return Result(rows, cur.rowcount)
            cur = self.conn.execute(self._pg(sql), tuple(params))
            rows = [Row(r) for r in cur.fetchall()] if cur.description else []
            return Result(rows, cur.rowcount)

    def ddl(self, sql: str) -> None:
        if self.dialect == "postgres":
            sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
            sql = re.sub(r"\bBLOB\b", "BYTEA", sql)
            sql = re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql)

        with self.lock:
            if self.dialect == "postgres":
                # Serialize schema changes across web, bot, and worker processes.
                lock_id = 741852963
                self.conn.execute("SELECT pg_advisory_lock(%s)", (lock_id,))
                try:
                    self.conn.execute(sql)
                finally:
                    self.conn.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
            else:
                self.conn.execute(sql)

    def insert_id(self, sql: str, params=()) -> int:
        with self.lock:
            if self.dialect == "sqlite":
                return self.conn.execute(sql, tuple(params)).lastrowid
            return self.conn.execute(self._pg(sql) + " RETURNING id", tuple(params)).fetchone()["id"]

    @contextmanager
    def transaction(self):
        with self.lock:
            self.execute("BEGIN IMMEDIATE" if self.dialect == "sqlite" else "BEGIN")
            try:
                yield
                self.execute("COMMIT")
            except BaseException:
                self.execute("ROLLBACK")
                raise
