"""The sentence checker is the last line of defence against invented names. No database needed."""
from orggraph.render import check, template
from orggraph.tools import Answer


def knows_answer():
    return Answer("answered_with_path", "who_knows", {"path": "pkg/kubelet/cm/cpumanager"}, data={
        "dir": "pkg/kubelet/cm/cpumanager", "since": "2025-10-16", "as_of": "2026-10-16",
        "approvers": [{"handle": "alice", "via": "direct", "prs_here": 0, "prs_anywhere": 0, "status": "silent"}],
        "shadow_owners": [{"handle": "bob", "prs_here": 22, "how": ["REVIEWED"], "example_prs": [1], "listed_below": []}]})


def test_accepts_sentence_built_from_result():
    a = knows_answer()
    assert check("@alice is listed but silent; @bob reviewed 22 PRs in pkg/kubelet/cm/cpumanager.", a) == (True, "ok")


def test_rejects_invented_person():
    ok, why = check("@alice is silent; @mallory took over.", knows_answer())
    assert not ok and "mallory" in why


def test_rejects_invented_directory():
    ok, why = check("See also pkg/kubelet/cm/memorymanager.", knows_answer())
    assert not ok and "memorymanager" in why


def test_rejects_invented_kep():
    ok, why = check("This follows KEP-1234.", knows_answer())
    assert not ok and "1234" in why


def test_empty_result_must_say_so():
    a = Answer("not_in_graph", "route", {"path": "x"}, note="nothing")
    assert not check("Send it to the usual reviewers.", a)[0]
    assert check("The graph has no review activity there.", a)[0]


def test_template_always_passes_its_own_check():
    a = knows_answer()
    assert check(template(a), a)[0]


def test_what_if_template_names_only_people_in_the_result():
    a = Answer("answered_with_path", "what_if", {"handles": ["alice"]}, data={
        "people": ["alice"], "stuck": [{"dir": "pkg/q", "prs": 5, "could_cover": [{"handle": "bob", "prs": 3}],
                                        "before": {}, "after": {}}], "fixed": [], "moved": []})
    text = template(a)
    assert "@alice" in text and "@bob" in text and "pkg/q" in text
    assert check(text, a)[0]
    assert not check(text + " Ask @mallory.", a)[0]


def test_check_fix_template_states_before_and_after():
    a = Answer("answered_with_path", "check_fix", {"dirs": ["a"]}, data={
        "changes": 3, "fixed": [{"dir": "a"}], "stuck": [], "moved": [], "stuck_before": 5, "stuck_after": 4, "safe": True})
    text = template(a)
    assert "5 before, 4 after" in text and check(text, a)[0]
