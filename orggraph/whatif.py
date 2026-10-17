"""
What-if, on a copy of the graph.

Two questions Drift answers by changing a throwaway copy and re-running its normal coverage
query there, so the live graph is never touched:

  step_back(handles)   "If these people stopped approving, which areas lose their nearest active
                        approver, where does approval fall back to, and who already reviews there?"
  check_fix(dirs)      "If a maintainer applied the drafted OWNERS changes for these directories,
                        would any area be left without an active approver?" A safety check run
                        before the patch is downloaded.

How: GRAPH.COPY kubernetes -> whatif_<id>, edit the copy (move the approvals to emeritus, exactly as
the patch would), compute coverage on both graphs with the same query, diff, GRAPH.DELETE the copy.
Coverage of a directory = the nearest level of its OWNERS chain (itself, then INSIDE parents, stopping
at no_parent_owners) that has at least one approver with activity in the last 12 months.
"""
from __future__ import annotations

import threading
import time
import uuid

ACT = "AUTHORED_PR|REVIEWED|LGTM|APPROVED_PR"
_lock = threading.Lock()   # FalkorDB copies by forking; one copy at a time is the safe way on small hosts


def coverage(t, since_day: int) -> dict:
    """For every OWNERS directory: where an approval request is routed and whether anyone there is active.

    Routing follows OWNERS inheritance: the nearest level of the chain (the directory itself, then its
    INSIDE parents, stopping at no_parent_owners) that lists at least one approver. The area is
    'covered' if at least one approver at that level has activity in the last 12 months, and 'stuck'
    if everyone listed there is silent.
    """
    active = {r[0] for r in t.q("active_people", f"""
        MATCH (p:Person)-[:{ACT}]->(pr:PR) WHERE pr.day >= $s RETURN DISTINCT p.handle""", {"s": since_day})}
    rows = t.q("ownership_levels", """
        MATCH (d:Dir {repo: 'kubernetes'}) WHERE d.has_owners
        MATCH chain = (d)-[:INSIDE*0..10]->(o:Dir)
        OPTIONAL MATCH (p:Person)-[:APPROVER_OF]->(o)
        RETURN d.path, length(chain) AS up, o.path, o.no_parent_owners, collect(DISTINCT p.handle)
        ORDER BY d.path, up""")
    levels: dict = {}
    for d, up, o, stop, approvers in rows:
        levels.setdefault(d, []).append((up, o, bool(stop), sorted(h for h in approvers if h)))
    out = {}
    for d, chain in levels.items():
        res = {"routed_to": None, "listed": [], "active": [], "state": "none"}
        for up, o, stop, approvers in sorted(chain):
            if approvers:
                live = [h for h in approvers if h in active]
                res.update(routed_to=o, listed=approvers, active=live, state="covered" if live else "stuck")
                break
            if stop:
                break
        out[d] = res
    return out


def _with_copy(t, edit, label: str):
    """Copy the graph, apply `edit(copy_tools)`, return coverage before and after. The copy is always deleted."""
    from orggraph.tools import Tools
    since_day = int(t.since(12).replace("-", ""))
    before = coverage(t, since_day)
    name = f"whatif_{uuid.uuid4().hex[:8]}"
    with _lock:
        t0 = time.perf_counter()
        for attempt in range(2):
            try:
                t.g.copy(name)
                break
            except Exception:
                if attempt:
                    raise
                time.sleep(0.5)
        t._trace.append({"query": f"GRAPH.COPY {t.g.name} -> {name}", "ms": round((time.perf_counter() - t0) * 1000, 1), "rows": 0})
        try:
            c = Tools(name)
            c._as_of = t.as_of
            edited = edit(c)
            t._trace.append({"query": f"{label} (on the copy)", "ms": c._trace[-1]["ms"] if c._trace else 0, "rows": edited})
            c._trace = []
            after = coverage(c, since_day)
            t._trace += [dict(x, query=x["query"] + " (copy)") for x in c._trace]
        finally:
            t1 = time.perf_counter()
            try:
                from orggraph.db import graph
                graph(name).delete()
            except Exception as e:  # never leave a copy behind silently
                print("could not delete", name, e, flush=True)
            t._trace.append({"query": f"GRAPH.DELETE {name}", "ms": round((time.perf_counter() - t1) * 1000, 1), "rows": 0})
    return before, after


def _write(c, name: str, cypher: str, params: dict) -> int:
    t0 = time.perf_counter()
    res = c.g.query(cypher, params, timeout=30000)
    c._trace.append({"query": name, "ms": round((time.perf_counter() - t0) * 1000, 1), "rows": res.relationships_deleted})
    return res.relationships_deleted


def _diff(t, before: dict, after: dict, prs: dict, exclude: list) -> dict:
    """Compare routing before and after. Returns areas that got stuck, got fixed, or moved."""
    stuck, fixed, moved = [], [], []
    for d, b in before.items():
        a = after.get(d) or {"routed_to": None, "listed": [], "active": [], "state": "none"}
        if a["routed_to"] == b["routed_to"] and a["active"] == b["active"]:
            continue
        row = {"dir": d, "prs": prs.get(d, 0),
               "before": {k: b[k] for k in ("routed_to", "state", "active", "listed")},
               "after": {k: a[k] for k in ("routed_to", "state", "active", "listed")}}
        if b["state"] == "covered" and a["state"] != "covered":
            stuck.append(row)
        elif b["state"] != "covered" and a["state"] == "covered":
            fixed.append(row)
        elif a["routed_to"] != b["routed_to"]:
            moved.append(row)
    key = lambda r: (-r["prs"], r["dir"])
    for x in (stuck, fixed, moved):
        x.sort(key=key)
    # Who already reviews where approval got stuck: the people to ask to step in.
    for r in stuck[:6]:
        rows = t.q("who_could_cover", """
            MATCH (d:Dir {repo: 'kubernetes', path: $p})
            MATCH (p:Person)-[:REVIEWED|LGTM|APPROVED_PR]->(pr:PR)-[:TOUCHES]->(:Dir)-[:INSIDE*0..10]->(d)
              WHERE pr.day >= $since_day AND NOT p.handle IN $exclude
            RETURN p.handle, count(DISTINCT pr) AS n ORDER BY n DESC, p.handle LIMIT 3""",
                   {"p": r["dir"], "since": t.since(6), "exclude": exclude})
        r["could_cover"] = [{"handle": h, "prs": n} for h, n in rows]
    return {"stuck": stuck, "fixed": fixed, "moved": moved,
            "stuck_before": sum(1 for v in before.values() if v["state"] == "stuck"),
            "stuck_after": sum(1 for v in after.values() if v["state"] == "stuck")}


def _prs(t) -> dict:
    return dict(t.q("prs_per_area", """
        MATCH (x:PR)-[:TOUCHES]->(d:Dir) WHERE x.day >= $since_day RETURN d.path, count(x)""", {"since": t.since(12)}))


def step_back(t, handles: list[str]):
    """What if these people stopped approving everywhere?"""
    from orggraph.tools import Answer  # noqa: F401  (type of the result)
    want = [h.lower().lstrip("@").strip() for h in handles if h and h.strip()][:10]
    args = {"handles": want}
    if not want:
        return t._answer("unknown_entity", "what_if", args, note="Name at least one GitHub handle, e.g. @liggitt.")
    found = {r[0] for r in t.q("resolve_people", "MATCH (p:Person) WHERE p.handle IN $h RETURN p.handle", {"h": want})}
    missing = [h for h in want if h not in found]
    if missing and not found:
        sugg = []
        for h in missing:
            sugg += [s for s in t.resolve_person(h)[1] if s not in sugg]
        return t._answer("unknown_entity", "what_if", args, data={"suggestions": sugg[:5]},
                         note=f"No person {', '.join('@' + h for h in missing)} in the graph.")
    people = sorted(found)
    listed = t.q("their_approvals", """
        MATCH (p:Person)-[r:APPROVER_OF]->(d:Dir) WHERE p.handle IN $h
        RETURN p.handle, count(DISTINCT d), sum(CASE WHEN r.via = 'direct' THEN 1 ELSE 0 END)""", {"h": people})
    if not listed:
        return t._answer("not_in_graph", "what_if", {"handles": people},
                         note=f"{', '.join('@' + h for h in people)} isn't listed as an approver anywhere, so nothing would change.")
    prs = _prs(t)

    def edit(c):
        return _write(c, "move their approvals to emeritus", """
            MATCH (p:Person)-[r:APPROVER_OF]->(d:Dir) WHERE p.handle IN $h
            MERGE (p)-[:EMERITUS_APPROVER_OF {via: 'what-if'}]->(d)
            DELETE r""", {"h": people})

    before, after = _with_copy(t, edit, "move their approvals to emeritus")
    diff = _diff(t, before, after, prs, people)
    data = {"people": people, "approver_of": {h: n for h, n, _ in listed}, "areas_checked": len(before), **diff,
            "copy": "the live graph was not changed"}
    paths = [[f"@{people[0]}", "steps back from", r["dir"], "approval now waits on silent approvers" if r["after"]["state"] == "stuck"
              else "no approver left in the chain"] for r in diff["stuck"][:2]]
    paths += [[f"@{people[0]}", "steps back from", r["dir"], f"approval moves up to {r['after']['routed_to']}"] for r in diff["moved"][:2]]
    return t._answer("answered_with_path", "what_if", {"handles": people}, data=data, paths=paths,
                     note=(f"Not in the graph: {', '.join('@' + h for h in missing)}." if missing else ""))


def check_fix(t, dirs: list[str], months: int = 12, min_prs: int = 10):
    """Apply the drafted OWNERS changes for these directories to a copy and check nothing loses coverage."""
    changes = []
    for d in dirs[:60]:
        p = t.propose_owners_update(d, months, min_prs)
        if p.outcome == "answered_with_path" and p.data["remove"]:
            changes.append({"dir": p.data["dir"], "remove": p.data["remove"]})
    drafted_ms = sum(x["ms"] for x in t._trace)
    t._trace = [{"query": f"draft the changes for {len(dirs[:60])} directories", "ms": round(drafted_ms, 1), "rows": 0}]
    args = {"dirs": [c["dir"] for c in changes]}
    if not changes:
        return t._answer("not_in_graph", "check_fix", args, note="None of these drafts removes an approver, so coverage can't change.")
    prs = _prs(t)

    def edit(c):
        n = 0
        for ch in changes:
            n += _write(c, "apply drafted change", """
                MATCH (p:Person)-[r:APPROVER_OF {via: 'direct'}]->(d:Dir {repo: 'kubernetes', path: $d})
                WHERE p.handle IN $h
                MERGE (p)-[:EMERITUS_APPROVER_OF {via: 'drift-draft'}]->(d)
                DELETE r""", {"d": ch["dir"], "h": ch["remove"]})
        return n

    before, after = _with_copy(t, edit, f"apply {len(changes)} drafted changes")
    removed = sorted({h for ch in changes for h in ch["remove"]})
    diff = _diff(t, before, after, prs, removed)
    data = {"changes": len(changes), "removed_listings": sum(len(c["remove"]) for c in changes), **diff,
            "safe": not diff["stuck"], "copy": "the live graph was not changed"}
    return t._answer("answered_with_path", "check_fix", args, data=data)


def main():
    """python -m orggraph.whatif: check all drafted fixes on a copy, and find whose step-back would hurt most."""
    import json
    import os
    from orggraph.tools import Tools
    t = Tools()
    queue = [x["dir"] for x in t.cleanup_queue(limit=40).data["queue"]]
    t._trace = []
    fix = check_fix(t, queue).data
    print(f"Drafted fixes ({fix['changes']} directories), applied to a copy of the graph:")
    print(f"  areas unstuck {len(fix['fixed'])}, areas made stuck {len(fix['stuck'])}, "
          f"areas waiting only on silent approvers {fix['stuck_before']} -> {fix['stuck_after']}")
    busiest = [r[0] for r in t.q("busiest_active_approvers", f"""
        MATCH (p:Person)-[:APPROVER_OF {{via: 'direct'}}]->(d:Dir) WITH p, count(d) AS n
        MATCH (p)-[:{ACT}]->(pr:PR) WHERE pr.day >= $since_day
        WITH p, n, count(DISTINCT pr) AS prs WHERE prs > 0
        RETURN p.handle ORDER BY n DESC, p.handle LIMIT 60""", {"since": t.since(12)})]
    impact = []
    for h in busiest:
        a = step_back(t, [h])
        t._trace = []
        if a.outcome == "answered_with_path":
            impact.append({"handle": h, "stuck": len(a.data["stuck"]), "areas": [r["dir"] for r in a.data["stuck"]][:5]})
    impact.sort(key=lambda x: (-x["stuck"], x["handle"]))
    print("\nIf one person stepped back, areas left waiting only on silent approvers (60 busiest active approvers):")
    for x in impact[:5]:
        print(f"  @{x['handle']:<18} {x['stuck']}  {', '.join(x['areas'][:3])}")
    print(f"  {sum(1 for x in impact if x['stuck'] == 0)} of {len(impact)} would leave nothing stuck")
    out = {"as_of": t.as_of, "fix_check": {k: fix[k] for k in ("changes", "removed_listings", "stuck_before", "stuck_after")}
           | {"unstuck": [r["dir"] for r in fix["fixed"]], "made_stuck": [r["dir"] for r in fix["stuck"]]},
           "single_step_back": impact}
    path = os.path.join(os.path.dirname(__file__), "..", "results", "whatif.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print("->", path)


if __name__ == "__main__":
    main()
