import os

import pytest

from butler.config import Settings
from butler.graph.store import GraphStore


def stop_embedded(conn) -> None:
    """Stop an embedded redislite/FalkorDB server by PID. (Its own shutdown() makes redis-py retry a dead
    connection for tens of seconds, and leaked servers pile up and stall interpreter exit.)"""
    import atexit
    import signal
    import time
    pid = getattr(conn, "pid", None)
    try:
        atexit.unregister(conn._cleanup)
        conn._cleanup = lambda *a, **k: None          # silence redislite's own atexit/__del__ cleanup
    except Exception:
        pass
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    for _ in range(150):
        try:
            if os.waitpid(pid, os.WNOHANG)[0]:
                break
        except ChildProcessError:
            break
        time.sleep(0.02)


@pytest.fixture(scope="session")
def _falkor_server(tmp_path_factory):
    try:
        from redislite.falkordb_client import FalkorDB  # embedded FalkorDB: ONE server for the whole session
    except ImportError:
        pytest.skip("falkordblite not installed")
    db = FalkorDB(str(tmp_path_factory.mktemp("falkor") / "f.db"))
    yield db
    stop_embedded(db.connection)


@pytest.fixture
def graph(_falkor_server):
    for name in _falkor_server.list_graphs():          # fresh state per test
        _falkor_server.select_graph(name).delete()
    return GraphStore(_falkor_server)


@pytest.fixture
def settings():
    return Settings(max_per_tx="50", max_per_day="100")


import sys
from pathlib import Path

from cryptography.fernet import Fernet

from butler.mcp_client import Rule, ServiceSpec
from butler.runtime import build_runtime


@pytest.fixture
def fake_service(tmp_path):
    log = tmp_path / "mcp.log"
    spec = ServiceSpec(
        "fake", "Fake", sys.executable, (str(Path(__file__).parent / "fake_mcp_server.py"),),
        rules=(Rule("list_*", "data.search", "low", False), Rule("get_*", "data.read", "low", False),
               Rule("create_order", "fake.orders.create", "high", True, True)),
        static_env={"FAKE_MCP_LOG": str(log)})
    return spec


@pytest.fixture
def rt(graph, fake_service, tmp_path):
    """Full runtime on in-memory SQLite + embedded FalkorDB with a scripted LLM and a real fake MCP server."""
    script: list[str] = []
    notes: list[tuple] = []

    class FakeProv:
        def __init__(self, model): self.model, self.last_usage = model, (10, 5)
        def complete(self, system, messages, max_tokens=1000):
            return script.pop(0) if script else "LLM:" + messages[-1]["content"][:300]

    s = Settings(master_key=Fernet.generate_key().decode(), db_path=":memory:", max_per_tx="50", max_per_day="100")
    r = build_runtime(s, graph=graph, notifier=lambda u, t, b=None: notes.append((u, t, b)),
                      services={"fake": fake_service}, llm_factory=lambda p, k, m: FakeProv(m))
    r.creds.add("1", "anthropic", "m1", "key-anthropic-123456")
    r.script, r.notes, r.fake_log = script, notes, tmp_path / "mcp.log"
    return r
