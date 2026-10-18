import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ingest import Base, Evidence, ImportedFile, ingest_dataset
from app.intel import build_graph
from app.main import map_analysis, row


def test_operation_nightfall_fixture_import_and_real_analysis():
    fixture = Path(__file__).parent / "fixtures" / "operation_nightfall_v2.zip"
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        result = ingest_dataset(session, fixture.name, fixture.read_bytes())
        case = result["case_id"]
        assert result["files_discovered"] == result["files_processed"] == 6
        assert result["records_detected"] == result["records_imported"] == 382
        assert result["records_failed"] == 0
        assert result["status"] == "processed"
        assert result["by_type"] == {"message": 157, "call": 75, "gps": 104, "browser": 23, "application": 23}

        records = session.query(Evidence).filter_by(case_id=case).all()
        assert len({e.evidence_id for e in records}) == 382
        assert all(e.case_id == case and e.source_file and e.sha256 and e.source_file_sha256 for e in records)
        archive_file=session.query(ImportedFile).filter_by(case_id=case,status="original_archive").one()
        assert archive_file.raw_bytes==fixture.read_bytes()
        assert all(json.loads(e.meta or "{}").get("raw_record") for e in records)
        assert all(e.original_timestamp and e.timezone == "Asia/Kolkata" for e in records)
        assert session.query(Evidence).filter_by(case_id=case, type="location").filter(Evidence.lat.isnot(None), Evidence.lon.isnot(None)).count() == 104
        assert session.query(Evidence).filter_by(case_id=case).filter(Evidence.ts.isnot(None)).count() == 382

        sample = session.query(Evidence).filter_by(case_id=case, original_id="GPS-061").one()
        sample_row = row(sample, {})
        assert sample_row["original_timestamp"] == "2026-10-14T00:03:16"
        assert sample_row["time"] == "2026-10-14T00:03:16+05:30"
        assert sample_row["timezone"] == "Asia/Kolkata"
        geo = map_analysis(session)
        assert geo["count"] == 104
        assert sum(len(route) for route in geo["routes"].values()) == 104
        assert geo["geofence_events"] and geo["dwell_periods"]

        graph = build_graph(session, case)
        assert graph["edges"]
        assert sum(edge["message_count"] for edge in graph["edges"]) == 157
        assert sum(edge["call_count"] for edge in graph["edges"]) == 75
        assert all(edge["evidence_ids"] and edge["first_interaction"] and edge["last_interaction"] for edge in graph["edges"])
        assert all(len(edge["interactions"]) == edge["total_interactions"] for edge in graph["edges"])
    finally:
        session.close()
        engine.dispose()
