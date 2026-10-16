# ForenSight AI (prototype) — PROJECT NIGHTFALL
Digital-forensics decision-support prototype. Uses synthetic, fictional data only. Integrity checks are a prototype workflow, not a legal admissibility claim.

## Run
Windows PowerShell (run from the repository root):

    py -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -r backend\requirements.txt
    cd backend
    uvicorn app.main:app --reload

Open http://localhost:8000 for the interface. The same FastAPI server serves the UI and API; there is no separate frontend server.

Use **Load case data** in the interface to generate and ingest PROJECT NIGHTFALL. The SQLite database is created in the backend working directory.

Tests (from `backend`): `pytest`

## Endpoints
POST /api/demo/load · GET /api/stats · /api/timeline · /api/search · /api/evidence · /api/evidence/{id} · /api/evidence/{id}/verify
GET /api/graph · /api/graph/strongest · /api/correlations · /api/anomalies · /api/relevance
GET /api/map/geofence · /api/map/path · POST /api/copilot

## Not built yet
Not included: auth/RBAC, computer vision (YOLO/OCR/faces/video), report export, vector retrieval. The map is a local GPS coordinate plot, not a tiled basemap.
