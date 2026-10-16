"""Forensic intelligence: graph, correlation, anomalies, relevance, geo. All derived from evidence rows."""
import math, statistics
from collections import Counter, defaultdict
from datetime import timedelta
import networkx as nx
from .ingest import Evidence, Entity

def hav(a, b, c, d):
    R = 6371000; p1, p2 = math.radians(a), math.radians(c); dp = p2 - p1; dl = math.radians(d - b)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(x))

def names(s, case): return {e.phone: e.name for e in s.query(Entity).filter_by(case_id=case)}
def nm(m, x): return m.get(x, x)

def build_graph(s, case):
    m = names(s, case); G = nx.Graph(); w = Counter(); kinds = defaultdict(Counter)
    for e in s.query(Evidence).filter_by(case_id=case).filter(Evidence.type.in_(["message", "call"])):
        a, b = nm(m, e.actor), nm(m, e.target)
        if a == b: continue
        key = tuple(sorted((a, b))); w[key] += 1; kinds[key][e.type] += 1
    for (a, b), n in w.items(): G.add_edge(a, b, weight=n, **dict(kinds[(a, b)]))
    for n in G.nodes: G.nodes[n]["unknown"] = n.startswith("+") or n == "Unknown"
    deg = nx.degree_centrality(G); btw = nx.betweenness_centrality(G, weight=None)
    nodes = [{"id": n, "degree": round(deg[n], 3), "betweenness": round(btw[n], 3), "unresolved_number": G.nodes[n]["unknown"]} for n in G.nodes]
    edges = [{"source": a, "target": b, **d} for a, b, d in G.edges(data=True)]
    return {"nodes": nodes, "edges": edges}

def strongest(s, case, person, k=5):
    g = build_graph(s, case); out = [(e["target"] if e["source"] == person else e["source"], e["weight"]) for e in g["edges"] if person in (e["source"], e["target"])]
    return sorted(out, key=lambda x: -x[1])[:k]

def correlations(s, case, window_min=5, radius_m=400):
    """Anchor on GPS fixes; attach non-GPS evidence by the same person within the window.
    Also flag co-location of different people within window/radius."""
    m = names(s, case); ev = s.query(Evidence).filter_by(case_id=case).order_by(Evidence.ts).all()
    locs = [e for e in ev if e.type == "location"]; other = [e for e in ev if e.type != "location"]
    win = timedelta(minutes=window_min); clusters = []; used = set()
    for a in locs:
        if a.evidence_id in used: continue
        members = [a]
        for b in locs:   # co-located others
            if b is not a and b.actor != a.actor and abs(b.ts - a.ts) <= win and hav(a.lat, a.lon, b.lat, b.lon) <= radius_m: members.append(b)
        people = {x.actor for x in members}
        for o in other:  # same-person activity near the fix
            if abs(o.ts - a.ts) <= win and (nm(m, o.actor) in people or nm(m, o.target) in people): members.append(o)
        if len(members) < 3 or len({x.type for x in members}) < 2 and len(people) < 2: continue
        used.update(x.evidence_id for x in members if x.type == "location")
        types = {x.type for x in members}; ts = sorted(x.ts for x in members)
        span = (ts[-1] - ts[0]).total_seconds() / 60
        conf = min(0.97, 0.35 + 0.12 * len(members) + 0.1 * len(types) + 0.08 * (len(people) > 1) - 0.01 * span)
        clusters.append({"confidence": round(conf, 2), "center": [a.lat, a.lon], "start": ts[0].isoformat(), "end": ts[-1].isoformat(),
            "people": sorted(people), "types": sorted(types), "evidence_ids": [x.evidence_id for x in members],
            "explanation": f"{len(members)} artifacts of {len(types)} types ({', '.join(sorted(types))}) fall within {span:.0f} min and {radius_m} m; {len(people)} person(s) involved."})
    return sorted(clusters, key=lambda c: -c["confidence"])

def anomalies(s, case):
    m = names(s, case); out = []; ev = s.query(Evidence).filter_by(case_id=case).order_by(Evidence.ts).all()
    # 1. communication bursts (hourly z-score)
    hrs = Counter(e.ts.replace(minute=0, second=0) for e in ev if e.type in ("message", "call"))
    if len(hrs) > 3:
        mu, sd = statistics.mean(hrs.values()), statistics.pstdev(hrs.values()) or 1
        for h, n in hrs.items():
            z = (n - mu) / sd
            if z > 2:
                ids = [e.evidence_id for e in ev if e.type in ("message", "call") and e.ts.replace(minute=0, second=0) == h]
                out.append({"kind": "communication_burst", "time": h.isoformat(), "score": round(z, 2), "detail": f"{n} communications at {h:%d %b %H}:00 (hourly mean {mu:.1f})", "evidence_ids": ids})
    # 2. contact with unresolved numbers
    for e in ev:
        if e.type in ("message", "call"):
            for p in (e.actor, e.target):
                if p and p.startswith("+") and nm(m, p) in ("Unknown", p): out.append({"kind": "unresolved_contact", "time": e.ts.isoformat(), "score": 1.0, "detail": f"Contact with unattributed number {p}", "evidence_ids": [e.evidence_id]})
    # 3. deletion indicators
    for e in ev:
        if e.type == "app" and ("delete" in (e.summary or "") or "clear" in (e.summary or "")):
            out.append({"kind": "deletion_indicator", "time": e.ts.isoformat(), "score": 2.0, "detail": f"{e.actor}: {e.target} {e.summary}", "evidence_ids": [e.evidence_id]})
    # 4. implausible speed per person
    by = defaultdict(list)
    for e in ev:
        if e.type == "location": by[e.actor].append(e)
    for who, pts in by.items():
        for a, b in zip(pts, pts[1:]):
            dt = (b.ts - a.ts).total_seconds()
            if dt < 60: continue
            kmh = hav(a.lat, a.lon, b.lat, b.lon) / dt * 3.6
            if kmh > 200: out.append({"kind": "rapid_location_change", "time": b.ts.isoformat(), "score": round(kmh / 100, 2), "detail": f"{who}: {kmh:.0f} km/h between fixes (possible GPS error or device change)", "evidence_ids": [a.evidence_id, b.evidence_id]})
    # 5. late-night activity (00:00-04:00), aggregated per person per day to limit noise
    agg = defaultdict(list)
    for e in ev:
        if e.type in ("message", "call") and e.ts.hour < 4: agg[(nm(m, e.actor), e.ts.date())].append(e)
    for (who, day), es in agg.items():
        if len(es) >= 2: out.append({"kind": "late_night_activity", "time": es[0].ts.isoformat(), "score": 0.4 + 0.1 * len(es), "detail": f"{who}: {len(es)} communications between 00:00-04:00 on {day}", "evidence_ids": [x.evidence_id for x in es]})
    for o in out: o["label"] = "Anomaly detected"   # never "criminal activity"
    return sorted(out, key=lambda o: -o["score"])

def relevance(s, case, incident, ref_loc, involved=None, k=15):
    m = names(s, case); ev = s.query(Evidence).filter_by(case_id=case).all(); res = []
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
        if sc > 0.15: res.append({"evidence_id": e.evidence_id, "type": e.type, "time": e.ts.isoformat(), "relevance": round(min(sc, 0.99), 2), "reasons": reasons, "summary": e.summary})
    return sorted(res, key=lambda r: -r["relevance"])[:k]

def geofence(s, case, lat, lon, radius_m):
    m = names(s, case); out = []
    for e in s.query(Evidence).filter_by(case_id=case, type="location").order_by(Evidence.ts):
        d = hav(lat, lon, e.lat, e.lon)
        if d <= radius_m: out.append({"id": e.evidence_id, "time": e.ts.isoformat(), "person": e.actor, "lat": e.lat, "lon": e.lon, "distance_m": round(d)})
    return out
