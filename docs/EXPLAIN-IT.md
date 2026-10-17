# Explaining Drift

A plain-language guide to how Drift works and why it is built the way it is, written so the author can
explain every part to a judge (rules 12 and 13). Read it top to bottom once; the last section is the
questions a judge is most likely to ask, each with a short answer.

## 1. The one-paragraph version

Big projects write down who owns what (Kubernetes uses `OWNERS` files). People move on, others quietly
take over, and the files stop matching reality. Drift loads both into one FalkorDB graph: **what the
records say** (who is listed in `OWNERS`) and **what people actually do** (who wrote, reviewed, `/lgtm`'d
and `/approve`'d merged pull requests). Every question becomes a walk through that graph. Drift uses it to
pick the right reviewer for a change, to draft fixes to the records, to check those fixes on a copy of the
graph, and to show the path behind every answer.

## 2. Where the data comes from

| Source | What we take | How |
|---|---|---|
| `kubernetes/community/sigs.yaml` | the 24 teams (SIGs), their leads, subprojects | `scripts/fetch_data.py`, a sparse git clone |
| `kubernetes/kubernetes` `OWNERS` + `OWNERS_ALIASES` | who approves / reviews each directory | same; aliases like `sig-node-approvers` are expanded into people |
| `kubernetes/enhancements` `kep.yaml` | ~680 decision records (KEPs) | same |
| git history of `kubernetes/kubernetes` | every merged PR since Oct 2024 and the files it changed | `scripts/fetch_history.py`: each "Merge pull request #N" commit diffed against its first parent |
| GitHub GraphQL API | reviews, and `/lgtm` `/approve` comments, per PR | `orggraph/fetch_reviews.py`, 7-day windows, resumable; bots removed |

Only public GitHub handles are stored. No names, emails or comment text (rule 6).

`scripts/build.py` runs everything in order: fetch, load the static structure twice (today and a year
ago), load activity twice, working groups, what-if results, the backtest and the reviewer evaluation.

## 3. The graph model

See the picture in the README (`docs/model.svg`). The important parts:

- `(:Person)-[:APPROVER_OF {via}]->(:Dir)`: **the record**. `via` says whether the person is listed directly
  or through an alias.
- `(:Person)-[:AUTHORED_PR | REVIEWED | LGTM | APPROVED_PR]->(:PR)` and `(:PR)-[:TOUCHES]->(:Dir)`: **the work**.
  `TOUCHES` points at the nearest directory that has its own `OWNERS` file, because that is the file that
  governs the changed code.
- `(:Dir)-[:INSIDE]->(:Dir)`: each directory points to its nearest ancestor with an `OWNERS` file.
  `OWNERS` files inherit, so `[:INSIDE*0..10]` walks up the chain. `no_parent_owners` stops the walk.
- `(:Dir)-[:WORKS_WITH]->(:Dir)` and `Dir.group`: the working-group projection and its communities.
- `(:Finding)-[:ABOUT]->(...)` and `(:Finding)-[:CITES]->(:Person)`: the agent's memory, in the same graph.
- `PR.day` is the merge date as an integer (`20260910`). That exists because of a FalkorDB bug (section 7).

Two graphs live side by side in one FalkorDB instance: `kubernetes` (today) and `kubernetes@2025-10-16`
(the `OWNERS` files as they were a year ago, with only the activity before that date). The second one lets
us test predictions honestly: we run Drift on the past and compare with what happened since.

**Why a graph and not tables?** The questions are chains: person → PR → directory → parent directory →
who is listed there. Ownership inheritance is a variable-length walk up a tree (`[:INSIDE*0..10]`), which is
one line of Cypher and painful recursive SQL. And the what-if needs a whole-graph copy (`GRAPH.COPY`).

## 4. The architecture

```
 data sources ──► scripts/fetch_*.py ──► data/*.jsonl ──► orggraph/load_*.py ──► FalkorDB (two graphs)
                                                                                      │
   orggraph/tools.py  ◄───────────── fixed, parameterised Cypher ─────────────────────┘
        │   (who_owns, who_knows, suggest_reviewers, what_if, check_fix, working_group, ...)
        ├──► orggraph/agent.py   picks a tool (OpenAI model, or keyword router), writes a checked sentence
        ├──► orggraph/api.py     FastAPI: the web app's endpoints, rate limits, caches
        ├──► orggraph/mcp_server.py   the same tools for Claude Desktop / Cursor (MCP)
        └──► orggraph/patch.py   turns drafted changes into a real `git apply`-able OWNERS patch
 web/index.html  one page app: Overview, Find a reviewer, Areas, Fix the records, How we know
```

Deployed: the app runs on Render (Singapore), FalkorDB on FalkorDB Cloud (Mumbai). GitHub Actions runs the
tests on every push against a real FalkorDB container.

## 5. How the agent answers a question

1. **Pick a tool.** The model (gpt-4o-mini) sees only the list of tools and their arguments and must pick
   one (`tool_choice: required`). It never sees or writes Cypher. Without a key, a keyword router does it.
2. **Resolve names first.** Every directory, person or KEP in the arguments is looked up before anything
   else runs. A typo (`pkg/kublet`) returns `unknown_entity` with suggestions, and no traversal runs.
3. **Run the tool.** Hand-written, parameterised Cypher. Each query is timed and listed in the answer's
   trace ("How this was answered").
4. **Write the sentence, then check it.** The model writes one or two sentences from the tool's JSON.
   `render.check` rejects the sentence if it names a person, directory or KEP that is not in the result,
   includes a number that is not in the result (or simply derived from it), or claims a finding when the
   result was empty. If rejected, a template built from the rows is used instead.
5. **Remember.** The answer is stored as a `Finding` linked to what it is about and the people it named.
   Next time, Drift compares the new answer with the remembered one and says what changed.

Four outcomes, as the organisers' blog recommends: `answered_with_path`, `answered_from_memory`,
`not_in_graph` (exists, but nothing connects it that way), `unknown_entity` (no such thing; nothing ran).

## 6. The features, and the query behind each

**Who really works here** (`who_knows`). Listed approvers with their activity in the last 12 months
("silent" if none), and unlisted people ranked by merged PRs they wrote, reviewed or approved in the
directory or below it.

**Find a reviewer** (`suggest_reviewers`). For each directory the change touches, people who reviewed or
approved PRs there in the last 6 months. Score = reviews + 2 × approvals. People covering more of the
change's directories rank first. The core query:

```cypher
MATCH (d:Dir {repo: 'kubernetes', path: $p})
MATCH (p:Person)-[a:REVIEWED|LGTM|APPROVED_PR]->(pr:PR)-[:TOUCHES]->(:Dir)-[:INSIDE*0..10]->(d)
  WHERE pr.day >= $since_day AND pr.day < $until_day AND NOT p.handle IN $exclude
...
RETURN p.handle, reviewed, approvals, last, examples ORDER BY approvals DESC, reviewed DESC, p.handle LIMIT 8
```

`$until_day` lets the evaluation ask "what would Drift have said before this PR?". `$exclude` removes the
PR's author.

**Drafted fixes** (`propose_owners_update`, `patch.py`). Silent approvers listed directly in a directory
move to `emeritus_approvers` (Kubernetes' own way of recording someone stepped back), and up to three people
who reviewed at least 10 PRs there are added as reviewers. `patch.py` edits the real file text, keeping
comments, re-parses the result to verify it, and skips files using `filters:` (too risky to edit
automatically) and people listed only through an alias.

**Check the fixes** (`check_fix`) and **what if** (`what_if`), in `orggraph/whatif.py`:
1. `GRAPH.COPY kubernetes whatif_<id>`: copy the graph.
2. On the copy, move the approvals to emeritus (for a what-if: everything the people approve).
3. On both graphs, compute where an approval request goes: the nearest level of the `OWNERS` chain that
   lists any approver. The area is *covered* if someone there was active in the last year, *stuck* if all
   of them were silent.
4. Diff, then `GRAPH.DELETE` the copy. The live graph is never written; a test checks it.

**Working groups** (`groups.py`, `working_group`). Two directories are linked when the same people work in
both; each keeps its 4 strongest links (cosine of the shared people). People active in more than 40 areas are
left out, or everything joins one blob. Then `CALL algo.labelPropagation(...)` finds communities.

## 7. How we know it works (and what went wrong)

**Reviewer evaluation** (`eval_reviewers.py`). 400 PRs merged between 2026-07-16 and 2026-10-16, sampled with
a fixed seed (7) from the 556 that had an approver and touched at most 15 directories. For each PR, Drift
only sees activity merged before that PR, with the author excluded. Did its top 3 include someone who
actually typed `/approve`?

| | top 1 | top 3 |
|---|---|---|
| Drift | 53% | 78% (CI 73–81%) |
| 3 people picked from the OWNERS files | 13% | 37% |
| The 3 busiest approvers overall | n/a | 46% |

OWNERS files don't rank people, so their figure is the exact probability of a random pick from the nearest
`OWNERS` level, not a simulation.

**The bug we found.** The first run said 86%. A leak check showed future PRs in the "past" window. Cause: in
FalkorDB (4.18.3 and 4.22.1), a range filter on an *indexed string* property ignores the upper bound
(`p.s < X` returns every row). Integer ranges are correct. Fix: store dates as integers (`PR.day`) and filter
on those. `scripts/repro_falkordb_range.py` reproduces it in 20 lines; `tests/test_eval_no_leak.py` makes
sure the leak can't come back.

**Backtest** (`backtest.py`). On the year-old graph: 49 of 152 approvers had been silent for a year, and 44
of them were still listed a year later. Predicting who gets removed only beats chance by 1.31× (records are
rarely cleaned up, which is the problem). The 30 busiest unlisted reviewers became owners at 5.3× the base rate.

**Working groups.** Built from activity before July, they put 47% of the area pairs changed together in
later PRs in the same group, against 15% for random groups of the same sizes (the best of 200 shuffles: 19%).

**A claim we dropped.** We first claimed PRs that cross groups need more approvers (3×). On a second
download of the data it showed no difference, so we removed it.

**Patch check.** The 40 drafted fixes, applied to a copy, unstick 12 areas and make none stuck (19 stuck
areas before, 7 after).

## 8. Decisions and trade-offs

- **Real data over synthetic.** Every number can be checked against public Kubernetes repositories. The
  cost: no Slack, tickets or docs, which the Company Brain track mentions.
- **Fixed tools over model-written Cypher.** Safer and checkable; the model can't invent a query or a name.
  The cost: it only answers the question shapes we built.
- **Integer dates.** Forced by the FalkorDB bug; also faster.
- **"Silent", never "gone".** Activity outside kubernetes/kubernetes isn't counted, so we say "silent here".
- **Drift never edits Kubernetes.** Every change is a draft for a maintainer.
- **The evaluation compares against the status quo** (what OWNERS would suggest), not against another AI
  method, because that is the decision a contributor actually faces.

## 9. Honest limits

- One repository; merged PRs only (reviews on unmerged PRs are not counted).
- `no_parent_owners` is applied in ownership routing and `who_owns`, not in the activity queries.
- Working-group boundaries move a little with the data (label propagation is not deterministic across data
  versions); the 47% vs 15% result held on both downloads.
- The free hosting sleeps after 15 quiet minutes; the first visit then takes about a minute.

## 10. Questions a judge may ask

**Why FalkorDB and not Postgres?** Ownership inheritance is a variable-length walk (`[:INSIDE*0..10]`), every
answer is a multi-hop path we show to the user, the what-if needs a whole-graph copy (`GRAPH.COPY`), community
detection is built in (`algo.labelPropagation`), and two graphs (today and a year ago) live in one instance.

**What happens if you take FalkorDB out?** Nothing works: every answer, the evaluation, the drafted fixes and
the what-if are graph queries. The site reports that it can't reach the graph.

**Does the model write queries?** No. It picks one of nine tools and fills in arguments. Names are resolved
against the graph first, and its sentence is checked against the result.

**How do you stop it inventing people?** `render.check`: any @handle, directory, KEP number or number in the
sentence that isn't in the tool's result gets the sentence rejected, and a template is used instead.

**How is the 78% measured, and why trust it?** Section 7. Time-split (only earlier data), author excluded,
fixed seed, a confidence interval, a leak test, and the bug that inflated it is documented.

**What is the graph algorithm doing?** Label propagation over the who-works-where projection finds groups of
directories that really work as one team, which often cross SIG labels. It is checked against later PRs.

**What does `GRAPH.COPY` buy you?** A what-if on the whole graph without touching it: edit a copy, re-run the
same routing query, compare, delete. About 0.4 s on this graph.

**What is memory for?** Remembered answers link to the people they named. When asked again, Drift says what
changed since. The web app, CLI and MCP share it.

**What did you build vs the AI assistant?** See the README's AI disclosure.

**What would you do next?** CODEOWNERS support for any GitHub repo, a GitHub Action that comments suggested
reviewers on new PRs, and counting reviews on unmerged PRs.
