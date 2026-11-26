"""Tests for hybrid semantic and literal evidence search using FastEmbed."""
import os
from io import BytesIO
from zipfile import ZipFile
import pytest
from fastapi.testclient import TestClient
from app import main, embeddings, copilot
from app.ingest import Evidence, EvidenceEmbedding, make_session


def test_hybrid_search_semantic_and_literal(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'hybrid.db'}")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(main, "_db", None)

    # Dataset with distinct conceptual evidence
    # 1. Financial wire transfer
    # 2. Covert meeting rendezvous
    # 3. Routine call
    csv_data = (
        "Call Time,Calling Number,Called Number,Duration Seconds,Summary\n"
        "2025-05-01T10:00:00Z,Alice,Bob,60,Wire transfer of funds sent to offshore account\n"
        "2025-05-01T11:00:00Z,Charlie,Dave,120,Rendezvous tonight behind the abandoned warehouse at midnight\n"
        "2025-05-01T12:00:00Z,Eve,Frank,45,Routine check-in regarding project schedule\n"
    )

    with TestClient(main.app) as client:
        # Upload evidence
        upload = client.post(
            "/api/datasets/upload",
            files={"file": ("evidence.csv", csv_data.encode("utf-8"))},
        )
        assert upload.status_code == 200
        manifest = upload.json()
        case_id = manifest["case_id"]

        db = make_session(f"sqlite:///{tmp_path / 'hybrid.db'}")
        evidence_count = db.query(Evidence).filter_by(case_id=case_id).count()
        assert evidence_count == 3

        # Verify embeddings were NOT created during upload (lazy semantic indexing)
        emb_count_pre = db.query(EvidenceEmbedding).filter_by(case_id=case_id).count()
        assert emb_count_pre == 0

        # Test 1: Pure semantic query with no literal keyword match triggers lazy embedding
        # "payment to offshore account" does not appear literally in "Wire transfer of funds sent to offshore account"
        sem_res = client.post(
            f"/api/cases/{case_id}/copilot/query",
            json={"query": "payment to offshore account"},
        ).json()
        assert len(sem_res["evidence"]) > 0
        assert "offshore" in sem_res["evidence"][0]["summary"]

        # Verify persistent embeddings were lazily created on search
        emb_count_post = db.query(EvidenceEmbedding).filter_by(case_id=case_id).count()
        assert emb_count_post == 3

        # Verify embeddings have text_fingerprint and 384-dim vector (1536 bytes)
        emb_row = db.query(EvidenceEmbedding).filter_by(case_id=case_id).first()
        assert emb_row.text_fingerprint is not None
        assert len(emb_row.text_fingerprint) == 64  # sha256 hex
        assert len(emb_row.embedding) == 384 * 4  # 384 float32 = 1536 bytes

        # Test 2: Another semantic query
        # "meeting behind the warehouse" does not appear literally in "Rendezvous tonight behind the abandoned warehouse at midnight"
        rendezvous_res = client.post(
            f"/api/cases/{case_id}/copilot/query",
            json={"query": "meeting behind the warehouse"},
        ).json()
        assert len(rendezvous_res["evidence"]) > 0
        assert "warehouse" in rendezvous_res["evidence"][0]["summary"]

        # Test 3: Exact match reliability
        # Exact evidence ID search
        first_ev = db.query(Evidence).filter_by(case_id=case_id).first()
        exact_id_res = client.post(
            f"/api/cases/{case_id}/copilot/query",
            json={"query": f"find {first_ev.evidence_id}"},
        ).json()
        assert len(exact_id_res["evidence"]) == 1
        assert exact_id_res["evidence"][0]["id"] == first_ev.evidence_id

        # Test 4: Completely unrelated query returns 0 matches
        unrelated_res = client.post(
            f"/api/cases/{case_id}/copilot/query",
            json={"query": "chocolate cake recipe baking instructions"},
        ).json()
        assert len(unrelated_res["evidence"]) == 0
        assert "No records match" in unrelated_res["answer"]


def test_embedding_fingerprint_change_detection(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'fp.db'}")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(main, "_db", None)

    db = make_session(f"sqlite:///{tmp_path / 'fp.db'}")
    case_id = "CASE-FP"
    ev = Evidence(
        evidence_id="EVID-1",
        case_id=case_id,
        type="message",
        summary="Initial evidence description",
        source_file="test.txt",
    )
    db.add(ev)
    db.commit()

    # Sync embeddings
    synced = embeddings.sync_case_embeddings(db, case_id)
    assert synced == 1

    emb1 = db.query(EvidenceEmbedding).filter_by(evidence_id="EVID-1").first()
    fp1 = emb1.text_fingerprint
    updated_at_1 = emb1.updated_at

    # Re-sync without changes -> 0 updated
    assert embeddings.sync_case_embeddings(db, case_id) == 0

    # Modify evidence record text
    ev.summary = "Updated completely different evidence text regarding finances"
    db.commit()

    # Re-sync -> detects fingerprint mismatch and re-embeds
    synced2 = embeddings.sync_case_embeddings(db, case_id)
    assert synced2 == 1

    emb2 = db.query(EvidenceEmbedding).filter_by(evidence_id="EVID-1").first()
    assert emb2.text_fingerprint != fp1


def test_fallback_to_literal_when_model_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'fallback.db'}")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(main, "_db", None)

    # Force model failure
    monkeypatch.setattr(embeddings, "get_model", lambda: None)

    db = make_session(f"sqlite:///{tmp_path / 'fallback.db'}")
    case_id = "CASE-FALLBACK"
    from app.ingest import CaseRecord
    db.add(CaseRecord(case_id=case_id, name="Fallback Test Case", active=True))
    ev = Evidence(
        evidence_id="EVID-FB-1",
        case_id=case_id,
        type="message",
        summary="Exact literal keyword target in evidence",
        source_file="test.txt",
    )
    db.add(ev)
    db.commit()

    with TestClient(main.app) as client:
        # Literal search should still succeed
        res = client.post(
            f"/api/cases/{case_id}/copilot/query",
            json={"query": "find Exact literal keyword"},
        ).json()
        assert len(res["evidence"]) == 1
        assert res["evidence"][0]["id"] == "EVID-FB-1"
        assert "Literal search" in res["reasoning_summary"]

        # Backend health is unaffected
        health = client.get("/api/health").json()
        assert health["status"] == "ok"
