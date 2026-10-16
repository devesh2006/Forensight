from datetime import datetime, timedelta
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func
from sqlalchemy.orm import sessionmaker
from .ingest import make_session, ingest_dir, Evidence, Entity
from .synth import generate, INCIDENT
import hashlib, json, os

DATA = Path(os.getenv("NIGHTFALL_DATA_DIR", str(Path(__file__).resolve().parents[2] / "data" / "nightfall")))
CASE = "NIGHTFALL"
app = FastAPI(title="ForenSight AI API")
origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:8000,http://127.0.0.1:8000").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_origin_regex=os.getenv("CORS_ORIGIN_REGEX") or None,
                   allow_methods=["GET", "POST"], allow_headers=["*"])
_db = None
def db():
    global _db
    if _db is None:
        url = os.getenv("DATABASE_URL")
        if not url and os.getenv("DEPLOYMENT_ENV") == "render":
            raise RuntimeError("DATABASE_URL must point to a persistent PostgreSQL database on the hosted backend.")
        url = url or "sqlite:///forensight.db"
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        _db = make_session(url)
    session = sessionmaker(bind=_db.get_bind())()
    try:
        yield session
    finally:
        session.close()

def row(e, names):
    nm = lambda x: names.get(x, x)
    return {"id": e.evidence_id, "type": e.type, "time": e.ts.isoformat(), "actor": nm(e.actor), "target": nm(e.target),
            "summary": e.summary, "source": f"{e.source_file}:{e.source_artifact}", "lat": e.lat, "lon": e.lon, "sha256": e.sha256,
            "metadata": json.loads(e.meta or "{}")}
def names(s): return {x.phone: x.name for x in s.query(Entity).filter_by(case_id=CASE)}

@app.post("/api/demo/load")
def load_demo(s=Depends(db)):
    counts = generate(DATA); n = ingest_dir(s, CASE, DATA); return {"case": CASE, "evidence": n, "generated": counts}

@app.get("/api/stats")
def stats(s=Depends(db)):
    q = dict(s.query(Evidence.type, func.count()).filter_by(case_id=CASE).group_by(Evidence.type).all())
    return {"case": CASE, "total": sum(q.values()), "by_type": q, "contacts": s.query(Entity).filter_by(case_id=CASE).count()}

@app.get("/api/timeline")
def timeline(start: datetime | None = None, end: datetime | None = None, types: str | None = None,
             entity: str | None = None, incident_window_min: int | None = Query(None, ge=1, le=720), s=Depends(db)):
    if incident_window_min: start, end = INCIDENT - timedelta(minutes=incident_window_min), INCIDENT + timedelta(minutes=incident_window_min)
    q = s.query(Evidence).filter_by(case_id=CASE); nm = names(s)
    if start: q = q.filter(Evidence.ts >= start)
    if end: q = q.filter(Evidence.ts <= end)
    if types: q = q.filter(Evidence.type.in_(types.split(",")))
    out = [row(e, nm) for e in q.order_by(Evidence.ts).limit(2000)]
    if entity: out = [r for r in out if entity.lower() in f'{r["actor"]} {r["target"]}'.lower()]
    return {"count": len(out), "events": out}

@app.get("/api/search")
def search(q: str = Query(..., min_length=2, max_length=200), s=Depends(db)):
    like = f"%{q}%"; nm = names(s)
    rs = s.query(Evidence).filter_by(case_id=CASE).filter(Evidence.summary.ilike(like) | Evidence.actor.ilike(like) | Evidence.target.ilike(like)).order_by(Evidence.ts).limit(200)
    return {"count": rs.count(), "results": [row(e, nm) for e in rs]}

@app.get("/api/evidence")
def evidence_list(q: str | None = Query(None, min_length=2, max_length=200), offset: int = Query(0, ge=0),
                  limit: int = Query(100, ge=1, le=500), s=Depends(db)):
    nm = names(s); query = s.query(Evidence).filter_by(case_id=CASE)
    if q:
        like = f"%{q}%"
        query = query.filter(Evidence.summary.ilike(like) | Evidence.actor.ilike(like) | Evidence.target.ilike(like) | Evidence.evidence_id.ilike(like))
    total = query.count()
    return {"count": total, "results": [row(e, nm) for e in query.order_by(Evidence.ts).offset(offset).limit(limit)]}

@app.get("/api/evidence/{eid}")
def evidence_detail(eid: str, s=Depends(db)):
    e = s.query(Evidence).filter_by(case_id=CASE, evidence_id=eid).first()
    if not e: raise HTTPException(404, "Not found")
    return row(e, names(s))

@app.get("/api/evidence/{eid}/verify")
def verify(eid: str, s=Depends(db)):
    e = s.query(Evidence).filter_by(case_id=CASE, evidence_id=eid).first()
    if not e: raise HTTPException(404, "Not found")
    canon = json.dumps([e.type, e.ts.isoformat(), e.actor, e.target, e.summary, e.lat, e.lon], sort_keys=True)
    ok = hashlib.sha256(canon.encode()).hexdigest() == e.sha256
    return {"evidence_id": eid, "verified": ok, "sha256": e.sha256, "source_file": e.source_file, "source_file_sha256": e.source_file_sha256,
            "ingested_at": e.ingested_at.isoformat(), "note": "Prototype integrity check; not a legal admissibility claim."}

from . import intel
OLD_FORT = (28.6118, 77.2437)

@app.get("/api/graph")
def graph(s=Depends(db)): return intel.build_graph(s, CASE)

@app.get("/api/graph/strongest")
def strongest(person: str, s=Depends(db)): return [{"entity": a, "interactions": n} for a, n in intel.strongest(s, CASE, person)]

@app.get("/api/correlations")
def correlations(window_min: int = Query(5, ge=1, le=60), radius_m: int = Query(400, ge=10, le=5000), s=Depends(db)):
    return intel.correlations(s, CASE, window_min, radius_m)

@app.get("/api/anomalies")
def anomalies(s=Depends(db)): return intel.anomalies(s, CASE)

@app.get("/api/relevance")
def relevance(involved: str | None = None, s=Depends(db)):
    return intel.relevance(s, CASE, INCIDENT, OLD_FORT, involved.split(",") if involved else None)

@app.get("/api/map/geofence")
def geofence(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180), radius_m: int = Query(500, ge=10, le=50000), s=Depends(db)):
    r = intel.geofence(s, CASE, lat, lon, radius_m); return {"count": len(r), "points": r}

@app.get("/api/map/path")
def path(person: str, s=Depends(db)):
    q = s.query(Evidence).filter_by(case_id=CASE, type="location", actor=person).order_by(Evidence.ts)
    return [{"id": e.evidence_id, "time": e.ts.isoformat(), "lat": e.lat, "lon": e.lon} for e in q]

from pydantic import BaseModel, Field
from . import copilot
class AskIn(BaseModel):
    query: str = Field(..., min_length=3, max_length=500)

@app.post("/api/copilot")
def ask(body: AskIn, s=Depends(db)): return copilot.ask(s, CASE, body.query)

WEB = Path(__file__).resolve().parents[2] / "frontend"
if WEB.is_dir():
    app.mount("/assets", StaticFiles(directory=WEB), name="assets")

    @app.get("/")
    def home(): return FileResponse(WEB / "index.html")
