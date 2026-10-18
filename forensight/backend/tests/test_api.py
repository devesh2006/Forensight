import os
os.environ["DATABASE_URL"] = "sqlite:///forensight-test.db"
from fastapi.testclient import TestClient
from app.main import app
import pytest

@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client

def test_api_endpoints_and_empty_results(client):
    assert client.get("/api/health").json()["status"] == "ok"
    stats = client.get("/api/stats").json()
    assert stats["total"] > 250 and stats["contacts"] > 0
    page = client.get("/api/evidence", params={"offset": 0, "limit": 10})
    assert page.status_code == 200 and len(page.json()["results"]) == 10
    eid = page.json()["results"][0]["id"]
    assert client.get(f"/api/evidence/{eid}").json()["id"] == eid
    assert client.get("/api/search", params={"q": "delete this chat"}).json()["count"] == 1
    assert client.get("/api/search", params={"q": "no-such-artifact-term"}).json()["results"] == []
    assert client.get("/api/timeline", params={"limit": 5}).json()["count"] == 5
    assert client.get("/api/graph").json()["edges"]
    assert client.get("/api/connections").json()["results"]
    assert isinstance(client.get("/api/correlations").json(), list)
    assert isinstance(client.get("/api/anomalies").json(), list)
    answer = client.post("/api/copilot", json={"query": "What happened between 9 PM and 11 PM?", "context": {"case": "NIGHTFALL"}})
    assert answer.status_code == 200 and answer.json()["evidence"]

def test_api_validation_and_missing_ids(client):
    assert client.get("/api/search", params={"q": "x"}).status_code == 422
    assert client.get("/api/evidence/NO-SUCH-ID").status_code == 404
    assert client.get("/api/evidence", params={"offset": -1}).status_code == 422
    assert client.get("/api/evidence", params={"type": "made-up"}).status_code == 422
    assert client.get("/api/timeline", params={"start": "2026-10-16T00:00:00", "end": "2026-10-15T00:00:00"}).status_code == 422
    assert client.post("/api/copilot", json={"query": "x"}).status_code == 422

def test_demo_loader_does_not_duplicate_evidence(client):
    before = client.get("/api/stats").json()["total"]
    loaded = client.post("/api/demo/load").json()
    assert loaded["evidence"] == before
    assert client.get("/api/stats").json()["total"] == before

def test_report_upload_is_additive_and_deduplicated(client):
    payload = {"filename": "messages.csv", "content": "timestamp,sender,receiver,text,app\n2026-10-18T12:00:00,+10000000001,+10000000002,Uploaded test record,SMS\n"}
    first = client.post("/api/reports/upload", json=payload)
    assert first.status_code == 200 and first.json()["added"] == 1
    second = client.post("/api/reports/upload", json=payload)
    assert second.status_code == 200 and second.json()["added"] == 0 and second.json()["skipped"] == 1
    assert client.post("/api/reports/upload", json={"filename": "report.pdf", "content": "not supported"}).status_code == 422

