# Drift

[![ci](https://github.com/oscarosk/drift/actions/workflows/ci.yml/badge.svg)](https://github.com/oscarosk/drift/actions/workflows/ci.yml)

**Your ownership records are already out of date. Drift shows where, proves it, and drafts the fix.**

On 400 recent Kubernetes PRs, Drift's top 3 suggested reviewers included the person who actually
approved the change **78%** of the time. Picking from the OWNERS files: **37%**.

Every large engineering organisation writes down who owns what: CODEOWNERS, OWNERS files, team
pages. Then people move on, new people quietly take over, and the records stop matching reality.
Drift is a company brain built on [FalkorDB](https://www.falkordb.com/) that compares **what the
records say** with **what people actually do**, and shows the graph path behind every answer.

It runs on a real organisation: **Kubernetes**, with 24 teams (SIGs), ~800 people, ~950 owned
directories, ~680 decision records (KEPs) and 6,700+ merged pull requests with their reviews and
approvals, all public.

Tracks: **Company Brain** (primary), **Agents That Act on Connected Data**.

> Status: work in progress during Graph Hacks (Oct 15–18, 2026).

## Does it work? Measured on 400 real PRs

Tested on 400 Kubernetes PRs merged between 2026-07-16 and 2026-10-16. For each one, Drift suggested
reviewers using **only activity from before that PR**, and we checked against who actually typed `/approve`:

| Method | Right person first | Right person in top 3 |
|---|---|---|
| **Drift** (Find a reviewer) | **52%** | **78%** (95% CI 74–82%) |
| Pick from the OWNERS files (what the records say) | 13% | 37% |
| Ask the busiest approvers | n/a | 46% |

OWNERS files don't rank people, so their figures are the exact chance that a pick from the nearest
OWNERS files includes the right approver (today's files, which slightly favours them). Re-run with
`python -m orggraph.eval_reviewers`; results in [`results/reviewer_eval.json`](results/reviewer_eval.json).
If you fetch the data on a different day, expect the figures to move by a point or two.

An earlier run said 86%. That number was wrong: a FalkorDB index bug (below) let activity from after
each PR leak into the prediction. The figure above is after the fix, and `tests/test_eval_no_leak.py`
keeps it that way.

## What it found

Numbers below are produced by `python -m orggraph.backtest` and stored in
[`results/backtest.json`](results/backtest.json).

- **A third of approvers were silent for a year.** On 2025-10-16, 49 of 152 Kubernetes approvers had
  no reviews, `/lgtm`, `/approve` or merged PRs in kubernetes/kubernetes in the previous 12 months.
- **The records did not notice.** A year later, 44 of those 49 were still listed as approvers.
- **The people doing the work show up early.** Ranking non-owners by review activity on
  2025-10-16, the top 30 became reviewers or approvers within a year at **5.3× the base rate**.

Scope note: activity covers merged PRs in kubernetes/kubernetes only. Someone "silent" here may be
active in other Kubernetes repositories, or in work that leaves no PR trail. Drift reports what the
graph shows, not judgements about people.

## Quick start (local)

```bash
docker compose up -d                                  # FalkorDB on :6379, Browser on :3000
pip install -r requirements.txt
cp .env.example .env                                  # add GITHUB_TOKEN (and OPENAI_API_KEY)
python -m orggraph.fetch_reviews --since 2024-10-16   # reviews + approvals via GitHub API (~20 min)
python scripts/build.py                               # downloads public files, builds both graphs, runs the backtest
```

Then run the app:

```bash
uvicorn orggraph.api:app --port 8000      # web UI at http://localhost:8000
python -m orggraph.agent "Who really works on pkg/kubelet/cm/cpumanager?"   # agent from the terminal
python -m orggraph.mcp_server             # MCP server (stdio) for Claude Desktop, Cursor, VS Code
python -m pytest -q tests                 # tests
```

Open http://localhost:3000 (FalkorDB Browser, host `localhost`, port `6379`) to explore the graphs.
With `OPENAI_API_KEY` set, a model picks the tool and writes the sentence; without it, a keyword
router and templates do, so everything works offline.

### MCP

```json
{"mcpServers": {"drift": {"command": "python", "args": ["-m", "orggraph.mcp_server"], "cwd": "/path/to/drift"}}}
```

## Using the app

| Screen | For | What it does |
|---|---|---|
| **Find a reviewer** | Contributors, every PR | Paste the files a change touches (or a merged PR number): the people who actually review that code now, with recent PRs as evidence, plus which listed owners have gone quiet. |
| **Areas** | Anyone new to a part of the code | Every OWNERS directory, searchable, marked drifting / healthy / inherits; each opens a view of *what OWNERS says* next to *who actually does the work*. |
| **Cleanup queue** | Maintainers, monthly | Drafted OWNERS fixes for the most drifted directories. Tick the ones you agree with and download one patch: `git apply drift-owners.patch`. Edits keep comments and formatting, are re-parsed and checked before export, and files using `filters:` are left for humans. |
| **How we know** | Skeptics | The year-later backtest, its method and its limits. |

Ask anything from the top bar; answers open beside the page with their graph path and query trace.
The data refreshes by re-running `fetch_reviews` and `build.py` (both incremental-friendly), e.g. weekly.

## How the agent works

1. **A fixed tool catalogue.** The model chooses one of six tools and fills in arguments; it never
   sees or writes Cypher. Every tool is a hand-written, parameterised query.
2. **Names are resolved first.** A directory, person or KEP is looked up before any traversal runs.
   Typos get suggestions (`pkg/kublet` → `pkg/kubelet`); a file path resolves to the directory whose
   OWNERS file governs it, and the answer says so.
3. **Four kinds of answer:** `answered_with_path`, `answered_from_memory`, `not_in_graph`
   (the thing exists but nothing connects it that way) and `unknown_entity` (no query ran).
4. **Checked sentences.** The model's sentence is rejected if it names a person, directory or KEP the
   graph didn't return, or claims a finding when the result was empty; a template built from the rows
   is used instead.
5. **Memory is more nodes.** Answers are saved as `(:Finding)-[:ABOUT]->(...)` and shown, with the date
   they were learned, next to a fresh re-check the next time the same thing comes up.

| Tool | What it answers |
|---|---|
| `who_owns(path)` | The OWNERS chain from a directory up to the root, approvers and teams at each level |
| `who_knows(path, months)` | Listed approvers with recent activity (silent if none), and shadow owners |
| `drift_summary(months)` | Share of silent approvers org-wide, and the most drifted directories |
| `route(path, months)` | Who to send new work to, by recent review and approval work, with example PRs |
| `why(kep)` | A decision: authors, approvers, owning SIG, milestones, related KEPs, who is still active |
| `propose_owners_update(path)` | A drafted OWNERS change with evidence for every line (never applied) |
| `suggest_reviewers(paths or pr)` | Who should review a concrete change, across every area it touches |
| `owners_patch(dirs)` | A `git apply`-able patch for drafted fixes (MCP and the Cleanup queue) |

## Data sources

| Source | What it gives the graph |
|---|---|
| `kubernetes/community/sigs.yaml` | Teams, chairs, tech leads, subprojects and the code they own |
| `kubernetes/kubernetes/**/OWNERS` + `OWNERS_ALIASES` | Approvers, reviewers and past (emeritus) owners of every directory |
| `kubernetes/enhancements/keps/**/kep.yaml` | Decisions: authors, reviewers, approvers, owning team, status, milestones, feature gates |
| git history of `kubernetes/kubernetes` | Every merged PR since Oct 2024 and the files it changed |
| GitHub GraphQL API | Who reviewed each merged PR, and who typed Prow `/lgtm` and `/approve` |

**Privacy:** only public GitHub handles are stored. Names, emails and employers present in
`sigs.yaml` are deliberately not loaded, and no comment text is stored.

## Two graphs, one instance

FalkorDB holds many isolated graphs in one instance. Drift uses that for time travel:

| Graph | OWNERS files | Activity |
|---|---|---|
| `kubernetes` | today | Oct 2024 to today |
| `kubernetes@2025-10-16` | as of 2025-10-16 | only the 12 months before 2025-10-16 |

The backtest runs Drift's queries on the year-ago graph, which never sees the future, and checks
the predictions against today's graph.

## Graph data model

```
(:Person {handle})-[:CHAIR_OF|TECH_LEAD_OF]->(:SIG {id, name, mission})
(:SIG)-[:HAS_SUBPROJECT]->(:Subproject {name})-[:OWNS]->(:Dir {id, repo, path})
(:Person)-[:APPROVER_OF|REVIEWER_OF|EMERITUS_APPROVER_OF|EMERITUS_REVIEWER_OF {via}]->(:Dir)
(:Dir)-[:INSIDE]->(:Dir)          # nearest ancestor with its own OWNERS file (ownership inheritance)
(:Dir)-[:LABELED]->(:SIG)

(:Person)-[:AUTHORED_PR]->(:PR {number, merged})
(:Person)-[:REVIEWED {state}]->(:PR)
(:Person)-[:LGTM]->(:PR)          # Prow /lgtm
(:Person)-[:APPROVED_PR]->(:PR)   # Prow /approve
(:PR)-[:TOUCHES {files}]->(:Dir)  # the OWNERS directory responsible for the changed files

(:KEP {number, title, status, stage, created, updated, latest_milestone, url})-[:OWNED_BY|INVOLVES]->(:SIG)
(:Person)-[:AUTHORED|REVIEWED_KEP|APPROVED_KEP]->(:KEP)
(:KEP)-[:SEE_ALSO|REPLACES|SUPERSEDED_BY]->(:KEP)
(:KEP)-[:INTRODUCES]->(:FeatureGate {name})-[:AFFECTS]->(:Component {name})
(:KEP)-[:REACHED {stage}]->(:Release {version})
```

`via` on ownership edges records whether a person is listed directly or through a team alias
(e.g. `sig-node-approvers`), so answers can show *why* someone is an owner.

## Cypher queries the product depends on

**Silent owners of a directory**: listed approvers, with their activity on that directory's
subtree and anywhere. Work under a directory counts for it through `INSIDE*`, the same way
OWNERS inheritance works.

```cypher
MATCH (p:Person)-[:APPROVER_OF]->(d:Dir {path: $path})
OPTIONAL MATCH (p)-[a:AUTHORED_PR|REVIEWED|LGTM|APPROVED_PR]->(pr:PR)-[:TOUCHES]->(:Dir)-[:INSIDE*0..10]->(d)
  WHERE pr.merged >= $since
WITH p, d, count(DISTINCT pr) AS here
OPTIONAL MATCH (p)-[:AUTHORED_PR|REVIEWED|LGTM|APPROVED_PR]->(pr2:PR) WHERE pr2.merged >= $since
RETURN p.handle AS person, here, count(DISTINCT pr2) AS anywhere
ORDER BY here, anywhere
```

**Shadow owners of a directory**: people doing the work who are not listed on it or on any
directory above it.

```cypher
MATCH (d:Dir {path: $path})
MATCH (p:Person)-[a:AUTHORED_PR|REVIEWED|LGTM|APPROVED_PR]->(pr:PR)-[:TOUCHES]->(:Dir)-[:INSIDE*0..10]->(d)
  WHERE pr.merged >= $since
WITH d, p, count(DISTINCT pr) AS prs, collect(DISTINCT type(a)) AS how
WHERE NOT (p)-[:APPROVER_OF|REVIEWER_OF]->(d)
OPTIONAL MATCH (d)-[:INSIDE*1..10]->(up:Dir)<-[:APPROVER_OF|REVIEWER_OF]-(p)
WITH p, prs, how, count(up) AS listed_above
WHERE listed_above = 0
RETURN p.handle AS person, prs, how ORDER BY prs DESC LIMIT 8
```

**Org-wide silent approvers**

```cypher
MATCH (p:Person)-[:APPROVER_OF]->(:Dir)
WITH DISTINCT p
OPTIONAL MATCH (p)-[:AUTHORED_PR|REVIEWED|LGTM|APPROVED_PR]->(pr:PR) WHERE pr.merged >= $since
WITH p, count(DISTINCT pr) AS n
RETURN count(p) AS approvers, sum(CASE WHEN n = 0 THEN 1 ELSE 0 END) AS silent
```

More queries are added here as the agent tools are built.

## A FalkorDB issue we hit

**An index range scan on a string property ignores the upper bound.** We first saw it on graph module
4.18.3 (falkordblite) and confirmed it on the current `falkordb/falkordb:latest` Docker image. With an
index on `PR.merged` (ISO date strings), `WHERE p.merged >= '2026-09-05' AND p.merged < '2026-09-10'`
returned rows up to `2026-09-19`, and `p.merged < X` alone returned every row. Integer ranges are
correct. Reproduce with `python scripts/repro_falkordb_range.py` (prints `5 / 15 / 5`; all three should be 5).
CI runs it on every push against the latest FalkorDB image, so we'll see when it's fixed. Workaround in Drift: every PR also
stores `day` as an integer (`20260910`), and all date filters use it.

## Honest limits

- Activity is merged PRs in one repository. Reviews on unmerged PRs and work in other repos are not counted.
- Predicting *removal* of silent owners is weak (1.3× chance), because Kubernetes rarely cleans up
  its OWNERS files. We report it anyway; the stale records are the finding.
- OWNERS `no_parent_owners` (which stops inheritance) is applied in `who_owns`, not yet in the activity queries.
- Memory lives in the same graph, so rebuilding the graph clears it.

## Use of AI coding assistants

This project was built with help from Claude (Anthropic) as a coding assistant, as permitted by
rule 11. All design decisions, queries and code were reviewed, run and verified by the author.
