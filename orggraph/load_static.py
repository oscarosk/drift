"""
Loads the *official* structure of Kubernetes into FalkorDB:

  sigs.yaml       -> teams (SIGs), their chairs/tech leads, subprojects
  OWNERS files    -> who approves / reviews each directory (incl. past "emeritus" owners)
  kep.yaml files  -> enhancement proposals: the formal decision records

Privacy (hackathon rule 6): we keep only public GitHub handles.
Names, emails and employers in sigs.yaml are deliberately NOT loaded.

Run:  python -m orggraph.load_static
"""
import glob
import os
import re

import yaml

from orggraph.db import graph

DATA = os.path.join(os.path.dirname(__file__), "..", "data")
BATCH = 500


def handle(h: str) -> str:
    """GitHub handles are case-insensitive; normalise '@Foo' -> 'foo'."""
    return h.strip().lstrip("@").strip().lower()


def run_batched(g, query: str, rows: list[dict]):
    """Send rows in chunks with UNWIND: far faster than one query per row."""
    for i in range(0, len(rows), BATCH):
        g.query(query, {"rows": rows[i : i + BATCH]})


# ---------------------------------------------------------------------------
# 1. Teams
# ---------------------------------------------------------------------------
OWNERS_URL = re.compile(
    r"https://raw\.githubusercontent\.com/(?P<org>[^/]+)/(?P<repo>[^/]+)/[^/]+/(?P<path>.*?)/?OWNERS$"
)


def dir_id(repo: str, path: str) -> str:
    """One id format for a directory everywhere: 'kubernetes:pkg/kubelet'."""
    return f"{repo}:{path.strip('/') or '.'}"


def load_sigs(g):
    d = yaml.safe_load(open(os.path.join(DATA, "community", "sigs.yaml")))
    sigs, leads, subs, sub_dirs = [], [], [], []
    for s in d["sigs"]:
        sid = s["dir"]  # e.g. 'sig-node' (same id KEPs and OWNERS labels use)
        sigs.append({"id": sid, "name": s["name"], "mission": s.get("mission_statement", "")[:500]})
        for role, key in (("CHAIR_OF", "chairs"), ("TECH_LEAD_OF", "tech_leads")):
            for p in (s.get("leadership") or {}).get(key) or []:
                leads.append({"handle": handle(p["github"]), "sig": sid, "role": role})
        for sp in s.get("subprojects") or []:
            subs.append({"name": sp["name"], "sig": sid})
            for url in sp.get("owners") or []:
                m = OWNERS_URL.match(url)
                if m:
                    sub_dirs.append({"sub": sp["name"], "dir": dir_id(m["repo"], m["path"]),
                                     "repo": m["repo"], "path": m["path"].strip("/") or "."})

    run_batched(g, "UNWIND $rows AS r MERGE (s:SIG {id: r.id}) SET s.name = r.name, s.mission = r.mission", sigs)
    for role in ("CHAIR_OF", "TECH_LEAD_OF"):  # relationship types can't be parameters in Cypher
        run_batched(g, f"""
            UNWIND $rows AS r
            MERGE (p:Person {{handle: r.handle}})
            WITH p, r MATCH (s:SIG {{id: r.sig}})
            MERGE (p)-[:{role}]->(s)""", [x for x in leads if x["role"] == role])
    run_batched(g, """
        UNWIND $rows AS r
        MATCH (s:SIG {id: r.sig})
        MERGE (sp:Subproject {name: r.name})
        MERGE (s)-[:HAS_SUBPROJECT]->(sp)""", subs)
    run_batched(g, """
        UNWIND $rows AS r
        MATCH (sp:Subproject {name: r.sub})
        MERGE (d:Dir {id: r.dir}) SET d.repo = r.repo, d.path = r.path
        MERGE (sp)-[:OWNS]->(d)""", sub_dirs)
    print(f"SIGs {len(sigs)}, leadership edges {len(leads)}, subprojects {len(subs)}, subproject->dir {len(sub_dirs)}")


# ---------------------------------------------------------------------------
# 2. OWNERS files
# ---------------------------------------------------------------------------
ROLES = {  # OWNERS key -> relationship type
    "approvers": "APPROVER_OF",
    "reviewers": "REVIEWER_OF",
    "emeritus_approvers": "EMERITUS_APPROVER_OF",
    "emeritus_reviewers": "EMERITUS_REVIEWER_OF",
}


def load_owners(g):
    root = os.path.join(DATA, "k8s")
    aliases = yaml.safe_load(open(os.path.join(root, "OWNERS_ALIASES")))["aliases"]

    def expand(names):
        """Turn ['sig-node-approvers', 'dims'] into [('alice','sig-node-approvers'), ('dims', None)]."""
        out = []
        for n in names or []:
            n = str(n).split("#")[0].strip()
            if n in aliases:
                out += [(handle(m), n) for m in aliases[n] or []]
            elif n and not TEAM_ALIAS.search(handle(n)):  # alias defined in another repo: skip
                out.append((handle(n), None))
        return out

    dirs, edges, labels = [], {r: [] for r in ROLES.values()}, []
    for f in glob.glob(os.path.join(root, "**", "OWNERS"), recursive=True):
        path = os.path.relpath(os.path.dirname(f), root)
        d = yaml.safe_load(open(f)) or {}
        if "filters" in d:  # filtered OWNERS: the ".*" filter applies to every file
            d = {**d, **(d["filters"].get(".*") or {})}
        did = dir_id("kubernetes", path)
        dirs.append({"id": did, "path": "." if path == "." else path,
                     "no_parent": bool((d.get("options") or {}).get("no_parent_owners"))})
        for key, rel in ROLES.items():
            for h, via in expand(d.get(key)):
                edges[rel].append({"handle": h, "dir": did, "via": via or "direct"})
        for lab in d.get("labels") or []:
            if str(lab).startswith("sig/"):
                labels.append({"dir": did, "sig": "sig-" + lab[4:]})

    run_batched(g, """
        UNWIND $rows AS r
        MERGE (d:Dir {id: r.id})
        SET d.repo = 'kubernetes', d.path = r.path, d.no_parent_owners = r.no_parent, d.has_owners = true""", dirs)

    # Parent links: each OWNERS dir points at the nearest ancestor dir that has its own OWNERS file.
    # Ownership is inherited upward, so "who owns X" walks this chain.
    have = {x["path"] for x in dirs}
    parents = []
    for x in dirs:
        p = x["path"]
        while p not in (".", ""):
            p = os.path.dirname(p) or "."
            if p in have:
                parents.append({"child": x["id"], "parent": dir_id("kubernetes", p)})
                break
    run_batched(g, """
        UNWIND $rows AS r
        MATCH (c:Dir {id: r.child}), (p:Dir {id: r.parent})
        MERGE (c)-[:INSIDE]->(p)""", parents)

    for rel, rows in edges.items():
        run_batched(g, f"""
            UNWIND $rows AS r
            MERGE (p:Person {{handle: r.handle}})
            WITH p, r MATCH (d:Dir {{id: r.dir}})
            MERGE (p)-[e:{rel}]->(d) SET e.via = r.via""", rows)
    run_batched(g, """
        UNWIND $rows AS r
        MATCH (d:Dir {id: r.dir}), (s:SIG {id: r.sig})
        MERGE (d)-[:LABELED]->(s)""", labels)
    print(f"OWNERS dirs {len(dirs)}, parent links {len(parents)}, "
          + ", ".join(f"{k} {len(v)}" for k, v in edges.items()) + f", sig labels {len(labels)}")


# ---------------------------------------------------------------------------
# 3. Enhancement proposals (decisions)
# ---------------------------------------------------------------------------
TEAM_ALIAS = re.compile(r"^sig-|^wg-|-(leads|tech-leads|chairs|approvers|reviewers)$")


def kep_num(ref: str):
    m = re.search(r"/(\d+)-", str(ref))
    return int(m.group(1)) if m else None


def load_keps(g):
    keps, people, links, gates, releases, sigs = [], [], [], [], [], []
    for f in glob.glob(os.path.join(DATA, "enh", "keps", "**", "kep.yaml"), recursive=True):
        try:
            # BaseLoader keeps every value as a string: some KEPs contain invalid dates
            # (e.g. month 13) that would crash a normal YAML loader.
            k = yaml.load(open(f), Loader=yaml.BaseLoader) or {}
        except yaml.YAMLError:
            continue
        num = k.get("kep-number")
        if not str(num).isdigit():
            continue
        num = int(num)
        keps.append({
            "number": num, "title": str(k.get("title", "")), "status": str(k.get("status", "")),
            "stage": str(k.get("stage", "")), "created": str(k.get("creation-date", "")),
            "updated": str(k.get("last-updated", "")), "latest": str(k.get("latest-milestone", "")),
            "url": "https://github.com/kubernetes/enhancements/tree/master/"
                   + os.path.relpath(os.path.dirname(f), os.path.join(DATA, "enh")),
        })
        if k.get("owning-sig"):
            sigs.append({"kep": num, "sig": k["owning-sig"], "rel": "OWNED_BY"})
        for s in k.get("participating-sigs") or []:
            sigs.append({"kep": num, "sig": s, "rel": "INVOLVES"})
        for key, rel in (("authors", "AUTHORED"), ("reviewers", "REVIEWED_KEP"), ("approvers", "APPROVED_KEP")):
            for h in k.get(key) or []:
                # Skip team aliases written like handles, e.g. "@sig-node-tech-leads":
                # they are groups, not people.
                if isinstance(h, str) and h.strip().startswith("@") and not TEAM_ALIAS.search(handle(h)):
                    people.append({"kep": num, "handle": handle(h), "rel": rel})
        for key, rel in (("see-also", "SEE_ALSO"), ("replaces", "REPLACES"), ("superseded-by", "SUPERSEDED_BY")):
            for ref in k.get(key) or []:
                if (t := kep_num(ref)) and t != num:
                    links.append({"kep": num, "other": t, "rel": rel})
        for fg in k.get("feature-gates") or []:
            if isinstance(fg, dict) and fg.get("name"):
                for comp in fg.get("components") or []:
                    gates.append({"kep": num, "gate": fg["name"], "component": str(comp)})
        for stage, ver in (k.get("milestone") or {}).items():
            if ver:
                releases.append({"kep": num, "stage": str(stage), "version": str(ver).lstrip("v")})

    run_batched(g, """
        UNWIND $rows AS r
        MERGE (k:KEP {number: r.number})
        SET k.title = r.title, k.status = r.status, k.stage = r.stage, k.created = r.created,
            k.updated = r.updated, k.latest_milestone = r.latest, k.url = r.url""", keps)
    for rel in ("OWNED_BY", "INVOLVES"):
        run_batched(g, f"""
            UNWIND $rows AS r
            MATCH (k:KEP {{number: r.kep}}), (s:SIG {{id: r.sig}})
            MERGE (k)-[:{rel}]->(s)""", [x for x in sigs if x["rel"] == rel])
    for rel in ("AUTHORED", "REVIEWED_KEP", "APPROVED_KEP"):
        run_batched(g, f"""
            UNWIND $rows AS r
            MERGE (p:Person {{handle: r.handle}})
            WITH p, r MATCH (k:KEP {{number: r.kep}})
            MERGE (p)-[:{rel}]->(k)""", [x for x in people if x["rel"] == rel])
    for rel in ("SEE_ALSO", "REPLACES", "SUPERSEDED_BY"):
        run_batched(g, f"""
            UNWIND $rows AS r
            MATCH (a:KEP {{number: r.kep}}), (b:KEP {{number: r.other}})
            MERGE (a)-[:{rel}]->(b)""", [x for x in links if x["rel"] == rel])
    run_batched(g, """
        UNWIND $rows AS r
        MATCH (k:KEP {number: r.kep})
        MERGE (f:FeatureGate {name: r.gate})
        MERGE (c:Component {name: r.component})
        MERGE (k)-[:INTRODUCES]->(f)
        MERGE (f)-[:AFFECTS]->(c)""", gates)
    run_batched(g, """
        UNWIND $rows AS r
        MATCH (k:KEP {number: r.kep})
        MERGE (v:Release {version: r.version})
        MERGE (k)-[:REACHED {stage: r.stage}]->(v)""", releases)
    print(f"KEPs {len(keps)}, people edges {len(people)}, KEP links {len(links)}, "
          f"feature gates {len(gates)}, milestones {len(releases)}")


def create_indexes(g):
    for label, prop in [("Person", "handle"), ("SIG", "id"), ("Subproject", "name"), ("Dir", "id"),
                        ("Dir", "path"), ("KEP", "number"), ("FeatureGate", "name"),
                        ("Component", "name"), ("Release", "version")]:
        try:
            g.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
        except Exception:
            pass  # already exists


def main():
    g = graph()
    create_indexes(g)
    load_sigs(g)
    load_owners(g)
    load_keps(g)
    for label in ["Person", "SIG", "Subproject", "Dir", "KEP", "FeatureGate", "Component", "Release"]:
        n = g.ro_query(f"MATCH (n:{label}) RETURN count(n)").result_set[0][0]
        print(f"  {label:12} {n}")


if __name__ == "__main__":
    main()
