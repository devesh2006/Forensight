import os
os.environ["DATABASE_URL"] = "sqlite:///forensight-test.db"
from fastapi.testclient import TestClient
from app.main import app
import pytest

@pytest.fixture(scope="module")
def client(tmp_path_factory):
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_path_factory.mktemp('api') / 'evidence.db'}"
    import app.main as main_module
    main_module._db = None
    with TestClient(app) as test_client:
        yield test_client

def test_api_starts_with_an_empty_evidence_workspace(client):
    assert client.get("/api/health").json()["status"] == "ok"
    stats = client.get("/api/stats").json()
    assert stats["total"] == 0 and stats["contacts"] == 0
    page = client.get("/api/evidence", params={"offset": 0, "limit": 10})
    assert page.status_code == 200 and len(page.json()["results"]) == 0
    assert client.get("/api/search", params={"q": "delete this chat"}).json()["count"] == 0
    assert client.get("/api/search", params={"q": "no-such-artifact-term"}).json()["results"] == []
    assert client.get("/api/timeline", params={"limit": 5}).json()["count"] == 0
    assert client.get("/api/graph").json()["edges"] == []
    assert client.get("/api/connections").json()["results"] == []
    assert client.get("/api/correlations").json() == []
    assert client.get("/api/anomalies").json() == []
    answer = client.post("/api/copilot", json={"query": "What happened between 9 PM and 11 PM?", "context": {"case": "TEST-CASE"}})
    assert answer.status_code == 200 and not answer.json()["evidence"]

def test_api_validation_and_missing_ids(client):
    assert client.get("/api/search", params={"q": "x"}).status_code == 422
    assert client.get("/api/evidence/NO-SUCH-ID").status_code == 404
    assert client.get("/api/evidence", params={"offset": -1}).status_code == 422
    assert client.get("/api/evidence", params={"type": "made-up"}).status_code == 422
    assert client.get("/api/timeline", params={"start": "2026-10-16T00:00:00", "end": "2026-10-15T00:00:00"}).status_code == 422
    assert client.post("/api/copilot", json={"query": "x"}).status_code == 422

def test_demo_loader_is_not_exposed(client):
    assert client.get("/api/demo/load").status_code == 404

def test_upload_uses_only_records_in_the_supplied_file(client):
    case = client.post("/api/cases", json={"name": "Synthetic API test case"})
    assert case.status_code == 200
    payload = "timestamp,sender,receiver,text,app\n2026-10-18T12:00:00,+10000000001,+10000000002,Uploaded test record,SMS\n"
    uploaded = client.post("/api/datasets/upload", params={"case_id": case.json()["case_id"]}, files={"file": ("messages.csv", payload, "text/csv")})
    assert uploaded.status_code == 200
    assert uploaded.json()["records_imported"] == 1
    result = client.get("/api/evidence", headers={"X-Case-ID": case.json()["case_id"]})
    assert result.json()["count"] == 1
    assert result.json()["results"][0]["summary"] == "Uploaded test record"

