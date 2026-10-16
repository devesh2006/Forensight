import os, tempfile
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/t.db"
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
def test_pipeline():
    r = c.post("/api/demo/load").json(); assert r["evidence"] > 250
    st = c.get("/api/stats").json(); assert st["by_type"]["message"] >= 150 and st["by_type"]["call"] >= 65 and st["by_type"]["location"] >= 100
    tl = c.get("/api/timeline", params={"incident_window_min": 60}).json(); assert tl["count"] > 0
    assert all("22:" in e["time"] or "21:" in e["time"] or "23:" in e["time"] for e in tl["events"])
    hit = c.get("/api/search", params={"q": "delete this chat"}).json(); assert hit["count"] == 1
    eid = hit["results"][0]["id"]; v = c.get(f"/api/evidence/{eid}/verify").json(); assert v["verified"]
    assert c.get("/api/evidence/NOPE/verify").status_code == 404
