"""The reviewer evaluation is only honest if a prediction never uses activity from on/after the PR."""
import pytest

from orggraph.tools import Tools


@pytest.fixture(scope="module")
def t():
    try:
        tools = Tools()
        if not tools.q("built", "MATCH (p:PR) RETURN count(p) > 0")[0][0]:
            raise RuntimeError("no activity loaded")
    except Exception as e:  # pragma: no cover
        pytest.skip(f"graph not available: {e}")
    return tools


def test_until_cutoff_is_respected(t):
    for until in ("2026-07-20", "2026-08-27", "2026-09-17"):
        a = t.suggest_reviewers(paths="pkg/kubelet pkg/volume staging/src/k8s.io/client-go", until=until, with_owners=False)
        assert a.outcome == "answered_with_path"
        assert all(p["last_active"] < until for p in a.data["people"]), until


def test_author_is_excluded(t):
    a = t.suggest_reviewers(paths="staging/src/k8s.io/client-go", exclude=["liggitt"], with_owners=False)
    assert "liggitt" not in [p["handle"] for p in a.data["people"]]
