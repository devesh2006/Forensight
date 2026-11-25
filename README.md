# ForenSight
ForenSight opens with an empty evidence workspace. Upload an export to create the first case. Further uploads through the dashboard append to the selected case. The API also supports creating and activating additional cases.

## Run locally
From this directory, run:

    python -m venv .venv

On Windows, activate the environment in PowerShell:

    .\.venv\Scripts\Activate.ps1

Then run:

    python -m pip install -r backend/requirements.txt
    python -m uvicorn app.main:app --app-dir backend

Open http://127.0.0.1:8000. The default database is forensight-evidence.db. For a hosted backend, set DATABASE_URL to a fresh PostgreSQL database and CORS_ORIGINS to the frontend origin. Keep existing databases as backups and import original exports into the new database. Configure VITE_API_URL before running npm run build for a separate static frontend.

## Interpreting results
The Copilot answers case summaries, timeline questions such as “What happened at 9:25 PM?”, participant queries such as “What messages did Alex send?”, and literal searches. A clock time without a date checks all dates in the case. Use YYYY-MM-DD to narrow it to a particular date. Citations open the evidence register. Source-local timestamps without offsets are interpreted using the configured case zone (Asia/Kolkata by default). Missing incident metadata leaves incident-based results unavailable. Temporal overlap and anomaly flags are calculated leads. File and record hashes are integrity checks. Source authenticity requires independent verification.


## Gemini question interpretation
Set GEMINI_API_KEY in the backend environment to enable Gemini to convert natural-language questions into a small structured retrieval command. The model receives only the question text. Evidence remains in the ForenSight database, and results are retrieved and cited by the backend. Built-in questions are interpreted locally. For other questions, Gemini is optional and the backend can still search for matching text. Set GEMINI_MODEL to select a supported model. Never put the key in frontend variables or commit it.


## Evidence formats and scope

| Input | What ForenSight uses |
| --- | --- |
| CSV and TSV, including call and message exports | Header-mapped fields, original values and source line. Use separate caller and callee columns. An incoming or outgoing direction can also link an explicit owner to a contact. Provide duration in an explicitly labeled seconds column. |
| JSON, JSONL, NDJSON and XML | Recognized structured records with original fields. |
| XLSX | Cells from supported worksheets, with sheet and row references. |
| PDF, HTML, TXT and LOG | Extracted page or document text, and stored text lines. A PDF statement is searchable text from its source page. It does not automatically become a call, a location or a timeline event. |
| GPX, GeoJSON Point and KML Point | Recorded coordinates. Points without reliable times can be plotted but are excluded from timeline and geofence time analysis. |
| ZIP | Mixed supported exports with an import manifest for parsed, failed and skipped files. |

Scanned, encrypted and damaged PDFs may need an external text export. Proprietary images and extraction containers such as E01 and UFDR must be acquired and exported with an appropriate forensic tool before analysis. GeoJSON and KML geometries other than points need conversion to explicit point observations where appropriate. Preserve originals. File hashes show that stored bytes match the import. They cannot establish whether a claim in a source is accurate.

For a local regression check, run the following from the project root with the backend dependencies installed:

    cd backend
    python -m pytest -q verification

Verification fixtures are synthetic and are isolated from application startup data. For Gemini testing, use non-sensitive question text. The question goes to Gemini for interpretation when configured. ForenSight retrieves, counts and cites the evidence locally.
