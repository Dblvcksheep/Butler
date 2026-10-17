"""python -m butler.cli <command>
  rotate                     re-encrypt all stored secrets under BUTLER_MASTER_KEY (see README: key rotation)
  discover USER SERVICE      list what an MCP server exposes and what Butler hides (verify tool names!)
  ablate [--embedded]        same scenarios with FalkorDB on vs off: shows what the graph actually decides
  graphdoc                   regenerate docs/GRAPH.md (data model + every Cypher query the product uses)
"""
import argparse
import re


from .config import Settings

NVIDIA = re.compile(r"nvidia|nemotron", re.I)

def main(argv=None):
    ap = argparse.ArgumentParser(prog="butler.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("rotate")
    d = sub.add_parser("discover"); d.add_argument("user"); d.add_argument("service")

    ab = sub.add_parser("ablate"); ab.add_argument("--embedded", action="store_true",
                                                    help="use an embedded FalkorDB (redislite) instead of FALKORDB_HOST")
    sub.add_parser("graphdoc")
    a = ap.parse_args(argv)
    if a.cmd == "graphdoc":
        from .docs_graph import write
        print("wrote", write())
        return
    if a.cmd == "ablate":
        import os
        from .ablation import report
        from .graph.store import GraphStore
        if a.embedded:
            import signal, tempfile
            from redislite.falkordb_client import FalkorDB
            db = FalkorDB(os.path.join(tempfile.mkdtemp(), "ablate.db"))
            try:
                print(report(GraphStore(db)))
            finally:
                import atexit
                try:
                    atexit.unregister(db.connection._cleanup)
                    db.connection._cleanup = lambda *a, **k: None
                except Exception:
                    pass
                os.kill(db.connection.pid, signal.SIGKILL)
        else:
            print(report(GraphStore.connect(os.environ.get("FALKORDB_HOST", "localhost"),
                                            int(os.environ.get("FALKORDB_PORT", "6379")))))
        return
    s = Settings.from_env()
    from .credentials import CredentialStore, Vault
    from .db import Db
    db = Db(s.database_url or s.db_path)
    creds = CredentialStore(db, Vault(s.master_key, s.old_master_keys))
    if a.cmd == "rotate":
        print(f"re-encrypted {creds.rotate_all()} secret(s) under the primary key. "
              "You can now remove BUTLER_OLD_MASTER_KEYS.")

    else:
        from .mcp_client import McpRunner, load_services
        exposed, hidden = McpRunner(creds, load_services(), s.allow_live).discover(a.user, a.service)
        print("EXPOSED (matched a rule):")
        for t in exposed:
            print(f"  {t['tool_id']:45} {t['capability']:24} risk={t['risk']:6} approval={t['approval']} financial={t['financial']}")
        print("HIDDEN (no rule = not usable):", ", ".join(hidden) or "(none)")


if __name__ == "__main__":
    main()
