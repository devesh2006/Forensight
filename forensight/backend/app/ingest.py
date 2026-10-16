"""Ingestion: parse CSV/JSON/XML -> normalized evidence rows with SHA-256 + provenance."""
import csv, hashlib, json
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET  # stdlib parser; no external entities resolved
from sqlalchemy import create_engine, Column, Integer, String, Float, Text, DateTime, text
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()
class Evidence(Base):
    __tablename__ = "evidence"
    id = Column(Integer, primary_key=True); evidence_id = Column(String, unique=True, index=True)
    case_id = Column(String, index=True); type = Column(String, index=True); ts = Column(DateTime, index=True)
    source_file = Column(String); source_artifact = Column(String); actor = Column(String, index=True)
    target = Column(String, index=True); summary = Column(Text); lat = Column(Float); lon = Column(Float)
    sha256 = Column(String); source_file_sha256 = Column(String); ingested_at = Column(DateTime); meta = Column(Text)
class Entity(Base):
    __tablename__ = "entities"
    id = Column(Integer, primary_key=True); case_id = Column(String, index=True); phone = Column(String, index=True)
    name = Column(String); device = Column(String)

def make_session(url="sqlite:///forensight.db"):
    eng = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})
    Base.metadata.create_all(eng); return sessionmaker(bind=eng)()

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
