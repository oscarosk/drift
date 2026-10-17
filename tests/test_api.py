"""The HTTP API the web app and the live demo use."""
import pytest

try:
    from fastapi.testclient import TestClient
    from orggraph import api
    from orggraph.tools import Tools
    t = Tools()
    BUILT = bool(t.q("built", "MATCH (p:PR) RETURN count(p) > 0")[0][0])
except Exception:  # pragma: no cover
    BUILT = False

pytestmark = pytest.mark.skipif(not BUILT, reason="graph not available")


@pytest.fixture(scope="module")
def client():
    with TestClient(api.app) as c:
        yield c


def test_health_and_page(client):
    assert client.get("/api/health").json()["ok"]
    assert "Drift" in client.get("/").text


def test_reviewers_needs_input(client):
    assert client.post("/api/reviewers", json={}).status_code == 400


def test_reviewers_answers_with_evidence(client):
    r = client.post("/api/reviewers", json={"paths": "pkg/kubelet/kubelet.go"}).json()
    assert r["outcome"] == "answered_with_path"
    top = r["data"]["people"][0]
    assert top["example_prs"] and top["by_area"]


def test_whatif_endpoint(client):
    r = client.post("/api/whatif", json={"handles": "@no-such-person-zz"}).json()
    assert r["outcome"] == "unknown_entity"


def test_patch_for_unknown_dirs_is_refused(client):
    assert client.post("/api/patch", json={"dirs": ["no/such/dir"]}).status_code == 422


def test_rate_limit(client):
    api._hits.clear()
    codes = [client.post("/api/reviewers", json={}).status_code for _ in range(api.PER_MINUTE + 2)]
    assert 429 in codes
    api._hits.clear()
