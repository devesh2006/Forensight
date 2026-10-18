# FORENSIGHT AI · PROJECT NIGHTFALL

ForenSight is an evidence-analysis and correlation platform. It does not replace forensic acquisition tools. The existing static JavaScript frontend and FastAPI backend support case-scoped evidence ingestion, provenance, timelines, communications, geospatial analysis, correlations, anomaly indicators, and evidence-grounded investigative assistance. The bundled NIGHTFALL data is fictional and is used for development and regression checks.

## Local backend

From the repository root (the directory containing this README):

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
$env:DATABASE_URL = "sqlite:///forensight.db"
$env:NIGHTFALL_DATA_DIR = "data/nightfall"
$env:CORS_ORIGINS = "http://localhost:3000,http://localhost:5173,http://localhost:8000,http://127.0.0.1:8000"
python -m uvicorn app.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000
```

The API creates its tables at startup and imports the bundled demo data only when the NIGHTFALL case has no evidence. Repeated restarts do not duplicate or replace existing evidence. SQLite is the default if DATABASE_URL is omitted. To use PostgreSQL, set DATABASE_URL to a PostgreSQL connection URL; the psycopg v3 driver is included.

The header theme control switches between dark and light appearance and remembers the selection in the browser. The evidence import workflow supports CSV, JSON, XML, XLSX, TXT, HTML, PDF, and ZIP exports. ZIP contents are inspected with traversal, size, file-count, and nesting limits. Parsing preserves source records, timestamps, IDs, hashes, parser metadata, and import manifests. Unsupported formats and malformed records are reported in the manifest. Image-only PDF pages are marked `OCR required`; OCR is not implemented. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for architecture, data flow, and operational limits.

Operation Nightfall regression fixture imports 382 evidence records: 157 messages, 75 calls, 104 GPS fixes, 23 browser artifacts, and 23 application artifacts. `GET /api/cases/{case_id}/manifest` returns the import result, and case-scoped API routes and the case selector keep analysis separate by case.

Copy `.env.example` as a reference for configuration. The app does not load .env files automatically: export the variables in the shell or configure them in the hosting platform. Keep secrets in environment settings and never commit .env.

Health check: [http://127.0.0.1:8000/api/health](http://127.0.0.1:8000/api/health). Interactive API docs: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

Set `FORENSIGHT_MODE=offline` (the default) for local operation. No external map tiles or cloud service are required by the default analysis flow. An optional Anthropic key allows answer rephrasing; omit it when evidence must not leave the host. Imports currently run in FastAPI's in-process background task, so a process restart interrupts an active job. Local authentication/role management and OCR are not implemented. Proprietary forensic formats are not supported; export them to one of the supported formats. Original bytes from imports predating original-file retention cannot be reconstructed from their stored hash.

## Local frontend

To build the static frontend for Vercel or a static preview server:

```powershell
npm install
$env:VITE_API_URL = "http://127.0.0.1:8000"
npm run build
npx serve dist
```

Set VITE_API_URL to the backend origin only (no trailing slash and no `/api` suffix). The build writes it once to `dist/assets/config.js`; the frontend API client uses that shared setting. Leave it blank when FastAPI itself serves the frontend on the same origin. The Vercel environment variable is a build-time setting.

## Production deployment

### Render backend

This repository's `render.yaml` defines a Python web service rooted at `backend/`, installs `requirements.txt`, runs Uvicorn on Render's PORT, and checks `/api/health`. Create a Render PostgreSQL database and set these web-service environment variables:

- `DATABASE_URL`: the Render PostgreSQL connection string (use the internal URL for a same-region service).
- `CORS_ORIGINS`: comma-separated exact origins, including the deployed Vercel URL, e.g. `https://your-project.vercel.app`. Add each custom domain as its own origin. Avoid `*`.
- `ANTHROPIC_API_KEY`: optional; omit or leave unset to use deterministic evidence-grounded answers.

Render also sets `PORT`. `DEPLOYMENT_ENV=render` prevents accidentally starting a hosted instance without a persistent database. Keep `NIGHTFALL_DATA_DIR` at the configured writable path or change it to another persistent input directory. Schema creation and initial case ingestion happen on startup.

### Vercel frontend

Set Root Directory to `forensight` when the Git repository contains this project in that subdirectory. Use Framework Preset **Other**, Install Command `npm install`, Build Command `npm run build`, and Output Directory `dist`. Add this Vercel environment variable for Production (and Preview if needed):

- `VITE_API_URL`: your Render backend origin, e.g. `https://forensight-nightfall-api.onrender.com` (no trailing slash and no `/api`).

Then add the exact Vercel production/custom-domain origins to the Render `CORS_ORIGINS` value. For Vercel preview URLs, either add their exact origins or configure a narrowly scoped `CORS_ORIGIN_REGEX` such as `https://[a-z0-9-]+-your-team\\.vercel\\.app`. Do not use a wildcard origin.

## API

All endpoints return JSON. Validation errors use HTTP 422, missing evidence returns 404, and unexpected server errors return a generic JSON 500 while details are logged server-side. Evidence rows include identifiers, type, timestamp, normalized actor/target, summary, source provenance, metadata, coordinates when present, and SHA-256.

| Method and path | Purpose and inputs | Response shape |
|---|---|---|
| GET `/api/health` | Backend/database state, active case, loaded evidence count, readiness, and mode | `{"status":"ok","backend":"connected","active_case":"...","evidence_count":382,...}` |
| GET `/api/cases` | List isolated cases and active case | `{"active_case":"...","cases":[...]}` |
| POST `/api/cases` | Create a case with timezone and incident metadata | Case object |
| GET `/api/cases/{case_id}` | Retrieve case metadata and status | Case object |
| GET `/api/cases/{case_id}/summary` | Database-backed evidence counts and timeline total | Summary object |
| GET `/api/cases/{case_id}/audit` | Retrieve case audit events | `{"case_id":"...","events":[...]}` |
| POST `/api/cases/{case_id}/activate` | Switch the active case | `{"active_case":"..."}` |
| POST `/api/cases/{case_id}/archive` | Archive a case and select another available case | `{"case_id":"...","status":"archived",...}` |
| GET `/api/cases/{case_id}/manifest` | Retrieve exact import counts, warnings, file hashes, and type breakdown | Import manifest object |
| GET `/api/cases/{case_id}/files` | File names, sizes, SHA-256 hashes, import time and record counts | `{"case_id":"...","files":[...]}` |
| GET `/api/cases/{case_id}/files/{file_id}/original` | Download retained original import bytes | Original file bytes |
| POST `/api/cases/{case_id}/imports` | Start a multipart evidence import | `{"import_id":"...","status":"queued"}` |
| GET `/api/imports/{import_id}` | Current import stage, progress, errors, and manifest | Import job object |
| GET `/api/cases/{case_id}/evidence` | Case-scoped, paginated evidence search and filtering | `{"count":...,"results":[...],...}` |
| GET `/api/cases/{case_id}/evidence/{evidence_id}` | Retrieve case-scoped evidence detail | Evidence object |
| GET `/api/cases/{case_id}/evidence/{evidence_id}/provenance` | Source and linked derived evidence references | Provenance object |
| GET `/api/cases/{case_id}/timeline` | Case-scoped timestamped evidence | Timeline object |
| GET `/api/cases/{case_id}/communications` | Case-scoped communication graph | Graph object |
| GET `/api/cases/{case_id}/locations` | Case-scoped, paginated GPS evidence | Location object |
| GET `/api/cases/{case_id}/correlations` | Case-scoped correlation candidates | Correlation list |
| GET `/api/cases/{case_id}/anomalies` | Case-scoped anomaly indicators | Anomaly list |
| GET `/api/cases/{case_id}/search?q=term` | Case-scoped evidence search | Search object |
| POST `/api/cases/{case_id}/copilot/query` | Case-scoped, evidence-grounded question | Copilot answer with evidence references |
| GET `/api/stats` | Active-case inventory counts and incident context | Case summary object |
| GET `/api/entities` | Case contacts and device identifiers | `{"count":12,"results":[{"name":"...","number":"...","device":"..."}]}` |
| GET `/api/evidence` | Paginated list/filter. Query: `q`, `type`, `source_file`, `device_id`, `start`, `end`, `relevance_min`, `offset`, `limit` | `{"count":382,"results":[...],"offset":0,"limit":50}` |
| GET `/api/evidence/{id}` | One evidence record; unknown ID returns 404 | One evidence object |
| GET `/api/evidence/{id}/verify` | Verify normalized record hash and return provenance | `{"evidence_id":"E-0001","verified":true,...}` |
| GET `/api/search?q=delete` | Search summary, participants and evidence ID; optional `limit` (1–500) | `{"count":1,"results":[...]}` |
| GET `/api/timeline` | All timestamped types in chronological order. Optional `start`, `end`, `types`, `entity`, `incident_window_min`, `limit` | `{"count":...,"events":[...]}` |
| GET `/api/graph` | Communication graph nodes, centrality and weighted edges | `{"nodes":[...],"edges":[...]}` |
| GET `/api/connections` | Strongest communication links. Optional `person`, `limit` (1–100) | `{"count":...,"results":[...]}` |
| GET `/api/graph/strongest?person=Arjun%20Mehra` | Backward-compatible strongest links for one person | Array of `{"entity":"...","interactions":N}` |
| GET `/api/correlations` | Cross-source timestamp clusters with evidence IDs | Array of clusters with confidence and evidence IDs |
| GET `/api/anomalies` | Rule-based anomaly indicators linked to evidence IDs | Array of anomaly records |
| GET `/api/relevance` | Rank evidence around the incident; optional comma-separated `involved` names | Array of ranked evidence records |
| GET `/api/map/geofence?lat=28.6118&lon=77.2437&radius_m=500` | GPS points in a validated radius | `{"count":...,"points":[...]}` |
| GET `/api/map/path?person=Arjun%20Mehra` | GPS route for a named entity | Array of timestamped coordinates |
| POST `/api/copilot` | Evidence-grounded question; body `{"query":"What happened between 9 PM and 11 PM?","context":{"case":"NIGHTFALL"}}` | `{"answer":"...","confidence":0.8,"evidence":[...],"source_artifacts":[...],...}` |
| POST `/api/datasets/upload` | Multipart upload of ZIP or supported evidence file; creates/activates a case | Import manifest with files, record counts, hashes, warnings, and breakdown |
| POST `/api/reports/upload` | Backward-compatible JSON single-file upload | Import manifest |
| POST `/api/demo/load` | Backward-compatible idempotent initial demo import | `{"case":"NIGHTFALL","evidence":361,"generated":{}}` |

The Copilot retrieves from stored evidence, includes artifact citations, and uses its deterministic response when no Anthropic key is configured. If enabled, the optional LLM only rephrases the already-grounded answer; failure falls back to the deterministic text.

## Development checks

```powershell
Push-Location backend
python -m pytest tests
Pop-Location
npm run build
```
