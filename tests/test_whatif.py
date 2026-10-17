"""What-if runs on a copy of the graph. These tests pin the routing rules on a tiny hand-built graph,
and check on the real graph that a copy is always deleted and the live graph never changes."""
import uuid

import pytest

from orggraph.db import graph
from orggraph.tools import Tools
from orggraph.whatif import coverage


@pytest.fixture
def tiny():
    """root (approvers: carol, active) <- a (approvers: alice active, bob silent) <- a/b (none)
       root <- c (approvers: dave silent) ; root <- s (no_parent_owners, approvers: erin silent)"""
    name = f"test_whatif_{uuid.uuid4().hex[:8]}"
    try:
        g = graph(name)
        g.query("""
            CREATE (r:Dir {repo:'kubernetes', path:'.', has_owners:true}),
                   (a:Dir {repo:'kubernetes', path:'a', has_owners:true}),
                   (ab:Dir {repo:'kubernetes', path:'a/b', has_owners:true}),
                   (c:Dir {repo:'kubernetes', path:'c', has_owners:true}),
                   (s:Dir {repo:'kubernetes', path:'s', has_owners:true, no_parent_owners:true}),
                   (a)-[:INSIDE]->(r), (ab)-[:INSIDE]->(a), (c)-[:INSIDE]->(r), (s)-[:INSIDE]->(r),
                   (carol:Person {handle:'carol'})-[:APPROVER_OF {via:'direct'}]->(r),
                   (alice:Person {handle:'alice'})-[:APPROVER_OF {via:'direct'}]->(a),
                   (bob:Person {handle:'bob'})-[:APPROVER_OF {via:'direct'}]->(a),
                   (dave:Person {handle:'dave'})-[:APPROVER_OF {via:'direct'}]->(c),
                   (erin:Person {handle:'erin'})-[:APPROVER_OF {via:'direct'}]->(s),
                   (pr:PR {number:1, merged:'2026-10-01', day:20261001}),
                   (alice)-[:REVIEWED]->(pr), (carol)-[:APPROVED_PR]->(pr)""")
    except Exception as e:  # pragma: no cover
        pytest.skip(f"FalkorDB not available: {e}")
    yield name, Tools(name)
    graph(name).delete()


def test_routing_follows_inheritance_and_marks_silent_levels_stuck(tiny):
    _, t = tiny
    cov = coverage(t, 20251016)
    assert cov["a"]["state"] == "covered" and cov["a"]["active"] == ["alice"]
    assert cov["a/b"]["routed_to"] == "a"                      # no approvers of its own: inherits
    assert cov["c"]["state"] == "stuck"                        # dave is listed, and silent
    assert cov["s"]["state"] == "stuck" and cov["s"]["routed_to"] == "s"


def test_no_parent_owners_stops_the_walk(tiny):
    name, t = tiny
    graph(name).query("MATCH (:Person {handle:'erin'})-[r:APPROVER_OF]->() DELETE r")
    cov = coverage(t, 20251016)
    assert cov["s"]["state"] == "none"                         # does not fall through to root's carol


def test_removing_a_silent_approver_unsticks_the_area(tiny):
    name, t = tiny
    graph(name).query("MATCH (:Person {handle:'dave'})-[r:APPROVER_OF]->() DELETE r")
    cov = coverage(t, 20251016)
    assert cov["c"]["state"] == "covered" and cov["c"]["routed_to"] == "."


@pytest.fixture(scope="module")
def real():
    try:
        t = Tools()
        if not t.q("built", "MATCH (p:PR) RETURN count(p) > 0")[0][0]:
            raise RuntimeError("no activity loaded")
    except Exception as e:  # pragma: no cover
        pytest.skip(f"graph not available: {e}")
    return t


def _approver_edges(t):
    return t.g.ro_query("MATCH ()-[r:APPROVER_OF]->() RETURN count(r)").result_set[0][0]


def test_what_if_never_changes_the_live_graph_and_deletes_its_copy(real):
    before = _approver_edges(real)
    top = real.q("busiest", "MATCH (p:Person)-[:APPROVER_OF]->() RETURN p.handle, count(*) AS n ORDER BY n DESC LIMIT 1")[0][0]
    a = real.what_if([top])
    assert a.outcome == "answered_with_path"
    assert _approver_edges(real) == before
    assert any(x["query"].startswith("GRAPH.COPY") for x in a.trace)
    assert any(x["query"].startswith("GRAPH.DELETE") for x in a.trace)
    import redis
    from orggraph import db  # noqa: F401  (loads .env)
    import os
    r = redis.Redis(host=os.getenv("FALKORDB_HOST", "localhost"), port=int(os.getenv("FALKORDB_PORT", "6379")),
                    username=os.getenv("FALKORDB_USERNAME") or None, password=os.getenv("FALKORDB_PASSWORD") or None)
    assert not [k for k in r.keys("whatif_*")]


def test_unknown_people_run_no_copy(real):
    a = real.what_if(["@definitely-not-a-real-handle-zz"])
    assert a.outcome == "unknown_entity"
    assert not any("GRAPH.COPY" in x["query"] for x in a.trace)


def test_drafted_fixes_never_leave_an_area_stuck(real):
    dirs = [x["dir"] for x in real.cleanup_queue(limit=40).data["queue"]]
    real._trace = []
    a = real.check_fix(dirs)
    assert a.outcome == "answered_with_path"
    assert a.data["stuck"] == [] and a.data["safe"]
    assert a.data["stuck_after"] <= a.data["stuck_before"]
