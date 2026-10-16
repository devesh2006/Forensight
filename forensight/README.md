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

## Deploy the UI to Vercel

Vercel hosts the static UI only. Keep the Python API on a normal web service; its database must be external PostgreSQL because SQLite files and generated files are not durable across serverless invocations.

1. Create a PostgreSQL database (for example, Neon) and copy its connection URL. It must include SSL options if the provider requires them.
2. In Render, create a Blueprint from this repository using `render.yaml`. Set `DATABASE_URL` to the database URL and `CORS_ORIGINS` to the exact Vercel site origin, such as `https://your-project.vercel.app` (no trailing slash). The service uses `/tmp` only for transient synthetic input files; evidence rows live in PostgreSQL.
3. In Vercel, set **Root Directory** to the repository root, **Build Command** to `npm run build`, and **Output Directory** to `dist`. Add `VITE_API_BASE_URL` with the Render service URL, such as `https://forensight-nightfall-api.onrender.com` (no trailing slash), for Production and Preview environments as needed.
4. Redeploy the Vercel project and the Render service. On the first visit, choose **Load case data** to generate the synthetic case and store its normalized records in PostgreSQL.

No API URL or database secret is hard-coded in the checked-in source. `VITE_API_BASE_URL` is a public URL embedded during the build; keep `DATABASE_URL` only in Render's environment settings. The free Render service may sleep while idle; its first request can take longer to wake. Use the Vercel site's exact origin in `CORS_ORIGINS`; for preview deployments, include their origins too.

To build the production UI locally, run `node scripts/build.mjs` from the repository root. Set `VITE_API_BASE_URL` first if previewing that build against a separately running API.

## Endpoints
POST /api/demo/load · GET /api/stats · /api/timeline · /api/search · /api/evidence · /api/evidence/{id} · /api/evidence/{id}/verify
GET /api/graph · /api/graph/strongest · /api/correlations · /api/anomalies · /api/relevance
GET /api/map/geofence · /api/map/path · POST /api/copilot

## Not built yet
Not included: auth/RBAC, computer vision (YOLO/OCR/faces/video), report export, vector retrieval. The map is a GPS coordinate plot, not a tiled basemap.
