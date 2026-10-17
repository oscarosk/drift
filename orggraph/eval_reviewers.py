"""
Does "Find a reviewer" pick better reviewers than the OWNERS files? Measured on real PRs.

For each sampled PR merged in the test window, using ONLY activity from before that PR merged:
  Drift      suggest_reviewers() on the directories the PR touched (the product's own tool),
             PR author excluded, top 1 / top 3.
  OWNERS     the approvers listed in the nearest OWNERS file(s) with approvers, walking up the
             tree the way Prow does. OWNERS doesn't rank people, so we report the exact expected
             hit rate of picking 1 or 3 of them at random, plus "anywhere in the list" (the most
             OWNERS could ever do).
  Popular    the 3 people who approved the most PRs anywhere in the 6 months before (a "just ask
             the busiest approvers" baseline, to show that knowing the area matters).

Ground truth: people who typed /approve on the PR (the decision Kubernetes actually made), and,
separately, anyone who reviewed, /lgtm'd or /approve'd it. Authors never count.
The OWNERS files used are today's, which slightly favours the OWNERS baseline.

Run:  python -m orggraph.eval_reviewers            -> results/reviewer_eval.json
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import math
import os
import random
import time

from orggraph.tools import Tools

TEST_FROM, SAMPLE, SEED, K = "2026-07-16", 400, 7, 3
OUT = os.path.join(os.path.dirname(__file__), "..", "results", "reviewer_eval.json")


def wilson(hits: int, n: int, z: float = 1.96):
    if not n:
        return (0.0, 0.0)
    p = hits / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(c - h, 3), round(c + h, 3))


def p_random_hit(n: int, h: int, k: int) -> float:
    """Exact chance that k people picked at random from n (h of them correct) include a correct one."""
    if n == 0 or h == 0:
        return 0.0
    k = min(k, n)
    return 1 - math.comb(n - h, k) / math.comb(n, k)


def main():
    t = Tools()
    g = t.g
    rows = g.ro_query("""
        MATCH (pr:PR)-[:TOUCHES]->(d:Dir) WHERE pr.day >= $from_day
        WITH pr, collect(DISTINCT d.path) AS dirs
        MATCH (a:Person)-[:APPROVED_PR]->(pr)
        WITH pr, dirs, collect(DISTINCT a.handle) AS approvers
        OPTIONAL MATCH (au:Person)-[:AUTHORED_PR]->(pr)
        OPTIONAL MATCH (r:Person)-[:REVIEWED|LGTM|APPROVED_PR]->(pr)
        RETURN pr.number, pr.merged, au.handle, dirs, approvers, collect(DISTINCT r.handle)""",
                      {"from_day": int(TEST_FROM.replace("-", ""))}, timeout=120000).result_set
    prs = []
    for num, merged, author, dirs, appr, revs in rows:
        appr = sorted(set(appr) - {author})
        revs = sorted(set(revs) - {author})
        if appr and dirs and len(dirs) <= 15:
            prs.append({"number": num, "merged": merged, "author": author, "dirs": dirs,
                        "approvers": appr, "reviewers": revs})
    random.Random(SEED).shuffle(prs)
    sample = sorted(prs[:SAMPLE], key=lambda p: p["number"])

    # Every /approve with its date, for the "popular" baseline.
    approvals = g.ro_query("MATCH (a:Person)-[:APPROVED_PR]->(pr:PR) RETURN a.handle, pr.merged", timeout=120000).result_set

    owners_cache: dict = {}

    def nearest_approvers(d: str) -> set:
        if d not in owners_cache:
            levels = g.ro_query("""
                MATCH chain = (x:Dir {repo: 'kubernetes', path: $p})-[:INSIDE*0..10]->(o:Dir)
                WITH o, length(chain) AS up
                OPTIONAL MATCH (a:Person)-[:APPROVER_OF]->(o)
                RETURN up, collect(DISTINCT a.handle) ORDER BY up""", {"p": d}, timeout=30000).result_set
            owners_cache[d] = next((set(h for h in hs if h) for _, hs in levels if any(hs)), set())
        return owners_cache[d]

    score = collections.Counter()
    per_pr = []
    t0 = time.time()
    for i, p in enumerate(sample):
        truth_a, truth_r = set(p["approvers"]), set(p["reviewers"])
        a = t.suggest_reviewers(paths=p["dirs"], until=p["merged"], exclude=[p["author"]], with_owners=False)
        t._trace = []
        drift = [x["handle"] for x in a.data.get("people", [])] if a.outcome == "answered_with_path" else []
        owners = set().union(*(nearest_approvers(d) for d in p["dirs"])) - {p["author"]}
        start = (dt.date.fromisoformat(p["merged"]) - dt.timedelta(days=183)).isoformat()
        busiest = collections.Counter(h for h, m in approvals if start <= m < p["merged"] and h != p["author"])
        popular = [h for h, _ in busiest.most_common(K)]

        for name, truth in (("approver", truth_a), ("reviewer", truth_r)):
            score[f"drift@1:{name}"] += bool(set(drift[:1]) & truth)
            score[f"drift@3:{name}"] += bool(set(drift[:K]) & truth)
            score[f"popular@3:{name}"] += bool(set(popular) & truth)
            score[f"owners_any:{name}"] += bool(owners & truth)
            hits = len(owners & truth)
            score[f"owners@1:{name}"] += p_random_hit(len(owners), hits, 1)
            score[f"owners@3:{name}"] += p_random_hit(len(owners), hits, K)
        per_pr.append({"pr": p["number"], "merged": p["merged"], "dirs": p["dirs"], "approved_by": p["approvers"],
                       "drift_top3": drift[:K], "owners_listed": len(owners), "popular_top3": popular})
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(sample)} PRs ({time.time() - t0:.0f}s)")

    n = len(sample)

    def row(key, exact=False):
        v = score[key]
        out = {"rate": round(v / n, 3)}
        if not exact:
            out["ci95"] = wilson(int(v), n)
        return out

    result = {
        "test_window": [TEST_FROM, t.as_of], "prs_in_window_with_an_approver": len(prs), "sampled": n, "seed": SEED,
        "method": "Predictions use only activity merged before each PR. Truth = people who /approve'd the PR "
                  "(approver) or reviewed, /lgtm'd or /approve'd it (reviewer); the PR author never counts. "
                  "OWNERS = approvers in the nearest OWNERS file(s) with approvers (today's files); its @1/@3 "
                  "are exact expectations of a random pick, since OWNERS does not rank people.",
        "approver": {"drift@1": row("drift@1:approver"), "drift@3": row("drift@3:approver"),
                     "owners@1_random": row("owners@1:approver", True), "owners@3_random": row("owners@3:approver", True),
                     "owners_anywhere_in_list": row("owners_any:approver"), "popular@3": row("popular@3:approver")},
        "reviewer": {"drift@1": row("drift@1:reviewer"), "drift@3": row("drift@3:reviewer"),
                     "owners@1_random": row("owners@1:reviewer", True), "owners@3_random": row("owners@3:reviewer", True),
                     "owners_anywhere_in_list": row("owners_any:reviewer"), "popular@3": row("popular@3:reviewer")},
        "examples": per_pr[:25],
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(f"\n{n} PRs merged {TEST_FROM}..{t.as_of} (sampled from {len(prs)}), predictions from earlier data only\n")
    print(f"{'who actually approved it':38}{'top 1':>10}{'top 3':>10}")
    for label, a1, a3 in [("Drift (Find a reviewer)", "drift@1", "drift@3"),
                          ("OWNERS file, random pick", "owners@1_random", "owners@3_random")]:
        print(f"{label:38}{result['approver'][a1]['rate']:>10.0%}{result['approver'][a3]['rate']:>10.0%}")
    print(f"{'Busiest approvers overall':38}{'':>10}{result['approver']['popular@3']['rate']:>10.0%}")
    print(f"{'(approver is anywhere in OWNERS list)':38}{'':>10}{result['approver']['owners_anywhere_in_list']['rate']:>10.0%}")
    print(f"\nany reviewer: Drift top 3 {result['reviewer']['drift@3']['rate']:.0%}  vs OWNERS random 3 {result['reviewer']['owners@3_random']['rate']:.0%}")
    print(f"-> {os.path.normpath(OUT)}  ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
