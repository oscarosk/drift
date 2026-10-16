"""
Backtest: does Drift's view of the org one year ago predict what the org changed since?

Two graphs live in the same FalkorDB instance:
  kubernetes@2025-10-16   OWNERS as of 2025-10-16 + activity from the 12 months BEFORE that date only
  kubernetes              OWNERS today (the "answer key")

Drift runs its normal queries on the year-ago graph (it never sees the future), then we check
the predictions against today's OWNERS files.

  Silent owners:  approvers with zero reviews, /lgtm, /approve or merged PRs in the prior year.
                  Outcome: lost at least one approver listing by today.
  Shadow owners:  people listed nowhere in OWNERS, ranked by review activity in the prior year.
                  Outcome: listed as a reviewer or approver somewhere today.

Every rate is compared with the base rate (what you'd get picking at random), so "lift" means
"times better than chance". Results go to results/backtest.json, which the README and UI read.

Run:  python -m orggraph.backtest
"""
import json
import os

from orggraph.db import graph

THEN, NOW = "kubernetes@2025-10-16", "kubernetes"
WINDOW_START = "2024-10-16"
ACT = "AUTHORED_PR|REVIEWED|LGTM|APPROVED_PR"
REVIEW_ACT = "REVIEWED|LGTM|APPROVED_PR"
OUT = os.path.join(os.path.dirname(__file__), "..", "results", "backtest.json")

# Each approver at T0 with their activity count in the prior year.
Q_APPROVER_ACTIVITY = f"""
MATCH (p:Person)-[:APPROVER_OF]->(:Dir)
WITH DISTINCT p
OPTIONAL MATCH (p)-[:{ACT}]->(pr:PR) WHERE pr.merged >= $since
RETURN p.handle AS person, count(DISTINCT pr) AS prs"""

# Approver listings per (person, dir), for checking who was removed where.
Q_APPROVER_LISTINGS = """
MATCH (p:Person)-[:APPROVER_OF]->(d:Dir {repo: 'kubernetes'})
WHERE d.has_owners
RETURN p.handle, d.path"""

# Everyone ranked by how much reviewing they did; owners are filtered out in Python using
# Q_LISTED_PEOPLE (a per-row "NOT (p)-[...]->()" check here was too slow on large graphs).
Q_SHADOW_PEOPLE = f"""
MATCH (p:Person)-[:{REVIEW_ACT}]->(pr:PR) WHERE pr.merged >= $since
WITH p, count(DISTINCT pr) AS reviewed
RETURN p.handle AS person, reviewed ORDER BY reviewed DESC, person"""

Q_LISTED_PEOPLE = "MATCH (p:Person)-[:APPROVER_OF|REVIEWER_OF]->(:Dir) RETURN DISTINCT p.handle"


def rows(g, q, params=None):
    # Explicit 60 s limit: FalkorDB's default query timeout can be lower than this needs.
    return g.ro_query(q, params or {}, timeout=60000).result_set


def main():
    then, now = graph(THEN), graph(NOW)

    # --- silent owners --------------------------------------------------------------
    then_listings = {(h, p) for h, p in rows(then, Q_APPROVER_LISTINGS)}
    now_listings = {(h, p) for h, p in rows(now, Q_APPROVER_LISTINGS)}
    now_paths = {p for _, p in now_listings} | {p for (p,) in rows(now, "MATCH (d:Dir) WHERE d.has_owners RETURN d.path")}
    # Only judge listings in directories that still exist (a deleted directory is not a removal).
    lost = {h for h, p in then_listings if p in now_paths and (h, p) not in now_listings}
    activity = {h: n for h, n in rows(then, Q_APPROVER_ACTIVITY, {"since": WINDOW_START})}
    approvers = set(activity)
    silent = {h for h, n in activity.items() if n == 0}
    silent_still_listed = sorted(h for h in silent if any(h == x for x, _ in now_listings))
    base = len(lost & approvers) / len(approvers)
    hit = len(silent & lost)
    silent_res = {
        "approvers_then": len(approvers),
        "silent_then": len(silent),
        "silent_share": round(len(silent) / len(approvers), 3),
        "lost_a_listing_by_now": len(lost & approvers),
        "base_rate": round(base, 3),
        "silent_who_lost_a_listing": hit,
        "precision": round(hit / max(1, len(silent)), 3),
        "lift": round((hit / max(1, len(silent))) / base, 2) if base else None,
        "silent_still_listed_now": len(silent_still_listed),
    }

    # --- shadow owners --------------------------------------------------------------
    listed_then = {h for (h,) in rows(then, Q_LISTED_PEOPLE)}
    pool = [(h, n) for h, n in rows(then, Q_SHADOW_PEOPLE, {"since": WINDOW_START}) if h not in listed_then]
    listed_now = {h for (h,) in rows(now, Q_LISTED_PEOPLE)}
    base_s = sum(h in listed_now for h, _ in pool) / len(pool)
    shadow_res = {"active_non_owners_then": len(pool), "base_rate": round(base_s, 4), "top_k": []}
    for k in (10, 20, 30, 50, 100):
        top = [h for h, _ in pool[:k]]
        hits = [h for h in top if h in listed_now]
        shadow_res["top_k"].append({
            "k": k, "became_owners": len(hits), "precision": round(len(hits) / k, 3),
            "lift": round(len(hits) / k / base_s, 1), "who": hits,
        })

    result = {
        "snapshot": "2025-10-16", "activity_window": [WINDOW_START, "2025-10-16"],
        "scope": "merged PRs in kubernetes/kubernetes only; activity in other repos is not counted",
        "silent_owners": silent_res, "shadow_owners": shadow_res,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    s = silent_res
    print(f"SILENT  {s['silent_then']}/{s['approvers_then']} approvers ({s['silent_share']:.0%}) had zero activity "
          f"in the prior year; {s['silent_still_listed_now']} of them are still approvers somewhere today.")
    print(f"        predicting removal: precision {s['precision']:.0%} vs base {s['base_rate']:.0%} "
          f"(lift {s['lift']}x) -> weak, because records are rarely cleaned up")
    print(f"SHADOW  {len(pool)} active non-owners; base rate of becoming an owner {base_s:.1%}")
    for t in shadow_res["top_k"]:
        print(f"        top {t['k']:3}: {t['became_owners']:2} became owners ({t['precision']:.0%}, {t['lift']}x)  {t['who'][:6]}")
    print(f"-> {os.path.normpath(OUT)}")


if __name__ == "__main__":
    main()
