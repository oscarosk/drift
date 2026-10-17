"""Memory says what changed, not just what was said."""
import uuid

import pytest

from orggraph.db import graph
from orggraph.memory import Memory
from orggraph.tools import Answer


def test_changes_reports_added_and_dropped_people():
    m = Memory.changes([{"text": "ask @alice and @bob", "people": ["alice", "bob"]}], "ask @bob and @carol")
    assert m[0]["added"] == ["carol"] and m[0]["dropped"] == ["alice"] and not m[0]["same_as_now"]


def test_same_answer_is_flagged_as_unchanged():
    m = Memory.changes([{"text": "ask @alice", "people": ["alice"]}], "ask @alice")
    assert m[0]["same_as_now"] and not m[0]["added"] and not m[0]["dropped"]


@pytest.fixture
def mem():
    name = f"test_mem_{uuid.uuid4().hex[:8]}"
    try:
        graph(name).query("CREATE (:Dir {repo:'kubernetes', path:'pkg/x'}), (:Person {handle:'alice'}), (:Person {handle:'bob'})")
    except Exception as e:  # pragma: no cover
        pytest.skip(f"FalkorDB not available: {e}")
    yield name, Memory(name)
    graph(name).delete()


def test_findings_cite_the_people_they_name_and_skip_repeats(mem):
    name, m = mem
    a = Answer("answered_with_path", "who_knows", {"path": "pkg/x"}, data={"dir": "pkg/x"})
    assert m.remember(a, "pkg/x: ask @alice and @bob", "s1") == 1
    assert m.remember(a, "pkg/x: ask @alice and @bob", "s2") == 0          # exact repeat is not stored twice
    cited = graph(name).ro_query("MATCH (:Finding)-[:CITES]->(p:Person) RETURN p.handle ORDER BY p.handle").result_set
    assert [r[0] for r in cited] == ["alice", "bob"]
    got = m.recall(path="pkg/x")
    assert got[0]["people"] == ["alice", "bob"] and got[0]["session"] == "s1"
