import os
os.environ["DATABASE_URL"] = "sqlite:///forensight-test.db"
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app); c.post("/api/demo/load")
def test_graph_and_strongest():
    g = c.get("/api/graph").json(); assert len(g["nodes"]) >= 8 and g["edges"]
    top = c.get("/api/graph/strongest", params={"person": "Arjun Mehra"}).json(); assert top
def test_correlation_finds_old_fort_cluster():
    cl = c.get("/api/correlations").json()
    hit = [x for x in cl if x["start"].startswith("2026-10-15T22:") and len(x["people"]) >= 2]
    assert hit and hit[0]["confidence"] > 0.5 and hit[0]["evidence_ids"]
def test_anomalies_include_deletion_and_unresolved():
    kinds = {a["kind"] for a in c.get("/api/anomalies").json()}
    assert {"deletion_indicator", "unresolved_contact"} <= kinds
def test_relevance_and_geofence():
    r = c.get("/api/relevance", params={"involved": "Arjun Mehra"}).json(); assert r and r[0]["reasons"]
    g = c.get("/api/map/geofence", params={"lat": 28.6118, "lon": 77.2437, "radius_m": 300}).json(); assert g["count"] >= 3
    assert c.get("/api/map/geofence", params={"lat": 999, "lon": 0}).status_code == 422
