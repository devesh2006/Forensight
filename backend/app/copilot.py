"""Deterministic case queries. Every returned example points to an imported artifact."""
from datetime import date, datetime, time, timedelta
import json
import logging
import os
from pathlib import Path
import re

logger = logging.getLogger("forensight.copilot")

try:
    if "PYTEST_CURRENT_TEST" not in os.environ:
        from dotenv import load_dotenv
        for _p in [Path(__file__).resolve().parents[2] / ".env", Path(__file__).resolve().parents[1] / ".env"]:
            if _p.exists():
                load_dotenv(_p)
        load_dotenv()
except Exception:
    pass

from sqlalchemy import extract, func, or_

from . import intel
from .ingest import CaseRecord, Entity, Evidence

CLOCK = r"(?<![\w:])(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<period>[ap]\.?m\.?)?(?![\w:])"
CLOCK_PATTERN = re.compile(CLOCK, re.I)
DATE_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
TIME_QUESTION = re.compile(r"\b(?:happened|occurred|events?|timeline|activity|records?|evidence)\b", re.I)
SUMMARY_QUESTION = re.compile(
    r"(?:what(?: is|'s) (?:this|the) case about|"
    r"(?:tell me|what do we know) about (?:this|the) case|"
    r"(?:summarize|describe) (?:this|the) case|"
    r"(?:give me |show me )?(?:an? |the )?(?:case summary|case overview|summary of (?:this|the) case|overview of (?:this|the) case))"
)
PARTICIPANT_QUESTION = re.compile(
    r"(?:what messages did (?P<sender>.+) send|"
    r"who did (?P<caller>.+) call|"
    r"(?:show(?: me)? |list )?(?P<kind>messages|calls) (?:from|by) (?P<actor>.+)|"
    r"(?:show(?: me)? |list )?(?P<target_kind>messages|calls) to (?P<target>.+)|"
    r"(?:show(?: me)? |list )?(?P<participant_kind>messages|calls) (?:with|involving) (?P<participant>.+))"
)
SEARCH_QUESTION = re.compile(
    r"^(?:find|search(?: for)?|show(?: me)?|do we have (?:any )?(?:evidence|records) "
    r"(?:about|for|mentioning)|what (?:do we know|does the evidence say) about)\s+", re.I
)
TYPE_LABELS = {
    "message": ("message", "messages"),
    "call": ("call log", "call logs"),
    "location": ("location record", "location records"),
    "browser": ("browser record", "browser records"),
    "app": ("app activity record", "app activity records"),
    "file": ("file artifact", "file artifacts"),
    "other": ("other record", "other records"),
    "system": ("system record", "system records"),
}


def ai_provider():
    return "gemini" if os.getenv("GEMINI_API_KEY") else "none"


def interpret_question(query):
    """Use Gemini only to map the question to a small, validated retrieval command."""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    try:
        from google import genai
        from google.genai import types

        with genai.Client(api_key=key, http_options=types.HttpOptions(timeout=5000)) as client:
            response = client.models.generate_content(
                model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
                contents=(
                    "Convert the user's question into a retrieval command. Do not answer it. "
                    "Choose summary, strongest_connections, timeline, or search. "
                    "For timeline, return one explicit calendar date as YYYY-MM-DD. "
                    "For search, return concise literal terms found in the question. "
                    "Do not infer names, dates, events, or evidence. If no date is explicit, "
                    "do not choose timeline. User question: " + query[:500]
                ),
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema={
                        "type": "object",
                        "properties": {
                            "intent": {"type": "string", "enum": ["summary", "strongest_connections", "timeline", "search"]},
                            "date": {"type": "string"},
                            "terms": {"type": "string"},
                        },
                        "required": ["intent", "date", "terms"],
                    },
                    temperature=0,
                    max_output_tokens=512,
                ),
            )
        command = json.loads(response.text or "{}")
        if not isinstance(command, dict):
            return None
        intent = command.get("intent")
        if intent not in {"summary", "strongest_connections", "timeline", "search"}:
            return None
        date = command.get("date", "")
        terms = command.get("terms", "")
        if not isinstance(date, str) or not isinstance(terms, str):
            return None
        if intent == "timeline":
            if date not in query:
                return None
            datetime.strptime(date, "%Y-%m-%d")
        if intent == "search" and (not terms.strip() or terms.casefold() not in query.casefold()):
            return None
        return {"intent": intent, "date": date, "terms": terms[:120]}
    except Exception:
        return None


def clock_minute(match):
    hour = int(match["hour"])
    minute = int(match["minute"] or 0)
    period = (match["period"] or "").replace(".", "").lower()
    if minute > 59 or hour > 23 or (period and not 1 <= hour <= 12):
        raise ValueError("Invalid clock time")
    if period:
        hour = hour % 12 + (12 if period == "pm" else 0)
    return hour * 60 + minute


def timeline_command(question):
    calendar_date = DATE_PATTERN.search(question)
    time_text = DATE_PATTERN.sub(" ", question)
    clocks = []
    for match in CLOCK_PATTERN.finditer(time_text):
        if match["minute"] or match["period"]:
            clocks.append(match)
    is_range = re.search(r"\bbetween\b|\bfrom\b.*(?:\bto\b|[–—])", time_text)
    if is_range and clocks:
        # Allow "between 9 and 11 pm", where one endpoint supplies the meridiem.
        clocks = list(CLOCK_PATTERN.finditer(time_text))
    if clocks:
        end = clocks[1] if len(clocks) == 2 and is_range else None
        return {"intent": "clock", "start": clocks[0], "end": end,
                "ambiguous": len(clocks) > (2 if end else 1) or bool(is_range and not end),
                "date": calendar_date[0] if calendar_date else None}
    if calendar_date or question.startswith("timeline "):
        return {"intent": "timeline", "date": calendar_date[0] if calendar_date else ""}
    if question == "timeline" or question in {"show timeline", "show me the timeline"}:
        return {"intent": "timeline", "date": ""}
    return None


def local_command(question):
    if question in {"summary", "overview"} or SUMMARY_QUESTION.fullmatch(question):
        return {"intent": "summary"}
    if question in {"strongest connections", "what are the strongest connections"}:
        return {"intent": "strongest_connections"}
    if question in {"locations", "map", "gps fixes", "show recorded locations"}:
        return {"intent": "locations"}
    if question in {"call logs", "calls", "show calls", "show call logs"}:
        return {"intent": "calls"}
    if not question.startswith(("find ", "search ")) and (
        TIME_QUESTION.search(question) or question.startswith(("at ", "between "))
    ):
        command = timeline_command(question)
        if command:
            return command
    participant = PARTICIPANT_QUESTION.fullmatch(question)
    if participant:
        actor = participant["sender"] or participant["caller"] or participant["actor"]
        kind = participant["kind"] or participant["target_kind"] or participant["participant_kind"]
        if participant["sender"]:
            kind = "messages"
        elif participant["caller"]:
            kind = "calls"
        return {"intent": "participant", "kind": "call" if kind == "calls" else "message",
                "term": actor or participant["target"] or participant["participant"],
                "fields": (Evidence.actor,) if actor else (Evidence.target,) if participant["target"]
                else (Evidence.actor, Evidence.target)}
    return None


def select_clock_records(records, command):
    if command["ambiguous"]:
        raise ValueError("Use one clock time or one time range.")
    start_match, end_match = command["start"], command["end"]
    start = clock_minute(start_match)
    end = clock_minute(end_match) if end_match else start
    if end_match and bool(start_match["period"]) != bool(end_match["period"]):
        missing = start_match if not start_match["period"] else end_match
        if 1 <= int(missing["hour"]) <= 12 and (end - start) % 1440 > 720:
            if missing is start_match:
                start = (start + 720) % 1440
            else:
                end = (end + 720) % 1440
    label = f"{start // 60:02d}:{start % 60:02d}"
    if end_match:
        label += f"–{end // 60:02d}:{end % 60:02d}"
    if command["date"]:
        day = datetime.combine(date.fromisoformat(command["date"]), time.min)
        first = day + timedelta(minutes=start)
        last = day + timedelta(minutes=end + 1, days=int(end < start))
        return records.filter(Evidence.ts >= first, Evidence.ts < last), label
    minute_of_day = extract("hour", Evidence.ts) * 60 + extract("minute", Evidence.ts)
    if end < start:
        return records.filter(or_(minute_of_day >= start, minute_of_day <= end)), label
    return records.filter(minute_of_day.between(start, end)), label


def evidence_counts(records):
    return records.order_by(None).with_entities(Evidence.type, func.count()).group_by(Evidence.type).all()


def describe_counts(counts):
    kinds = []
    for kind, count in sorted(counts, key=lambda item: (-item[1], item[0] or "")):
        singular, plural = TYPE_LABELS.get(kind, ("unclassified record", "unclassified records"))
        kinds.append(f"{count} {singular if count == 1 else plural}")
    return ", ".join(kinds)


def case_summary(session, case, records):
    counts = evidence_counts(records)
    files = records.order_by(None).with_entities(Evidence.source_file).filter(
        Evidence.source_file.isnot(None)
    ).distinct().count()
    case_record = session.get(CaseRecord, case) if case else None
    name = case_record.name if case_record else "This case"
    answer = f"{name} contains {sum(count for _, count in counts)} imported records"
    if files:
        answer += f" from {files} source file{'s' if files != 1 else ''}"
    answer += ". The evidence includes " + describe_counts(counts) + "."
    if case_record and case_record.description:
        answer += " Case description: " + case_record.description
    elif case_record and case_record.incident_type:
        answer += f" The case metadata lists the incident type as {case_record.incident_type}."
    else:
        answer += " Add a case description to document what is being investigated."
    return answer


def ask(session, case, query, incident=None, reference_location=None):
    question = " ".join(query.strip().casefold().rstrip("?!").split())
    records = session.query(Evidence).filter_by(case_id=case).order_by(Evidence.ts, Evidence.id)
    has_records = records.first() is not None
    command = local_command(question) if has_records else None
    if has_records and command is None:
        search = SEARCH_QUESTION.match(query.strip())
        if search:
            term = query.strip()[search.end():].strip().rstrip("?").strip('"“”')
            command = {"intent": "search", "terms": term}
        else:
            command = interpret_question(query)
    command = command or {"intent": "search", "terms": query.strip().rstrip("?")}
    matches = []
    reasoning = "Read from the selected case."
    if not has_records:
        answer = "No evidence has been imported into this case."
    elif command["intent"] == "summary":
        answer = case_summary(session, case, records)
        matches = records.limit(20).all()
        reasoning = "Counted imported records by type. Citations show up to 20 examples."
    elif command["intent"] == "strongest_connections":
        edges = intel.build_graph(session, case)["edges"]
        edges.sort(key=lambda edge: -edge["total_interactions"])
        if edges:
            edge = edges[0]
            answer = f'{edge["source"]} and {edge["target"]} have {edge["total_interactions"]} recorded interactions.'
            matches = records.filter(Evidence.evidence_id.in_(edge["evidence_ids"][:20])).all()
            reasoning = "Highest combined count of timestamped calls and messages. Ties can occur."
        else:
            answer = "No timestamped calls or messages with two identified endpoints were found."
    elif command["intent"] in {"locations","calls"}:
        kind = "location" if command["intent"] == "locations" else "call"
        selected = records.filter_by(type=kind)
        if kind == "location":
            selected = selected.filter(Evidence.lat.isnot(None),Evidence.lon.isnot(None))
        answer = f"{selected.count()} imported {kind} records. Showing up to 20."
        matches = selected.limit(20).all()
        reasoning = "Filtered imported records by evidence type."
    elif command["intent"] == "timeline":
        try:
            day = datetime.combine(date.fromisoformat(command.get("date", "")), time.min)
            end = day + timedelta(days=1)
        except (OverflowError, ValueError):
            answer = "Enter a case-local date, for example: What happened on 2025-04-16?"
        else:
            events = records.filter(Evidence.ts >= day, Evidence.ts < end)
            count = events.count()
            answer = f"{count} timestamped records on {day.date()}."
            matches = events.limit(20).all()
            reasoning = "Matched the requested date in the case timezone."
    elif command["intent"] == "clock":
        try:
            events, clock_label = select_clock_records(records, command)
        except (OverflowError, ValueError):
            answer = "Enter a valid time or range, for example: What happened at 9:25 PM?"
        else:
            counts = evidence_counts(events)
            count = sum(total for _, total in counts)
            scope = f'on {command["date"]}' if command["date"] else "across all recorded dates"
            when = f"{clock_label} {scope}"
            if count:
                kinds = describe_counts(counts)
                answer = f"Found {count} matching timestamped {'records' if count != 1 else 'record'} at {when}. Evidence includes {kinds}."
                matches = events.limit(20).all()
                if count > len(matches):
                    answer += f" Showing the first {len(matches)}."
            else:
                answer = f"No timestamped records match {when}. Try a wider time range."
            reasoning = "Matched the requested clock time in the case timezone, to the minute."
    elif command["intent"] == "participant":
        term = command["term"]
        names = session.query(Entity.phone).filter(Entity.case_id == case, func.lower(Entity.name) == term)
        results = records.filter(Evidence.type == command["kind"], or_(*(
            or_(func.lower(field) == term, field.in_(names)) for field in command["fields"]
        )))
        count = results.count()
        label = TYPE_LABELS[command["kind"]][count != 1]
        answer = f"Found {count} matching {label} for “{term}”."
        matches = results.limit(20).all()
        reasoning = "Matched the participant against stored names and identifiers."
    else:
        term = command.get("terms", query.strip()).strip()
        if not term:
            return {"case_id": case, "query": query, "answer": "Please include a person, evidence ID, source filename, or text to search for.",
                    "reasoning_summary": "No search term was provided.", "evidence": [], "source_artifacts": [], "assumptions": []}
        fields = (Evidence.evidence_id, Evidence.actor, Evidence.target, Evidence.summary, Evidence.source_file, Evidence.original_id, Evidence.meta)
        exact_results = records.filter(or_(*(func.lower(field).contains(term.lower(), autoescape=True) for field in fields)))
        exact_matches = exact_results.all()
        exact_ids = {e.evidence_id for e in exact_matches}

        def _match_exact_content(text, target):
            if not text:
                return False
            for line in text.splitlines():
                line_str = line.strip().lower()
                if line_str == target:
                    return True
                cleaned = re.sub(r"^\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?\s*", "", line_str, flags=re.I).strip()
                if cleaned == target:
                    return True
            return False

        # Keep exact evidence IDs, participant names, filenames, and exact records reliable
        is_exact_identity = bool(exact_matches) and any(
            term.lower() == (e.evidence_id or "").lower()
            or term.lower() == (e.original_id or "").lower()
            or term.lower() == (e.source_file or "").lower()
            or (e.source_file and (e.source_file.lower().endswith("/" + term.lower()) or e.source_file.lower().endswith("\\" + term.lower())))
            or _match_exact_content(e.summary, term.lower())
            or (e.actor and term.lower() == e.actor.lower())
            or (e.target and term.lower() == e.target.lower())
            for e in exact_matches
        )

        semantic_matches = []
        if not is_exact_identity:
            try:
                from . import embeddings
                semantic_candidates = embeddings.semantic_search(session, case, term, top_k=20, min_similarity=0.65)
                if not semantic_candidates and query.strip().casefold() != term.casefold():
                    semantic_candidates = embeddings.semantic_search(session, case, query.strip(), top_k=20, min_similarity=0.65)
                semantic_matches = [e for e, score in semantic_candidates if e.evidence_id not in exact_ids]
            except Exception as exc:
                logger.warning("Semantic search fallback to literal: %s", exc)
                semantic_matches = []

        combined_matches = exact_matches + semantic_matches
        count = len(combined_matches)
        matches = combined_matches[:20]

        if not count:
            answer = f'No records match “{term}”. Try a name, evidence ID, or shorter phrase.'
            reasoning = "Literal and semantic search found no supporting records in this case."
        elif exact_matches and semantic_matches:
            answer = f'{count} records match “{term}” ({len(exact_matches)} exact, {len(semantic_matches)} semantic).'
            if count > len(matches):
                answer += f" Showing the first {len(matches)}."
            reasoning = "Hybrid search combining exact matches (record IDs, participants, summaries, filenames) with semantic vector similarity (BAAI/bge-small-en-v1.5). Exact matches are ranked first."
        elif exact_matches:
            answer = f'{len(exact_matches)} records match “{term}”.'
            if len(exact_matches) > len(matches):
                answer += f" Showing the first {len(matches)}."
            reasoning = "Literal search over record IDs, participants, summaries and filenames."
        else:
            answer = f'{len(semantic_matches)} records semantically match “{term}”.'
            if len(semantic_matches) > len(matches):
                answer += f" Showing the first {len(matches)}."
            reasoning = "Semantic search using local BAAI/bge-small-en-v1.5 embeddings."
    evidence = []
    names = intel.names(session, case) if matches else {}
    term = command.get("terms", "")
    for e in matches:
        summary = e.summary or ""
        hit = summary.lower().find(term.lower()) if term else 0
        offset = max(0, hit - 80)
        excerpt = ("…" if offset else "") + summary[offset:offset+240]
        if len(summary) > offset+240:
            excerpt += "…"
        evidence.append({"id":e.evidence_id,"source":e.source_file,"locator":e.source_artifact,
                         "source_sha256":e.source_file_sha256,"summary":excerpt,"type":e.type,
                         "actor":names.get(e.actor, e.actor),"target":names.get(e.target, e.target),
                         "lat":e.lat,"lon":e.lon,"time":intel.case_time(e) if e.ts else None})
    return {"case_id": case, "query": query, "answer": answer, "reasoning_summary": reasoning,
            "evidence": evidence, "source_artifacts": sorted({e.source_file for e in matches if e.source_file}), "assumptions":[]}
