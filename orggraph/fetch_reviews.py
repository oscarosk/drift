"""
Fetches who REVIEWED and APPROVED each merged Kubernetes PR, from the public GitHub GraphQL API.

Why: approvers mostly review rather than write code, so git history alone can't tell an
active approver from one who has quietly left. This adds the missing signal:
  - GitHub reviews (APPROVED / CHANGES_REQUESTED / COMMENTED) with their authors
  - Prow commands in comments: "/lgtm" and "/approve" lines, with who typed them

Files touched by each PR are NOT fetched here: we already get them from git history.

Usage (needs GITHUB_TOKEN in .env; a token with no extra scopes is enough):
    python -m orggraph.fetch_reviews --since 2024-10-16
Output: data/reviews.jsonl, one JSON object per merged PR. Safe to re-run: months already
fetched are skipped, so an interrupted run resumes where it stopped.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time

import requests

import orggraph.db  # noqa: F401  (loads .env)

API = "https://api.github.com/graphql"
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "reviews.jsonl")
DONE = OUT + ".done"  # list of finished day-windows, for resuming

QUERY = """
query($q: String!, $after: String) {
  rateLimit { remaining resetAt cost }
  search(query: $q, type: ISSUE, first: 50, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number mergedAt
        author { login }
        reviews(first: 50) { nodes { author { login } state submittedAt } }
        comments(last: 100) { nodes { author { login } createdAt body } }
      }
    }
  }
}"""

# Prow commands must start a line: "/lgtm", "/approve" (but not "/lgtm cancel", "/approve no-issue" is fine)
CMD = re.compile(r"^/(lgtm|approve)\b(?!\s+cancel)", re.M)
BOTS = {"k8s-ci-robot", "k8s-triage-robot", "k8s-infra-cherrypick-robot", "dependabot"}


def gql(token: str, variables: dict) -> dict:
    for attempt in range(6):
        r = requests.post(API, json={"query": QUERY, "variables": variables},
                          headers={"Authorization": f"bearer {token}"}, timeout=60)
        if r.status_code == 200 and "errors" not in r.json():
            data = r.json()["data"]
            rl = data["rateLimit"]
            if rl["remaining"] < 50:  # be polite: wait for the hourly reset
                wait = (dt.datetime.fromisoformat(rl["resetAt"].replace("Z", "+00:00"))
                        - dt.datetime.now(dt.timezone.utc)).total_seconds() + 5
                print(f"  rate limit low, sleeping {wait:.0f}s"); time.sleep(max(wait, 0))
            return data
        print(f"  attempt {attempt + 1} failed ({r.status_code}): {r.text[:200]}")
        time.sleep(10 * (attempt + 1))
    sys.exit("GitHub API kept failing; re-run later, finished windows are kept.")


def slim(pr: dict) -> dict:
    """Keep only what the graph needs: handles, states, dates. No comment text is stored."""
    commands = []
    for c in pr["comments"]["nodes"]:
        who = (c.get("author") or {}).get("login", "")
        if who and who.lower() not in BOTS:
            for cmd in CMD.findall(c["body"] or ""):
                commands.append({"by": who.lower(), "cmd": cmd, "at": c["createdAt"]})
    reviews = [{"by": r["author"]["login"].lower(), "state": r["state"], "at": r["submittedAt"]}
               for r in pr["reviews"]["nodes"] if r.get("author")
               and r["author"]["login"].lower() not in BOTS]
    return {"number": pr["number"], "merged_at": pr["mergedAt"],
            "author": ((pr.get("author") or {}).get("login") or "").lower(),
            "reviews": reviews, "commands": commands}


def windows(since: dt.date, until: dt.date, days: int):
    d = since
    while d <= until:
        e = min(d + dt.timedelta(days=days - 1), until)
        yield d, e
        d = e + dt.timedelta(days=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2024-10-16")
    ap.add_argument("--until", default=dt.date.today().isoformat())
    ap.add_argument("--repo", default="kubernetes/kubernetes")
    a = ap.parse_args()
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        sys.exit("Set GITHUB_TOKEN in .env first.")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    done = set(open(DONE, encoding="utf-8").read().split()) if os.path.exists(DONE) else set()

    # Search returns at most 1000 results per query, so query in 7-day windows (~60-120 PRs each).
    total = 0
    for s, e in windows(dt.date.fromisoformat(a.since), dt.date.fromisoformat(a.until), 7):
        key = f"{s}..{e}"
        if key in done:
            continue
        q = f"repo:{a.repo} is:pr is:merged merged:{key}"
        after, rows = None, []
        while True:
            data = gql(token, {"q": q, "after": after})
            res = data["search"]
            rows += [slim(n) for n in res["nodes"] if n]
            if not res["pageInfo"]["hasNextPage"]:
                break
            after = res["pageInfo"]["endCursor"]
        if res["issueCount"] > 1000:
            print(f"  warning: {key} has {res['issueCount']} PRs (>1000), some missed")
        with open(OUT, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        with open(DONE, "a", encoding="utf-8") as f:
            f.write(key + "\n")
        total += len(rows)
        print(f"{key}: {len(rows)} PRs (total {total}, API points left {data['rateLimit']['remaining']})")
    print(f"done -> {OUT}")


if __name__ == "__main__":
    main()
