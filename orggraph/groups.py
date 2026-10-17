"""
Working groups: which areas are really worked on by the same people, whatever their SIG labels say.

1. Projection. Two directories are linked (Dir)-[:WORKS_WITH {shared, people}]-(Dir) when the same
   people review and write PRs in both. Each directory keeps its 4 strongest links (cosine of the
   shared people). People active in more than 40 areas are left out of the projection: a handful of
   generalists review almost everywhere and would join everything into one blob.
2. Community detection. FalkorDB's algo.labelPropagation runs on that projection; each directory
   gets d.group. Groups of one are dropped.
3. Check. We rebuild the groups from activity before 2026-07-16 only, then look at PRs merged after
   it: does a PR that crosses groups need more than one approver more often than one that stays
   inside a group? If the groups were arbitrary, it wouldn't.

    python -m orggraph.groups            # build groups for the app + run the check
Writes d.group / d.group_name on Dir nodes and results/groups.json.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import math
import os

from orggraph.db import graph

HUB = 40          # people active in more areas than this are generalists: left out of the projection
K = 4             # strongest links kept per directory
MIN_PRS = 2       # a person "works in" an area with at least this many PRs there
CHECK_FROM = "2026-07-16"
ROOT = os.path.join(os.path.dirname(__file__), "..")


def day(iso: str) -> int:
    return int(iso.replace("-", ""))


def build(g, start: str, end: str) -> dict:
    """Create WORKS_WITH links from activity in [start, end) and run label propagation. Returns {dir: group}."""
    rows = g.ro_query("""
        MATCH (p:Person)-[:REVIEWED|LGTM|APPROVED_PR|AUTHORED_PR]->(pr:PR)-[:TOUCHES]->(d:Dir)
          WHERE pr.day >= $s AND pr.day < $e
        RETURN p.handle, d.path, count(DISTINCT pr)""", {"s": day(start), "e": day(end)}, timeout=120000).result_set
    areas_of = collections.defaultdict(dict)
    for h, d, n in rows:
        if n >= MIN_PRS:
            areas_of[h][d] = n
    people_in = collections.defaultdict(set)
    for h, ds in areas_of.items():
        if len(ds) <= HUB:
            for d in ds:
                people_in[d].add(h)
    links = {}
    dirs = sorted(people_in)
    for a in dirs:
        scored = []
        for b in dirs:
            if a == b:
                continue
            shared = people_in[a] & people_in[b]
            if len(shared) >= 2:
                scored.append((len(shared) / math.sqrt(len(people_in[a]) * len(people_in[b])), b, shared))
        for w, b, shared in sorted(scored, key=lambda x: (-x[0], x[1]))[:K]:
            key = tuple(sorted((a, b)))
            top = sorted(shared, key=lambda h: -(areas_of[h][a] + areas_of[h][b]))[:3]
            links[key] = {"a": key[0], "b": key[1], "shared": len(shared), "people": top}
    g.query("MATCH ()-[w:WORKS_WITH]->() DELETE w")
    g.query("MATCH (d:Dir) WHERE d.group IS NOT NULL SET d.group = NULL, d.group_name = NULL")
    g.query("""UNWIND $rows AS r
               MATCH (a:Dir {repo: 'kubernetes', path: r.a}), (b:Dir {repo: 'kubernetes', path: r.b})
               CREATE (a)-[:WORKS_WITH {shared: r.shared, people: r.people}]->(b)""", {"rows": list(links.values())})
    res = g.ro_query("""CALL algo.labelPropagation({nodeLabels: ['Dir'], relationshipTypes: ['WORKS_WITH']})
                        YIELD node, communityId RETURN node.path, communityId""", timeout=120000).result_set
    members = collections.defaultdict(list)
    for path, cid in res:
        members[cid].append(path)
    return {p: cid for cid, ps in members.items() if len(ps) > 1 for p in ps}


def check(g, groups: dict) -> dict:
    """PRs merged on/after CHECK_FROM: share needing 2+ approvers, by how many groups they cross."""
    rows = g.ro_query("""
        MATCH (pr:PR)-[:TOUCHES]->(d:Dir) WHERE pr.day >= $c
        WITH pr, collect(DISTINCT d.path) AS dirs
        MATCH (a:Person)-[:APPROVED_PR]->(pr)
        RETURN pr.number, dirs, count(DISTINCT a)""", {"c": day(CHECK_FROM)}, timeout=120000).result_set
    by = collections.defaultdict(lambda: [0, 0])
    two_dirs = {"same_group": [0, 0], "different_groups": [0, 0]}
    for _, dirs, approvers in rows:
        if len(dirs) < 2 or not all(d in groups for d in dirs):
            continue
        n = len({groups[d] for d in dirs})
        b = by["1" if n == 1 else "2" if n == 2 else "3+"]
        b[0] += 1
        b[1] += approvers >= 2
        if len(dirs) == 2:
            t = two_dirs["same_group" if n == 1 else "different_groups"]
            t[0] += 1
            t[1] += approvers >= 2
    fmt = lambda v: {"prs": v[0], "needed_2plus_approvers": v[1], "rate": round(v[1] / v[0], 3) if v[0] else None}
    return {"groups_built_from": f"activity before {CHECK_FROM}", "prs_from": CHECK_FROM,
            "multi_area_prs_by_groups_crossed": {k: fmt(by[k]) for k in ("1", "2", "3+")},
            "two_area_prs": {k: fmt(v) for k, v in two_dirs.items()}}


def describe(g, groups: dict, start: str) -> list[dict]:
    """Name each group after its busiest area, list its SIG labels and the people doing most of its work."""
    rows = g.ro_query("""
        MATCH (d:Dir) WHERE d.group IS NOT NULL
        OPTIONAL MATCH (d)-[:LABELED]->(s:SIG)
        OPTIONAL MATCH (x:PR)-[:TOUCHES]->(d) WHERE x.day >= $s
        RETURN d.group, d.path, collect(DISTINCT s.name), count(DISTINCT x)""", {"s": day(start)}, timeout=120000).result_set
    out = collections.defaultdict(lambda: {"dirs": [], "sigs": collections.Counter(), "prs": 0})
    for gid, path, sigs, prs in rows:
        e = out[gid]
        e["dirs"].append((prs, path))
        e["prs"] += prs
        e["sigs"].update(sigs)
    people = g.ro_query("""
        MATCH (d:Dir) WHERE d.group IS NOT NULL
        MATCH (p:Person)-[:REVIEWED|LGTM|APPROVED_PR]->(pr:PR)-[:TOUCHES]->(d) WHERE pr.day >= $s
        WITH d.group AS gid, p, count(DISTINCT pr) AS n ORDER BY n DESC, p.handle
        RETURN gid, collect(p.handle)[..5]""", {"s": day(start)}, timeout=120000).result_set
    people = dict(people)
    result = []
    for gid, e in out.items():
        dirs = [p for _, p in sorted(e["dirs"], key=lambda x: (-x[0], x[1]))]
        named = [p for p in dirs if not p.startswith(("test", "hack", "vendor", "build")) and p != "."]
        result.append({"id": gid, "name": (named or dirs)[0], "dirs": dirs, "prs": e["prs"],
                       "sigs": [s for s, _ in e["sigs"].most_common()],
                       "people": people.get(gid, [])})
    result.sort(key=lambda x: -x["prs"])
    g.query("""UNWIND $rows AS r MATCH (d:Dir {repo: 'kubernetes'}) WHERE d.path IN r.dirs
               SET d.group_name = r.name""", {"rows": [{"name": x["name"], "dirs": x["dirs"]} for x in result]})
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", default=None)
    a = ap.parse_args()
    g = graph(a.graph) if a.graph else graph()
    as_of = g.ro_query("MATCH (p:PR) RETURN max(p.merged)").result_set[0][0]
    year = (dt.date.fromisoformat(as_of) - dt.timedelta(days=365)).isoformat()
    check_start = (dt.date.fromisoformat(CHECK_FROM) - dt.timedelta(days=365)).isoformat()

    # 1. The check: groups from earlier activity only, tested on later PRs.
    result = check(g, build(g, check_start, CHECK_FROM))
    # 2. The groups the app uses: the last 12 months.
    groups = build(g, year, "9999-12-31")
    g.query("UNWIND $rows AS r MATCH (d:Dir {repo: 'kubernetes', path: r[0]}) SET d.group = r[1]",
            {"rows": [[p, c] for p, c in groups.items()]})
    described = describe(g, groups, year)
    cross = [x for x in described if len(x["sigs"]) > 1]
    out = {"as_of": as_of, "window": f"{year}..{as_of}", "settings": {"hub": HUB, "links_per_dir": K, "min_prs": MIN_PRS},
           "groups": described, "dirs_grouped": len(groups), "cross_sig_groups": len(cross), "check": result}
    os.makedirs(os.path.join(ROOT, "results"), exist_ok=True)
    path = os.path.join(ROOT, "results", "groups.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)

    print(f"{len(described)} working groups over {len(groups)} directories; {len(cross)} span more than one SIG label")
    for x in described[:8]:
        print(f"  {x['name']:55} {len(x['dirs']):3} areas  SIGs: {', '.join(x['sigs'][:3]) or '-'}")
    print("\ncheck (groups from activity before %s, PRs after it): needed a 2nd approver" % CHECK_FROM)
    for k, v in result["multi_area_prs_by_groups_crossed"].items():
        if v["prs"]:
            print(f"  crosses {k:2} group(s): {v['needed_2plus_approvers']:3}/{v['prs']:<3} = {v['rate']:.0%}")
    for k, v in result["two_area_prs"].items():
        if v["prs"]:
            print(f"  2-area PRs, {k:16}: {v['needed_2plus_approvers']:3}/{v['prs']:<3} = {v['rate']:.0%}")
    print("->", path)


if __name__ == "__main__":
    main()
