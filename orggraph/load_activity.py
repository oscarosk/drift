"""
Loads what people actually DID into the graph, next to what the OWNERS files say:

  (:Person)-[:AUTHORED_PR]->(:PR {number, merged})
  (:Person)-[:REVIEWED {state}]->(:PR)        GitHub review (approved / commented / changes requested)
  (:Person)-[:LGTM]->(:PR)                    Prow "/lgtm" command
  (:Person)-[:APPROVED_PR]->(:PR)             Prow "/approve" command
  (:PR)-[:TOUCHES {files}]->(:Dir)            the OWNERS directory responsible for the changed files

Sources: data/pr_files.jsonl (git: which files each merged PR changed) and
data/reviews.jsonl (GitHub API: reviews and Prow commands). Handles only, no comment text.

Run:  python -m orggraph.load_activity                      (current graph)
      python -m orggraph.load_activity --graph kubernetes@2025-10-16 --until 2025-10-16
"""
import argparse
import collections
import json
import os
import posixpath

from orggraph.db import graph, GRAPH_NAME
from orggraph.load_static import run_batched

DATA = os.path.join(os.path.dirname(__file__), "..", "data")
BOTS = {"k8s-ci-robot", "k8s-triage-robot", "k8s-infra-cherrypick-robot", "dependabot", "dependabot[bot]"}


def read_jsonl(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", default=GRAPH_NAME)
    ap.add_argument("--since", default="2024-10-16")
    ap.add_argument("--until", default="9999-12-31", help="only PRs merged before this date")
    a = ap.parse_args()
    g = graph(a.graph)

    # Directories that have their own OWNERS file in THIS graph's snapshot.
    owner_dirs = {r[0] for r in g.ro_query(
        "MATCH (d:Dir {repo: 'kubernetes'}) WHERE d.has_owners RETURN d.path").result_set}

    def owner_dir(path: str) -> str:
        """The nearest directory at or above a changed file that has an OWNERS file."""
        p = posixpath.dirname(path) or "."
        while p not in owner_dirs and p not in (".", ""):
            p = posixpath.dirname(p) or "."
        return p if p in owner_dirs else "."

    in_window = lambda d: a.since <= d[:10] < a.until  # noqa: E731
    files = {p["number"]: p for p in read_jsonl("pr_files.jsonl") if in_window(p["merged"])}
    reviews = {r["number"]: r for r in read_jsonl("reviews.jsonl") if in_window(r["merged_at"])}

    prs, authored, touches = [], [], []
    acts = collections.defaultdict(list)  # rel type -> rows
    for num in sorted(set(files) | set(reviews)):
        f, r = files.get(num), reviews.get(num)
        merged = (r["merged_at"][:10] if r else f["merged"])
        prs.append({"number": num, "merged": merged})
        author = (r and r["author"]) or (f and f["author"])
        if author and author not in BOTS:
            authored.append({"pr": num, "handle": author})
        if f:
            per_dir = collections.Counter(owner_dir(x) for x in f["files"])
            touches += [{"pr": num, "dir": f"kubernetes:{d}", "files": n} for d, n in per_dir.items()]
        if r:
            seen = set()
            for v in r["reviews"]:
                if v["by"] not in BOTS and (v["by"], v["state"]) not in seen:
                    seen.add((v["by"], v["state"]))
                    acts["REVIEWED"].append({"pr": num, "handle": v["by"], "state": v["state"]})
            for c in r["commands"]:
                rel = "LGTM" if c["cmd"] == "lgtm" else "APPROVED_PR"
                if c["by"] not in BOTS and (c["by"], rel) not in seen:
                    seen.add((c["by"], rel))
                    acts[rel].append({"pr": num, "handle": c["by"]})

    try:
        g.query("CREATE INDEX FOR (p:PR) ON (p.number)")
        g.query("CREATE INDEX FOR (p:PR) ON (p.merged)")
    except Exception:
        pass
    run_batched(g, "UNWIND $rows AS r MERGE (p:PR {number: r.number}) SET p.merged = r.merged", prs)
    run_batched(g, """
        UNWIND $rows AS r
        MATCH (pr:PR {number: r.pr})
        MERGE (p:Person {handle: r.handle})
        MERGE (p)-[:AUTHORED_PR]->(pr)""", authored)
    run_batched(g, """
        UNWIND $rows AS r
        MATCH (pr:PR {number: r.pr}), (d:Dir {id: r.dir})
        MERGE (pr)-[t:TOUCHES]->(d) SET t.files = r.files""", touches)
    run_batched(g, """
        UNWIND $rows AS r
        MATCH (pr:PR {number: r.pr})
        MERGE (p:Person {handle: r.handle})
        MERGE (p)-[e:REVIEWED {state: r.state}]->(pr)""", acts["REVIEWED"])
    for rel in ("LGTM", "APPROVED_PR"):
        run_batched(g, f"""
            UNWIND $rows AS r
            MATCH (pr:PR {{number: r.pr}})
            MERGE (p:Person {{handle: r.handle}})
            MERGE (p)-[:{rel}]->(pr)""", acts[rel])
    print(f"[{a.graph}] PRs {len(prs)} ({a.since}..{a.until}), authored {len(authored)}, "
          f"touches {len(touches)}, reviews {len(acts['REVIEWED'])}, lgtm {len(acts['LGTM'])}, "
          f"approve {len(acts['APPROVED_PR'])}")


if __name__ == "__main__":
    main()
