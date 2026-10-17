"""FalkorDB access. One graph per tenant => hard tenant isolation at the database level."""
import re

_SAFE = re.compile(r"^[A-Za-z0-9_]{1,64}$")


class TenantGraph:
    def __init__(self, graph):
        self._g = graph

    def query(self, cypher: str, params: dict | None = None) -> list:
        # Values ALWAYS go through params; never format user input into `cypher`.
        return self._g.query(cypher, params or {}).result_set

    def ensure_schema(self) -> None:
        for stmt in (
            "CREATE INDEX FOR (u:User) ON (u.id)",
            "CREATE INDEX FOR (p:Preference) ON (p.key)",
            "CREATE INDEX FOR (c:Company) ON (c.key)",
        ):
            try:
                self._g.query(stmt)
            except Exception:  # index already exists
                pass


class GraphStore:
    def __init__(self, client):
        self._db = client
        self._ready: set[str] = set()

    @classmethod
    def connect(cls, host: str, port: int) -> "GraphStore":
        from falkordb import FalkorDB
        return cls(FalkorDB(host=host, port=port))

    def tenant(self, user_id: str) -> TenantGraph:
        uid = str(user_id)
        if not _SAFE.match(uid):  # graph name is derived from the id, so validate it
            raise ValueError("invalid user id")
        tg = TenantGraph(self._db.select_graph(f"butler_{uid}"))
        if uid not in self._ready:
            tg.ensure_schema()
            tg.query("MERGE (u:User {id: $uid})", {"uid": uid})
            self._ready.add(uid)
        return tg
