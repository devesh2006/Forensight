import io
import zipfile

import pytest
from pypdf import PdfWriter
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ingest import Base, Evidence, ImportedFile, ingest_dataset


def _workbook():
    out=io.BytesIO()
    with zipfile.ZipFile(out,"w",zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/workbook.xml",'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Calls" sheetId="1" r:id="rId1"/><sheet name="Messages" sheetId="2" r:id="rId2"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
        for name,contact,content in [("sheet1.xml","Rook","Call record"),("sheet2.xml","Mira","Multiline\nmessage")]:
            z.writestr("xl/worksheets/"+name,'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>timestamp</t></is></c><c r="B1" t="inlineStr"><is><t>sender</t></is></c><c r="C1" t="inlineStr"><is><t>receiver</t></is></c><c r="D1" t="inlineStr"><is><t>text</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>2026-10-14T20:01:00</t></is></c><c r="B2" t="inlineStr"><is><t>'+contact+'</t></is></c><c r="C2" t="inlineStr"><is><t>DEV-1</t></is></c><c r="D2" t="inlineStr"><is><t>'+content+'</t></is></c></row></sheetData></worksheet>')
    return out.getvalue()


def _import(filename,content):
    engine=create_engine("sqlite://");Base.metadata.create_all(engine);session=sessionmaker(bind=engine)()
    manifest=ingest_dataset(session,filename,content,"PARSER-CASE")
    return engine,session,manifest


def test_xlsx_reads_every_sheet_and_keeps_headers_and_rows():
    engine,session,result=_import("messages.xlsx",_workbook())
    try:
        rows=session.query(Evidence).filter_by(case_id="PARSER-CASE").all()
        assert result["records_imported"]==2
        assert {json_value(e)["raw_record"]["sheet_name"] for e in rows}=={"Calls","Messages"}
        assert all(json_value(e)["raw_record"]["original_headers"]==["timestamp","sender","receiver","text"] for e in rows)
        assert any("Multiline\nmessage" in e.summary for e in rows)
    finally:session.close();engine.dispose()


def json_value(e):
    import json
    return json.loads(e.meta)


def test_html_extracts_text_metadata_and_tables_without_running_scripts():
    source=b'<html><head><meta name="author" content="Analyst"></head><body><script>throw new Error("must not execute")</script><h1>Export</h1><table><tr><th>timestamp</th><th>title</th></tr><tr><td>2026-10-14T20:04:00</td><td>Search history</td></tr></table></body></html>'
    engine,session,result=_import("report.html",source)
    try:
        rows=session.query(Evidence).filter_by(case_id="PARSER-CASE").all()
        assert result["records_imported"]==2
        assert all("throw new Error" not in e.summary for e in rows)
        assert any("Analyst" in str(json_value(e)) and "original_html" in str(json_value(e)) for e in rows)
        assert any(e.ts is not None and e.summary=="Search history" for e in rows)
    finally:session.close();engine.dispose()


def test_pdf_reads_pages_and_marks_image_only_pages_for_ocr():
    out=io.BytesIO();writer=PdfWriter();writer.add_blank_page(width=300,height=300);writer.write(out)
    engine,session,result=_import("scan.pdf",out.getvalue())
    try:
        rows=session.query(Evidence).filter_by(case_id="PARSER-CASE").all()
        assert result["records_imported"]==1
        assert rows[0].summary=="OCR required — page has no extractable text"
        assert json_value(rows[0])["raw_record"]["page_number"]==1
    finally:session.close();engine.dispose()


def test_xml_rejects_entity_declarations_and_original_upload_is_retained():
    engine,session,result=_import("danger.xml",b'<!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><x><record timestamp="2026-10-14T20:00:00">&xxe;</record></x>')
    try:
        assert result["records_imported"]==0 and result["status"]=="failed"
        stored=session.query(ImportedFile).filter_by(case_id="PARSER-CASE").first()
        assert stored.raw_bytes.startswith(b"<!DOCTYPE")
    finally:session.close();engine.dispose()
