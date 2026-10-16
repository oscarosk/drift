"""
Builds the git-side activity data Drift needs (works on Windows, macOS, Linux):

  data/pr_files.jsonl   one line per merged PR: number, merged date, author, files touched
  data/k8s_2025/        every OWNERS file as it was on 2025-10-16 (for the backtest)

Only commit and tree metadata is downloaded (no file contents), so this stays small.
Run after scripts/fetch_data.py:   python scripts/fetch_history.py
"""
import json
import os
import re
import subprocess

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
K8S = os.path.join(DATA, "k8s")
SINCE = "2024-10-01"          # a little before the backtest window starts
SNAPSHOT_DATE = "2025-10-16"  # "one year ago": the org as the backtest sees it
MERGE = re.compile(r"Merge pull request #(\d+) from ([^/\s]+)/")


def git(*args, cwd=K8S, capture=False):
    print("+ git", " ".join(args))
    r = subprocess.run(["git", *args], cwd=cwd, check=True, text=True, encoding="utf-8",
                       stdout=subprocess.PIPE if capture else None)
    return r.stdout


def main():
    # 1. Deepen the shallow clone back to SINCE (metadata only).
    git("fetch", "--filter=blob:none", f"--shallow-since={SINCE}", "origin", "master")
    tip = git("rev-parse", "FETCH_HEAD", capture=True).strip()

    # 2. Every merged PR with the files it changed. Kubernetes merges every PR with a
    #    "Merge pull request #N from user/branch" commit, so diffing each merge against its
    #    first parent gives exactly the PR's files.
    log = git("log", tip, "--merges", "--diff-merges=first-parent", "--name-only",
              f"--since={SINCE}", "--format=@@%cd|%s", "--date=short", capture=True)
    out, cur = [], None
    for line in log.splitlines():
        if line.startswith("@@"):
            date, subject = line[2:].split("|", 1)
            m = MERGE.search(subject)
            cur = {"number": int(m.group(1)), "merged": date, "author": m.group(2).lower(),
                   "files": []} if m else None
            if cur:
                out.append(cur)
        elif line and cur is not None:
            cur["files"].append(line)
    # A merge sitting on the shallow boundary diffs against an empty tree and lists every
    # file in the repo; those are not real PR contents, so drop them.
    out = [p for p in out if len(p["files"]) < 3000]
    with open(os.path.join(DATA, "pr_files.jsonl"), "w", encoding="utf-8") as f:
        for p in out:
            f.write(json.dumps(p) + "\n")
    print(f"pr_files.jsonl: {len(out)} merged PRs")

    # 3. OWNERS snapshot as of SNAPSHOT_DATE: the last merge on master before that date.
    shas = git("log", tip, "--merges", "--first-parent", f"--until={SNAPSHOT_DATE}T23:59:59",
               "-1", "--format=%H", capture=True).split()
    if not shas:  # first-parent chain can hit a shallow graft; fall back to any merge
        shas = git("log", tip, "--merges", f"--until={SNAPSHOT_DATE}T23:59:59", "-1",
                   "--format=%H", capture=True).split()
    snap = os.path.join(DATA, "k8s_2025")
    if not os.path.isdir(snap):
        git("worktree", "add", "--no-checkout", snap, shas[0])
        git("sparse-checkout", "init", "--no-cone", cwd=snap)
        git("sparse-checkout", "set", "--no-cone", "/OWNERS_ALIASES", "OWNERS", cwd=snap)
        git("checkout", shas[0], cwd=snap)
    print(f"OWNERS snapshot {SNAPSHOT_DATE}: commit {shas[0][:10]}")


if __name__ == "__main__":
    main()
