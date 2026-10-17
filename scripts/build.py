"""
One command to rebuild everything from public data (Windows, macOS, Linux):

    python scripts/build.py              # full: download, both graphs, activity, backtest
    python scripts/build.py --no-fetch   # rebuild graphs from data already downloaded

Needs FalkorDB running (docker compose up -d) and data/reviews.jsonl
(from: python -m orggraph.fetch_reviews --since 2024-10-16).
"""
import os
import subprocess
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PY = sys.executable
THEN = "kubernetes@2025-10-16"


def run(*args):
    print("\n>>", " ".join(args), flush=True)
    subprocess.run([PY, *args], cwd=ROOT, check=True)


def check_db():
    """Fail early with a plain message if FalkorDB isn't reachable."""
    sys.path.insert(0, ROOT)
    from orggraph.db import graph
    try:
        graph("drift_ping").query("RETURN 1")
    except Exception as e:
        sys.exit(f"Can't reach FalkorDB ({type(e).__name__}). Is Docker running?  Try:  docker compose up -d")


def main():
    check_db()
    if "--no-fetch" not in sys.argv:
        run("scripts/fetch_data.py")
        run("scripts/fetch_history.py")
    if not os.path.exists(os.path.join(ROOT, "data", "reviews.jsonl")):
        sys.exit("data/reviews.jsonl missing: run  python -m orggraph.fetch_reviews --since 2024-10-16")
    run("-c", f"from orggraph.db import graph\nfor n in ['kubernetes', '{THEN}']:\n"
              "    try: graph(n).delete()\n    except Exception: pass")
    run("-m", "orggraph.load_static")
    run("-m", "orggraph.load_static", "--graph", THEN, "--owners", "data/k8s_2025")
    run("-m", "orggraph.load_activity")
    run("-m", "orggraph.load_activity", "--graph", THEN, "--until", "2025-10-16")
    run("-m", "orggraph.backtest")
    run("-m", "orggraph.eval_reviewers")
    print("\nall done")


if __name__ == "__main__":
    main()
