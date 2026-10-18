"""Ingestion: parse CSV/JSON/XML -> normalized evidence rows with SHA-256 + provenance."""
import csv, hashlib, io, json, re, zipfile
import os
from html.parser import HTMLParser
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from .timezones import case_zone
from .parsers.registry import ParserRegistry, ParserSpec
from xml.etree import ElementTree as ET  # stdlib parser; no external entities resolved
try:
    from defusedxml import ElementTree as SafeET
    from defusedxml.common import DefusedXmlException
except ImportError:
    SafeET=ET
    DefusedXmlException=ValueError
from sqlalchemy import create_engine, Column, Integer, String, Float, Text, DateTime, Boolean, Index, text, LargeBinary, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()
class Evidence(Base):
    __tablename__ = "evidence"
    id = Column(Integer, primary_key=True); evidence_id = Column(String, unique=True, index=True)
    case_id = Column(String, ForeignKey("cases.case_id"), index=True); type = Column(String, index=True); ts = Column(DateTime, index=True)
    source_file = Column(String); source_artifact = Column(String); actor = Column(String, index=True)
    target = Column(String, index=True); summary = Column(Text); lat = Column(Float); lon = Column(Float)
    sha256 = Column(String); source_file_sha256 = Column(String); ingested_at = Column(DateTime); meta = Column(Text)
    device_id = Column(String, index=True); original_id = Column(String)
    original_timestamp = Column(String); timezone = Column(String); timestamp_precision = Column(String)
Index("ix_evidence_case_ts", Evidence.case_id, Evidence.ts)
Index("ix_evidence_case_type", Evidence.case_id, Evidence.type)
Index("ix_evidence_case_source", Evidence.case_id, Evidence.source_file)
class CaseRecord(Base):
    __tablename__ = "cases"
    case_id = Column(String, primary_key=True); name = Column(String, nullable=False)
    metadata_json = Column(Text, default="{}"); manifest_json = Column(Text, default="{}")
    created_at = Column(DateTime); active = Column(Boolean, default=False, index=True)
    description=Column(Text);incident_type=Column(String);incident_location=Column(String);incident_start=Column(String);incident_end=Column(String)
    timezone=Column(String,default="Asia/Kolkata");status=Column(String,default="open");created_by=Column(String,default="local");updated_at=Column(DateTime)
class ImportedFile(Base):
    __tablename__ = "imported_files"
    id = Column(Integer, primary_key=True); case_id = Column(String, ForeignKey("cases.case_id"), index=True)
    filename = Column(String); size = Column(Integer); sha256 = Column(String)
    imported_at = Column(DateTime); status = Column(String); records_detected = Column(Integer, default=0)
    records_imported = Column(Integer, default=0); records_failed = Column(Integer, default=0); warning = Column(Text); raw_bytes=Column(LargeBinary)
    parser_name=Column(String);parser_version=Column(String)
class Entity(Base):
    __tablename__ = "entities"
    id = Column(Integer, primary_key=True); case_id = Column(String, ForeignKey("cases.case_id"), index=True); phone = Column(String, index=True)
    name = Column(String); device = Column(String)
class AuditEvent(Base):
    __tablename__="audit_events"
    id=Column(Integer,primary_key=True);timestamp=Column(DateTime,index=True);action=Column(String,index=True)
    case_id=Column(String,ForeignKey("cases.case_id"),index=True);evidence_id=Column(String,index=True);actor=Column(String);metadata_json=Column(Text,default="{}")
class ImportJob(Base):
    __tablename__="import_jobs"
    import_id=Column(String,primary_key=True);case_id=Column(String,ForeignKey("cases.case_id"),index=True)
    filename=Column(String);status=Column(String,index=True);current_stage=Column(String);files_processed=Column(Integer,default=0);files_total=Column(Integer,default=0)
    records_processed=Column(Integer,default=0);records_total=Column(Integer,default=0);errors_json=Column(Text,default="[]");manifest_json=Column(Text)
    created_at=Column(DateTime);updated_at=Column(DateTime);raw_bytes=Column(LargeBinary)

def make_session(url="sqlite:///forensight.db"):
    eng = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})
    Base.metadata.create_all(eng)
    # Additive migration for prototype SQLite/PostgreSQL databases created before richer provenance fields.
    with eng.begin() as conn:
        cols = {c["name"] for c in __import__("sqlalchemy").inspect(conn).get_columns("evidence")}
        for col, sql in (("device_id", "VARCHAR"), ("original_id", "VARCHAR"), ("original_timestamp", "VARCHAR"),
                         ("timezone", "VARCHAR"), ("timestamp_precision", "VARCHAR")):
            if col not in cols: conn.execute(text(f"ALTER TABLE evidence ADD COLUMN {col} {sql}"))
        file_cols={c["name"] for c in __import__("sqlalchemy").inspect(conn).get_columns("imported_files")}
        if "raw_bytes" not in file_cols: conn.execute(text("ALTER TABLE imported_files ADD COLUMN raw_bytes BLOB"))
        if "parser_name" not in file_cols: conn.execute(text("ALTER TABLE imported_files ADD COLUMN parser_name VARCHAR"))
        if "parser_version" not in file_cols: conn.execute(text("ALTER TABLE imported_files ADD COLUMN parser_version VARCHAR"))
        case_cols={c["name"] for c in __import__("sqlalchemy").inspect(conn).get_columns("cases")}
        for col,sql in (("description","TEXT"),("incident_type","VARCHAR"),("incident_location","VARCHAR"),("incident_start","VARCHAR"),
                        ("incident_end","VARCHAR"),("timezone","VARCHAR"),("status","VARCHAR"),("created_by","VARCHAR"),("updated_at","DATETIME")):
            if col not in case_cols:conn.execute(text(f"ALTER TABLE cases ADD COLUMN {col} {sql}"))
    return sessionmaker(bind=eng)()

def _h(b: bytes): return hashlib.sha256(b).hexdigest()
def _t(s): return datetime.fromisoformat(s)

def ingest_dir(db, case_id: str, folder: Path):
    folder = folder.resolve(); n = 0; now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.query(Evidence).filter_by(case_id=case_id).delete(); db.query(Entity).filter_by(case_id=case_id).delete()
    dev2name = {}
    def add(typ, ts, src, art, actor, target, summ, meta, lat=None, lon=None, fh=""):
        nonlocal n; n += 1
        canon = json.dumps([typ, ts.isoformat(), actor, target, summ, lat, lon], sort_keys=True)
        db.add(Evidence(evidence_id=f"E-{n:04d}", case_id=case_id, type=typ, ts=ts, source_file=src, source_artifact=art,
                        actor=actor, target=target, summary=summ, lat=lat, lon=lon, sha256=_h(canon.encode()),
                        source_file_sha256=fh, ingested_at=now, meta=json.dumps(meta)))
    # contacts + browser + app events (XML)
    p = folder / "report.xml"; raw = p.read_bytes(); fh = _h(raw); root = ET.fromstring(raw)
    for c in root.iter("contact"):
        db.add(Entity(case_id=case_id, phone=c.get("number"), name=c.get("name"), device=c.get("device")))
        if c.get("device"): dev2name[c.get("device")] = c.get("name")
    for b in root.iter("browser"):
        add("browser", _t(b.get("time")), p.name, "browser_history", dev2name.get(b.get("device"), b.get("device")), None, b.get("title"), dict(b.attrib), fh=fh)
    for a in root.iter("appevent"):
        add("app", _t(a.get("time")), p.name, "app_activity", dev2name.get(a.get("device"), a.get("device")), a.get("package"), a.get("activity"), dict(a.attrib), fh=fh)
    p = folder / "messages.csv"; raw = p.read_bytes(); fh = _h(raw)
    for row in csv.DictReader(raw.decode().splitlines()):
        add("message", _t(row["timestamp"]), p.name, row["app"], row["sender"], row["receiver"], row["text"], row, fh=fh)
    p = folder / "calls.json"; raw = p.read_bytes(); fh = _h(raw)
    for c in json.loads(raw):
        add("call", _t(c["time"]), p.name, "call_log", c["caller"], c["receiver"], f'{c["type"]} call, {c["duration"]}s', c, fh=fh)
    p = folder / "locations.json"; raw = p.read_bytes(); fh = _h(raw)
    for l in json.loads(raw):
        add("location", _t(l["time"]), p.name, "gps_cache", dev2name.get(l["device"], l["device"]), None, f'GPS fix ±{l["accuracy"]}m', l, l["lat"], l["lon"], fh=fh)
    db.commit(); return n


def ingest_upload(db, case_id: str, filename: str, raw: bytes):
    """Add a supported evidence export without replacing existing case records."""
    filename = Path(filename).name
    suffix = Path(filename).suffix.lower()
    if suffix not in {".csv", ".json", ".xml"}:
        raise ValueError("Upload a CSV, JSON, or XML evidence export.")
    if not raw or len(raw) > 5 * 1024 * 1024:
        raise ValueError("The report must be non-empty and no larger than 5 MB.")
    try:
        text_data = raw.decode("utf-8-sig")
        source_hash = _h(raw)
        parsed = []
        contacts = []
        if suffix == ".csv":
            records = csv.DictReader(io.StringIO(text_data))
            required = {"timestamp", "sender", "receiver", "text"}
            if not records.fieldnames or not required.issubset({h.strip().lower() for h in records.fieldnames}):
                raise ValueError("CSV must contain timestamp, sender, receiver, and text columns.")
            for item in records:
                item = {str(k).strip().lower(): v for k, v in item.items() if k}
                parsed.append(("message", item["timestamp"], item["sender"], item["receiver"], item["text"],
                               {"app": item.get("app", "unknown")}, None, None, item.get("app", "messages")))
        elif suffix == ".json":
            records = json.loads(text_data)
            if not isinstance(records, list):
                raise ValueError("JSON evidence exports must contain an array of records.")
            for item in records:
                if not isinstance(item, dict): raise ValueError("Each JSON record must be an object.")
                if {"caller", "receiver", "time"}.issubset(item):
                    parsed.append(("call", item["time"], item["caller"], item["receiver"],
                                   f'{item.get("type", "unknown")} call, {item.get("duration", 0)}s', item,
                                   None, None, "call_log"))
                elif {"lat", "lon", "time"}.issubset(item):
                    parsed.append(("location", item["time"], item.get("device", "Unknown"), None,
                                   f'GPS fix ±{item.get("accuracy", "unknown")}m', item, float(item["lat"]),
                                   float(item["lon"]), "gps_cache"))
                else:
                    raise ValueError("JSON rows must be call records or GPS records with their documented fields.")
        else:
            root = ET.fromstring(text_data)
            for c in root.iter("contact"):
                if c.get("number"):
                    contacts.append((c.get("number"), c.get("name", "Unknown"), c.get("device")))
            dev2name = {device: name for _, name, device in contacts if device}
            for item in root.iter("browser"):
                parsed.append(("browser", item.get("time"), dev2name.get(item.get("device"), item.get("device")),
                               None, item.get("title", "Browser activity"), dict(item.attrib), None, None, "browser_history"))
            for item in root.iter("appevent"):
                parsed.append(("app", item.get("time"), dev2name.get(item.get("device"), item.get("device")),
                               item.get("package"), item.get("activity", "App activity"), dict(item.attrib), None, None, "app_activity"))
            if not contacts and not parsed:
                raise ValueError("XML report contains no supported contact, browser, or app-event records.")
        if len(parsed) > 10000:
            raise ValueError("A report may contain at most 10,000 evidence records.")
    except (UnicodeDecodeError, json.JSONDecodeError, ET.ParseError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc): raise
        raise ValueError("The uploaded report is malformed or has unsupported fields.") from exc

    existing_contacts = {e.phone for e in db.query(Entity).filter_by(case_id=case_id)}
    contacts_added = 0
    for number, name, device in contacts:
        if number not in existing_contacts:
            db.add(Entity(case_id=case_id, phone=number, name=name, device=device))
            existing_contacts.add(number)
            contacts_added += 1
    db.flush()
    entity_rows = db.query(Entity).filter_by(case_id=case_id).all()
    names = {e.phone: e.name for e in entity_rows}
    devices = {e.device: e.name for e in entity_rows if e.device}
    existing_hashes = {h for (h,) in db.query(Evidence.sha256).filter_by(case_id=case_id)}
    ids = [e.evidence_id for e in db.query(Evidence.evidence_id).filter_by(case_id=case_id)]
    sequence = max([int(i[2:]) for i in ids if i.startswith("E-") and i[2:].isdigit()] or [0])
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    added = skipped = 0
    for typ, timestamp, actor, target, summary, meta, lat, lon, artifact in parsed:
        if not timestamp or not actor:
            skipped += 1
            continue
        ts = _t(timestamp)
        if typ == "location": actor = devices.get(actor, names.get(actor, actor))
        canon = json.dumps([typ, ts.isoformat(), actor, target, summary, lat, lon], sort_keys=True)
        digest = _h(canon.encode())
        if digest in existing_hashes:
            skipped += 1
            continue
        sequence += 1
        db.add(Evidence(evidence_id=f"E-{sequence:04d}", case_id=case_id, type=typ, ts=ts,
                        source_file=filename, source_artifact=artifact, actor=actor, target=target,
                        summary=summary, lat=lat, lon=lon, sha256=digest, source_file_sha256=source_hash,
                        ingested_at=now, meta=json.dumps(meta)))
        existing_hashes.add(digest)
        added += 1
    db.commit()
    return {"added": added, "skipped": skipped, "contacts_added": contacts_added}


# Dataset ingestion ---------------------------------------------------------
MAX_UPLOAD = 50 * 1024 * 1024
MAX_EXPANDED = 250 * 1024 * 1024
MAX_FILES = 2000
MAX_RECORDS = 250000
MAX_ZIP_DEPTH = 3
PARSER_REGISTRY=ParserRegistry()
ALIASES = {
    "timestamp": ("timestamp", "time", "datetime", "date_time", "created_at", "event_time"),
    "contact": ("contact", "person", "participant", "name", "phone", "number", "sender", "caller", "actor", "user"),
    "latitude": ("latitude", "lat"), "longitude": ("longitude", "lon", "lng", "long"),
    "device": ("device_id", "device", "deviceid"), "content": ("content", "message", "text", "body", "summary", "title", "activity", "event", "query", "url", "details", "application", "package", "action", "filename", "path"),
    "source": ("source", "source_file", "origin"), "type": ("type", "evidence_type", "event_type", "record_type", "artifact_type"),
}

def _key(k): return re.sub(r"[^a-z0-9]", "", str(k).casefold())
def _get(record, name, default=None):
    aliases = {_key(x) for x in ALIASES[name]}
    return next((v for k, v in record.items() if _key(k) in aliases and v not in (None, "")), default)

def _parse_ts(value, case_timezone="Asia/Kolkata"):
    if value in (None, ""): return None
    if isinstance(value, (int, float)):
        # Accept seconds and millisecond Unix timestamps.
        return datetime.fromtimestamp(value / (1000 if value > 10_000_000_000 else 1), timezone.utc).astimezone(case_zone(case_timezone)).replace(tzinfo=None)
    value = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    return dt.astimezone(case_zone(case_timezone)).replace(tzinfo=None) if dt.tzinfo else dt

def backfill_timestamp_provenance(db):
    """Add explicit source timestamp and case-zone metadata without changing legacy ts values."""
    cases={c.case_id:json.loads(c.metadata_json or "{}") for c in db.query(CaseRecord).all()}
    changed=0
    for e in db.query(Evidence).filter(Evidence.original_timestamp.is_(None)).yield_per(500):
        meta=json.loads(e.meta or "{}");raw=meta.get("raw_record") or {}
        original=next((str(v) for k,v in raw.items() if _key(k) in {_key(x) for x in ALIASES["timestamp"]} and v not in (None,"")),None)
        case_meta=cases.get(e.case_id,{})
        device=case_meta.get("device") or {}
        zone=str(device.get("timezone") or case_meta.get("timezone") or "Asia/Kolkata")
        e.original_timestamp=original or (e.ts.isoformat() if e.ts else None)
        e.timezone=zone
        e.timestamp_precision=_precision(e.original_timestamp)
        changed+=1
    if changed: db.commit()
    return changed

def _records_from_json(value):
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict): yield item
            elif isinstance(item, list): yield from _records_from_json(item)
        return
    if not isinstance(value, dict): return
    for key in ("records", "evidence", "items", "events", "artifacts", "messages", "calls", "locations", "data"):
        if isinstance(value.get(key), (list, dict)):
            found = list(_records_from_json(value[key]))
            if found:
                yield from found
                return
    signal_keys = {_key(k) for k in value}
    record_keys = {_key(k) for aliases in ALIASES.values() for k in aliases}
    if signal_keys & record_keys:
        yield value
    else:
        for child in value.values():
            if isinstance(child, (list, dict)): yield from _records_from_json(child)

def _decode(raw):
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try: return raw.decode(encoding), encoding
        except UnicodeDecodeError: pass
    raise ValueError("Text encoding could not be recognized")

def _safe_xml(data):
    if re.search(br"<!\s*(DOCTYPE|ENTITY)\b",data,re.I):
        raise ValueError("XML DTD and entity declarations are not allowed")
    try:return SafeET.fromstring(data)
    except DefusedXmlException as exc:raise ValueError("Unsafe XML entity declaration") from exc

def _xml_text(node):
    return "".join(node.itertext()).strip()

def _xlsx_records(data):
    try: archive=zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc: raise ValueError("Invalid XLSX archive") from exc
    with archive:
        names=set(archive.namelist())
        if "xl/workbook.xml" not in names: raise ValueError("ZIP file is not a valid XLSX workbook")
        workbook=_safe_xml(archive.read("xl/workbook.xml"));rels={}
        if "xl/_rels/workbook.xml.rels" in names:
            relroot=_safe_xml(archive.read("xl/_rels/workbook.xml.rels"))
            rels={x.attrib.get("Id"):x.attrib.get("Target","") for x in relroot}
        shared=[]
        if "xl/sharedStrings.xml" in names:
            sharedroot=_safe_xml(archive.read("xl/sharedStrings.xml"));shared=[_xml_text(x) for x in sharedroot]
        rows=[];errors=[]
        for sheet in workbook.iter():
            if sheet.tag.rsplit("}",1)[-1]!="sheet": continue
            sheet_name=sheet.attrib.get("name","Sheet")
            relation=next((v for k,v in sheet.attrib.items() if k.rsplit("}",1)[-1]=="id"),None)
            target=rels.get(relation,"")
            if target.startswith("/"): path=target.lstrip("/")
            elif target.startswith("xl/"): path=target
            else: path="xl/"+target
            if path not in names: errors.append({"sheet":sheet_name,"error":"Worksheet relationship target is missing"});continue
            root=_safe_xml(archive.read(path));sheet_rows=[]
            for row_node in root.iter():
                if row_node.tag.rsplit("}",1)[-1]!="row":continue
                cells={}
                for cell in row_node:
                    if cell.tag.rsplit("}",1)[-1]!="c":continue
                    ref=cell.attrib.get("r","");match=re.match(r"([A-Z]+)",ref.upper())
                    if not match:continue
                    col=0
                    for ch in match.group(1):col=col*26+ord(ch)-64
                    value=next((x.text or "" for x in cell if x.tag.rsplit("}",1)[-1]=="v"),"")
                    kind=cell.attrib.get("t")
                    if kind=="s" and value:
                        try:value=shared[int(value)]
                        except (ValueError,IndexError):errors.append({"sheet":sheet_name,"row":row_node.attrib.get("r"),"error":"Invalid shared-string reference"})
                    elif kind=="inlineStr":value=_xml_text(cell)
                    cells[col]=value
                if cells:sheet_rows.append((row_node.attrib.get("r"),cells))
            if not sheet_rows:continue
            headers=[str(sheet_rows[0][1].get(i,"")).strip() or f"column_{i}" for i in range(1,max(sheet_rows[0][1])+1)]
            for row_number,cells in sheet_rows[1:]:
                record={headers[i-1]:cells.get(i,"") for i in range(1,len(headers)+1)}
                record["sheet_name"]=sheet_name;record["original_row"]=row_number;record["original_headers"]=headers
                rows.append(record)
        if not rows and not errors:errors.append({"error":"Workbook contains no data rows"})
        return rows,{"errors":errors,"detected_rows":len(rows)}

class _HtmlExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.text=[];self.title=[];self.metadata=[];self.tables=[];self.table=None;self.row=None;self.cell=None;self.skip=0
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag in ("script","style","noscript"):self.skip+=1
        if tag=="meta":self.metadata.append({k:v for k,v in attrs.items() if k in ("name","property","content")})
        if tag=="table":self.table=[]
        if tag=="tr" and self.table is not None:self.row=[]
        if tag in ("td","th") and self.row is not None:self.cell=[]
        if tag=="a" and attrs.get("href"):self.text.append(" "+attrs["href"]+" ")
    def handle_endtag(self,tag):
        if tag in ("script","style","noscript") and self.skip:self.skip-=1
        if tag in ("td","th") and self.cell is not None:
            self.row.append(" ".join(" ".join(self.cell).split()));self.cell=None
        if tag=="tr" and self.row is not None:
            if any(self.row):self.table.append(self.row)
            self.row=None
        if tag=="table" and self.table is not None:
            self.tables.append(self.table);self.table=None
    def handle_data(self,data):
        if self.skip:return
        value=data.strip()
        if not value:return
        self.text.append(value)
        if self.cell is not None:self.cell.append(value)

def _html_records(data):
    text_data,encoding=_decode(data);parser=_HtmlExtractor()
    try:parser.feed(text_data);parser.close()
    except Exception as exc:raise ValueError(f"Malformed HTML: {exc}") from exc
    plain=" ".join(" ".join(parser.text).split());title=plain[:160]
    rows=[{"type":"file","content":plain or "HTML document contained no readable text","title":title,
           "html_metadata":parser.metadata,"html_tables":parser.tables,"original_html":text_data}]
    for table_index,table in enumerate(parser.tables,1):
        if not table:continue
        headers=[v or f"column_{i+1}" for i,v in enumerate(table[0])]
        for row_index,values in enumerate(table[1:],2):
            record={headers[i]:values[i] if i<len(values) else "" for i in range(len(headers))}
            record.update({"type":"other","source_table":table_index,"source_row":row_index,"original_headers":headers})
            rows.append(record)
    return rows,[],{"encoding":encoding,"detected_rows":len(rows)}

def _pdf_records(data):
    if not data.startswith(b"%PDF-"):raise ValueError("File does not have a PDF signature")
    try: from pypdf import PdfReader
    except ImportError as exc:raise ValueError("PDF parsing unavailable; install the pypdf dependency") from exc
    try:reader=PdfReader(io.BytesIO(data),strict=False)
    except Exception as exc:raise ValueError(f"Could not read PDF: {exc}") from exc
    rows=[];errors=[]
    for index,page in enumerate(reader.pages,1):
        try:content=(page.extract_text() or "").strip()
        except Exception as exc:content="";errors.append({"page":index,"error":f"Text extraction failed: {exc}"})
        rows.append({"type":"file","content":content or "OCR required — page has no extractable text","page_number":index,
                     "ocr_required":not bool(content),"pdf_page_text":content})
    if not rows:errors.append({"error":"PDF contains no pages"})
    return rows,[],{"pages":len(reader.pages),"detected_rows":len(rows),"errors":errors}

def _safe_archive_members(raw, archive_name, depth, budget):
    if depth > MAX_ZIP_DEPTH: raise ValueError("Nested ZIP depth limit exceeded")
    try: zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc: raise ValueError(f"{archive_name}: invalid ZIP archive") from exc
    out = []
    with zf:
        for info in zf.infolist():
            if info.is_dir(): continue
            p = PurePosixPath(info.filename.replace("\\", "/"))
            mode = info.external_attr >> 16
            if p.is_absolute() or ".." in p.parts or (mode & 0o170000) == 0o120000:
                budget["skipped"].append({"file": f"{archive_name}/{info.filename}", "status":"unsupported", "reason": "Unsafe archive path — skipped"})
                continue
            if info.file_size > MAX_EXPANDED or budget["expanded"] + info.file_size > MAX_EXPANDED:
                raise ValueError("Expanded dataset exceeds the 250 MB safety limit")
            budget["expanded"] += info.file_size
            if len(out) + len(budget["files"]) >= MAX_FILES: raise ValueError("Dataset contains more than 2,000 files")
            data = zf.read(info)
            name = f"{archive_name}/{p.as_posix()}"
            if Path(p.name).suffix.lower() == ".zip":
                _safe_archive_members(data, name, depth + 1, budget)
            else:
                budget["files"].append((name, p.name, data))
    return out

def _discover(filename, raw):
    if not raw or len(raw) > MAX_UPLOAD: raise ValueError("Upload must be non-empty and at most 50 MB")
    budget = {"expanded": 0, "files": [], "skipped": []}
    if Path(filename).suffix.lower() == ".zip": _safe_archive_members(raw, Path(filename).name, 1, budget)
    else: budget["files"].append((Path(filename).name, Path(filename).name, raw))
    return budget

def _xml_records(data):
    text_data, _ = _decode(data)
    root = _safe_xml(text_data.encode("utf-8"))
    contacts = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1].casefold()
        if tag in {"contact", "person", "entity"}:
            rec = {**el.attrib, **{c.tag.rsplit("}", 1)[-1]: c.text for c in el if c.text}}
            number = _get(rec, "contact")
            if number: contacts.append((str(number), str(rec.get("name") or number), _get(rec, "device")))
    entries = []
    record_tags = {"record", "evidence", "message", "sms", "mms", "call", "calllog", "location", "gps", "gpsfix", "browser", "browserhistory", "appevent", "app_event", "applicationevent", "event", "artifact", "item"}
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1].casefold()
        rec = {**el.attrib, **{c.tag.rsplit("}", 1)[-1]: c.text for c in el if c.text}}
        if tag in record_tags and rec and (_get(rec, "timestamp") is not None or _get(rec, "content") is not None):
            rec.setdefault("type",tag);entries.append(rec)
    if not entries and len(contacts) == 0: raise ValueError("XML contains no recognizable evidence records")
    return entries, contacts

def _parse_builtin_file(display_name, basename, data):
    suffix = Path(basename).suffix.lower()
    if suffix not in SUPPORTED: return [], [], None
    lowered = basename.casefold()
    if suffix == ".json":
        text_data, enc = _decode(data); parsed = json.loads(text_data)
        record_container=any(k in parsed and isinstance(parsed[k],(list,dict)) for k in ("records","evidence","items","events","artifacts","messages","calls","locations","data")) if isinstance(parsed,dict) else False
        if isinstance(parsed, dict) and (Path(basename).name.casefold() in {"case_metadata.json","evidence_manifest.json"} or (any(k in parsed for k in ("case_id", "case_name", "incident", "geofence")) and not record_container)):
            return [], [], {"metadata": parsed, "encoding": enc}
        return list(_records_from_json(parsed)), [], {"encoding": enc}
    if suffix == ".csv":
        text_data, enc = _decode(data); reader = csv.reader(io.StringIO(text_data, newline=""), strict=True)
        try: headers = next(reader)
        except StopIteration: return [], [], {"encoding": enc, "errors": ["Empty CSV"]}
        headers = [h.strip().lstrip("\ufeff") for h in headers]; rows=[]; errors=[]; detected_rows=0
        line_no=1
        while True:
            try: fields=next(reader); line_no=reader.line_num
            except StopIteration: break
            except csv.Error as exc:
                errors.append({"line":reader.line_num+1,"error":f"Malformed CSV row: {exc}"}); break
            detected_rows+=1
            if len(fields) != len(headers):
                errors.append({"line": line_no, "error": f"Expected {len(headers)} columns; found {len(fields)}"}); continue
            rows.append(dict(zip(headers, fields)))
        return rows, [], {"encoding": enc, "errors": errors,"detected_rows":detected_rows}
    if suffix == ".xml":
        rows, contacts = _xml_records(data); return rows, contacts, {"encoding": _decode(data)[1]}
    if suffix == ".xlsx":
        rows,info=_xlsx_records(data);return rows,[],info
    if suffix in {".html",".htm"}:return _html_records(data)
    if suffix == ".pdf":return _pdf_records(data)
    text_data, enc = _decode(data)
    # Text logs commonly contain JSON Lines; otherwise retain each non-empty timestamped line as an event.
    rows=[]; errors=[]
    for line_no, line in enumerate(text_data.splitlines(), 1):
        if not line.strip(): continue
        try:
            item=json.loads(line)
            if isinstance(item, dict): rows.append(item); continue
        except json.JSONDecodeError: pass
        match=re.match(r"^(\S+\s+\S+)\s+(.*)$", line)
        if match and re.match(r"\d{4}-\d\d-\d\d", match.group(1)):
            rows.append({"timestamp":match.group(1),"type":"system","content":match.group(2),"raw_line":line})
        else: errors.append({"line":line_no,"error":"Unrecognized text record"})
    return rows, [], {"encoding": enc, "errors": errors}

def _validate_pdf(data):
    if not data.startswith(b"%PDF-"):raise ValueError("File does not have a PDF signature")

_BUILTIN_PARSER_SPECS=(
    ("CSV evidence export",(".csv",),("text/csv",)),
    ("JSON evidence export",(".json",),("application/json",)),
    ("XML evidence export",(".xml",),("application/xml","text/xml")),
    ("Text evidence export",(".txt",),("text/plain",)),
    ("Excel workbook",(".xlsx",),("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",)),
    ("HTML evidence export",(".html",".htm"),("text/html",)),
    ("PDF evidence export",(".pdf",),("application/pdf",)),
)
for parser_name,extensions,mime_types in _BUILTIN_PARSER_SPECS:
    PARSER_REGISTRY.register(ParserSpec(parser_name,extensions,mime_types,_parse_builtin_file,
                                       validate=_validate_pdf if ".pdf" in extensions else None))
SUPPORTED=set(PARSER_REGISTRY.supported_extensions())

def _parse_file(display_name,basename,data):
    return PARSER_REGISTRY.parse_file(display_name,basename,data)

def _classify(record, basename):
    val = str(_get(record,"type", "")).casefold().replace("_", " ")
    if any(x in val for x in ("message", "sms", "chat")): return "message"
    if "call" in val: return "call"
    if any(x in val for x in ("gps", "location", "coordinate")): return "location"
    if "browser" in val or "web" in val: return "browser"
    if "app" in val: return "app"
    if "system" in val: return "system"
    if "file" in val or "document" in val: return "file"
    if _get(record,"latitude") is not None and _get(record,"longitude") is not None: return "location"
    name=basename.casefold()
    for term, typ in (("message","message"),("call","call"),("gps","location"),("location","location"),("browser","browser"),("app","app")):
        if term in name: return typ
    return "other"

def _precision(value):
    if value in (None, ""): return None
    value=str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}",value): return "date"
    if re.search(r"\.\d+",value): return "fractional_second"
    if re.search(r"T\d{2}:\d{2}:\d{2}",value): return "second"
    if re.search(r"T\d{2}:\d{2}",value): return "minute"
    return "unknown"

def _normalize(record, basename, case_timezone="Asia/Kolkata"):
    typ=_classify(record,basename); original_timestamp=_get(record,"timestamp")
    timestamp=_parse_ts(original_timestamp,case_timezone)
    actor=_get(record,"contact")
    device=_get(record,"device")
    direction=str(record.get("direction", "")).casefold()
    if actor is not None and direction in {"outgoing", "outbound", "sent", "out"}:
        actor=device or record.get("owner") or "Device owner"; target=None
        target=str(_get(record,"contact"))
    elif actor is not None and direction in {"incoming", "inbound", "received", "in"}:
        target=device or record.get("owner"); actor=str(_get(record,"contact"))
    else: target=None
    if _key("sender") in {_key(k) for k in record}: actor=record.get(next(k for k in record if _key(k)==_key("sender"))) or actor
    if _key("caller") in {_key(k) for k in record}: actor=record.get(next(k for k in record if _key(k)==_key("caller"))) or actor
    target=target or next((v for k,v in record.items() if _key(k) in {_key(x) for x in ("receiver","recipient","to","target","callee")} and v not in (None,"")),None)
    if target is None and actor == _get(record,"contact"):
        target=next((v for k,v in record.items() if _key(k) in {_key(x) for x in ("contact","participant","phone","number")} and v not in (None,"",actor)),None)
    if typ == "location": actor=device or actor or "Unknown"
    elif actor is None: actor=device or "Unknown"
    content=_get(record,"content", "")
    if typ == "call" and not content: content=f"{record.get('direction','')} call, {record.get('duration_seconds',record.get('duration',''))}s".strip(" ,")
    if typ == "location" and not content: content=f"GPS fix ±{record.get('accuracy_m',record.get('accuracy','unknown'))}m"
    lat=_get(record,"latitude"); lon=_get(record,"longitude")
    return {"type":typ,"timestamp":timestamp,"actor":str(actor) if actor is not None else "Unknown","target":str(target) if target is not None else None,
            "summary":str(content),"lat":float(lat) if lat not in (None,"") else None,"lon":float(lon) if lon not in (None,"") else None,
            "device_id":str(device) if device is not None else None,
            "original_timestamp":str(original_timestamp) if original_timestamp not in (None,"") else None,
            "timezone":case_timezone,"timestamp_precision":_precision(original_timestamp),
            "source_artifact":str(_get(record,"source", basename)),"original_id":str(record.get("evidence_id") or record.get("id") or "") or None,
            "raw_record":record}

def ingest_dataset(db, filename: str, raw: bytes, requested_case_id: str | None = None, merge_existing: bool = False, progress=None):
    """Safely discover, normalize, and store all evidence in a ZIP or single supported file."""
    discovered=_discover(filename,raw); files=discovered["files"]; ancillary=[]; supported=[]; skipped=list(discovered["skipped"])
    for display, basename, data in files:
        if Path(basename).suffix.lower() in SUPPORTED: supported.append((display,basename,data))
        else: skipped.append({"file":display,"status":"unsupported","reason":"Unsupported file — skipped"})
    files_total=len(supported);files_completed=0
    if progress:progress("detect",files_completed,files_total,0,0)
    file_results=[]; all_rows=[]; contacts=[]; case_metadata={}; files_discovered=0; detected_types=Counter()
    for display,basename,data in supported:
        is_aux=basename.casefold() in {"readme.txt","evidence_manifest.json"}
        try: rows, found_contacts, info=_parse_file(display,basename,data)
        except (ValueError, UnicodeError, json.JSONDecodeError, ET.ParseError, csv.Error) as exc:
            files_discovered+=int(basename.casefold() not in {"readme.txt","evidence_manifest.json"})
            file_results.append({"file":display,"detected":0,"parsed":0,"status":"failed","errors":[{"error":str(exc)}]});files_completed+=1
            if progress:progress("parse",files_completed,files_total,len(all_rows),0)
            continue
        if info and "metadata" in info:
            case_metadata.update(info["metadata"]); is_aux=True
        if is_aux:
            if basename.casefold() == "case_metadata.json": files_discovered += 1
            ancillary.append((display,basename,data,info));files_completed+=1
            if progress:progress("parse",files_completed,files_total,len(all_rows),0)
            continue
        files_discovered+=1; contacts.extend(found_contacts)
        for index,record in enumerate(rows):
            try:
                detected_types[_classify(record,basename)]+=1
                device_metadata=case_metadata.get("device") or {}
                case_timezone=str(device_metadata.get("timezone") or case_metadata.get("timezone") or "Asia/Kolkata")
                normalized=_normalize(record,basename,case_timezone)
                parser_spec=PARSER_REGISTRY.find(basename,data)
                normalized["parser"]=parser_spec.name if parser_spec else None;normalized["parser_version"]="1.0" if parser_spec else None
                if _get(record,"timestamp") not in (None, "") and not normalized["timestamp"]: raise ValueError("Invalid timestamp")
                if not normalized["timestamp"] and not (normalized["summary"] or _get(record,"type")):
                    raise ValueError("Record has no timestamp, type, or content")
                all_rows.append((display,basename,data,normalized))
                if len(all_rows)>MAX_RECORDS: raise ValueError("Dataset exceeds the 250,000 record limit")
            except (ValueError, TypeError, OverflowError) as exc:
                file_results.append({"file":display,"line":index+1,"error":str(exc)})
        undetailed=max(0,int((info or {}).get("detected_rows",len(rows)))-len(rows))
        if undetailed: detected_types[_classify({},basename)]+=undetailed
        parsed_count=sum(1 for x in all_rows if x[0]==display);detected_count=(info or {}).get("detected_rows",len(rows))
        parse_errors=(info or {}).get("errors",[]);record_errors=[x for x in file_results if x.get("file")==display and "error" in x]
        failed_records=max(0,detected_count-parsed_count)
        status="failed" if not parsed_count and (parse_errors or record_errors or detected_count) else "partially_processed" if parse_errors or record_errors else "processed"
        file_results.append({"file":display,"detected":detected_count,"parsed":parsed_count,"failed":failed_records,"status":status,"errors":parse_errors})
        files_completed+=1
        if progress:progress("parse",files_completed,files_total,len(all_rows),sum(int(x.get("detected",0)) for x in file_results))
    candidate=str(requested_case_id or case_metadata.get("case_id") or f"CASE-{datetime.now():%Y%m%d-%H%M%S}").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}",candidate): raise ValueError("Case ID may contain letters, numbers, dot, underscore, and hyphen")
    existing_case=db.query(CaseRecord).filter_by(case_id=candidate).first()
    if existing_case and db.query(Evidence).filter_by(case_id=candidate).count() and not merge_existing:
        # Re-import creates a separate case ID to avoid replacing or merging evidence.
        base=candidate; suffix=2
        while db.query(CaseRecord).filter_by(case_id=f"{base}-{suffix}").first(): suffix+=1
        candidate=f"{base}-{suffix}"
    now=datetime.now(timezone.utc).replace(tzinfo=None); source_hashes={}; counts=Counter(); imported=0; errors=[]
    if progress:progress("normalize",files_completed,files_total,len(all_rows),sum(int(x.get("detected",0)) for x in file_results))
    if progress:progress("store",files_completed,files_total,0,len(all_rows))
    used_ids={x for (x,) in db.query(Evidence.evidence_id).all()}
    next_id=1
    for display,basename,data,n in all_rows:
        original=n["original_id"]
        eid=original or f"{candidate}-E-{next_id:06d}"; next_id+=1
        if eid in used_ids:
            stem=f"{candidate}-{original or 'E-'+str(next_id-1).zfill(6)}"; eid=stem; dup=2
            while eid in used_ids: eid=f"{stem}-{dup:03d}"; dup+=1
        used_ids.add(eid)
        fh=source_hashes.setdefault(display,_h(data))
        meta={"raw_record":n["raw_record"],"normalized_record":{"type":n["type"],"timestamp":n["timestamp"].isoformat() if n["timestamp"] else None,
              "actor":n["actor"],"target":n["target"],"summary":n["summary"],"lat":n["lat"],"lon":n["lon"],"device_id":n["device_id"]},
              "source_type":n["type"],"source_filename":display,"parser":n["parser"],"parser_version":n["parser_version"]}
        canonical=json.dumps(n["raw_record"],sort_keys=True,ensure_ascii=False,default=str).encode()
        db.add(Evidence(evidence_id=eid,original_id=original,case_id=candidate,type=n["type"],ts=n["timestamp"],source_file=display,
                        source_artifact=n["source_artifact"],actor=n["actor"],target=n["target"],summary=n["summary"],lat=n["lat"],lon=n["lon"],device_id=n["device_id"],
                        sha256=_h(canonical),source_file_sha256=fh,ingested_at=now,meta=json.dumps(meta,ensure_ascii=False,default=str),
                        original_timestamp=n["original_timestamp"],timezone=n["timezone"],timestamp_precision=n["timestamp_precision"]))
        counts[n["type"]]+=1; imported+=1
    detected=sum(x["detected"] for x in file_results if "detected" in x)
    failures=[x for x in file_results if "error" in x]
    warnings=[{"file":x["file"],**(e if isinstance(e,dict) else {"error":str(e)})} for x in file_results if "errors" in x for e in x["errors"]]
    for display,basename,data,info in ancillary:
        file_results.append({"file":display,"detected":0,"parsed":0,"failed":0,"status":"processed","kind":"metadata"})
    name=str(case_metadata.get("case_name") or (existing_case.name if merge_existing and existing_case else candidate))
    outward_types={"gps":"location","application":"app"}
    display_counts={outward_types.get(t,t):v for t,v in counts.items()}
    display_counts={"gps" if k=="location" else "application" if k=="app" else k:v for k,v in display_counts.items()}
    detected_out={"gps" if k=="location" else "application" if k=="app" else k:v for k,v in detected_types.items()}
    failed_count=max(detected-imported,len(failures),len(warnings))
    file_statuses=[x.get("status") for x in file_results]
    processing_status="failed" if file_statuses and all(x=="failed" for x in file_statuses) else "partially_processed" if failed_count or skipped or any(x in {"failed","partially_processed"} for x in file_statuses) else "processed"
    processed_discovered=sum(x.get("status") in {"processed","partially_processed"} and (x.get("kind")!="metadata" or Path(x["file"]).name.casefold()=="case_metadata.json") for x in file_results)
    manifest={"case_id":candidate,"case_name":name,"status":processing_status,"files_discovered":files_discovered,"files_processed":processed_discovered,
              "files_skipped":len(skipped),"skipped_files":skipped,"records_detected":detected,"records_imported":imported,
              "records_failed":failed_count,"by_type":display_counts,"by_type_detected":detected_out,"files":file_results,"errors":failures,"warnings":warnings,
              "file_hashes":[{"filename":fn,"sha256":h,"size":len(data)} for fn, h in ((x[0],_h(x[2])) for x in files)],
              "original_archive":{"filename":Path(filename).name,"sha256":_h(raw),"size":len(raw)} if Path(filename).suffix.lower()==".zip" else None,
              "ancillary_files":[{"file":x[0],"sha256":_h(x[2]),"size":len(x[2])} for x in ancillary]}
    record=db.query(CaseRecord).filter_by(case_id=candidate).first()
    if record is None: record=CaseRecord(case_id=candidate,name=name,created_at=now,created_by=os.getenv("FORENSIGHT_USER","local")); db.add(record)
    record.name=name
    if case_metadata or not merge_existing:record.metadata_json=json.dumps(case_metadata,ensure_ascii=False,default=str)
    record.manifest_json=json.dumps(manifest,ensure_ascii=False,default=str);record.active=True;record.status="open";record.updated_at=now
    device_metadata=case_metadata.get("device") or {}
    if case_metadata:
        record.timezone=str(device_metadata.get("timezone") or case_metadata.get("timezone") or "Asia/Kolkata")
        incident=case_metadata.get("incident") or {}
        record.description=case_metadata.get("description") or record.description
        if isinstance(incident,dict):
            record.incident_type=incident.get("type")
            record.incident_location=incident.get("location")
            record.incident_start=incident.get("window_start")
            record.incident_end=incident.get("window_end")
    db.query(CaseRecord).filter(CaseRecord.case_id!=candidate).update({CaseRecord.active:False})
    known={e.phone for e in db.query(Entity).filter_by(case_id=candidate)}
    for number, contact_name, device_id in contacts:
        if number not in known:
            db.add(Entity(case_id=candidate,phone=number,name=contact_name,device=device_id));known.add(number)
    for display,basename,data in files:
        status="processed" if any(x[0]==display for x in supported) else "skipped"
        details=next((x for x in file_results if x.get("file")==display),{})
        detected_count=int(details.get("detected",0)); imported_count=int(details.get("parsed",0))
        failed_count=max(0,detected_count-imported_count,len(details.get("errors",[])))
        warning=json.dumps(details.get("errors",[]),ensure_ascii=False) if details.get("errors") else None
        fr=ImportedFile(case_id=candidate,filename=display,size=len(data),sha256=_h(data),imported_at=now,status=status,
                        records_detected=detected_count,records_imported=imported_count,records_failed=failed_count,warning=warning,raw_bytes=data,
                        parser_name=(PARSER_REGISTRY.find(basename,data).name if PARSER_REGISTRY.find(basename,data) else None),parser_version="1.0")
        db.add(fr)
    if Path(filename).suffix.lower()==".zip":
        db.add(ImportedFile(case_id=candidate,filename=Path(filename).name,size=len(raw),sha256=_h(raw),imported_at=now,
                            status="original_archive",records_detected=0,records_imported=0,records_failed=0,raw_bytes=raw,parser_name="ZIP archive processor",parser_version="1.0"))
    db.add(AuditEvent(timestamp=now,action="evidence.import",case_id=candidate,actor=os.getenv("FORENSIGHT_USER","local"),
                      metadata_json=json.dumps({"file_count":len(files),"records_detected":detected,"records_imported":imported,
                                                "records_failed":manifest["records_failed"],"file_hashes":manifest["file_hashes"]},ensure_ascii=False)))
    db.commit()
    if progress:progress("complete",files_completed,files_total,imported,imported)
    return manifest
