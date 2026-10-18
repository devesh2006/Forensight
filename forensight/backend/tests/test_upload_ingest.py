from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import pytest

from app.ingest import Base, CaseRecord, Evidence, ingest_dataset, ingest_upload

def test_csv_upload_adds_and_skips_duplicates():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    payload = b"timestamp,sender,receiver,text,app\n2026-10-18T12:00:00,+10000000001,+10000000002,Example message,SMS\n"
    first = ingest_upload(session, "NIGHTFALL", "messages.csv", payload)
    second = ingest_upload(session, "NIGHTFALL", "messages.csv", payload)
    assert first == {"added": 1, "skipped": 0, "contacts_added": 0}
    assert second == {"added": 0, "skipped": 1, "contacts_added": 0}
    assert session.query(Evidence).count() == 1
    session.close()
    engine.dispose()

def test_upload_rejects_unsupported_format_and_invalid_csv():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    with pytest.raises(ValueError):
        ingest_upload(session, "NIGHTFALL", "report.pdf", b"not a supported evidence export")
    with pytest.raises(ValueError, match="CSV must contain"):
        ingest_upload(session, "NIGHTFALL", "messages.csv", b"bad,headers\nno,data")
    session.close()
    engine.dispose()

def test_zip_dataset_imports_records_and_preserves_raw_fields():
    import io, json, zipfile
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("case_metadata.json", json.dumps({"case_id":"CASE-A","case_name":"Case A"}))
        z.writestr("messages.csv", "evidence_id,timestamp,type,direction,contact,content,device_id\nMSG-1,2026-10-18T12:00:00,message,outbound,Rook,Meet,DEV-1\n")
        z.writestr("browser.json", json.dumps({"evidence":[{"id":"B-1","timestamp":"2026-10-18T12:01:00","artifact_type":"browser_search","query":"warehouse","url":"https://example.invalid","extra":"kept"}]}))
        z.writestr("README.txt", "Dataset description")
        z.writestr("notes.exe", b"unsupported")
    result = ingest_dataset(session, "dataset.zip", archive.getvalue())
    rows = session.query(Evidence).filter_by(case_id="CASE-A").order_by(Evidence.evidence_id).all()
    assert result["records_detected"] == result["records_imported"] == 2
    assert result["by_type"] == {"message":1,"browser":1}
    assert result["files_skipped"] == 1
    browser = next(e for e in rows if e.type == "browser")
    assert json.loads(browser.meta)["raw_record"]["extra"] == "kept"
    assert browser.original_id == "B-1"
    assert session.query(CaseRecord).filter_by(case_id="CASE-A",active=True).count() == 1
    session.close(); engine.dispose()

def test_duplicate_source_ids_get_unique_internal_ids():
    import json
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    payload = json.dumps([{"evidence_id":"MSG-1","timestamp":"2026-10-18T12:00:00","type":"message","sender":"A","receiver":"B","content":"one"},
                          {"evidence_id":"MSG-1","timestamp":"2026-10-18T12:01:00","type":"message","sender":"A","receiver":"B","content":"two"}]).encode()
    result = ingest_dataset(session,"messages.json",payload,"CASE-DUP")
    rows = session.query(Evidence).filter_by(case_id="CASE-DUP").all()
    assert result["records_imported"] == 2
    assert len({e.evidence_id for e in rows}) == 2
    assert {e.original_id for e in rows} == {"MSG-1"}
    session.close(); engine.dispose()

def test_zip_path_traversal_is_skipped_and_malformed_csv_rows_are_reported():
    import io, zipfile
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive,"w") as z:
        z.writestr("../escape.csv","timestamp,sender,receiver,text\n2026-10-18T12:00:00,A,B,unsafe\n")
        z.writestr("messages.csv","timestamp,sender,receiver,text\n2026-10-18T12:00:00,A,B,valid\n2026-10-18T12:01:00,A,wrong-column-count\n")
    result=ingest_dataset(session,"safe.zip",archive.getvalue(),"CASE-SAFE")
    assert result["records_detected"] == 2
    assert result["records_imported"] == 1
    assert result["records_failed"] == 1
    assert result["files_skipped"] == 1
    assert result["skipped_files"][0]["reason"] == "Unsafe archive path — skipped"
    session.close();engine.dispose()

