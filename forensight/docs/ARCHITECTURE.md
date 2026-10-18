# ForenSight Architecture

ForenSight is an evidence-analysis and correlation platform. It does not replace forensic acquisition tools. It analyzes exported evidence while preserving source-file hashes, raw source records, normalized values, and links from derived results back to evidence.

## Components

- **Frontend:** the existing HTML, CSS, and JavaScript application under `frontend/`. It serves the dashboard, evidence explorer, timeline, connections, geo analysis, and Copilot views, and calls the local JSON API.
- **Backend:** FastAPI routes in `backend/app/main.py`. Routes resolve an explicit case or the active case and use SQLAlchemy for data access.
- **Parser registry:** `backend/app/parsers/registry.py` identifies supported file extensions and content types. `backend/app/ingest.py` implements the current parsing and normalization dispatch.
- **Evidence normalizer:** maps heterogeneous records into the case-scoped `Evidence` table while retaining source timestamps, source IDs, original record JSON, normalized fields, parser identity, and SHA-256 values.
- **Database:** SQLite is the local default; SQLAlchemy also supports PostgreSQL. Cases, evidence, imported-file bytes, entities, import jobs, and audit events are stored with case references and query indexes.
- **Analysis engine:** `backend/app/intel.py` builds timelines, evidence-grounded correlations, anomaly candidates, and relevance results.
- **Communication engine:** groups actual message and call records by participants; edges include counts, direction, time bounds, call duration, and evidence IDs.
- **Geospatial engine:** reads imported GPS evidence, orders it by case-local timestamp, and derives route, geofence, and dwell information. The UI uses an offline-safe coordinate/path view and does not require map tiles.
- **Copilot:** uses case-scoped evidence retrieval and returns supporting evidence references. Optional LLM configuration is not required for local operation.
- **Audit layer:** records case creation, import, verification, and other supported actions without writing raw evidence content into logs.

## Evidence flow

```text
REAL EVIDENCE
    ↓
HASH
    ↓
PRESERVE ORIGINAL FILE AND RAW RECORD
    ↓
DETECT AND PARSE
    ↓
NORMALIZE WITH TIMESTAMP PROVENANCE
    ↓
STORE IN CASE-SCOPED DATABASE
    ↓
INDEX
    ↓
ANALYZE TIMELINE, COMMUNICATIONS, AND LOCATIONS
    ↓
CORRELATE AND DETECT CANDIDATES
    ↓
EXPLAIN WITH EVIDENCE REFERENCES
    ↓
TRACE BACK TO THE ORIGINAL SOURCE
```

Original source timestamps are kept separately from normalized database timestamps. Naive source timestamps are interpreted in the case timezone; offset-aware timestamps are converted to that timezone. Normalized API timestamps carry a timezone offset, and the original string and precision remain available for provenance.

## Import and job lifecycle

`POST /api/cases/{case_id}/imports` accepts a file and returns an import job ID. `GET /api/imports/{import_id}` exposes the current stage, file and record progress, errors, and final manifest. The current implementation executes the task through FastAPI's in-process background task facility; it is suitable for this local prototype but is not a durable queue across process restarts. Imported original bytes are stored with SHA-256, size, parser metadata, and status. ZIP processing applies path and expansion limits before parsing children.

Supported exports are CSV, JSON, XML, XLSX, TXT, HTML, PDF, and ZIP. PDF text extraction processes pages; image-only pages explicitly report that OCR is required. Proprietary forensic formats are not claimed as supported; export them to a supported format first.

## Case isolation and operations

Evidence queries, timeline, graph, location, correlation, anomaly, search, and Copilot APIs are scoped to a case. Database foreign keys connect evidence and operational rows to cases. The regression suite includes tests that check for cross-case leakage.

SQLite is the default and supports local offline use. PostgreSQL is available for deployment. No remote map tiles, external fonts, telemetry, or cloud AI service is required for the default workflow. An optional LLM key enables rephrasing of grounded answers; do not configure it when the deployment must guarantee no external evidence processing.

## Current limits

- The in-process import task is not durable if the API process exits during an import.
- OCR is not implemented; image-only PDF pages are marked `OCR required`.
- Proprietary acquisition formats have no parser. ForenSight analyzes exports and does not acquire device data.
- Older imported files created before original-byte storage was added may have hashes and metadata but cannot provide a download of bytes that were never retained.
- Local prototype authentication and role-based authorization are not implemented; restrict access to the host and network accordingly.
- Derived correlations and anomaly indicators are investigative leads for human review, not proof or automated conclusions.
