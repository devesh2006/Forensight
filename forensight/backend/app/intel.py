"""Forensic intelligence: graph, correlation, anomalies, relevance, geo. All derived from evidence rows."""
import math, statistics, hashlib
import json
from collections import Counter, defaultdict
from datetime import timedelta
import networkx as nx
from .ingest import Evidence, Entity
from .timezones import case_zone

def hav(a, b, c, d):
    R = 6371000; p1, p2 = math.radians(a), math.radians(c); dp = p2 - p1; dl = math.radians(d - b)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(x))

def names(s, case): return {e.phone: e.name for e in s.query(Entity).filter_by(case_id=case)}
def nm(m, x): return m.get(x, x)
def case_time(e, value=None):
    value=value or e.ts
    return value.replace(tzinfo=case_zone(e.timezone or "Asia/Kolkata")).isoformat()

def build_graph(s, case):
    m = names(s, case); G = nx.Graph(); grouped = defaultdict(list)
    for e in s.query(Evidence).filter_by(case_id=case).filter(Evidence.type.in_(["message", "call"]),Evidence.ts.isnot(None)):
        a, b = nm(m, e.actor), nm(m, e.target)
        if not a or not b or a == b: continue
        grouped[tuple(sorted((str(a),str(b))))].append(e)
    for (a,b),records in grouped.items():
        records.sort(key=lambda e:e.ts); kinds=Counter(e.type for e in records);directions=Counter();duration=0;interactions=[]
        for e in records:
            try: raw=(json.loads(e.meta or "{}").get("raw_record") or {})
            except (ValueError,TypeError): raw={}
            direction=str(raw.get("direction") or "unspecified").casefold();directions[direction]+=1
            seconds=raw.get("duration_seconds",raw.get("duration",0)) if e.type=="call" else 0
            try: seconds=max(0,int(float(seconds or 0)))
            except (ValueError,TypeError,OverflowError): seconds=0
            duration+=seconds
            interactions.append({"evidence_id":e.evidence_id,"type":e.type,"time":case_time(e),"direction":direction,
                                "duration_seconds":seconds,"source_file":e.source_file})
        G.add_edge(a,b,weight=len(records),message=kinds["message"],call=kinds["call"],message_count=kinds["message"],
                   call_count=kinds["call"],total_interactions=len(records),first_interaction=case_time(records[0]),
                   last_interaction=case_time(records[-1]),call_duration_seconds=duration,directions=dict(directions),
                   evidence_ids=[e.evidence_id for e in records],interactions=interactions)
    for n in G.nodes: G.nodes[n]["unknown"] = n.startswith("+") or n == "Unknown"
    deg = nx.degree_centrality(G); btw = nx.betweenness_centrality(G, weight=None)
    nodes = [{"id": n, "degree": round(deg[n], 3), "betweenness": round(btw[n], 3), "unresolved_number": G.nodes[n]["unknown"]} for n in G.nodes]
    edges = [{"source": a, "target": b, **d} for a, b, d in G.edges(data=True)]
    return {"nodes": nodes, "edges": edges}

def strongest(s, case, person, k=5):
    g = build_graph(s, case); out = [(e["target"] if e["source"] == person else e["source"], e["weight"]) for e in g["edges"] if person in (e["source"], e["target"])]
    return sorted(out, key=lambda x: -x[1])[:k]

def correlations(s, case, window_min=5, radius_m=400):
    """Correlate all evidence types in bounded timestamp windows; emit only stored evidence IDs."""
    from collections import defaultdict
    ev=s.query(Evidence).filter_by(case_id=case).filter(Evidence.ts.isnot(None)).order_by(Evidence.ts).all()
    window=timedelta(minutes=window_min); buckets=defaultdict(list)
    for e in ev: buckets[int(e.ts.timestamp()//window.total_seconds())].append(e)
    clusters=[];seen=set()
    for bucket,rows in buckets.items():
        members=rows+buckets.get(bucket+1,[])
        if len(members)<2: continue
        types={e.type for e in members}
        if len(types)<2: continue
        ids=tuple(sorted(e.evidence_id for e in members))
        if ids in seen: continue
        seen.add(ids);times=[e.ts for e in members];people=sorted({x for e in members for x in (e.actor,e.target) if x})
        gps=next((e for e in members if e.type=="location" and e.lat is not None and e.lon is not None),None)
        span=(max(times)-min(times)).total_seconds()/60
        pairs=sorted({" ↔ ".join(sorted((a,b))) for a in types for b in types if a<b})
        earliest=min(members,key=lambda e:e.ts);latest=max(members,key=lambda e:e.ts)
        gps_rows=[e for e in members if e.type=="location" and e.lat is not None and e.lon is not None]
        spatial=(f"GPS fixes span {max(round(hav(gps.lat,gps.lon,e.lat,e.lon)) for e in gps_rows)} m" if len(gps_rows)>1 else None)
        explanation=f"{len(members)} actual artifacts across {', '.join(sorted(types))} overlap within {span:.1f} minutes."
        clusters.append({"correlation_id":"COR-"+hashlib.sha256((case+"|"+"|".join(ids)).encode()).hexdigest()[:12].upper(),
            "relationship_type":"cross_source_temporal","confidence":round(min(.97,.4+.08*len(types)+.02*min(len(members),8)-.005*span),2),
            "center":[gps.lat,gps.lon] if gps else None,"start":case_time(earliest,min(times)),"end":case_time(latest,max(times)),
            "people":people,"types":sorted(types),"type_pairs":pairs,"evidence_ids":[e.evidence_id for e in members[:200]],
            "member_count":len(members),"explanation":explanation,"reason":explanation,
            "timestamp_relationship":{"start":case_time(earliest,min(times)),"end":case_time(latest,max(times)),"span_minutes":round(span,2)},
            "spatial_relationship":spatial})
    return sorted(clusters,key=lambda c:-c["confidence"])

def anomalies(s, case):
    m = names(s, case); out = []; ev = s.query(Evidence).filter_by(case_id=case).filter(Evidence.ts.isnot(None)).order_by(Evidence.ts).all()
    # 1. communication bursts (hourly z-score)
    hrs = Counter(e.ts.replace(minute=0, second=0) for e in ev if e.type in ("message", "call"))
    if len(hrs) > 3:
        mu, sd = statistics.mean(hrs.values()), statistics.pstdev(hrs.values()) or 1
        for h, n in hrs.items():
            z = (n - mu) / sd
            if z > 2:
                ids = [e.evidence_id for e in ev if e.type in ("message", "call") and e.ts.replace(minute=0, second=0) == h]
                out.append({"kind": "communication_burst", "time":case_time(next(e for e in ev if e.ts.replace(minute=0,second=0)==h),h), "score": round(z, 2), "detail": f"{n} communications at {h:%d %b %H}:00 (hourly mean {mu:.1f})", "evidence_ids": ids})
    # 2. contact with unresolved numbers
    for e in ev:
        if e.type in ("message", "call"):
            for p in (e.actor, e.target):
                if p and p.startswith("+") and nm(m, p) in ("Unknown", p): out.append({"kind": "unresolved_contact", "time": case_time(e), "score": 1.0, "detail": f"Contact with unattributed number {p}", "evidence_ids": [e.evidence_id]})
    # 3. deletion indicators
    for e in ev:
        if e.type == "app" and ("delete" in (e.summary or "") or "clear" in (e.summary or "")):
            out.append({"kind": "deletion_indicator", "time": case_time(e), "score": 2.0, "detail": f"{e.actor}: {e.target} {e.summary}", "evidence_ids": [e.evidence_id]})
    # 4. implausible speed per person
    by = defaultdict(list)
    for e in ev:
        if e.type == "location" and e.lat is not None and e.lon is not None: by[e.actor].append(e)
    for who, pts in by.items():
        for a, b in zip(pts, pts[1:]):
            dt = (b.ts - a.ts).total_seconds()
            if dt < 60: continue
            kmh = hav(a.lat, a.lon, b.lat, b.lon) / dt * 3.6
            if kmh > 200: out.append({"kind": "rapid_location_change", "time": case_time(b), "score": round(kmh / 100, 2), "detail": f"{who}: {kmh:.0f} km/h between fixes (possible GPS error or device change)", "evidence_ids": [a.evidence_id, b.evidence_id]})
    # 5. late-night activity (00:00-04:00), aggregated per person per day to limit noise
    agg = defaultdict(list)
    for e in ev:
        if e.type in ("message", "call") and e.ts.hour < 4: agg[(nm(m, e.actor), e.ts.date())].append(e)
    for (who, day), es in agg.items():
        if len(es) >= 2: out.append({"kind": "late_night_activity", "time": case_time(es[0]), "score": 0.4 + 0.1 * len(es), "detail": f"{who}: {len(es)} communications between 00:00-04:00 on {day}", "evidence_ids": [x.evidence_id for x in es]})
    for cluster in correlations(s,case):
        if len(cluster["types"])>=3:
            out.append({"kind":"cross_source_overlap","time":cluster["start"],"score":cluster["confidence"],
                        "detail":"Cross-source temporal overlap requiring review: "+", ".join(cluster["types"]),
                        "evidence_ids":cluster["evidence_ids"]})
    for index,o in enumerate(out):
        o["label"]="Anomaly detected";o["reason"]=o.get("detail","");o["timestamp"]=o.get("time")
        o["id"]="AN-"+hashlib.sha256((case+"|"+o.get("kind","")+"|"+"|".join(o.get("evidence_ids",[]))).encode()).hexdigest()[:12].upper()
    return sorted(out, key=lambda o: -o["score"])

def relevance(s, case, incident, ref_loc, involved=None, k=15):
    m = names(s, case); ev = s.query(Evidence).filter_by(case_id=case).filter(Evidence.ts.isnot(None)).all(); res = []
    involved = set(involved or [])
    for e in ev:
        dt = abs((e.ts - incident).total_seconds()) / 60; t = math.exp(-dt / 90); reasons = []; sc = 0.45 * t
        if dt <= 120: reasons.append(f"{dt:.0f} min from incident")
        who = {nm(m, e.actor), nm(m, e.target)}
        if involved & who: sc += 0.25; reasons.append("involves " + ", ".join(sorted(involved & who)))
        if e.lat is not None:
            d = hav(e.lat, e.lon, *ref_loc)
            if d < 500: sc += 0.3 * (1 - d / 500); reasons.append(f"{d:.0f} m from incident location")
        if e.type == "app" and ("delete" in (e.summary or "") or "clear" in (e.summary or "")): sc += 0.15; reasons.append("deletion indicator")
        if sc > 0.15: res.append({"evidence_id": e.evidence_id, "type": e.type, "time": case_time(e), "relevance": round(min(sc, 0.99), 2), "reasons": reasons, "summary": e.summary})
    return sorted(res, key=lambda r: -r["relevance"])[:k]

def geofence(s, case, lat, lon, radius_m):
    m = names(s, case); out = []
    for e in s.query(Evidence).filter_by(case_id=case, type="location").filter(Evidence.lat.isnot(None),Evidence.lon.isnot(None)).order_by(Evidence.ts):
        d = hav(lat, lon, e.lat, e.lon)
        if d <= radius_m: out.append({"id": e.evidence_id, "time": case_time(e), "person": e.actor, "lat": e.lat, "lon": e.lon, "distance_m": round(d)})
    return out
