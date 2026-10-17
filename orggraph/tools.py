"""
Drift's tool catalogue. The agent never writes Cypher: it picks one of these tools and fills in
arguments. Every argument that names something (a directory, a person, a decision) is resolved
against the graph BEFORE any traversal runs, so the model can't reason about things that
don't exist, and no model text is ever pasted into a query.

Every tool returns an Answer with one of four outcomes:
  answered_with_path    the graph held the relationships; the hops are included
  answered_from_memory  a previous session established this (see memory.py)
  not_in_graph          the thing exists, but nothing connects it the way the question assumed
  unknown_entity        the name isn't in the graph, so no query ran (suggestions included)
"""
from __future__ import annotations

import collections
import datetime as dt
import difflib
import posixpath
import re
import time
from dataclasses import dataclass, field

from orggraph.db import graph

ACT = "AUTHORED_PR|REVIEWED|LGTM|APPROVED_PR"
HOW = {"AUTHORED_PR": "wrote", "REVIEWED": "reviewed", "LGTM": "/lgtm", "APPROVED_PR": "/approve"}
TIMEOUT_MS = 30000


@dataclass
class Answer:
    outcome: str
    tool: str
    args: dict
    data: dict = field(default_factory=dict)
    paths: list = field(default_factory=list)       # each path: list of node labels, in order
    trace: list = field(default_factory=list)       # one entry per query: name, ms, rows
    note: str = ""                                  # resolution notes, caveats

    def to_dict(self):
        return self.__dict__


class Tools:
    def __init__(self, graph_name: str | None = None):
        self.g = graph(graph_name) if graph_name else graph()
        self._trace: list = []
        self._as_of = None

    # ---- plumbing ---------------------------------------------------------------------
    def q(self, name: str, cypher: str, params: dict | None = None):
        t = time.perf_counter()
        params = dict(params or {})
        for k, v in list(params.items()):  # every ISO date also gets an integer twin: $since -> $since_day
            if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                params[k + "_day"] = int(v.replace("-", ""))
        rows = self.g.ro_query(cypher, params, timeout=TIMEOUT_MS).result_set
        self._trace.append({"query": name, "ms": round((time.perf_counter() - t) * 1000, 1), "rows": len(rows)})
        return rows

    def _answer(self, outcome, tool, args, **kw) -> Answer:
        a = Answer(outcome, tool, args, trace=self._trace, **kw)
        self._trace = []
        return a

    @property
    def as_of(self) -> str:
        """Latest merge date in the graph: 'today' for this dataset."""
        if not self._as_of:
            self._as_of = self.q("as_of", "MATCH (p:PR) RETURN max(p.merged)")[0][0] or dt.date.today().isoformat()
        return self._as_of

    def since(self, months: int) -> str:
        d = dt.date.fromisoformat(self.as_of) - dt.timedelta(days=round(months * 30.44))
        return d.isoformat()

    # ---- resolution (runs before any traversal) -----------------------------------------
    def resolve_dir(self, path: str):
        """Exact OWNERS directory, else the nearest ancestor that has one (files work too).
        Returns (dir_path, note) or (None, suggestions)."""
        p = (path or "").strip().strip("/").removeprefix("kubernetes/").removeprefix("./") or "."
        start = p
        while True:
            rows = self.q("resolve_dir", "MATCH (d:Dir {repo: 'kubernetes', path: $p}) WHERE d.has_owners RETURN d.path", {"p": p})
            if rows and (p == start or p != "."):
                # Walking up to the repo root would "resolve" any made-up path, so the root only
                # counts when it was asked for directly.
                if p == start:
                    return p, ""
                # A near-identical owned directory (a typo like "clientgo") beats walking up.
                close = self._suggest_dirs(start, with_scores=True)
                if close and close[0][0] >= 0.9 and close[0][1] != p and not start.startswith(close[0][1] + "/"):
                    return None, [c for _, c in close]
                return p, f"'{start}' has no OWNERS file of its own; using the nearest owned directory '{p}'."
            if p in (".", ""):
                break
            p = posixpath.dirname(p) or "."
        return None, self._suggest_dirs(start)

    def _suggest_dirs(self, start: str, with_scores: bool = False):
        """Close matches for a mistyped path, compared segment by segment (e.g. 'kublet' -> 'pkg/kubelet')."""
        paths = [r[0] for r in self.q("all_dirs", "MATCH (d:Dir {repo: 'kubernetes'}) WHERE d.has_owners RETURN d.path")]
        scored = []
        for path in paths:
            ratio = difflib.SequenceMatcher(None, start.lower(), path.lower()).ratio()
            tail = difflib.SequenceMatcher(None, start.split("/")[-1].lower(), path.split("/")[-1].lower()).ratio()
            scored.append((max(ratio, tail - 0.05), -len(path), path))
        top = [(round(score, 3), p) for score, _, p in sorted(scored, reverse=True)[:5] if score >= 0.6]
        return top if with_scores else [p for _, p in top]

    def resolve_person(self, handle: str):
        h = (handle or "").strip().lstrip("@").lower()
        if self.q("resolve_person", "MATCH (p:Person {handle: $h}) RETURN p.handle", {"h": h}):
            return h, None
        sugg = [r[0] for r in self.q("suggest_person",
                "MATCH (p:Person) WHERE p.handle CONTAINS $h RETURN p.handle LIMIT 5", {"h": h[:6]})]
        return None, sugg

    def resolve_kep(self, number):
        try:
            n = int(str(number).strip().lstrip("#").split("-")[0])
        except ValueError:
            return None, []
        return (n, None) if self.q("resolve_kep", "MATCH (k:KEP {number: $n}) RETURN k.number", {"n": n}) else (None, [])

    # ---- tool 1: who owns ----------------------------------------------------------------
    def who_owns(self, path: str) -> Answer:
        """Official ownership: the chain of OWNERS files from this directory up to the root,
        with approvers at each level, the teams (SIGs) labelling it and the subprojects owning it."""
        args = {"path": path}
        d, extra = self.resolve_dir(path)
        if not d:
            return self._answer("unknown_entity", "who_owns", args, data={"suggestions": extra},
                                note=f"No directory '{path}' with an OWNERS file in the graph.")
        rows = self.q("ownership_chain", """
            MATCH chain = (d:Dir {repo: 'kubernetes', path: $p})-[:INSIDE*0..10]->(o:Dir)
            WITH o, length(chain) AS up, [n IN nodes(chain) | n.path] AS via
            OPTIONAL MATCH (a:Person)-[r:APPROVER_OF]->(o)
            WITH o, up, via, collect(DISTINCT {handle: a.handle, via: r.via}) AS approvers
            OPTIONAL MATCH (o)-[:LABELED]->(s:SIG)
            OPTIONAL MATCH (sp:Subproject)-[:OWNS]->(o)
            OPTIONAL MATCH (sig2:SIG)-[:HAS_SUBPROJECT]->(sp)
            RETURN o.path, up, via, approvers, collect(DISTINCT s.id) + collect(DISTINCT sig2.id),
                   collect(DISTINCT sp.name), o.no_parent_owners
            ORDER BY up""", {"p": d})
        levels, path_nodes = [], []
        for opath, up, via, approvers, sigs, subs, no_parent in rows:
            levels.append({"dir": opath, "levels_up": up,
                           "approvers": sorted({a["handle"] for a in approvers if a["handle"]}),
                           "via_alias": sorted({a["via"] for a in approvers if a["handle"] and a["via"] != "direct"}),
                           "sigs": sorted(set(sigs)), "subprojects": sorted(set(subs))})
            path_nodes = via
            if no_parent:  # this OWNERS file stops inheritance: parents' owners don't apply
                levels[-1]["stops_inheritance"] = True
                break
        return self._answer("answered_with_path", "who_owns", args, data={"dir": d, "levels": levels},
                            paths=[path_nodes], note=extra)

    # ---- tool 2: who really knows (the drift view of one directory) ------------------------
    def who_knows(self, path: str, months: int = 12) -> Answer:
        """Listed approvers with their recent activity here and anywhere ("silent" if zero),
        and unlisted people doing the work here ("shadow owners")."""
        months = max(1, min(int(months), 24))
        args = {"path": path, "months": months}
        d, extra = self.resolve_dir(path)
        if not d:
            return self._answer("unknown_entity", "who_knows", args, data={"suggestions": extra},
                                note=f"No directory '{path}' with an OWNERS file in the graph.")
        since = self.since(months)
        listed = self.q("listed_activity", f"""
            MATCH (p:Person)-[r:APPROVER_OF]->(d:Dir {{repo: 'kubernetes', path: $p}})
            OPTIONAL MATCH (p)-[:{ACT}]->(pr:PR)-[:TOUCHES]->(:Dir)-[:INSIDE*0..10]->(d)
              WHERE pr.day >= $since_day
            WITH p, r, d, count(DISTINCT pr) AS here
            OPTIONAL MATCH (p)-[:{ACT}]->(pr2:PR) WHERE pr2.day >= $since_day
            RETURN p.handle, r.via, here, count(DISTINCT pr2) AS anywhere
            ORDER BY here, anywhere, p.handle""", {"p": d, "since": since})
        shadows = self.q("shadow_owners", f"""
            MATCH (d:Dir {{repo: 'kubernetes', path: $p}})
            MATCH (p:Person)-[a:{ACT}]->(pr:PR)-[:TOUCHES]->(:Dir)-[:INSIDE*0..10]->(d)
              WHERE pr.day >= $since_day
            WITH d, p, a, pr ORDER BY pr.merged DESC
            WITH d, p, count(DISTINCT pr) AS prs, collect(DISTINCT type(a)) AS how,
                 collect(DISTINCT pr.number)[..3] AS examples
            WHERE NOT (p)-[:APPROVER_OF|REVIEWER_OF]->(d)
            OPTIONAL MATCH (d)-[:INSIDE*1..10]->(up:Dir)<-[:APPROVER_OF|REVIEWER_OF]-(p)
            WITH p, prs, how, examples, count(up) AS listed_above
            WHERE listed_above = 0
            OPTIONAL MATCH (p)-[:APPROVER_OF|REVIEWER_OF]->(below:Dir)-[:INSIDE*1..10]->(:Dir {{repo: 'kubernetes', path: $p}})
            RETURN p.handle, prs, how, examples, collect(DISTINCT below.path)[..3]
            ORDER BY prs DESC, p.handle LIMIT 8""", {"p": d, "since": since})
        approvers = [{"handle": h, "via": via, "prs_here": here, "prs_anywhere": anywhere,
                      "status": "silent" if anywhere == 0 else ("quiet here" if here == 0 else "active")}
                     for h, via, here, anywhere in listed]
        shadow = [{"handle": h, "prs_here": n, "how": sorted(how), "example_prs": ex, "listed_below": below}
                  for h, n, how, ex, below in shadows]
        if not approvers and not shadow:
            return self._answer("not_in_graph", "who_knows", args, data={"dir": d, "since": since},
                                note=f"'{d}' lists no approvers of its own and saw no merged-PR activity since {since}. "
                                     "That means the graph has no evidence either way, not that nobody owns it.")
        paths = [[f"@{s['handle']}", f"PR #{s['example_prs'][0]}", d] for s in shadow[:3] if s["example_prs"]]
        return self._answer("answered_with_path", "who_knows", args,
                            data={"dir": d, "since": since, "as_of": self.as_of, "approvers": approvers, "shadow_owners": shadow},
                            paths=paths, note=extra)

    # ---- tool 3: org-wide drift --------------------------------------------------------
    def drift_summary(self, months: int = 12, limit: int = 10) -> Answer:
        """How stale the ownership records are across the whole org, and where it's worst."""
        months, limit = max(1, min(int(months), 24)), max(1, min(int(limit), 25))
        since = self.since(months)
        org = self.q("org_silent", f"""
            MATCH (p:Person)-[:APPROVER_OF]->(:Dir)
            WITH DISTINCT p
            OPTIONAL MATCH (p)-[:{ACT}]->(pr:PR) WHERE pr.day >= $since_day
            WITH p, count(DISTINCT pr) AS n
            RETURN count(p), sum(CASE WHEN n = 0 THEN 1 ELSE 0 END)""", {"since": since})[0]
        worst = self.q("worst_dirs", f"""
            MATCH (p:Person)-[:APPROVER_OF]->(:Dir)
            WITH DISTINCT p
            OPTIONAL MATCH (p)-[:{ACT}]->(pr:PR) WHERE pr.day >= $since_day
            WITH p, count(DISTINCT pr) = 0 AS silent
            MATCH (p)-[:APPROVER_OF]->(d:Dir) WHERE d.has_owners
            WITH d, count(p) AS approvers, sum(CASE WHEN silent THEN 1 ELSE 0 END) AS silent
            WHERE silent > 0
            OPTIONAL MATCH (x:PR)-[:TOUCHES]->(d) WHERE x.day >= $since_day
            WITH d, approvers, silent, count(x) AS prs
            WHERE prs > 0
            RETURN d.path, approvers, silent, prs
            ORDER BY prs * toFloat(silent) / approvers DESC, prs DESC LIMIT $limit""", {"since": since, "limit": limit})
        return self._answer("answered_with_path", "drift_summary", {"months": months, "limit": limit},
                            data={"since": since, "as_of": self.as_of, "approvers": org[0], "silent": int(org[1] or 0),
                                  "worst_dirs": [{"dir": a, "approvers": b, "silent": int(c), "prs": e} for a, b, c, e in worst]})

    # ---- tool 4: route new work to the right people --------------------------------------
    def route(self, path: str, months: int = 6) -> Answer:
        """Who to send a new issue or PR in this area to: people with recent review and approval
        work here, owners first, each with an example PR as evidence."""
        months = max(1, min(int(months), 24))
        args = {"path": path, "months": months}
        d, extra = self.resolve_dir(path)
        if not d:
            return self._answer("unknown_entity", "route", args, data={"suggestions": extra},
                                note=f"No directory '{path}' with an OWNERS file in the graph.")
        since = self.since(months)
        rows = self.q("route_candidates", """
            MATCH (d:Dir {repo: 'kubernetes', path: $p})
            MATCH (p:Person)-[a:REVIEWED|LGTM|APPROVED_PR]->(pr:PR)-[:TOUCHES]->(:Dir)-[:INSIDE*0..10]->(d)
              WHERE pr.day >= $since_day
            WITH d, p, a, pr ORDER BY pr.merged DESC
            WITH d, p, count(DISTINCT pr) AS reviewed, sum(CASE WHEN type(a) = 'APPROVED_PR' THEN 1 ELSE 0 END) AS approvals,
                 max(pr.merged) AS last, collect(DISTINCT pr.number)[..3] AS examples
            OPTIONAL MATCH (p)-[r:APPROVER_OF|REVIEWER_OF]->(:Dir)<-[:INSIDE*0..10]-(d)
            RETURN p.handle, reviewed, approvals, last, examples, collect(DISTINCT type(r))
            ORDER BY approvals DESC, reviewed DESC, p.handle LIMIT 5""", {"p": d, "since": since})
        if not rows:
            return self._answer("not_in_graph", "route", args, data={"dir": d, "since": since},
                                note=f"No reviews or approvals on '{d}' since {since}; fall back to who_owns.")
        people = [{"handle": h, "reviewed": n, "approvals": int(ap), "last_active": last, "example_prs": ex,
                   "listed_as": sorted({r.replace('_OF', '').lower() for r in roles}) or ["not listed"]}
                  for h, n, ap, last, ex, roles in rows]
        return self._answer("answered_with_path", "route", args, data={"dir": d, "since": since, "people": people},
                            paths=[[f"@{p['handle']}", f"reviewed PR #{p['example_prs'][0]}", d] for p in people[:3]],
                            note=extra)

    # ---- tool 5: decisions behind an area -------------------------------------------------
    def why(self, kep: int | str) -> Answer:
        """A decision record (KEP): who wrote, reviewed and approved it, which team owns it,
        how far it got, related decisions, and whether its approvers are still active."""
        args = {"kep": kep}
        n, _ = self.resolve_kep(kep)
        if n is None:
            return self._answer("unknown_entity", "why", args, note=f"No enhancement proposal '{kep}' in the graph.")
        since = self.since(12)
        k = self.q("kep", """
            MATCH (k:KEP {number: $n})
            OPTIONAL MATCH (k)-[:OWNED_BY]->(s:SIG)
            OPTIONAL MATCH (k)-[m:REACHED]->(v:Release)
            RETURN k.title, k.status, k.stage, k.created, k.updated, k.url, s.id,
                   collect(DISTINCT {stage: m.stage, version: v.version})""", {"n": n})[0]
        people = self.q("kep_people", f"""
            MATCH (p:Person)-[r:AUTHORED|REVIEWED_KEP|APPROVED_KEP]->(:KEP {{number: $n}})
            OPTIONAL MATCH (p)-[:{ACT}]->(pr:PR) WHERE pr.day >= $since_day
            RETURN p.handle, collect(DISTINCT type(r)), count(DISTINCT pr)""", {"n": n, "since": since})
        related = self.q("kep_related", """
            MATCH path = (k:KEP {number: $n})-[:SEE_ALSO|REPLACES|SUPERSEDED_BY*1..2]-(o:KEP)
            RETURN o.number, o.title, [r IN relationships(path) | type(r)] LIMIT 8""", {"n": n})
        role = {"AUTHORED": "author", "REVIEWED_KEP": "reviewer", "APPROVED_KEP": "approver"}
        return self._answer("answered_with_path", "why", args, data={
            "kep": n, "title": k[0], "status": k[1], "stage": k[2], "created": k[3], "updated": k[4], "url": k[5],
            "owning_sig": k[6], "milestones": sorted([m for m in k[7] if m["version"]], key=lambda m: m["version"]),
            "people": [{"handle": h, "roles": sorted(role[r] for r in rs), "prs_last_12m": c,
                        "status": "silent" if c == 0 else "active"} for h, rs, c in people],
            "related": [{"kep": a, "title": b, "via": c} for a, b, c in related]},
            paths=[[f"KEP-{n}", f"owned by {k[6]}"]] + [[f"KEP-{n}", *r[2], f"KEP-{r[0]}"] for r in related[:3]])

    # ---- tool 6: draft the fix (Act) ---------------------------------------------------------
    def propose_owners_update(self, path: str, months: int = 12, min_prs: int = 10) -> Answer:
        """Draft an OWNERS change for one directory: silent approvers move to
        emeritus_approvers, active unlisted people with real review work are proposed as
        reviewers. Every line carries its evidence. A draft for humans to approve; never applied."""
        args = {"path": path, "months": months, "min_prs": min_prs}
        k = self.who_knows(path, months)
        self._trace = list(k.trace)  # keep the queries who_knows ran in this answer's trace
        if k.outcome != "answered_with_path":
            k.tool = "propose_owners_update"; k.args = args
            return k
        d = k.data["dir"]
        silent = [a for a in k.data["approvers"] if a["status"] == "silent" and a["via"] == "direct"]
        silent_alias = [a for a in k.data["approvers"] if a["status"] == "silent" and a["via"] != "direct"]
        # Propose at most 3 reviewers, and only people who reviewed here (not just wrote code):
        # an OWNERS change should be small enough for a maintainer to say yes to.
        add = [s for s in k.data["shadow_owners"] if s["prs_here"] >= min_prs
               and ({"REVIEWED", "LGTM", "APPROVED_PR"} & set(s["how"]))][:3]
        lines = [f"# Draft OWNERS update for {d} (generated by Drift, {k.data['as_of']}; needs human review)"]
        evidence = []
        for a in silent:
            lines.append(f"-  approvers: {a['handle']}\n+  emeritus_approvers: {a['handle']}")
            evidence.append(f"{a['handle']}: 0 reviews, approvals or merged PRs in kubernetes/kubernetes since {k.data['since']}")
        for s in add:
            lines.append(f"+  reviewers: {s['handle']}")
            evidence.append(f"{s['handle']}: {s['prs_here']} merged PRs here since {k.data['since']} "
                            f"({', '.join(HOW[x] for x in s['how'] if x in HOW)}), e.g. PR #{', #'.join(map(str, s['example_prs']))}")
        notes = []
        direct = [a for a in k.data["approvers"] if a["via"] == "direct"]
        if silent and len(silent) == len(direct):
            notes.append(f"This removes every direct approver of {d}, so approval falls back to the parent OWNERS file "
                         "until someone is added as an approver.")
        if silent_alias:
            notes.append("Listed through a team alias, so the change belongs in OWNERS_ALIASES: "
                         + ", ".join(f"{a['handle']} ({a['via']})" for a in silent_alias))
        if not silent and not add and not silent_alias:
            return self._answer("not_in_graph", "propose_owners_update", args, data={"dir": d},
                                note=f"No drift found for '{d}': every listed approver is active and no unlisted person "
                                     f"passes the {min_prs}-PR bar.", paths=k.paths)
        return self._answer("answered_with_path", "propose_owners_update", args,
                            data={"dir": d, "diff": "\n".join(lines), "evidence": evidence, "notes": notes,
                                  "remove": [a["handle"] for a in silent], "add_reviewers": [s["handle"] for s in add]},
                            paths=k.paths, note=k.note)

    # ---- tool 7: reviewers for a change ----------------------------------------------------
    def suggest_reviewers(self, paths: str | list | None = None, pr: int | None = None, months: int = 6,
                          until: str | None = None, exclude: list | None = None, with_owners: bool = True) -> Answer:
        """Who should review a change touching these files (or a merged PR by number): people
        with recent review and approval work in the affected areas, plus whether each area's
        listed approvers are still active."""
        months = max(1, min(int(months), 24))
        args = {"paths": paths, "pr": pr, "months": months}
        if pr:
            rows = self.q("pr_dirs", "MATCH (p:PR {number: $n})-[:TOUCHES]->(d:Dir) RETURN d.path", {"n": int(pr)})
            if not rows:
                return self._answer("unknown_entity", "suggest_reviewers", args,
                                    note=f"PR #{pr} isn't in the graph (only merged PRs since Oct 2024 are). Paste its file paths instead.")
            dirs, notes = sorted({r[0] for r in rows}), []
        else:
            items = paths if isinstance(paths, list) else re.split(r"[\s,]+", paths or "")
            items = [x.strip() for x in items if x.strip()][:50]
            if not items:
                return self._answer("unknown_entity", "suggest_reviewers", args, note="Give at least one file or directory path.")
            dirs, notes, unknown = [], [], []
            for it in items:
                d, extra = self.resolve_dir(it)
                if d:
                    dirs.append(d)
                else:
                    unknown.append(it)
            dirs = sorted(set(dirs))
            if unknown:
                notes.append("Not found in the graph: " + ", ".join(unknown[:5]))
            if not dirs:
                return self._answer("unknown_entity", "suggest_reviewers", args, note=" ".join(notes))
        # `until` (exclusive) lets the evaluation ask "what would Drift have said before this PR?"
        if until:
            end = dt.date.fromisoformat(until)
            since = (end - dt.timedelta(days=round(months * 30.44))).isoformat()
            since12 = (end - dt.timedelta(days=365)).isoformat()
        else:
            until, (since, since12) = "9999-12-31", (self.since(months), self.since(12))
        exclude = [h.lower() for h in (exclude or [])]
        people, areas = {}, []
        for d in dirs[:15]:
            cand = self.q("reviewers_for_dir", """
                MATCH (d:Dir {repo: 'kubernetes', path: $p})
                MATCH (p:Person)-[a:REVIEWED|LGTM|APPROVED_PR]->(pr:PR)-[:TOUCHES]->(:Dir)-[:INSIDE*0..10]->(d)
                  WHERE pr.day >= $since_day AND pr.day < $until_day AND NOT p.handle IN $exclude
                WITH p, a, pr ORDER BY pr.merged DESC
                WITH p, count(DISTINCT pr) AS reviewed, sum(CASE WHEN type(a) = 'APPROVED_PR' THEN 1 ELSE 0 END) AS approvals,
                     max(pr.merged) AS last, collect(DISTINCT pr.number)[..2] AS examples
                RETURN p.handle, reviewed, approvals, last, examples ORDER BY approvals DESC, reviewed DESC, p.handle LIMIT 8""",
                         {"p": d, "since": since, "until": until, "exclude": exclude})
            if not with_owners:
                official = []
            else:
                official = self.q("official_status", f"""
                MATCH (p:Person)-[:APPROVER_OF]->(:Dir)<-[:INSIDE*0..10]-(:Dir {{repo: 'kubernetes', path: $p}})
                WITH DISTINCT p
                OPTIONAL MATCH (p)-[:{ACT}]->(pr:PR) WHERE pr.day >= $since_day
                RETURN p.handle, count(DISTINCT pr)""", {"p": d, "since": since12})
            areas.append({"dir": d, "approvers": len(official),
                          "silent_approvers": sorted(h for h, n in official if n == 0)})
            for h, n, ap, last, ex in cand:
                e = people.setdefault(h, {"handle": h, "score": 0, "reviewed": 0, "approvals": 0, "areas": [],
                                          "last_active": last, "example_prs": []})
                e["score"] += n + 2 * int(ap)
                e["reviewed"] += n
                e["approvals"] += int(ap)
                e["areas"].append(d)
                e["last_active"] = max(e["last_active"], last)
                e["example_prs"] += [x for x in ex if x not in e["example_prs"]]
                e.setdefault("by_area", {})[d] = list(ex)       # which PRs connect this person to this area
        ranked = sorted(people.values(), key=lambda e: (-len(e["areas"]), -e["score"], e["handle"]))[:6]
        if not ranked:
            return self._answer("not_in_graph", "suggest_reviewers", args, data={"areas": areas, "since": since},
                                note=f"No reviews or approvals in these areas since {since}.")
        for e in ranked:
            e["example_prs"] = e["example_prs"][:3]
        grp = self.q("groups_crossed", "MATCH (x:Dir {repo: 'kubernetes'}) WHERE x.path IN $d AND x.group IS NOT NULL "
                     "RETURN DISTINCT x.group_name", {"d": dirs[:15]})
        groups = sorted(r[0] for r in grp if r[0])
        if len(groups) > 1:
            notes.append(f"This change crosses {len(groups)} working groups ({', '.join(groups)}), "
                         "each with its own regular reviewers: make sure someone from each looks at it.")
        return self._answer("answered_with_path", "suggest_reviewers", args,
                            data={"since": since, "people": ranked, "areas": areas, "groups": groups},
                            paths=[[f"@{e['handle']}", f"reviewed PR #{e['example_prs'][0]}", e["areas"][0]] for e in ranked[:3] if e["example_prs"]],
                            note=" ".join(notes))

    # ---- tool 8: every area with its health --------------------------------------------------
    def areas(self, months: int = 12) -> Answer:
        """Every directory with an OWNERS file: listed approvers, how many are silent, and how
        much work merged there. Powers search and the cleanup queue."""
        since = self.since(max(1, min(int(months), 24)))
        rows = self.q("areas", f"""
            MATCH (p:Person)-[:APPROVER_OF]->(:Dir)
            WITH DISTINCT p
            OPTIONAL MATCH (p)-[:{ACT}]->(pr:PR) WHERE pr.day >= $since_day
            WITH p, count(DISTINCT pr) = 0 AS silent
            MATCH (p)-[r:APPROVER_OF]->(d:Dir) WHERE d.has_owners
            WITH d, count(p) AS approvers, sum(CASE WHEN silent THEN 1 ELSE 0 END) AS silent,
                 sum(CASE WHEN silent AND r.via = 'direct' THEN 1 ELSE 0 END) AS silent_direct
            OPTIONAL MATCH (x:PR)-[:TOUCHES]->(d) WHERE x.day >= $since_day
            RETURN d.path, approvers, silent, silent_direct, count(x) AS prs""", {"since": since})
        listed = {r[0] for r in rows}
        rest = self.q("areas_unlisted", f"""
            MATCH (d:Dir {{repo: 'kubernetes'}}) WHERE d.has_owners AND NOT d.path IN $listed
            OPTIONAL MATCH (x:PR)-[:TOUCHES]->(d) WHERE x.day >= $since_day
            RETURN d.path, count(x)""", {"since": since, "listed": list(listed)})
        items = [{"dir": a, "approvers": b, "silent": int(c), "silent_direct": int(e), "prs": f} for a, b, c, e, f in rows]
        items += [{"dir": a, "approvers": 0, "silent": 0, "silent_direct": 0, "prs": b} for a, b in rest]
        for it in items:
            it["drift"] = round(it["prs"] * it["silent"] / it["approvers"], 1) if it["approvers"] else 0.0
            it["state"] = "drifting" if it["silent"] else ("inherits" if it["approvers"] == 0 else "healthy")
        items.sort(key=lambda x: (-x["drift"], -x["prs"], x["dir"]))
        return self._answer("answered_with_path", "areas", {"months": months},
                            data={"since": since, "as_of": self.as_of, "areas": items})

    # ---- tool 9: the cleanup queue and a real patch ------------------------------------------
    def cleanup_queue(self, limit: int = 25, months: int = 12, min_prs: int = 10) -> Answer:
        """Drafted OWNERS fixes for the most drifted directories, ready to review together."""
        limit = max(1, min(int(limit), 60))
        candidates = [a["dir"] for a in self.areas(months).data["areas"] if a["silent_direct"] > 0][:limit]
        self._trace = []
        queue = []
        for d in candidates:
            p = self.propose_owners_update(d, months, min_prs)
            if p.outcome == "answered_with_path" and (p.data["remove"] or p.data["add_reviewers"]):
                queue.append({k: p.data[k] for k in ("dir", "remove", "add_reviewers", "evidence", "notes")})
        return self._answer("answered_with_path", "cleanup_queue", {"limit": limit, "months": months, "min_prs": min_prs},
                            data={"as_of": self.as_of, "since": self.since(months), "queue": queue})

    def owners_patch(self, dirs: list[str], months: int = 12, min_prs: int = 10) -> Answer:
        """A unified diff (git apply-able) for the drafted fixes in these directories."""
        from orggraph.patch import owners_patch
        changes = []
        for d in dirs[:60]:
            p = self.propose_owners_update(d, months, min_prs)
            if p.outcome != "answered_with_path":
                continue
            row = self.q("owners_text", "MATCH (x:Dir {repo: 'kubernetes', path: $p}) RETURN x.owners_text, x.has_filters", {"p": p.data["dir"]})
            text, filters = row[0] if row else ("", False)
            changes.append({"dir": p.data["dir"], "owners_text": text, "has_filters": filters,
                            "remove": p.data["remove"], "add_reviewers": p.data["add_reviewers"], "evidence": p.data["evidence"]})
        out = owners_patch(changes)
        header = (f"# Drift: drafted OWNERS changes for kubernetes/kubernetes (activity data up to {self.as_of}).\n"
                  "# Activity = merged PRs, reviews, /lgtm and /approve in kubernetes/kubernetes only.\n"
                  "# Review the evidence below, then:  git apply --check drift-owners.patch && git apply drift-owners.patch\n#\n")
        notes = "".join(f"# {a['dir']}/OWNERS\n" + "".join(f"#   - {e}\n" for e in a["evidence"]) for a in out["applied"]) + "#\n"
        out["patch"] = header + notes + out["patch"] if out["patch"] else ""
        return self._answer("answered_with_path" if out["patch"] else "not_in_graph", "owners_patch", {"dirs": dirs},
                            data=out, note="" if out["patch"] else "None of these directories had a change that could be applied safely.")

    # ---- tool 10: working group (FalkorDB label propagation, built by orggraph.groups) -------
    def working_group(self, path: str) -> Answer:
        """The areas worked on by the same people as this one (a community found by
        algo.labelPropagation over WORKS_WITH links), their SIG labels, and who does the work."""
        d, extra = self.resolve_dir(path)
        if not d:
            return self._answer("unknown_entity", "working_group", {"path": path}, data={"suggestions": extra},
                                note=f"No directory '{path}' with an OWNERS file in the graph.")
        row = self.q("group_of", "MATCH (x:Dir {repo: 'kubernetes', path: $p}) RETURN x.group, x.group_name", {"p": d})
        gid, name = row[0] if row else (None, None)
        if gid is None:
            return self._answer("not_in_graph", "working_group", {"path": d}, data={"dir": d},
                                note=f"{d} isn't part of a working group: too few people work there and in other areas.")
        members = self.q("group_members", """
            MATCH (x:Dir {repo: 'kubernetes'}) WHERE x.group = $g
            OPTIONAL MATCH (x)-[:LABELED]->(s:SIG)
            RETURN x.path, collect(DISTINCT s.name) ORDER BY x.path""", {"g": gid})
        links = self.q("group_links", """
            MATCH (x:Dir {repo: 'kubernetes', path: $p})-[w:WORKS_WITH]-(y:Dir)
            RETURN y.path, w.shared, w.people ORDER BY w.shared DESC, y.path LIMIT 6""", {"p": d})
        people = self.q("group_people", """
            MATCH (x:Dir {repo: 'kubernetes'}) WHERE x.group = $g
            MATCH (p:Person)-[:REVIEWED|LGTM|APPROVED_PR]->(pr:PR)-[:TOUCHES]->(x) WHERE pr.day >= $since_day
            WITH p, count(DISTINCT pr) AS n, count(DISTINCT x) AS areas
            RETURN p.handle, n, areas ORDER BY n DESC, p.handle LIMIT 6""", {"g": gid, "since": self.since(12)})
        own_sigs = next((sg for m, sg in members if m == d), [])
        sigs = collections.Counter(s for _, sg in members for s in sg)
        return self._answer("answered_with_path", "working_group", {"path": d}, data={
            "dir": d, "group": gid, "name": name, "own_sigs": own_sigs,
            "areas": [{"dir": m, "sigs": sg} for m, sg in members],
            "sigs": [s for s, _ in sigs.most_common()],
            "links": [{"dir": y, "shared": n, "people": ps} for y, n, ps in links],
            "people": [{"handle": h, "prs": n, "areas": a} for h, n, a in people]},
            paths=[[d, f"same people as {y} (e.g. @{ps[0]})"] for y, n, ps in links[:3] if ps])

    # ---- tool 11 / 12: what-if on a copy of the graph (orggraph.whatif) -------------------
    def what_if(self, handles) -> Answer:
        """If these people stopped approving, where would approval requests get stuck? Runs on a GRAPH.COPY."""
        from orggraph.whatif import step_back
        if isinstance(handles, str):
            handles = re.findall(r"@?([A-Za-z0-9][A-Za-z0-9-]*)", handles)
        return step_back(self, handles)

    def check_fix(self, dirs: list[str]) -> Answer:
        """Apply the drafted OWNERS changes to a GRAPH.COPY and check no area is left waiting on silent approvers."""
        from orggraph.whatif import check_fix
        return check_fix(self, dirs)


CATALOGUE = {
    "who_owns": {"fn": "who_owns", "args": {"path": "directory or file path in kubernetes/kubernetes"},
                 "desc": "Official owners: the OWNERS chain from a directory up to the root, approvers and teams at each level."},
    "who_knows": {"fn": "who_knows", "args": {"path": "directory path", "months": "int 1-24, default 12"},
                  "desc": "Listed approvers with recent activity (silent if none) and unlisted people doing the work (shadow owners)."},
    "drift_summary": {"fn": "drift_summary", "args": {"months": "int 1-24", "limit": "int 1-25"},
                      "desc": "Org-wide: how many approvers are silent and which directories have the most stale ownership."},
    "route": {"fn": "route", "args": {"path": "directory path", "months": "int 1-24, default 6"},
              "desc": "Who to send a new issue or PR in this area to, by recent review and approval work, with evidence."},
    "why": {"fn": "why", "args": {"kep": "KEP number, e.g. 2340"},
            "desc": "A decision record: authors, approvers, owning team, milestones, related decisions, approvers still active?"},
    "propose_owners_update": {"fn": "propose_owners_update",
                              "args": {"path": "directory path", "months": "int", "min_prs": "int, default 10"},
                              "desc": "Draft an OWNERS change for a directory with evidence for every line (a proposal, never applied)."},
    "what_if": {"fn": "what_if", "args": {"handles": "one or more GitHub handles, e.g. '@liggitt @dims'"},
                "desc": "What if these people stepped back: which areas' approval requests would wait on silent approvers, and who already reviews there. Simulated on a copy of the graph."},
    "working_group": {"fn": "working_group", "args": {"path": "directory path"},
                      "desc": "Areas worked on by the same people as this one (found with label propagation), their SIG labels and core people."},
    "suggest_reviewers": {"fn": "suggest_reviewers",
                          "args": {"paths": "file or directory paths, separated by spaces, commas or newlines",
                                   "pr": "int, a merged PR number (optional)", "months": "int 1-24, default 6"},
                          "desc": "Who should review a change touching these files: recent reviewers per area, and whether listed approvers are active."},
}
