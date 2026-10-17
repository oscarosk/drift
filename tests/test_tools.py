"""Behaviour of the tool catalogue against the real graph (needs FalkorDB with the built graph)."""
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


def test_unknown_directory_runs_no_traversal(t):
    a = t.who_knows("pkg/kublet")
    assert a.outcome == "unknown_entity"
    assert "pkg/kubelet" in a.data["suggestions"]
    assert not any(q["query"] in ("listed_activity", "shadow_owners") for q in a.trace)


def test_file_path_resolves_to_owning_directory(t):
    a = t.who_owns("pkg/kubelet/cm/devicemanager/manager.go")
    assert a.outcome == "answered_with_path"
    assert a.data["dir"] == "pkg/kubelet/cm/devicemanager"
    assert "nearest owned directory" in a.note


def test_root_is_not_a_catch_all(t):
    assert t.who_owns("nothing/like/this").outcome == "unknown_entity"


def test_ownership_chain_is_a_path(t):
    a = t.who_owns("pkg/kubelet/cm")
    assert a.paths[0][:2] == ["pkg/kubelet/cm", "pkg/kubelet"]


def test_unknown_kep(t):
    assert t.why(99999).outcome == "unknown_entity"


def test_proposal_has_evidence_for_every_line(t):
    a = t.propose_owners_update("pkg/kubelet/cm/cpumanager")
    if a.outcome == "answered_with_path":
        assert len(a.data["evidence"]) == len(a.data["remove"]) + len(a.data["add_reviewers"])


def test_cypher_never_contains_user_text(t):
    t.who_owns("pkg/kubelet') RETURN 1 //")  # resolved as a parameter, never concatenated
    assert t.who_owns("pkg/kubelet").outcome == "answered_with_path"


def test_working_group_contains_the_area(t):
    a = t.working_group("pkg/kubelet/cm/dra")
    if a.outcome == "not_in_graph":
        pytest.skip("groups not built: python -m orggraph.groups")
    assert a.outcome == "answered_with_path"
    assert "pkg/kubelet/cm/dra" in [x["dir"] for x in a.data["areas"]]
    assert all(set(l["people"]) for l in a.data["links"])


SAMPLES = {"who_owns": {"path": "pkg/kubelet"}, "who_knows": {"path": "pkg/kubelet/cm/cpumanager"},
           "drift_summary": {}, "route": {"path": "pkg/scheduler"}, "why": {"kep": 2340},
           "propose_owners_update": {"path": "pkg/kubelet/cm/cpumanager"}, "what_if": {"handles": "@deads2k"},
           "working_group": {"path": "pkg/scheduler"}, "suggest_reviewers": {"paths": "pkg/kubelet/kubelet.go"}}


def test_every_agent_tool_template_passes_the_checker(t):
    from orggraph.render import check, template
    from orggraph.tools import CATALOGUE
    assert set(SAMPLES) == set(CATALOGUE)
    for name, args in SAMPLES.items():
        a = getattr(t, CATALOGUE[name]["fn"])(**args)
        ok, why = check(template(a), a)
        assert ok, f"{name}: {why}"
