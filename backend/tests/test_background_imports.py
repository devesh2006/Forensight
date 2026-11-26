"""Tests for background import jobs, first uploads without case_id, and polling flow."""
import json
import os
import tempfile
from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from app import main
from app.ingest import CaseRecord, Evidence, EvidenceEmbedding, ImportJob, make_session


def test_first_upload_background_import_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'bg_test.db'}")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(main, "_db", None)

    csv_bytes = (
        b"Call Time,Calling Number,Called Number,Duration Seconds,Summary\n"
        b"2025-05-01T10:00:00Z,Alice,Bob,60,Wire transfer of funds sent\n"
    )

    with TestClient(main.app) as client:
        # 1. Post to /api/imports without case_id (first upload)
        res = client.post(
            "/api/imports",
            files={"file": ("evidence.csv", csv_bytes, "text/csv")},
        )
        assert res.status_code == 202
        data = res.json()
        assert "import_id" in data
        import_id = data["import_id"]
        assert data["status"] == "queued"
        assert data["case_id"] is None

        # 2. Poll /api/imports/{import_id}
        status_res = client.get(f"/api/imports/{import_id}")
        assert status_res.status_code == 200
        job_data = status_res.json()
        assert job_data["status"] == "complete"
        assert job_data["case_id"] is not None
        assert job_data["manifest"] is not None
        assert job_data["manifest"]["records_imported"] == 1

        created_case = job_data["case_id"]

        # 3. Verify no embeddings generated during upload (lazy indexing)
        db = make_session(f"sqlite:///{tmp_path / 'bg_test.db'}")
        emb_count = db.query(EvidenceEmbedding).filter_by(case_id=created_case).count()
        assert emb_count == 0


def test_upload_into_existing_case_background_import(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'bg_case.db'}")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(main, "_db", None)

    db = make_session(f"sqlite:///{tmp_path / 'bg_case.db'}")
    case = CaseRecord(case_id="EXISTING-CASE", name="Existing Case")
    db.add(case)
    db.commit()

    csv_bytes = (
        b"Call Time,Calling Number,Called Number,Duration Seconds,Summary\n"
        b"2025-05-01T10:00:00Z,Alice,Bob,60,Wire transfer\n"
    )

    with TestClient(main.app) as client:
        res = client.post(
            "/api/cases/EXISTING-CASE/imports",
            files={"file": ("evidence.csv", csv_bytes, "text/csv")},
        )
        assert res.status_code == 202
        import_id = res.json()["import_id"]

        status_res = client.get(f"/api/imports/{import_id}")
        assert status_res.status_code == 200
        job = status_res.json()
        assert job["status"] == "complete"
        assert job["case_id"] == "EXISTING-CASE"
        assert job["manifest"]["records_imported"] == 1


def test_failed_import_returns_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'bg_fail.db'}")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(main, "_db", None)

    with TestClient(main.app) as client:
        # Empty file upload should fail with 422
        res = client.post(
            "/api/imports",
            files={"file": ("empty.csv", b"", "text/csv")},
        )
        assert res.status_code == 422
