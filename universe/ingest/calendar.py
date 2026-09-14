#!/usr/bin/env python3
"""
Ingest the District 49 office calendar.

The calendar is the office's real operating system: hearings the CM must be
at, community events with a staff owner, RSVP deadlines, and budget-cycle
dates. Event titles follow the office's own conventions -- a staff-initials
prefix ("KH/AS/TP:"), a type marker ("HEARING:", "OVERSIGHT+", "TP:", "FYI:",
"RSVP due"), and sometimes a location.

This module reads that convention so the calendar becomes queryable: what is
Hanks personally committed to, what belongs to whom, and what is a deadline
rather than an event.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from ..core.config import PILLARS, STAFF_INITIALS
from ..core.store import Store

# "KH/AS/TP: Meeting with ..." or "OK: AG James Briefing"
PREFIX_RE = re.compile(r"^\s*([A-Z]{2}(?:\s*/\s*[A-Z]{2})*)\s*:\s*")
KNOWN = set(STAFF_INITIALS)

KIND_RULES = (
    ("hearing", ("hearing", "oversight", "committee on", "stated meeting",
                 "council chambers", "preconsidered", "budget hearing")),
    ("deadline", ("rsvp due", "deadline", "due ", "submission", "closes",
                  "last day", "filing")),
    ("caucus", ("dem conference", "conference", "caucus", "delegation",
                "borough board", "members lounge")),
    ("community", ("invitation", "civic", "community", "fair", "parade",
                   "walk", "festival", "block party", "open streets",
                   "ribbon", "groundbreaking", "town hall")),
    ("staff", ("staff:", "fyi:", "training", "check-in", "planning meeting",
               "briefing", "info session")),
    ("out", ("out", "closed", "holiday", "personal", "pto", "vacation")),
)


def classify(summary: str, location: str = "") -> str:
    low = f"{summary} {location}".lower()
    for kind, needles in KIND_RULES:
        if any(n in low for n in needles):
            return kind
    return "event"


def owners(summary: str) -> list[str]:
    """Staff initials the office prefixed onto the title."""
    m = PREFIX_RE.match(summary or "")
    if not m:
        # Also catch a bare "STAFF:" or trailing "KH OUT"
        tail = re.match(r"^\s*([A-Z]{2})\s+(OUT|OOO)\b", summary or "")
        return [tail.group(1)] if tail and tail.group(1) in KNOWN else []
    found = [p.strip() for p in m.group(1).split("/")]
    return [p for p in found if p in KNOWN]


def strip_prefix(summary: str) -> str:
    return PREFIX_RE.sub("", summary or "").strip()


def pillar_for(summary: str, location: str = "") -> str | None:
    low = f"{summary} {location}".lower()
    best, score = None, 0
    for key, spec in PILLARS.items():
        n = sum(1 for k in spec["keywords"] if k.lower() in low)
        if n > score:
            best, score = key, n
    return best


def _when(node: Any) -> str | None:
    if isinstance(node, dict):
        return node.get("dateTime") or node.get("date")
    return node if isinstance(node, str) else None


def normalize(events: Iterable[dict]) -> list[dict]:
    out = []
    for e in events:
        summary = e.get("summary") or ""
        location = e.get("location") or ""
        out.append({
            "event_id": e.get("id") or f"cal-{abs(hash((summary, _when(e.get('start')))))}",
            "start": _when(e.get("start")),
            "end": _when(e.get("end")),
            "summary": strip_prefix(summary) or summary,
            "location": location or None,
            "kind": classify(summary, location),
            "owners": json.dumps(owners(summary)),
            "pillar": pillar_for(summary, location),
            "link": e.get("htmlLink"),
            "updated": e.get("updated"),
        })
    return out


def ingest(store: Store, events: Iterable[dict],
           source_id: str = "D49_CALENDAR") -> dict:
    rows = normalize(events)
    n = store.upsert("calendar", rows)
    store.index_many([
        ("calendar", r["event_id"],
         r["summary"] or "",
         " ".join(str(x) for x in (r["location"], r["kind"], r["pillar"]) if x),
         " ".join(json.loads(r["owners"] or "[]")))
        for r in rows
    ])
    store.journal("ingest.calendar", {"events": n, "source": source_id})
    return {"calendar_events": n}


def ingest_connector_file(store: Store, path: Path | str) -> dict:
    """Load a Google Calendar connector result file."""
    raw = json.loads(Path(path).read_text())
    events = raw.get("events", raw if isinstance(raw, list) else [])
    return ingest(store, events)
