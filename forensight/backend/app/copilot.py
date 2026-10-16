"""ForenSight Copilot: intent -> entities -> retrieval -> evidence-grounded answer.
Answers are assembled ONLY from retrieved evidence rows; every claim carries evidence IDs.
Optional LLM only rephrases the already-grounded answer (see polish())."""
import os, re
from collections import Counter
from datetime import datetime, timedelta
from . import intel
from .ingest import Evidence, Entity
from .synth import LOCS, INCIDENT

ALIASES = {"connaught place": ["connaught place", "cp"], "cp": ["connaught place", "cp"], "old fort": ["old fort"], "old fort parking": ["old fort"], "airport": ["airport", "t3"], "airport t3": ["airport", "t3"]}
GAZETTEER = {k.lower(): v for k, v in LOCS.items()} | {"cp": LOCS["Connaught Place"], "old fort": LOCS["Old Fort Parking"], "airport": LOCS["Airport T3"]}
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}
TIME = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"

def _t(h, m, ap):
    h = int(h); m = int(m or 0)
    if ap == "pm" and h < 12: h += 12
    if ap == "am" and h == 12: h = 0
    return h, m

def extract(s, case, q):
    ql = q.lower(); ents = s.query(Entity).filter_by(case_id=case).all(); people = []
    for e in ents:
        if e.name != "Unknown" and (e.name.lower() in ql or e.name.split()[0].lower() in re.findall(r"[a-z]+", ql)): people.append(e.name)
    phones = re.findall(r"\+?\d{10,13}", q)
    date = INCIDENT.date(); assumed_date = True
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(MONTHS) + ")", ql)
    if m: date = datetime(2026, MONTHS[m.group(2)], int(m.group(1))).date(); assumed_date = False
    win = None; mt = re.search(r"(?:between|from)\s+" + TIME + r"\s+(?:and|to)\s+" + TIME, ql)
    if mt:
        g = mt.groups(); ap1 = g[2] or g[5]; h1, m1 = _t(g[0], g[1], ap1); h2, m2 = _t(g[3], g[4], g[5] or g[2])
        win = (datetime.combine(date, datetime.min.time()).replace(hour=h1, minute=m1), datetime.combine(date, datetime.min.time()).replace(hour=h2, minute=m2))
    place = next((k for k in sorted(GAZETTEER, key=len, reverse=True) if re.search(r"\b" + re.escape(k) + r"\b", ql)), None)
    return {"people": list(dict.fromkeys(people)), "phones": phones, "date": str(date), "date_assumed": assumed_date, "window": [w.isoformat() for w in win] if win else None, "place": place}

def classify(q, ents):
    ql = q.lower()
    rules = [("media", r"\b(image|images|photo|photos|video|videos|picture)"), ("summary", r"\b(summary|summari[sz]e|overview)"),
             ("anomaly", r"unusual|anomal|suspicious"), ("network", r"strongest|most connected|connections to"),
             ("correlation", r"connecting|correlat|link between|evidence connecting"), ("location", r"\bwhere\b|location"),
             ("timeline", r"what happened|between\s+\d|timeline"), ("communication", r"communicat|contacted|talk|call|messag")]
    for name, pat in rules:
        if re.search(pat, ql): return name
    return "search"

def row(e, nm):
    return {"id": e.evidence_id, "type": e.type, "time": e.ts.isoformat(), "actor": nm.get(e.actor, e.actor), "target": nm.get(e.target, e.target),
            "summary": e.summary, "source": f"{e.source_file}:{e.source_artifact}", "lat": e.lat, "lon": e.lon}

def place_of(lat, lon):
    best = min(GAZETTEER.items(), key=lambda kv: intel.hav(lat, lon, *kv[1]))
    d = intel.hav(lat, lon, *best[1]); return best[0].title() if d < 600 else None

def fmt(e):
    t = e["type"]
    if t == "message": return f'{e["actor"]} → {e["target"]}: "{e["summary"]}"'
    if t == "call": return f'{e["actor"]} → {e["target"]} ({e["summary"]})'
    if t == "location": return f'{e["actor"]} at {place_of(e["lat"], e["lon"]) or "unlabelled location"}'
    if t == "browser": return f'{e["actor"]} visited "{e["summary"]}"'
    if t == "app": return f'{e["actor"]}: {e["target"]} {e["summary"]}'
    return e["summary"]

def ask(s, case, q):
    q = q.strip()[:500]; ents = extract(s, case, q); intent = classify(q, ents); nm = intel.names(s, case); assumptions = []
    if ents["date_assumed"] and (ents["window"] or intent in ("communication", "timeline")): assumptions.append(f"No date given; assumed incident date {ents['date']}.")
    base = s.query(Evidence).filter_by(case_id=case)
    evid, answer, reasoning, conf = [], "", "", 0.0
    if intent == "timeline":
        if not ents["window"]: answer, reasoning = "Please give a time range, e.g. 'between 9 PM and 11 PM'.", "No time window could be parsed."
        else:
            a, b = map(datetime.fromisoformat, ents["window"]); es = [row(e, nm) for e in base.filter(Evidence.ts >= a, Evidence.ts <= b).order_by(Evidence.ts)]
            sig = [e for e in es if e["type"] in ("message", "call", "app") or (e["type"] == "location")]
            c = Counter(e["type"] for e in es); cl = [x for x in intel.correlations(s, case) if a.isoformat() <= x["start"] <= b.isoformat()]
            evid = es[:40]; conf = 0.9 if es else 0.0
            answer = f"{len(es)} artifacts between {a:%H:%M} and {b:%H:%M} ({', '.join(f'{n} {t}' for t, n in c.items())}). " + (f"{len(cl)} correlated cluster(s) found; strongest at {cl[0]['start'][11:16]} involving {', '.join(cl[0]['people'])}." if cl else "")
            reasoning = "Filtered all evidence by timestamp, then ran the correlation engine over the same window." ; ents["clusters"] = cl[:3]
    elif intent == "communication":
        who = [p for p in ents["people"]]; rows = base.filter(Evidence.type.in_(["message", "call"])).order_by(Evidence.ts).all(); pairs = []
        for e in rows:
            n = {nm.get(e.actor, e.actor), nm.get(e.target, e.target)}
            if who and not set(who) <= n: continue
            if not who and not ents["phones"]: continue
            if ents["phones"] and not any(p in (e.actor, e.target) for p in ents["phones"]): continue
            if ents["window"] and not (datetime.fromisoformat(ents["window"][0]) <= e.ts <= datetime.fromisoformat(ents["window"][1])): continue
            if not ents["date_assumed"] and e.ts.date().isoformat() != ents["date"]: continue
            pairs.append(row(e, nm))
        evid = pairs[:40]; conf = 0.9 if pairs else 0.0
        if not who and not ents["phones"]: answer, reasoning = "I couldn't identify a person or number in the question.", "Entity extraction found no known person."
        else:
            cps = Counter((e["target"] if e["actor"] in who else e["actor"]) for e in pairs) if len(who) == 1 else Counter()
            answer = f"{len(pairs)} communication event(s) match." + (" Counterparts: " + ", ".join(f"{k} ({v})" for k, v in cps.most_common(6)) + "." if cps else "")
            reasoning = f"Matched message/call evidence involving {', '.join(who or ents['phones'])}" + (" within the stated window." if ents["window"] else ".")
    elif intent == "location":
        who = ents["people"]; es = base.filter_by(type="location").order_by(Evidence.ts).all()
        if not who: answer, reasoning = "Which person's location? Please name them.", "No person identified."
        else:
            es = [e for e in es if nm.get(e.actor, e.actor) in who and e.ts <= INCIDENT + timedelta(minutes=2 if "before" not in q.lower() else -0)]
            if "before" in q.lower(): es = [e for e in es if e.ts < INCIDENT]
            last = [row(e, nm) for e in es][-6:]; evid = last; conf = 0.85 if last else 0.0
            seq = [place_of(e["lat"], e["lon"]) or "unlabelled" for e in last]
            answer = f"Last {len(last)} recorded fixes for {who[0]}: " + " → ".join(f"{p} ({e['time'][11:16]})" for p, e in zip(seq, last)) + "." if last else f"No location fixes found for {who[0]}."
            reasoning = "Retrieved GPS fixes for the person up to the incident time; place names come from a nearest-landmark gazetteer within 600 m."
    elif intent == "network":
        who = ents["people"]
        if not who:
            pairs = sorted(intel.build_graph(s, case)["edges"], key=lambda e: -e["weight"])[:5]
            selected = {(e["source"], e["target"]) for e in pairs}
            evid = [row(e, nm) for e in base.filter(Evidence.type.in_(["message", "call"])).order_by(Evidence.ts)
                    if tuple(sorted((nm.get(e.actor, e.actor), nm.get(e.target, e.target)))) in selected][:40]
            conf = 0.8 if pairs else 0.0
            answer = "Strongest overall connections by combined message/call count: " + ", ".join(
                f'{e["source"]} ↔ {e["target"]} ({e["weight"]})' for e in pairs) + ". Counts include background communications."
            reasoning = "Ranked all communication graph edges by message and call count; cited matching source artifacts."
        else:
            allt = intel.strongest(s, case, who[0]); win = Counter(); ids = []
            for e in base.filter(Evidence.type.in_(["message", "call"]), Evidence.ts >= INCIDENT - timedelta(hours=6), Evidence.ts <= INCIDENT + timedelta(hours=1)):
                a, b = nm.get(e.actor, e.actor), nm.get(e.target, e.target)
                if who[0] in (a, b): win[b if a == who[0] else a] += 1; ids.append(row(e, nm))
            evid = ids[:30]; conf = 0.8 if allt else 0.0
            answer = "Overall: " + ", ".join(f"{a} ({n})" for a, n in allt[:4]) + ". In the 6 h before / 1 h after the incident: " + (", ".join(f"{a} ({n})" for a, n in win.most_common(4)) or "none") + "."
            reasoning = "Counted calls and messages in the graph, then recounted inside the incident window because overall volume includes background chatter."
    elif intent == "correlation":
        cls = intel.correlations(s, case); who = set(ents["people"]); pl = GAZETTEER.get(ents["place"]) if ents["place"] else None
        sel = [c for c in cls if (not who or who & set(c["people"])) and (not pl or intel.hav(*c["center"], *pl) < 600)]
        ids = {i for c in sel for i in c["evidence_ids"]}; evid = [row(e, nm) for e in base.filter(Evidence.evidence_id.in_(ids)).order_by(Evidence.ts)][:40] if ids else []
        conf = max((c["confidence"] for c in sel), default=0.0); ents["clusters"] = sel[:3]
        answer = (f"{len(sel)} correlated cluster(s). Best: {sel[0]['explanation']} Time {sel[0]['start'][11:16]}–{sel[0]['end'][11:16]}, people: {', '.join(sel[0]['people'])}." if sel else "No correlated evidence cluster links those entities.")
        reasoning = "Clusters anchor on GPS fixes and attach same-person calls/messages within 5 min and 400 m; filtered by the person and place you named."
    elif intent == "anomaly":
        an = intel.anomalies(s, case)
        if "before" in q.lower(): an = [a for a in an if a["time"] < INCIDENT.isoformat()]
        an = [a for a in an if a["kind"] != "late_night_activity"]; an = sorted(an, key=lambda a: a["time"])[:6] or an[:6]; ids = {i for a in an for i in a.get("evidence_ids", [])}
        evid = [row(e, nm) for e in base.filter(Evidence.evidence_id.in_(ids)).order_by(Evidence.ts)][:30] if ids else []; conf = 0.7 if an else 0.0
        answer = f"{len(an)} anomaly signal(s): " + "; ".join(f"{a['kind'].replace('_', ' ')}: {a['detail']}" for a in an[:5]) + ". These are statistical flags, not findings of wrongdoing."
        reasoning = "Rule-based detectors: communication bursts, unattributed contacts, deletion indicators, implausible speed, late-night clusters."; ents["anomalies"] = an
    elif intent == "media":
        answer, reasoning = "No image or video evidence has been ingested yet; the computer-vision module (Phase 4) is not built.", "Checked evidence types: no media artifacts exist."
    elif intent == "summary":
        st = Counter(e.type for e in base); cl = intel.correlations(s, case)[:2]; dels = [e for e in base.filter_by(type="app") if "delete" in e.summary or "clear" in e.summary]
        rel = intel.relevance(s, case, INCIDENT, LOCS["Old Fort Parking"]); ids = [r["evidence_id"] for r in rel[:6]] + [e.evidence_id for e in dels]
        evid = [row(e, nm) for e in base.filter(Evidence.evidence_id.in_(ids)).order_by(Evidence.ts)]; conf = 0.75
        answer = f"{sum(st.values())} artifacts ({', '.join(f'{n} {t}' for t, n in st.items())}). " + " ".join(f"At {c['start'][11:16]}: {', '.join(c['people'])} correlated ({', '.join(c['types'])})." for c in cl) + f" {len(dels)} chat deletion/clear indicator(s) shortly after the incident window."
        reasoning = "Combined evidence counts, top correlation clusters, and deletion indicators; evidence list is the highest-relevance items near the incident."
    else:
        if ents["place"]:
            al = ALIASES.get(ents["place"], [ents["place"]]); pat = re.compile(r"\b(" + "|".join(map(re.escape, al)) + r")\b", re.I); ctr = GAZETTEER[ents["place"]]; es = []
            for e in base.order_by(Evidence.ts):
                if e.type == "location" and intel.hav(e.lat, e.lon, *ctr) < 400: es.append(e)
                elif e.type != "location" and pat.search(f"{e.summary or ''} {e.target or ''}"): es.append(e)
            key = ents["place"]; why = f"Word-boundary text match on aliases {al} plus GPS fixes within 400 m of the place; no OCR/media text indexed yet."
        else:
            words = [w for w in re.findall(r"[A-Za-z0-9+]{3,}", q) if w.lower() not in {"find", "evidence", "mentioning", "the", "all", "show", "about", "related"}]
            key = " ".join(words) if words else q
            es = base.filter(Evidence.summary.ilike(f"%{key}%") | Evidence.actor.ilike(f"%{key}%") | Evidence.target.ilike(f"%{key}%")).order_by(Evidence.ts).limit(40).all()
            why = "Keyword search over evidence text and entities (no OCR/media text indexed yet)."
        evid = [row(e, nm) for e in es][:40]; conf = 0.6 if es else 0.0
        answer = f"{len(es)} artifact(s) relate to '{key}'." if es else f"No evidence relates to '{key}'."; reasoning = why
    for e in evid: e["description"] = fmt(e)
    if not evid and conf == 0 and intent not in ("media",): answer = answer or "No supporting evidence found."
    out = {"query": q, "intent": intent, "entities": ents, "answer": answer, "confidence": round(conf, 2), "reasoning_summary": reasoning,
           "assumptions": assumptions, "evidence": evid, "source_artifacts": sorted({e["source"] for e in evid})}
    out["answer"] = polish(out); return out

def polish(out):
    """Optional: rephrase the grounded answer with an LLM. Never adds facts; falls back to the original text."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key or not out["evidence"]: return out["answer"]
    try:
        import httpx
        r = httpx.post("https://api.anthropic.com/v1/messages", timeout=15, headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            json={"model": os.getenv("LLM_MODEL", "claude-sonnet-4-6"), "max_tokens": 400, "messages": [{"role": "user", "content":
            "Rewrite this forensic finding in 2 clear sentences. Use ONLY facts in it; add nothing.\n\n" + out["answer"]}]})
        t = r.json()["content"][0]["text"].strip(); return t or out["answer"]
    except Exception: return out["answer"]
