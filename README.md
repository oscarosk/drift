# OrgGraph

**Ask who owns, who really knows, and why anything in Kubernetes was decided, and see the exact path behind every answer.**

OrgGraph is a company brain for an open-source organisation, built on [FalkorDB](https://www.falkordb.com/).
It uses Kubernetes as the "company": 24 teams (SIGs), ~800 people, ~950 owned directories and ~680
enhancement proposals (KEPs), all from public files.

Tracks: **Company Brain** (primary), **Agents That Act on Connected Data**.

> Status: work in progress during Graph Hacks (Oct 15–18, 2026).

## Quick start (local)

```bash
docker compose up -d                 # FalkorDB on :6379, Browser on :3000
pip install -r requirements.txt
cp .env.example .env                 # add your keys
./scripts/fetch_data.sh              # sparse-clones the public Kubernetes files (~1 min)
python -m orggraph.load_static       # builds the graph (~3 s)
```

Open http://localhost:3000 to explore the graph in FalkorDB Browser.

## Data sources

| Source | What it gives the graph |
|---|---|
| `kubernetes/community/sigs.yaml` | Teams, chairs, tech leads, subprojects and the code they own |
| `kubernetes/kubernetes/**/OWNERS` + `OWNERS_ALIASES` | Approvers, reviewers and past (emeritus) owners of every directory |
| `kubernetes/enhancements/keps/**/kep.yaml` | Decisions: authors, reviewers, approvers, owning team, status, milestones, feature gates, links between decisions |

**Privacy:** only public GitHub handles are stored. Names, emails and employers present in
`sigs.yaml` are deliberately not loaded.

## Graph data model

```
(:Person {handle})-[:CHAIR_OF|TECH_LEAD_OF]->(:SIG {id, name, mission})
(:SIG)-[:HAS_SUBPROJECT]->(:Subproject {name})-[:OWNS]->(:Dir {id, repo, path})
(:Person)-[:APPROVER_OF|REVIEWER_OF|EMERITUS_APPROVER_OF|EMERITUS_REVIEWER_OF {via}]->(:Dir)
(:Dir)-[:INSIDE]->(:Dir)          # nearest ancestor with its own OWNERS file (ownership inheritance)
(:Dir)-[:LABELED]->(:SIG)
(:KEP {number, title, status, stage, created, updated, latest_milestone, url})-[:OWNED_BY|INVOLVES]->(:SIG)
(:Person)-[:AUTHORED|REVIEWED_KEP|APPROVED_KEP]->(:KEP)
(:KEP)-[:SEE_ALSO|REPLACES|SUPERSEDED_BY]->(:KEP)
(:KEP)-[:INTRODUCES]->(:FeatureGate {name})-[:AFFECTS]->(:Component {name})
(:KEP)-[:REACHED {stage}]->(:Release {version})
```

`via` on ownership edges records whether a person is listed directly or through a team alias
(e.g. `sig-node-approvers`), so answers can show *why* someone is an owner.

## Cypher queries the product depends on

_Filled in as each agent tool is built._

## Use of AI coding assistants

This project was built with help from Claude (Anthropic) as a coding assistant, as permitted by
rule 11. All design decisions, queries and code were reviewed, run and verified by the author.
