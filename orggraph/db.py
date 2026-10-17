"""One place that knows how to reach FalkorDB. Works the same locally and on FalkorDB Cloud."""
import os

from falkordb import FalkorDB

# Minimal .env loader (avoids an extra dependency): KEY=value lines, real env vars win.
_env = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(_env):
    for line in open(_env, encoding="utf-8"):
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.strip().split("=", 1)
            os.environ.setdefault(k, v)

GRAPH_NAME = os.getenv("ORGGRAPH_GRAPH", "kubernetes")  # one graph per organisation (tenant)


def client() -> FalkorDB:
    return FalkorDB(
        host=os.getenv("FALKORDB_HOST", "localhost"),
        port=int(os.getenv("FALKORDB_PORT", "6379")),
        username=os.getenv("FALKORDB_USERNAME") or None,
        password=os.getenv("FALKORDB_PASSWORD") or None,
        **({"ssl": True} if os.getenv("FALKORDB_SSL", "").lower() in ("1", "true", "yes") else {}),
    )


def graph(name: str = GRAPH_NAME):
    db = FalkorDB(
        host=os.getenv("FALKORDB_HOST", "localhost"),
        port=int(os.getenv("FALKORDB_PORT", "6379")),
        username=os.getenv("FALKORDB_USERNAME") or None,
        password=os.getenv("FALKORDB_PASSWORD") or None,
        **({"ssl": True} if os.getenv("FALKORDB_SSL", "").lower() in ("1", "true", "yes") else {}),
    )
    return db.select_graph(name)
