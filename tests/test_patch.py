"""The patch generator must produce edits that say exactly what was intended, or nothing."""
import yaml

from orggraph.patch import edit_owners, owners_patch, verify

SAMPLE = """# See the OWNERS docs at https://go.k8s.io/owners

approvers:
  - alice   # original author
  - bob
reviewers:
  - carol
emeritus_approvers:
  - zed
labels:
  - sig/node
"""


def test_moves_approver_to_emeritus_and_keeps_comments():
    new, skipped = edit_owners(SAMPLE, ["bob"], ["dave"])
    d = yaml.safe_load(new)
    assert skipped == []
    assert d["approvers"] == ["alice"]
    assert d["emeritus_approvers"] == ["zed", "bob"]
    assert d["reviewers"] == ["carol", "dave"]
    assert d["labels"] == ["sig/node"]
    assert "# original author" in new and new.startswith("# See the OWNERS docs")
    assert verify(new, ["bob"], ["dave"]) is None


def test_creates_missing_sections():
    new, _ = edit_owners("approvers:\n- alice\n- bob\n", ["alice"], ["erin"])
    d = yaml.safe_load(new)
    assert d == {"approvers": ["bob"], "emeritus_approvers": ["alice"], "reviewers": ["erin"]}


def test_alias_listed_approver_is_skipped_not_guessed():
    new, skipped = edit_owners("approvers:\n  - sig-node-approvers\n", ["alice"], [])
    assert skipped and skipped[0][0] == "alice"
    assert yaml.safe_load(new) == {"approvers": ["sig-node-approvers"]}


def test_patch_is_a_unified_diff_with_git_paths():
    out = owners_patch([{"dir": "pkg/x", "owners_text": SAMPLE, "has_filters": False,
                         "remove": ["bob"], "add_reviewers": ["dave"], "evidence": []}])
    assert out["patch"].startswith("--- a/pkg/x/OWNERS\n+++ b/pkg/x/OWNERS\n")
    assert "-  - bob\n" in out["patch"] and "+  - dave\n" in out["patch"]
    assert out["applied"][0]["removed"] == ["bob"]


def test_filters_files_are_left_for_humans():
    out = owners_patch([{"dir": "pkg/y", "owners_text": "filters:\n  '.*':\n    approvers: [a]\n",
                         "has_filters": True, "remove": ["a"], "add_reviewers": []}])
    assert out["patch"] == "" and "filters" in out["skipped"][0]["reason"]
