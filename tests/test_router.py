"""The keyword router is what answers when no model is configured (the public demo's fallback)."""
import pytest

from orggraph.agent import route_offline


@pytest.mark.parametrize("question, tool", [
    ("What if @deads2k stepped back?", "what_if"),
    ("what happens if @klueska leaves", "what_if"),
    ("Which areas work with pkg/kubelet/cm/dra?", "working_group"),
    ("Who really works on pkg/kubelet/cm/cpumanager?", "who_knows"),
    ("Who owns pkg/kubelet?", "who_owns"),
    ("Who should review pkg/kubelet/kubelet.go and pkg/kubelet/cm/cpumanager/policy.go?", "suggest_reviewers"),
    ("Who should I send a bug in pkg/scheduler to?", "route"),
    ("Draft a fix for pkg/kubelet/cm/cpumanager", "propose_owners_update"),
    ("Why was KEP 2340 decided?", "why"),
    ("How stale is ownership across Kubernetes?", "drift_summary"),
])
def test_routes(question, tool):
    assert route_offline(question)[0] == tool


def test_what_if_extracts_every_handle():
    _, args = route_offline("what if @liggitt and @dims both stepped back")
    assert args["handles"] == "@liggitt @dims"


def test_unrelated_question_is_declined():
    assert route_offline("write me a poem about the sea")[0] is None
