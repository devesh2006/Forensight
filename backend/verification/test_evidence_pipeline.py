"""Upload-to-answer regression checks using small synthetic source documents."""
from hashlib import sha256
from io import BytesIO
from zipfile import ZipFile

from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from app import main


def test_mixed_evidence_pipeline(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'evidence.db'}")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(main, "_db", None)
    pdf = BytesIO()
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): DictionaryObject({
            NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })}),
    })
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 100 Td (Invoice finding ZEBRA-19) Tj ET")
    page[NameObject("/Contents")] = stream
    writer.add_blank_page(width=200, height=200)
    writer.write(pdf)

    calls = b"Call Time;Calling Number;Called Number;Duration Seconds\n2025-04-16T04:00:00Z;+91000001;+91000002;12\n"
    bundle = BytesIO()
    with ZipFile(bundle, "w") as archive:
        archive.writestr("exports/calls.csv", calls)
        archive.writestr("report.pdf", pdf.getvalue())
        archive.writestr("points.gpx", '<gpx><wpt lat="28.6" lon="77.2"/></gpx>')
        archive.writestr("track.geojson", '{"type":"Feature","geometry":{"type":"Point","coordinates":[77.3,28.7]},"properties":{"timestamp":"2025-04-16T04:05:00Z"}}')
        archive.writestr("invalid.kml", '<kml><Placemark><Point><coordinates>900,28</coordinates></Point></Placemark></kml>')
        archive.writestr("messages.ndjson", '{"type":"message","sender":"a","receiver":"b","text":"message artifact"}\ninvalid')
        archive.writestr("cdr.tsv", "caller\tcallee\tdate\n+91000003\t+91000004\t2025-04-16\n")
        archive.writestr("broken.pdf", "invalid pdf")
        archive.writestr("unknown.E01", b"unsupported placeholder bytes")

    with TestClient(main.app) as client:
        assert client.get("/api/stats").json()["total"] == 0
        upload = client.post("/api/datasets/upload", files={"file":("case.zip",bundle.getvalue())})
        assert upload.status_code == 200, upload.text
        manifest = upload.json()
        assert manifest["records_imported"] == 6, manifest
        assert manifest["status"] == "partially_processed"
        assert manifest["records_failed"] == 3
        assert any(item.get("page") == 2 for item in manifest["warnings"])
        assert len(manifest["skipped_files"]) == 1
        case = manifest["case_id"]
        files = client.get(f"/api/cases/{case}/files").json()["files"]
        call_file = next(item for item in files if item["filename"].endswith("calls.csv"))
        original = client.get(f"/api/cases/{case}/files/{call_file['id']}/original")
        assert original.content == calls

        def ask(question):
            result = client.post(f"/api/cases/{case}/copilot/query", json={"query":question})
            assert result.status_code == 200, result.text
            return result.json()

        pdf_result, = ask("find ZEBRA-19")["evidence"]
        assert pdf_result["locator"] == "page 1"
        assert "Invoice finding ZEBRA-19" in pdf_result["summary"]
        assert not ask("find OCR required")["evidence"]
        assert not ask("find made up finding")["evidence"]
        call_result, = [item for item in ask("call logs")["evidence"] if item["locator"] == "line 2" and item["source"].endswith(".csv")]
        assert call_result["source_sha256"] == sha256(calls).hexdigest()
        detail = client.get(f"/api/cases/{case}/evidence/{call_result['id']}").json()
        assert detail["actor"] == "+91000001" and detail["target"] == "+91000002"
        assert call_result["time"] == "2025-04-16T09:30:00+05:30"
        assert len(ask("timeline 2025-04-16")["evidence"]) == 2
        coordinates = {(e["lat"],e["lon"],e["time"] is None) for e in ask("locations")["evidence"]}
        assert coordinates == {(28.6,77.2,True),(28.7,77.3,False)}
        assert client.get("/api/map/analysis").json()["geofence_events"] == []

        another = client.post("/api/cases",json={"name":"Empty case"}).json()["case_id"]
        assert client.post(f"/api/cases/{another}/copilot/query",json={"query":"find ZEBRA-19"}).json()["evidence"] == []


def test_structured_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'structured.db'}")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(main, "_db", None)
    xlsx = BytesIO()
    with ZipFile(xlsx, "w") as archive:
        archive.writestr("xl/workbook.xml", '<workbook xmlns:r="urn:rel"><sheet name="Records" r:id="r1"/></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr("xl/worksheets/sheet1.xml", '<worksheet><row r="1"><c r="A1" t="inlineStr"><is><t>text</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>ledger shard</t></is></c></row></worksheet>')
    bundle = BytesIO()
    with ZipFile(bundle, "w") as archive:
        archive.writestr("evidence.xlsx", xlsx.getvalue())
        archive.writestr("messages.xml", '<root><message sender="A" receiver="B" text="xml shard"/></root>')
        archive.writestr("report.html", '<article>html shard</article><script>omit this script</script>')
        archive.writestr("trace.log", '2025-02-07T00:00:00Z log shard')
        archive.writestr("notes.txt", 'note shard')
        archive.writestr("case.json", '{"calls":[{"caller":"A","callee":"B","summary":"json shard"}],"locations":[{"lat":0,"lon":0}]}')
        archive.writestr("point.kml", '<kml><Placemark><name>point shard</name><Point><coordinates>71.5,22.5</coordinates></Point></Placemark></kml>')
    with TestClient(main.app) as client:
        manifest = client.post("/api/datasets/upload", files={"file":("sources.zip",bundle.getvalue())}).json()
        assert manifest["records_imported"] == 8, manifest
        assert manifest["status"] == "processed"
        for term, locator in (
            ("ledger shard","sheet Records, row 2"), ("xml shard","record 1"), ("html shard","record 1"),
            ("log shard","line 1"), ("note shard","line 1"), ("json shard","record 1"),
        ):
            evidence, = client.post("/api/copilot", json={"query":f"find {term}"}).json()["evidence"]
            assert evidence["locator"] == locator
        fixes = client.get("/api/evidence?type=gps").json()["results"]
        assert {(fix["lat"],fix["lon"]) for fix in fixes} == {(0,0),(22.5,71.5)}
        assert all(fix["time"] is None for fix in fixes)


def test_natural_language_copilot_with_imported_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'copilot.db'}")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(main, "_db", None)
    calls = (
        "Call Time,Calling Number,Called Number,Duration Seconds\n"
        "2025-04-16T15:55:42Z,Asha,Ravi,18\n"
        "2025-04-16T15:56:00Z,Ravi,Asha,12\n"
        "2025-04-17T21:25:03,Asha,Ravi,34\n"
        "2025-04-17T23:59:00,Asha,Ravi,12\n"
        "2025-04-18T00:01:00,Asha,Ravi,12\n"
    )
    with TestClient(main.app) as client:
        created = client.post("/api/cases", json={"name": "Test case", "description": "Review of reported calls."})
        assert created.status_code == 200, created.text
        case = created.json()["case_id"]
        headers = {"X-Case-ID": case}
        upload = client.post("/api/datasets/upload", headers=headers, params={"case_id": case},
                             files={"file": ("calls.csv", calls)})
        assert upload.status_code == 200, upload.text
        other = client.post("/api/cases", json={"name": "Unrelated case"}).json()["case_id"]
        upload = client.post("/api/datasets/upload", params={"case_id": other},
                             files={"file": ("other.csv", calls)})
        assert upload.status_code == 200, upload.text

        def ask(question):
            response = client.post("/api/copilot", headers=headers, json={"query": question})
            assert response.status_code == 200, response.text
            return response.json()

        summary = ask("what is this case about")
        assert summary["case_id"] == case
        assert "5 call logs" in summary["answer"]
        assert "Review of reported calls." in summary["answer"]
        assert len(summary["evidence"]) == 5

        timed = ask("what happened at 9:25 pm")
        assert "across all recorded dates" in timed["answer"]
        assert [e["time"] for e in timed["evidence"]] == [
            "2025-04-16T21:25:42+05:30", "2025-04-17T21:25:03+05:30"
        ]
        assert all(e["actor"] == "Asha" and e["target"] == "Ravi" for e in timed["evidence"])
        assert timed["source_artifacts"] == ["calls.csv"]
        for evidence in timed["evidence"]:
            assert client.get(f"/api/cases/{case}/evidence/{evidence['id']}").status_code == 200

        scoped = ask("What happened at 21:25 on 2025-04-17?")
        assert scoped["evidence"] == timed["evidence"][1:]
        assert len(ask("What happened between 9:25 PM and 9:26 PM on 2025-04-16?")["evidence"]) == 2
        assert len(ask("What happened between 11:55 PM and 12:05 AM on 2025-04-17?")["evidence"]) == 2
        assert len(ask("What happened between 9 and 11 PM?")["evidence"]) == 3
        assert "Try a wider time range" in ask("What happened at 8:04 PM?")["answer"]
        assert "valid time" in ask("What happened at 9:75 PM?")["answer"]
        assert len(ask("Who did Asha call?")["evidence"]) == 4
        assert not ask("What messages did Asha send?")["evidence"]
