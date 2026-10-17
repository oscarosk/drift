"""
Minimal reproduction: an index range scan on a STRING property ignores the upper bound.

Found while building Drift's reviewer evaluation (which must only see data from before each PR).
Observed on FalkorDB graph module 4.18.3 (falkordblite). Run it against your own instance:

    python scripts/repro_falkordb_range.py            # uses FALKORDB_HOST / FALKORDB_PORT from .env
Expected: every line prints 5. On an affected version the indexed string lines print more.
Workaround used in Drift: store dates as integers (PR.day = 20260910) and filter on those.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from orggraph.db import graph  # noqa: E402

g = graph("drift_repro_range")
try:
    g.delete()
except Exception:
    pass
g = graph("drift_repro_range")
g.query("UNWIND range(1, 30) AS i CREATE (:PR {day: 20260900 + i, "
        "s: '2026-09-' + CASE WHEN i < 10 THEN '0' + toString(i) ELSE toString(i) END})")
q_str = "MATCH (p:PR) WHERE p.s >= '2026-09-05' AND p.s < '2026-09-10' RETURN count(p)"
q_int = "MATCH (p:PR) WHERE p.day >= 20260905 AND p.day < 20260910 RETURN count(p)"
print("string range, no index :", g.ro_query(q_str).result_set[0][0])
g.query("CREATE INDEX FOR (p:PR) ON (p.s)")
g.query("CREATE INDEX FOR (p:PR) ON (p.day)")
print("string range, indexed  :", g.ro_query(q_str).result_set[0][0])
print("integer range, indexed :", g.ro_query(q_int).result_set[0][0])
g.delete()
