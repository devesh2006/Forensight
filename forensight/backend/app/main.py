"""ForenSight API and static prototype host."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
import hashlib, json, logging, os, re
import uuid
from email.parser import BytesParser
from email.policy import default as email_policy
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session, sessionmaker
from . import copilot, intel
from .ingest import AuditEvent, CaseRecord, Entity, Evidence, ImportJob, ImportedFile, backfill_timestamp_provenance, ingest_dataset, ingest_dir, ingest_upload, make_session
from .synth import INCIDENT, generate
from .timezones import case_zone

logger = logging.getLogger("forensight.api")
DATA = Path(os.getenv("NIGHTFALL_DATA_DIR", str(Path(__file__).resolve().parents[2] / "data" / "nightfall")))
CASE = "NIGHTFALL"
OLD_FORT = (28.6118, 77.2437)
_db = None

def engine():
    global _db
    if _db is None:
        url = os.getenv("DATABASE_URL")
        if not url and os.getenv("DEPLOYMENT_ENV") == "render":
            raise RuntimeError("DATABASE_URL must be configured for the hosted backend.")
        url = url or "sqlite:///forensight.db"
        if url.startswith("postgres://"): url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"): url = "postgresql+psycopg://" + url[len("postgresql://"):]
        _db = make_session(url)
    return _db.get_bind()

def db():
    s = sessionmaker(bind=engine())()
    try: yield s
    finally: s.close()

def initialize_database():
    engine()
    if not DATA.exists(): generate(DATA)
    s = sessionmaker(bind=engine())()
    try:
        if s.query(Evidence).filter_by(case_id=CASE).count() == 0: ingest_dir(s, CASE, DATA)
        if not s.query(CaseRecord).filter_by(case_id=CASE).first():
            s.add(CaseRecord(case_id=CASE,name=CASE,created_at=datetime.utcnow(),active=True))
        elif not s.query(CaseRecord).filter_by(active=True).first():
            s.query(CaseRecord).filter_by(case_id=CASE).update({CaseRecord.active:True})
        s.commit()
        backfill_timestamp_provenance(s)
    finally: s.close()

@asynccontextmanager
async def lifespan(_app):
    initialize_database()
    yield

app = FastAPI(title="ForenSight AI API", version="1.0.0", lifespan=lifespan)
origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:5173,http://localhost:8000,http://127.0.0.1:8000").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_origin_regex=os.getenv("CORS_ORIGIN_REGEX") or None,
                   allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Content-Type", "Authorization"])

class AskIn(BaseModel):
    query: str = Field(..., min_length=3, max_length=500)
    context: dict | None = None

class CaseCreate(BaseModel):
    case_id:str|None=Field(None,min_length=1,max_length=80)
    name:str=Field(...,min_length=1,max_length=160)
    description:str=""
    incident_type:str|None=None
    incident_location:str|None=None
    incident_start:str|None=None
    incident_end:str|None=None
    timezone:str="Asia/Kolkata"

@app.exception_handler(Exception)
async def unhandled_error(_request: Request, exc: Exception):
    logger.exception("Unhandled API error", exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "An unexpected server error occurred."})

def active_case(s):
    row=s.query(CaseRecord).filter_by(active=True).first()
    return row.case_id if row else CASE

def case_record(s, case_id):
    return s.query(CaseRecord).filter_by(case_id=case_id).first()

def audit(s,action,case_id,evidence_id=None,metadata=None):
    s.add(AuditEvent(timestamp=datetime.utcnow(),action=action,case_id=case_id,evidence_id=evidence_id,
                     actor=os.getenv("FORENSIGHT_USER","local"),metadata_json=json.dumps(metadata or {},ensure_ascii=False,default=str)))

def case_context(s, case_id=None):
    c=case_record(s,case_id or active_case(s)); metadata=json.loads(c.metadata_json or "{}") if c else {}
    incident=metadata.get("incident") or {}; incident_value=(incident.get("timestamp") or incident.get("time") or incident.get("datetime")) if isinstance(incident,dict) else incident
    try:
        if incident_value: incident_dt=datetime.fromisoformat(str(incident_value).replace("Z","+00:00")).replace(tzinfo=None)
        elif isinstance(incident,dict) and incident.get("window_start") and incident.get("window_end"):
            start=datetime.fromisoformat(str(incident["window_start"]).replace("Z","+00:00")).replace(tzinfo=None)
            end=datetime.fromisoformat(str(incident["window_end"]).replace("Z","+00:00")).replace(tzinfo=None)
            incident_dt=start+(end-start)/2
        elif isinstance(incident,dict) and incident.get("date"): incident_dt=datetime.fromisoformat(str(incident["date"]))
        else: incident_dt=INCIDENT
    except ValueError: incident_dt=INCIDENT
    geo=metadata.get("geofence") or {}; center=geo.get("center",geo) if isinstance(geo,dict) else {}
    try: reference=(float(center.get("latitude",center.get("lat",OLD_FORT[0]))),float(center.get("longitude",center.get("lon",center.get("lng",OLD_FORT[1])))))
    except (ValueError,TypeError): reference=OLD_FORT
    return metadata,incident_dt,reference

def row(e, nm):
    meta=json.loads(e.meta or "{}")
    public_type={"location":"gps","app":"application"}.get(e.type,e.type)
    zone=e.timezone or "Asia/Kolkata"
    normalized=e.ts.replace(tzinfo=case_zone(zone)).isoformat() if e.ts else None
    return {"id": e.evidence_id, "original_id":e.original_id, "case_id":e.case_id, "type": public_type, "time": normalized,
            "original_timestamp":e.original_timestamp,"normalized_timestamp":normalized,"timezone":zone,"timestamp_precision":e.timestamp_precision,
            "actor": nm.get(e.actor, e.actor),
            "target": nm.get(e.target, e.target), "summary": e.summary, "source": f"{e.source_file}:{e.source_artifact}",
            "source_file":e.source_file,"source_file_sha256":e.source_file_sha256,"source_record_id":e.original_id,"source_artifact":e.source_artifact,
            "device_id":e.device_id,"lat": e.lat, "lon": e.lon, "sha256": e.sha256, "metadata": meta,"raw_record":meta.get("raw_record")}

def names(s, case=None): return {x.phone: x.name for x in s.query(Entity).filter_by(case_id=case or active_case(s))}

@app.get("/api/health")
def health(s: Session = Depends(db)):
    case=active_case(s); evidence_count = s.query(Evidence).filter_by(case_id=case).count()
    database_type = s.get_bind().dialect.name
    loaded=bool(case_record(s,case) and evidence_count)
    mode="offline" if os.getenv("FORENSIGHT_MODE","offline").casefold()!="online" else "online"
    return {"status":"ok","backend":"connected","case":case if loaded else None,"active_case":case if loaded else None,
            "case_loaded":loaded,"evidence_count":evidence_count,"database":"connected","database_type":database_type,
            "analysis_status":"ready" if loaded else "waiting","mode":mode}

@app.get("/api/cases")
def cases(s: Session = Depends(db)):
    active=active_case(s)
    return {"active_case":active,"cases":[{"case_id":c.case_id,"name":c.name,"active":c.case_id==active,
            "evidence_count":s.query(Evidence).filter_by(case_id=c.case_id).count()} for c in s.query(CaseRecord).order_by(CaseRecord.created_at.desc())]}

@app.post("/api/cases")
def create_case(body:CaseCreate,s:Session=Depends(db)):
    case_id=body.case_id or f"FS-{datetime.utcnow():%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}",case_id):raise HTTPException(422,"Invalid case ID")
    if case_record(s,case_id):raise HTTPException(409,"Case ID already exists")
    if not body.name.strip():raise HTTPException(422,"Case name is required")
    try:case_zone(body.timezone)
    except ValueError as exc:raise HTTPException(422,str(exc)) from exc
    now=datetime.utcnow();metadata={"timezone":body.timezone,"incident":{"type":body.incident_type,"location":body.incident_location,
            "window_start":body.incident_start,"window_end":body.incident_end}}
    c=CaseRecord(case_id=case_id,name=body.name.strip(),description=body.description,incident_type=body.incident_type,
                 incident_location=body.incident_location,incident_start=body.incident_start,incident_end=body.incident_end,
                 timezone=body.timezone,status="open",created_by=os.getenv("FORENSIGHT_USER","local"),created_at=now,updated_at=now,
                 metadata_json=json.dumps(metadata,ensure_ascii=False))
    s.query(CaseRecord).update({CaseRecord.active:False});s.add(c);audit(s,"case.create",case_id,metadata={"name":body.name.strip()});s.commit()
    return {"case_id":case_id,"name":c.name,"status":c.status,"active":True,"timezone":c.timezone}

@app.get("/api/cases/{case_id}")
def get_case(case_id:str,s:Session=Depends(db)):
    c=case_record(s,case_id)
    if not c:raise HTTPException(404,"Case not found")
    metadata=json.loads(c.metadata_json or "{}")
    return {"case_id":c.case_id,"name":c.name,"description":c.description,"incident_type":c.incident_type,
            "incident_location":c.incident_location,"incident_start":c.incident_start,"incident_end":c.incident_end,
            "timezone":c.timezone or (metadata.get("device") or {}).get("timezone") or metadata.get("timezone") or "Asia/Kolkata",
            "status":c.status or "open","created_at":c.created_at.isoformat() if c.created_at else None,"updated_at":c.updated_at.isoformat() if c.updated_at else None,
            "active":c.case_id==active_case(s),"metadata":metadata}

@app.get("/api/cases/{case_id}/summary")
def case_summary(case_id:str,s:Session=Depends(db)):
    if not case_record(s,case_id):raise HTTPException(404,"Case not found")
    q=dict(s.query(Evidence.type,func.count()).filter_by(case_id=case_id).group_by(Evidence.type).all())
    by_type={"message":q.get("message",0),"call":q.get("call",0),"gps":q.get("location",0),"browser":q.get("browser",0),"application":q.get("app",0),"file":q.get("file",0),"system":q.get("system",0),"other":q.get("other",0)}
    return {"case_id":case_id,"total":sum(by_type.values()),"by_type":by_type,"contacts":s.query(Entity).filter_by(case_id=case_id).count(),
            "timeline_events":s.query(Evidence).filter(Evidence.case_id==case_id,Evidence.ts.isnot(None)).count()}

@app.get("/api/cases/{case_id}/audit")
def case_audit(case_id:str,limit:int=Query(100,ge=1,le=500),s:Session=Depends(db)):
    if not case_record(s,case_id):raise HTTPException(404,"Case not found")
    events=s.query(AuditEvent).filter_by(case_id=case_id).order_by(AuditEvent.timestamp.desc()).limit(limit).all()
    return {"case_id":case_id,"events":[{"timestamp":x.timestamp.isoformat(),"action":x.action,"actor":x.actor,"evidence_id":x.evidence_id,
            "metadata":json.loads(x.metadata_json or "{}") } for x in events]}

@app.get("/api/cases/{case_id}/manifest")
def case_manifest(case_id: str, s: Session = Depends(db)):
    c=case_record(s,case_id)
    if not c: raise HTTPException(404,"Case not found")
    return json.loads(c.manifest_json or "{}")

@app.get("/api/cases/{case_id}/files")
def case_files(case_id: str, s: Session = Depends(db)):
    if not case_record(s,case_id): raise HTTPException(404,"Case not found")
    files=s.query(ImportedFile).filter_by(case_id=case_id).order_by(ImportedFile.filename,ImportedFile.id).all()
    return {"case_id":case_id,"files":[{"id":f.id,"filename":f.filename,"size":f.size,"sha256":f.sha256,"imported_at":f.imported_at.isoformat(),"status":f.status,
            "records_detected":f.records_detected,"records_imported":f.records_imported,"records_failed":f.records_failed,"warning":f.warning} for f in files]}

@app.get("/api/cases/{case_id}/files/{file_id}/original")
def original_file(case_id: str,file_id: int,s: Session=Depends(db)):
    if not case_record(s,case_id):raise HTTPException(404,"Case not found")
    f=s.query(ImportedFile).filter_by(case_id=case_id,id=file_id).first()
    if not f:raise HTTPException(404,"Evidence file not found")
    if f.raw_bytes is None:raise HTTPException(410,"Original file bytes were not retained for this historical import")
    from urllib.parse import quote
    return Response(content=f.raw_bytes,media_type="application/octet-stream",
                    headers={"Content-Disposition":f"attachment; filename*=UTF-8''{quote(Path(f.filename).name)}"})

@app.post("/api/cases/{case_id}/activate")
def activate_case(case_id: str, s: Session = Depends(db)):
    c=case_record(s,case_id)
    if not c: raise HTTPException(404,"Case not found")
    if c.status=="archived":raise HTTPException(409,"Archived cases cannot be activated")
    s.query(CaseRecord).update({CaseRecord.active:False})
    c.active=True;c.updated_at=datetime.utcnow();audit(s,"case.activate",case_id);s.commit()
    return {"active_case":case_id}

@app.post("/api/cases/{case_id}/archive")
def archive_case(case_id:str,s:Session=Depends(db)):
    c=case_record(s,case_id)
    if not c:raise HTTPException(404,"Case not found")
    c.status="archived";c.active=False;c.updated_at=datetime.utcnow();audit(s,"case.archive",case_id)
    replacement=s.query(CaseRecord).filter(CaseRecord.case_id!=case_id,CaseRecord.status!="archived").order_by(CaseRecord.created_at.desc()).first()
    if replacement is not None:replacement.active=True
    s.commit()
    return {"case_id":case_id,"status":"archived","active_case":replacement.case_id if replacement else None}

def _multipart_upload(content_type:str,body:bytes):
    message=BytesParser(policy=email_policy).parsebytes((f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode()+body)
    part=next((p for p in message.iter_parts() if p.get_filename()),None)
    if part is None:raise ValueError("Choose a dataset file to upload")
    return part.get_filename(),part.get_payload(decode=True) or b""

def process_import_job(import_id:str):
    s=sessionmaker(bind=engine())()
    try:
        job=s.query(ImportJob).filter_by(import_id=import_id).first()
        if not job:return
        filename,raw,case=job.filename,job.raw_bytes,job.case_id
        job.status="running";job.current_stage="detect";job.updated_at=datetime.utcnow();s.commit()
        def progress(stage,files_done,files_total,records_done,records_total):
            current=s.query(ImportJob).filter_by(import_id=import_id).first()
            if not current:return
            current.current_stage=stage;current.files_processed=files_done;current.files_total=files_total
            current.records_processed=records_done;current.records_total=records_total;current.updated_at=datetime.utcnow()
            s.commit()
        result=ingest_dataset(s,filename,raw,requested_case_id=case,merge_existing=True,progress=progress)
        current=s.query(ImportJob).filter_by(import_id=import_id).first()
        current.status="complete";current.current_stage="complete";current.files_processed=result["files_processed"]
        current.files_total=result["files_discovered"];current.records_processed=result["records_imported"]
        current.records_total=result["records_detected"];current.manifest_json=json.dumps(result,ensure_ascii=False)
        current.errors_json=json.dumps([*result.get("errors",[]),*result.get("warnings",[])],ensure_ascii=False);current.raw_bytes=None;current.updated_at=datetime.utcnow()
        audit(s,"import.complete",case,metadata={"import_id":import_id,"records_imported":result["records_imported"]});s.commit()
    except Exception as exc:
        s.rollback();current=s.query(ImportJob).filter_by(import_id=import_id).first()
        if current:
            current.status="failed";current.current_stage="failed";current.errors_json=json.dumps([{"error":str(exc)}])
            current.updated_at=datetime.utcnow();audit(s,"import.failed",current.case_id,metadata={"import_id":import_id,"error_type":type(exc).__name__});s.commit()
        logger.exception("Evidence import job failed (%s)",import_id)
    finally:s.close()

@app.post("/api/cases/{case_id}/imports",status_code=202)
async def create_import(case_id:str,background_tasks:BackgroundTasks,request:Request,s:Session=Depends(db)):
    c=case_record(s,case_id)
    if not c:raise HTTPException(404,"Case not found")
    if c.status=="archived":raise HTTPException(409,"Cannot import evidence into an archived case")
    body=await request.body()
    if len(body)>52*1024*1024:raise HTTPException(413,"Upload exceeds the 50 MB limit")
    try:filename,raw=_multipart_upload(request.headers.get("content-type",""),body)
    except ValueError as exc:raise HTTPException(422,str(exc)) from exc
    if not raw or len(raw)>50*1024*1024:raise HTTPException(422,"Upload must be non-empty and at most 50 MB")
    import_id=uuid.uuid4().hex;now=datetime.utcnow()
    job=ImportJob(import_id=import_id,case_id=case_id,filename=Path(filename).name,status="queued",current_stage="upload",
                  files_processed=0,files_total=0,records_processed=0,records_total=0,errors_json="[]",created_at=now,updated_at=now,raw_bytes=raw)
    s.add(job);audit(s,"import.queued",case_id,metadata={"import_id":import_id,"filename":Path(filename).name,"size":len(raw)});s.commit()
    background_tasks.add_task(process_import_job,import_id)
    return {"import_id":import_id,"case_id":case_id,"status":"queued"}

@app.get("/api/imports/{import_id}")
def import_status(import_id:str,s:Session=Depends(db)):
    job=s.query(ImportJob).filter_by(import_id=import_id).first()
    if not job:raise HTTPException(404,"Import job not found")
    return {"import_id":job.import_id,"case_id":job.case_id,"status":job.status,"current_stage":job.current_stage,
            "files_processed":job.files_processed,"files_total":job.files_total,"records_processed":job.records_processed,
            "records_total":job.records_total,"errors":json.loads(job.errors_json or "[]"),
            "manifest":json.loads(job.manifest_json) if job.manifest_json else None,
            "created_at":job.created_at.isoformat() if job.created_at else None,"updated_at":job.updated_at.isoformat() if job.updated_at else None}

@app.post("/api/demo/load")
def load_demo(s: Session = Depends(db)):
    if not DATA.exists(): generate(DATA)
    count = s.query(Evidence).filter_by(case_id=CASE).count()
    if count == 0: count = ingest_dir(s, CASE, DATA)
    c=case_record(s,CASE)
    if c is None: c=CaseRecord(case_id=CASE,name=CASE,created_at=datetime.utcnow());s.add(c)
    s.query(CaseRecord).update({CaseRecord.active:False});c.active=True;s.commit()
    return {"case": CASE, "evidence": count, "generated": {}}

class ReportUpload(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255)
    content: str = Field(..., min_length=1, max_length=6_000_000)

@app.post("/api/reports/upload")
def upload_report(body: ReportUpload, s: Session = Depends(db)):
    try:
        result = ingest_dataset(s, body.filename, body.content.encode("utf-8"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return result

@app.post("/api/datasets/upload")
async def upload_dataset(request: Request, case_id: str | None = Query(None), s: Session = Depends(db)):
    content_type=request.headers.get("content-type",""); body=await request.body()
    if len(body)>52*1024*1024: raise HTTPException(413,"Upload exceeds the 50 MB limit")
    try:
        message=BytesParser(policy=email_policy).parsebytes((f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode()+body)
        part=next((p for p in message.iter_parts() if p.get_filename()),None)
        if part is None: raise ValueError("Choose a dataset file to upload")
        return ingest_dataset(s,part.get_filename(),part.get_payload(decode=True) or b"",case_id)
    except ValueError as exc: raise HTTPException(422,str(exc)) from exc

@app.get("/api/stats")
def stats(s: Session = Depends(db)):
    case=active_case(s); q = dict(s.query(Evidence.type, func.count()).filter_by(case_id=case).group_by(Evidence.type).all())
    c=case_record(s,case); metadata=json.loads(c.metadata_json or "{}") if c else {}
    counts={"message":q.get("message",0),"call":q.get("call",0),"gps":q.get("location",0),"browser":q.get("browser",0),"application":q.get("app",0),"file":q.get("file",0),"system":q.get("system",0),"other":q.get("other",0)}
    total=sum(counts.values())
    metadata,incident_dt,reference=case_context(s,case);incident_value=incident_dt.isoformat();lat,lon=reference
    incident_data=metadata.get("incident") or {};incident_label=incident_data.get("location") if isinstance(incident_data,dict) else None
    geofence_data=metadata.get("geofence") or {};incident_label=incident_label or (geofence_data.get("name") if isinstance(geofence_data,dict) else None)
    zone_name=str((metadata.get("device") or {}).get("timezone") or metadata.get("timezone") or "Asia/Kolkata")
    def zoned(value):
        if not value:return None
        dt=datetime.fromisoformat(str(value).replace("Z","+00:00"))
        return (dt.astimezone(case_zone(zone_name)) if dt.tzinfo else dt.replace(tzinfo=case_zone(zone_name))).isoformat()
    return {"case":case,"case_name":c.name if c else case,"total":total,"by_type":counts,"contacts":s.query(Entity).filter_by(case_id=case).count(),
            "timeline_events":s.query(Evidence).filter(Evidence.case_id==case,Evidence.ts.isnot(None)).count(),"incident":zoned(incident_value),"timezone":zone_name,
            "incident_window_start":zoned(incident_data.get("window_start")) if isinstance(incident_data,dict) else None,
            "incident_window_end":zoned(incident_data.get("window_end")) if isinstance(incident_data,dict) else None,
            "incident_label":incident_label,"incident_location":{"lat":lat,"lon":lon}}

@app.get("/api/entities")
def entities(s: Session = Depends(db)):
    rows = s.query(Entity).filter_by(case_id=active_case(s)).order_by(Entity.name, Entity.phone).all()
    return {"count": len(rows), "results": [{"name": e.name, "number": e.phone, "device": e.device} for e in rows]}

@app.get("/api/timeline")
def timeline(start: datetime | None = None, end: datetime | None = None, types: str | None = Query(None, max_length=120),
             entity: str | None = Query(None, max_length=120), incident_window_min: int | None = Query(None, ge=1, le=720),
             offset: int = Query(0,ge=0),limit: int = Query(500, ge=1, le=5000), s: Session = Depends(db)):
    if start and end and start > end: raise HTTPException(422, "start must be earlier than or equal to end")
    case=active_case(s)
    if incident_window_min is not None:
        _,incident_dt,_=case_context(s,case);start, end = incident_dt - timedelta(minutes=incident_window_min), incident_dt + timedelta(minutes=incident_window_min)
    q = s.query(Evidence).filter_by(case_id=case).filter(Evidence.ts.isnot(None))
    if start: q = q.filter(Evidence.ts >= start)
    if end: q = q.filter(Evidence.ts <= end)
    if types:
        selected = [v.strip() for v in types.split(",") if v.strip()]
        aliases={"gps":"location","application":"app"};selected=[aliases.get(v,v) for v in selected]
        if any(v not in {"message", "call", "location", "browser", "app", "file", "system", "other"} for v in selected): raise HTTPException(422, "Unsupported evidence type")
        if selected: q = q.filter(Evidence.type.in_(selected))
    if entity:
        like=f"%{entity}%";q=q.filter(Evidence.actor.ilike(like)|Evidence.target.ilike(like))
    count=q.count();result = [row(e, names(s)) for e in q.order_by(Evidence.ts).offset(offset).limit(limit)]
    return {"count": count, "events": result,"offset":offset,"limit":limit}

@app.get("/api/search")
def search(q: str = Query(..., min_length=2, max_length=200), type: str | None = Query(None, max_length=30),
           offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), source_file: str | None = None,
           device_id: str | None = None, start: datetime | None = None, end: datetime | None = None,
           relevance_min: float | None = Query(None, ge=0, le=1), s: Session = Depends(db)):
    like = f"%{q}%"
    case=active_case(s)
    query = s.query(Evidence).filter_by(case_id=case).filter(Evidence.summary.ilike(like) | Evidence.actor.ilike(like) | Evidence.target.ilike(like) | Evidence.evidence_id.ilike(like) | Evidence.source_file.ilike(like) | Evidence.device_id.ilike(like) | Evidence.meta.ilike(like))
    if source_file: query=query.filter(Evidence.source_file==source_file)
    if device_id: query=query.filter(Evidence.device_id==device_id)
    if start: query=query.filter(Evidence.ts>=start)
    if end: query=query.filter(Evidence.ts<=end)
    if relevance_min is not None:
        _,incident_dt,reference=case_context(s,case)
        rel={x["evidence_id"] for x in intel.relevance(s,case,incident_dt,reference) if x["relevance"]>=relevance_min}
        query=query.filter(Evidence.evidence_id.in_(rel))
    if type and type != "all":
        type={"gps":"location","application":"app"}.get(type,type)
        if type not in {"message", "call", "location", "browser", "app", "file", "system", "other"}: raise HTTPException(422, "Unsupported evidence type")
        query = query.filter(Evidence.type == type)
    total = query.count()
    rs = query.order_by(Evidence.ts).offset(offset).limit(limit).all()
    return {"count": total, "results": [row(e, names(s)) for e in rs], "offset": offset, "limit": limit}

@app.get("/api/evidence")
def evidence_list(q: str | None = Query(None, min_length=2, max_length=200), type: str | None = Query(None, max_length=30),
                  offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), s: Session = Depends(db),
                  source_file: str | None = None, device_id: str | None = None, start: datetime | None = None, end: datetime | None = None,
                  relevance_min: float | None = Query(None, ge=0, le=1)):
    case=active_case(s); query = s.query(Evidence).filter_by(case_id=case)
    if q:
        like = f"%{q}%"
        query = query.filter(Evidence.summary.ilike(like) | Evidence.actor.ilike(like) | Evidence.target.ilike(like) | Evidence.evidence_id.ilike(like) | Evidence.source_file.ilike(like) | Evidence.device_id.ilike(like) | Evidence.meta.ilike(like))
    if type and type != "all":
        type={"gps":"location","application":"app"}.get(type,type)
        if type not in {"message", "call", "location", "browser", "app", "file", "system", "other"}: raise HTTPException(422, "Unsupported evidence type")
        query = query.filter(Evidence.type == type)
    if source_file: query=query.filter(Evidence.source_file==source_file)
    if device_id: query=query.filter(Evidence.device_id==device_id)
    if start: query=query.filter(Evidence.ts>=start)
    if end: query=query.filter(Evidence.ts<=end)
    if relevance_min is not None:
        # Relevance scores are case-derived; retain records that meet this threshold.
        _,incident_dt,reference=case_context(s,case)
        rel={x["evidence_id"] for x in intel.relevance(s,case,incident_dt,reference) if x["relevance"]>=relevance_min}
        query=query.filter(Evidence.evidence_id.in_(rel))
    total = query.count()
    result = query.order_by(Evidence.ts).offset(offset).limit(limit).all()
    return {"count": total, "results": [row(e, names(s)) for e in result], "offset": offset, "limit": limit}

@app.get("/api/evidence/{eid}")
def evidence_detail(eid: str, s: Session = Depends(db)):
    e = s.query(Evidence).filter_by(case_id=active_case(s), evidence_id=eid).first()
    if not e: raise HTTPException(404, "Evidence not found")
    return row(e, names(s))

@app.get("/api/evidence/{eid}/verify")
def verify(eid: str, s: Session = Depends(db)):
    e = s.query(Evidence).filter_by(case_id=active_case(s), evidence_id=eid).first()
    if not e: raise HTTPException(404, "Evidence not found")
    metadata=json.loads(e.meta or "{}")
    canon=(json.dumps(metadata["raw_record"],sort_keys=True,ensure_ascii=False,default=str).encode() if "raw_record" in metadata else
           json.dumps([e.type, e.ts.isoformat(), e.actor, e.target, e.summary, e.lat, e.lon], sort_keys=True).encode())
    audit(s,"evidence.integrity.verify",e.case_id,e.evidence_id,{"verified":hashlib.sha256(canon).hexdigest()==e.sha256});s.commit()
    return {"evidence_id": eid, "verified": hashlib.sha256(canon).hexdigest() == e.sha256, "sha256": e.sha256,
            "source_file": e.source_file, "source_file_sha256": e.source_file_sha256, "ingested_at": e.ingested_at.isoformat(),
            "note": "Prototype integrity check; not a legal admissibility claim."}

@app.get("/api/evidence/{eid}/provenance")
def evidence_provenance(eid:str,s:Session=Depends(db)):
    e=s.query(Evidence).filter_by(case_id=active_case(s),evidence_id=eid).first()
    if not e:raise HTTPException(404,"Evidence not found")
    case=e.case_id;clusters=[c for c in intel.correlations(s,case) if eid in c["evidence_ids"]]
    anomalies=[a for a in intel.anomalies(s,case) if eid in a.get("evidence_ids",[])]
    return {"evidence_id":eid,"case_id":case,"original_timestamp":e.original_timestamp,"normalized_timestamp":row(e,names(s))["time"],
            "timezone":e.timezone,"source_file":e.source_file,"source_artifact":e.source_artifact,"source_record_id":e.original_id,
            "record_sha256":e.sha256,"source_file_sha256":e.source_file_sha256,"correlations":clusters,"anomalies":anomalies}

@app.get("/api/graph")
def graph(s: Session = Depends(db)): return intel.build_graph(s, active_case(s))

@app.get("/api/cases/{case_id}/communications")
def case_communications(case_id:str,s:Session=Depends(db)):
    if not case_record(s,case_id):raise HTTPException(404,"Case not found")
    return intel.build_graph(s,case_id)

@app.get("/api/cases/{case_id}/evidence")
def case_evidence(case_id:str,q:str|None=Query(None,min_length=2,max_length=200),type:str|None=None,
                  offset:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=500),s:Session=Depends(db)):
    if not case_record(s,case_id):raise HTTPException(404,"Case not found")
    query=s.query(Evidence).filter_by(case_id=case_id)
    if q:
        like=f"%{q}%";query=query.filter(Evidence.summary.ilike(like)|Evidence.actor.ilike(like)|Evidence.target.ilike(like)|Evidence.evidence_id.ilike(like)|Evidence.meta.ilike(like))
    if type and type!="all":
        kind={"gps":"location","application":"app"}.get(type,type)
        if kind not in {"message","call","location","browser","app","file","system","other"}:raise HTTPException(422,"Unsupported evidence type")
        query=query.filter(Evidence.type==kind)
    count=query.count();items=query.order_by(Evidence.ts,Evidence.id).offset(offset).limit(limit).all()
    return {"case_id":case_id,"count":count,"results":[row(e,names(s,case_id)) for e in items],"offset":offset,"limit":limit}

@app.get("/api/cases/{case_id}/evidence/{evidence_id}")
def case_evidence_detail(case_id:str,evidence_id:str,s:Session=Depends(db)):
    e=s.query(Evidence).filter_by(case_id=case_id,evidence_id=evidence_id).first()
    if not e:raise HTTPException(404,"Evidence not found in this case")
    return row(e,names(s,case_id))

@app.get("/api/cases/{case_id}/timeline")
def case_timeline(case_id:str,start:datetime|None=None,end:datetime|None=None,types:str|None=None,entity:str|None=None,
                  offset:int=Query(0,ge=0),limit:int=Query(500,ge=1,le=5000),s:Session=Depends(db)):
    if not case_record(s,case_id):raise HTTPException(404,"Case not found")
    if start and end and start>end:raise HTTPException(422,"start must be earlier than or equal to end")
    query=s.query(Evidence).filter(Evidence.case_id==case_id,Evidence.ts.isnot(None))
    if start:query=query.filter(Evidence.ts>=start.replace(tzinfo=None))
    if end:query=query.filter(Evidence.ts<=end.replace(tzinfo=None))
    if types:
        kinds=[{"gps":"location","application":"app"}.get(x.strip(),x.strip()) for x in types.split(",") if x.strip()]
        query=query.filter(Evidence.type.in_(kinds))
    if entity:query=query.filter(Evidence.actor.ilike(f"%{entity}%")|Evidence.target.ilike(f"%{entity}%"))
    count=query.count();events=query.order_by(Evidence.ts,Evidence.id).offset(offset).limit(limit).all()
    return {"case_id":case_id,"count":count,"events":[row(e,names(s,case_id)) for e in events],"offset":offset,"limit":limit}

@app.get("/api/cases/{case_id}/locations")
def case_locations(case_id:str,offset:int=Query(0,ge=0),limit:int=Query(500,ge=1,le=2000),s:Session=Depends(db)):
    if not case_record(s,case_id):raise HTTPException(404,"Case not found")
    query=s.query(Evidence).filter(Evidence.case_id==case_id,Evidence.type=="location",Evidence.ts.isnot(None)).order_by(Evidence.ts,Evidence.id)
    count=query.count();items=query.offset(offset).limit(limit).all()
    return {"case_id":case_id,"count":count,"results":[row(e,names(s,case_id)) for e in items],"analysis":map_analysis_for_case(s,case_id)}

@app.get("/api/cases/{case_id}/correlations")
def case_correlations(case_id:str,window_min:int=Query(5,ge=1,le=60),radius_m:int=Query(400,ge=10,le=5000),s:Session=Depends(db)):
    if not case_record(s,case_id):raise HTTPException(404,"Case not found")
    return intel.correlations(s,case_id,window_min,radius_m)

@app.get("/api/cases/{case_id}/anomalies")
def case_anomalies(case_id:str,s:Session=Depends(db)):
    if not case_record(s,case_id):raise HTTPException(404,"Case not found")
    return intel.anomalies(s,case_id)

@app.get("/api/cases/{case_id}/search")
def case_search(case_id:str,q:str=Query(...,min_length=2,max_length=200),type:str|None=None,offset:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=500),s:Session=Depends(db)):
    return case_evidence(case_id,q,type,offset,limit,s)

@app.post("/api/cases/{case_id}/copilot/query")
def case_copilot(case_id:str,body:AskIn,s:Session=Depends(db)):
    if not case_record(s,case_id):raise HTTPException(404,"Case not found")
    _,incident,location=case_context(s,case_id)
    return copilot.ask(s,case_id,body.query,incident,location)

@app.get("/api/graph/strongest")
def strongest(person: str = Query(..., min_length=1, max_length=120), s: Session = Depends(db)):
    return [{"entity": a, "interactions": n} for a, n in intel.strongest(s, active_case(s), person)]

@app.get("/api/connections")
def connections(person: str | None = Query(None, max_length=120), limit: int = Query(20, ge=1, le=100), s: Session = Depends(db)):
    edges = intel.build_graph(s, active_case(s))["edges"]
    if person: edges = [e for e in edges if person in (e["source"], e["target"])]
    result = sorted(edges, key=lambda e: (-e["weight"], e["source"], e["target"]))[:limit]
    return {"count": len(result), "results": result}

@app.get("/api/correlations")
def correlations(window_min: int = Query(5, ge=1, le=60), radius_m: int = Query(400, ge=10, le=5000), s: Session = Depends(db)):
    return intel.correlations(s, active_case(s), window_min, radius_m)

@app.get("/api/anomalies")
def anomalies(s: Session = Depends(db)): return intel.anomalies(s, active_case(s))

@app.get("/api/relevance")
def relevance(involved: str | None = Query(None, max_length=500), s: Session = Depends(db)):
    case=active_case(s);_,incident_dt,reference=case_context(s,case)
    return intel.relevance(s, case, incident_dt, reference, involved.split(",") if involved else None)

@app.get("/api/map/geofence")
def geofence(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180),
            radius_m: int = Query(500, ge=10, le=50000), s: Session = Depends(db)):
    points = intel.geofence(s, active_case(s), lat, lon, radius_m)
    return {"count": len(points), "points": points}

@app.get("/api/map/analysis")
def map_analysis(s: Session = Depends(db)):
    return map_analysis_for_case(s,active_case(s))

def map_analysis_for_case(s:Session,case:str):
    metadata,_,center=case_context(s,case);geo=metadata.get("geofence") or {}
    radius=float(geo.get("radius_m",500)) if isinstance(geo,dict) else 500
    points=s.query(Evidence).filter_by(case_id=case,type="location").filter(Evidence.ts.isnot(None),Evidence.lat.isnot(None),Evidence.lon.isnot(None)).order_by(Evidence.ts).all()
    if not points:return {"case_id":case,"count":0,"message":"No geospatial evidence available.","routes":{},"geofence_events":[],"dwell_periods":[]}
    routes={};groups={}
    for e in points:
        routes.setdefault(e.actor,[]).append({"evidence_id":e.evidence_id,"time":intel.case_time(e),"lat":e.lat,"lon":e.lon})
        groups.setdefault(e.actor,[]).append(e)
    events=[];dwells=[]
    for actor,fixes in groups.items():
        in_zone=False;dwell=[]
        for e in fixes:
            distance=intel.hav(center[0],center[1],e.lat,e.lon);inside=distance<=radius
            if inside and not in_zone: events.append({"event":"entered_geofence","actor":actor,"evidence_id":e.evidence_id,"time":intel.case_time(e),"distance_m":round(distance)})
            elif not inside and in_zone: events.append({"event":"exited_geofence","actor":actor,"evidence_id":e.evidence_id,"time":intel.case_time(e),"distance_m":round(distance)})
            in_zone=inside
            if dwell and (e.ts-dwell[-1].ts).total_seconds()>1200 or dwell and intel.hav(dwell[-1].lat,dwell[-1].lon,e.lat,e.lon)>150:
                if len(dwell)>1: dwells.append({"actor":actor,"start":intel.case_time(dwell[0]),"end":intel.case_time(dwell[-1]),"duration_minutes":round((dwell[-1].ts-dwell[0].ts).total_seconds()/60,1),"evidence_ids":[x.evidence_id for x in dwell]})
                dwell=[]
            dwell.append(e)
        if dwell and len(dwell)>1:dwells.append({"actor":actor,"start":intel.case_time(dwell[0]),"end":intel.case_time(dwell[-1]),"duration_minutes":round((dwell[-1].ts-dwell[0].ts).total_seconds()/60,1),"evidence_ids":[x.evidence_id for x in dwell]})
        if in_zone and fixes:events.append({"event":"geofence_status_open","actor":actor,"evidence_id":fixes[-1].evidence_id,"time":intel.case_time(fixes[-1]),"distance_m":round(intel.hav(center[0],center[1],fixes[-1].lat,fixes[-1].lon))})
    return {"case_id":case,"count":len(points),"center":{"lat":center[0],"lon":center[1]},"radius_m":radius,
            "routes":routes,"geofence_events":events,"dwell_periods":dwells}

@app.get("/api/map/path")
def path(person: str = Query(..., min_length=1, max_length=120), s: Session = Depends(db)):
    case=active_case(s);person_id = next((e.phone for e in s.query(Entity).filter_by(case_id=case) if e.name == person), person)
    query = s.query(Evidence).filter_by(case_id=case, type="location", actor=person_id).filter(Evidence.ts.isnot(None),Evidence.lat.isnot(None),Evidence.lon.isnot(None)).order_by(Evidence.ts)
    return [{"id": e.evidence_id, "time": intel.case_time(e), "lat": e.lat, "lon": e.lon,"source_file":e.source_file} for e in query]

@app.post("/api/copilot")
def ask(body: AskIn, s: Session = Depends(db)):
    case=active_case(s);_,incident_dt,reference=case_context(s,case)
    return copilot.ask(s,case,body.query,incident_dt,reference)

WEB = Path(__file__).resolve().parents[2] / "frontend"
if WEB.is_dir():
    app.mount("/assets", StaticFiles(directory=WEB), name="assets")
    @app.get("/")
    def home(): return FileResponse(WEB / "index.html")
