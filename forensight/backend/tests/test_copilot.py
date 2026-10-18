import os
os.environ["DATABASE_URL"] = "sqlite:///forensight-test.db"; os.environ.pop("ANTHROPIC_API_KEY", None)
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app); c.post("/api/demo/load")
ask = lambda q: c.post("/api/copilot", json={"query": q}).json()
def test_timeline_window():
    r = ask("What happened between 9 PM and 11 PM?"); assert r["intent"] == "timeline" and r["evidence"] and r["confidence"] > 0.5
    assert all("T21" <= e["time"][10:13] or e["time"][11:13] in ("21", "22") for e in r["evidence"])
def test_communication_two_people():
    r = ask("Show all communication between Arjun and Bhavna"); assert r["evidence"]
    assert all({e["actor"], e["target"]} == {"Arjun Mehra", "Bhavna Rao"} for e in r["evidence"])
def test_location_before_incident():
    r = ask("Where was Chirag before the incident?"); assert "Old Fort" in r["answer"] or "Connaught" in r["answer"]
    assert all(e["time"] < "2026-10-15T22:30" for e in r["evidence"])
def test_correlation_person_place():
    r = ask("Find evidence connecting Arjun to Old Fort"); assert r["evidence"] and r["confidence"] > 0.5
def test_anomaly_before_incident_and_media_honesty():
    assert ask("What unusual activity occurred before the incident?")["intent"] == "anomaly"
    assert "No image or video evidence" in ask("Show images containing vehicles")["answer"]
def test_no_invention_and_validation():
    r = ask("Find evidence mentioning submarine"); assert r["evidence"] == [] and r["confidence"] == 0
    assert c.post("/api/copilot", json={"query": "x"}).status_code == 422
def test_summary_has_citations():
    r = ask("Give me a concise evidence-based summary of the case"); assert r["evidence"] and r["source_artifacts"]
def test_place_query_finds_alias_and_gps():
    r = ask("Find evidence mentioning Connaught Place"); types = {e["type"] for e in r["evidence"]}
    assert "message" in types and "location" in types
    assert any("cp" in (e["summary"] or "").lower() for e in r["evidence"] if e["type"] == "message")
def test_anomaly_answers_cite_evidence():
    r = ask("What unusual activity occurred before the incident?"); assert r["evidence"] and "communication burst:" in r["answer"]
