#!/usr/bin/env python3
"""
Meeting packets: the calendar as a work queue.

Every item on the office calendar implies a deliverable. A committee meeting
needs a read on what is on the agenda and where the Councilmember stands. A
budget hearing needs the district's numbers at hand. An oversight hearing needs
something harder: a question set sharp enough that the agency cannot run out
the clock on it.

This module turns a calendar entry into that packet. The evidence is assembled
from the lake first -- the agenda items, the money at stake, the district
figures, the office's own record on the subject -- and only then does the AI
layer write over it. With no key the packet is plainer in its prose and
identical in its facts.

The shape follows the house style without being asked: a paragraph that states
the answer, bullets that each carry one fact, what it means for Staten Island
and District 49, and three questions she can ask out loud. An oversight hearing
gets the long form instead: an opening statement, question blocks with the
follow-up ready for the answer the agency will give, and a closing.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from ..ai import council as AI
from ..core.citations import DEFAULT_SOURCES, CitationRegistry, VERIFY_TAG
from ..core.config import (DISTRICT, DISTRICT_LABEL, LAND_USE_DOCTRINE,
                           MEMBER_NAME, PILLARS)
from ..core.store import Store
from ..intel import funding as FI
from ..intel import legislation as LI
from . import breakdown as BD

PACKET_SCHEMA = """
CREATE TABLE IF NOT EXISTS ws_packets (
  id TEXT PRIMARY KEY,
  event_id TEXT, kind TEXT, title TEXT, meeting_at TEXT,
  committee TEXT, body TEXT, evidence TEXT, mode TEXT,
  created TEXT, created_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_packets_event ON ws_packets(event_id);
CREATE INDEX IF NOT EXISTS ix_packets_when ON ws_packets(meeting_at);
"""

# A calendar title carries the committee. These are the ones this office sits
# on or cares about; the match is on the phrase the Council itself uses.
COMMITTEE_HINTS = (
    "public safety", "finance", "land use", "housing and buildings",
    "transportation", "cultural affairs", "parks", "health", "hospitals",
    "economic development", "small business", "sanitation", "education",
    "aging", "mental health", "youth services", "civil service",
    "governmental operations", "resiliency", "environmental protection",
    "fire and emergency management", "oversight and investigations",
    "contracts", "immigration", "veterans", "waterfronts",
    "zoning and franchises", "landmarks", "planning dispositions",
)

OVERSIGHT_MARKERS = ("oversight", "preliminary budget hearing",
                     "executive budget hearing", "budget hearing")

# What an agency says when it does not want to answer, and what to say back.
# An oversight question without a prepared follow-up is a press release.
DEFLECTIONS = (
    ("we are working on it",
     "What is the date, and who owns it? If there is no date, say so on the record."),
    ("we don't have that data",
     "Who does have it, and when will this committee get it in writing?"),
    ("that is a budget question",
     "Then give the committee the line and the fiscal year, and we will take it up in Finance."),
    ("we will follow up",
     "Within how many days? The committee will hold the record open for that answer."),
    ("that is operational",
     "Operations are what this committee oversees. Walk us through the decision."),
    ("citywide, the numbers are improving",
     f"I asked about District {DISTRICT}. Give me the borough and the district figure."),
)


def init(store: Store) -> None:
    store.conn.executescript(PACKET_SCHEMA)
    store.conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------- reading the event ----
# Scaffolding words in a calendar title that are not the subject of the
# hearing. Stripped, what is left is what the hearing is actually about.
_TITLE_NOISE = re.compile(
    r"(?i)\b(committee on|subcommittee on|oversight|hearing|meeting|joint|"
    r"preconsidered|stated|remote|virtual|public)\b")


def clean_topic(title: str) -> str:
    """
    The subject of a meeting, with the scaffolding removed.

    Calendar titles read "HEARING: Committee on Public Safety -- Oversight:
    NYPD Response Times". The subject is the last clause; everything before it
    names the room. Getting this wrong puts "Committee on -- :" into a question
    the Councilmember reads out loud, so the separators are cleaned too.
    """
    text = re.sub(r"^[A-Z]{2,}(?:\s*/\s*[A-Z]{2,})*\s*:\s*", "", (title or "").strip())
    # Prefer the clause after the last separator: that is the actual subject.
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+|:", text) if p.strip()]
    parts = [p for p in parts if _TITLE_NOISE.sub("", p).strip(" -–—:,")]
    subject = parts[-1] if parts else text
    subject = _TITLE_NOISE.sub(" ", subject)
    subject = re.sub(r"[\s\-–—:,]{2,}", " ", subject).strip(" -–—:,")
    return subject or re.sub(r"[\s\-–—:,]{2,}", " ", _TITLE_NOISE.sub(" ", text)).strip(" -–—:,")


# "OVERSIGHT: Committee on Children and Youth jointly with the Committee on ..."
COMMITTEE_RE = re.compile(
    r"(?i)\b(?:sub)?committee\s+on\s+(?P<name>[A-Za-z&'\- ]{3,80}?)"
    r"(?=\s+(?:jointly|and the|with)\b|[,:;\u2013\u2014]|\s+\d|$)")

# A meeting that was called off still sits on the calendar. Preparing a packet
# for it wastes a staffer's morning and, worse, leaves the office believing a
# hearing is happening that is not.
DEFERRED_RE = re.compile(r"(?i)^\s*(deferr?ed|defered|deffered|cancell?ed|"
                         r"postponed|rescheduled)\b|\((?i:cancell?ed)\)")


def is_deferred(text: str) -> bool:
    return bool(DEFERRED_RE.search(text or ""))


def committee_of(text: str) -> str | None:
    """
    The committee a meeting belongs to.

    Reads the Council's own phrasing first -- "Committee on X" -- so a
    committee this module has never heard of still resolves. The fixed list is
    the fallback for titles that name a subject without the formal phrase.
    """
    m = COMMITTEE_RE.search(text or "")
    if m:
        name = re.sub(r"\s+", " ", m.group("name")).strip(" ,;-")
        name = re.sub(r"(?i)\s+jointly.*$", "", name).strip()
        if name:
            return name.title()
    low = (text or "").lower()
    hits = [c for c in COMMITTEE_HINTS if c in low]
    return max(hits, key=len).title() if hits else None


def is_oversight(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in OVERSIGHT_MARKERS)


# Calendar kinds that are not meetings at all. A day off and an RSVP deadline
# do not need a briefing packet, and building one for "Out" spends a model call
# to tell the office that nothing is happening.
NOT_A_MEETING = {"out", "deadline"}


def classify(event: dict) -> str:
    """What kind of packet this meeting needs, or none at all."""
    blob = f"{event.get('summary', '')} {event.get('location', '')}"
    if is_deferred(event.get("summary") or ""):
        return "deferred"
    if (event.get("kind") or "").lower() in NOT_A_MEETING:
        return "none"
    if is_oversight(blob):
        return "oversight"
    if (event.get("kind") or "") == "hearing" or "hearing" in blob.lower():
        return "hearing"
    if any(w in blob.lower() for w in ("stated meeting", "charter", "vote")):
        return "stated"
    if (event.get("kind") or "") == "community":
        return "community"
    return "meeting"


def upcoming(store: Store, days: int = 21, include_past_days: int = 1) -> list[dict]:
    """Calendar items worth preparing for, soonest first."""
    lo = (date.today() - timedelta(days=include_past_days)).isoformat()
    hi = (date.today() + timedelta(days=days)).isoformat()
    rows = [dict(r) for r in store.q(
        "SELECT * FROM calendar WHERE start >= ? AND start <= ? ORDER BY start",
        (lo, hi + "T23:59:59"))]
    for row in rows:
        row["packet_kind"] = classify(row)
        row["deferred"] = row["packet_kind"] == "deferred"
        # The title names the committee; the location names a room. Reading
        # both turned "Committee on Sanitation and Solid Waste Management" into
        # "...Management 250 Broadway", which matched no committee at all and
        # silently produced an empty agenda.
        row["committee"] = committee_of(row["summary"] or "")
        row["owners"] = json.loads(row.get("owners") or "[]")
        row["has_packet"] = bool(store.one(
            "SELECT 1 FROM ws_packets WHERE event_id=?", (row["event_id"],)))
    return rows


# ---------------------------------------------------------------- agenda ----
def agenda_items(store: Store, committee: str | None, when: str | None,
                 topic: str = "", limit: int = 25) -> list[dict]:
    """
    What is likely on the agenda.

    Legistar publishes the agenda itself; where the office can reach it, that
    is authoritative. Where it cannot, this falls back to the live matters
    before that committee -- clearly labelled as inferred, because a staffer
    walking into a room with the wrong agenda is worse than one walking in
    knowing the agenda is unconfirmed.
    """
    if not committee and not topic:
        return []
    session = LI.current_session(store)
    words = [w.lower() for w in re.findall(r"[A-Za-z]{4,}", topic or "")
             if w.lower() not in _STOP]

    clauses, params = [], []
    if committee:
        clauses.append("LOWER(m.committee) LIKE ?")
        params.append(f"%{committee.lower()}%")
    if session:
        clauses.append("m.session = ?")
        params.append(session)
    where = " AND ".join(clauses) if clauses else "1=1"

    # The topic ranks; it does not filter. Requiring the whole hearing title to
    # appear in a bill name returns nothing, and an empty agenda reads as "the
    # committee has no live items" when the truth is "the query was too narrow".
    score = "0"
    if words:
        score = " + ".join(
            ["(CASE WHEN LOWER(m.name) LIKE ? THEN 2 ELSE 0 END)" for _ in words] +
            ["(CASE WHEN LOWER(m.file) LIKE ? THEN 3 ELSE 0 END)" for _ in words])
        params = ([f"%{w}%" for w in words] + [f"%{w}%" for w in words]) + params
        where_params = params
    rows = [dict(r) for r in store.q(
        f"SELECT m.*, mb.name AS prime_name, ({score}) AS topic_score "
        f"FROM matters m LEFT JOIN members mb ON mb.person_id=m.prime_id "
        f"WHERE {where} "
        f"ORDER BY topic_score DESC, COALESCE(m.pending,0) DESC, "
        f"         COALESCE(m.d49_score,0) DESC, m.updated DESC LIMIT ?",
        params + [limit])]

    matched = sum(1 for r in rows if (r.get("topic_score") or 0) > 0)
    out = []
    for row in rows:
        pillars = json.loads(row.get("pillars") or "[]")
        out.append({
            "matter_id": row["matter_id"], "file": row["file"],
            "title": row["name"], "status": row["status"],
            "committee": row["committee"], "prime": row["prime_name"],
            "sponsors": row["n_sponsors"], "pending": bool(row["pending"]),
            "d49_score": row["d49_score"],
            "on_topic": bool(row.get("topic_score")),
            "pillars": [PILLARS[p]["label"] for p in pillars if p in PILLARS],
            "url": (f"https://legistar.council.nyc.gov/LegislationDetail.aspx"
                    f"?ID={row['matter_id']}"),
            "hanks_position": _position(store, row["matter_id"]),
        })
    out.sort(key=lambda r: (not r["on_topic"], not r["pending"]))
    return out


_STOP = {"oversight", "hearing", "committee", "council", "meeting", "budget",
         "preliminary", "executive", "update", "report", "joint", "with",
         "from", "that", "this", "into", "york", "city"}


def _agenda_confidence(items: list[dict]) -> str:
    """Say plainly how the agenda was arrived at."""
    if not items:
        return ("no live items resolved for this committee; confirm the agenda "
                "on Legistar before the meeting")
    on_topic = sum(1 for i in items if i.get("on_topic"))
    if on_topic:
        return (f"inferred from the committee's live docket; {on_topic} of "
                f"{len(items)} items match the hearing subject. Not the "
                f"published agenda -- confirm on Legistar")
    return ("inferred from the committee's live docket. Nothing on the docket "
            "matches the hearing subject, so these are the committee's open "
            "items generally, not the agenda. Confirm on Legistar")


def _position(store: Store, matter_id: int) -> str:
    """Is the Councilmember already on this bill?"""
    member = LI.resolve_member(store, MEMBER_NAME)
    if not member:
        return "unknown"
    row = store.one("SELECT role FROM sponsorships WHERE matter_id=? AND person_id=?",
                    (matter_id, member["person_id"]))
    if not row:
        return "not a sponsor"
    return "prime sponsor" if (row["role"] or "").lower().startswith("prime") \
        else "co-sponsor"


# ------------------------------------------------------- district evidence ----
def district_stake(store: Store, committee: str | None, topic: str,
                   fy: int = 2027) -> dict:
    """
    What District 49 has riding on this subject: money, organisations, record.

    This is the half of a hearing packet that nobody else in the room has. A
    citywide question answered with a district number is how a member changes
    the subject to their own borough.
    """
    words = [w for w in re.findall(r"[A-Za-z]{4,}", f"{committee or ''} {topic}")
             if w.lower() not in ("committee", "oversight", "hearing", "council")]
    query = " ".join(words[:6])

    money: dict = {}
    if query:
        try:
            money = BD.break_by(store, "initiative", query,
                                {"kind": "funding", "fy": fy}, top=8)
        except Exception:
            money = {}

    # The same organisation appears on several lines across books. Rolled up,
    # the list reads as five providers; left raw, it reads as the same charity
    # five times and buries the rest of the district.
    # The books append the district or programme to the recipient -- "Acme,
    # Inc. - Council District 49". That is the same organisation, and listing
    # it twice makes one provider look like two and halves each figure.
    trimmed = ("TRIM(CASE WHEN INSTR(org, ' - ') > 0 "
               "          THEN SUBSTR(org, 1, INSTR(org, ' - ') - 1) ELSE org END)")
    d49_lines = [dict(r) for r in store.q(
        f"SELECT {trimmed} AS org, MAX(pot) AS pot, MAX(NULLIF(agency,'')) AS agency, "
        f"       SUM(amount) AS amount, COUNT(*) AS lines, "
        f"       GROUP_CONCAT(DISTINCT source_id) AS source_id "
        f"FROM funding WHERE fy=? AND in_d49=1 AND amount IS NOT NULL "
        f"  AND org IS NOT NULL AND org<>'' "
        f"GROUP BY 1 ORDER BY SUM(amount) DESC LIMIT 12", (fy,))]

    # Which of those are actually about this subject, and which are simply the
    # district's biggest awards. Presenting the second as the first is how a
    # member ends up citing a parks grant in a policing hearing.
    topical = [r for r in d49_lines
               if any(w.lower() in f"{r['org']} {r['pot']} {r['agency']}".lower()
                      for w in words[:6])] if words else []

    pillar = _pillar_for(f"{committee or ''} {topic}")
    return {
        "fiscal_year": fy,
        "pillar": PILLARS.get(pillar, {}).get("label") if pillar else None,
        "money_by_initiative": money.get("buckets", [])[:8],
        "money_totals": money.get("totals", {}),
        "d49_largest_awards": d49_lines,
        "d49_topical_awards": topical,
        "awards_are_topical": bool(topical),
        "awards_note": ("District 49 awards matching this subject."
                        if topical else
                        "No District 49 award in the lake matches this subject. "
                        "The list below is the district's largest awards overall "
                        "and is context, not evidence about this hearing."),
        "doctrine": (LAND_USE_DOCTRINE if _is_land_use(committee, topic) else {}),
        "sources": money.get("sources", []) or ["SCHEDULE_C"],
    }


def _is_land_use(committee: str | None, topic: str) -> bool:
    blob = f"{committee or ''} {topic}".lower()
    return any(w in blob for w in ("land use", "zoning", "ulurp", "housing",
                                   "development", "landmark", "planning"))


def _pillar_for(text: str) -> str | None:
    low = (text or "").lower()
    best, score = None, 0
    for key, spec in PILLARS.items():
        n = sum(1 for k in spec["keywords"] if k.lower() in low)
        if n > score:
            best, score = key, n
    return best


# --------------------------------------------------------------- evidence ----
def evidence_for(store: Store, event: dict, fy: int = 2027) -> dict:
    """Everything the lake knows that bears on one meeting."""
    title = event.get("summary") or ""
    committee = event.get("committee") or committee_of(title)
    kind = event.get("packet_kind") or classify(event)
    topic = clean_topic(title)

    items = agenda_items(store, committee, event.get("start"), topic)
    stake = district_stake(store, committee, topic, fy)
    record = {}
    try:
        record = LI.legislative_record(store, MEMBER_NAME)
    except Exception:
        record = {}

    return {
        "event": {k: event.get(k) for k in
                  ("event_id", "summary", "start", "end", "location", "kind",
                   "owners", "link")},
        "packet_kind": kind,
        "committee": committee,
        "topic": topic or title,
        "is_oversight": kind == "oversight",
        "agenda": items,
        "agenda_confidence": _agenda_confidence(items),
        "district": stake,
        "member_record": {k: record.get(k) for k in ("totals", "enactment_rate")}
                         if record else {},
        "member_on_agenda": [i for i in items
                             if i["hanks_position"] in ("prime sponsor", "co-sponsor")],
        "sources": sorted(set(stake.get("sources", []) + ["LEGISTAR", "COUNCIL_RECORD"])),
    }


# -------------------------------------------------------------- questions ----
def questions_for(evidence: dict, count: int = 3) -> list[dict]:
    """
    Questions built from the evidence, each with the follow-up ready.

    A question the agency can answer with "we're working on it" has cost the
    Councilmember her turn. Every question here carries the prepared response
    to the answer she is actually going to get.
    """
    committee = evidence.get("committee") or "this committee"
    topic = evidence.get("topic") or "the subject"
    out: list[dict] = []

    on_agenda = evidence.get("member_on_agenda") or []
    if on_agenda:
        item = on_agenda[0]
        out.append({
            "ask": (f"{item['file']} has been before {committee} since it was "
                    f"referred. What is holding it, and what does this committee "
                    f"need from the administration to move it?"),
            "why": f"She is {item['hanks_position']} on it; the question is hers to ask.",
            "follow_up": "If the answer is a date, get the date on the record.",
            "cite": item["url"],
        })

    totals = evidence.get("district", {}).get("money_totals") or {}
    if totals.get("amount"):
        out.append({
            "ask": (f"Of the ${totals['amount']:,.0f} tracked in this area for "
                    f"FY{evidence['district']['fiscal_year']}, what share reached "
                    f"Staten Island, and what share reached the North Shore?"),
            "why": ("The borough's share is the office's standing test, and the "
                    "agency rarely brings the district-level cut unprompted."),
            "follow_up": ("If they answer citywide, ask again for the borough and "
                          "then for District 49. Do not accept the citywide figure."),
            "cite": ", ".join(evidence["district"].get("sources", [])) or "SCHEDULE_C",
        })
    if totals.get("pending"):
        out.append({
            "ask": (f"${totals['pending']:,.0f} in this area is still pending a "
                    f"budget modification. When does that modification move?"),
            "why": "Pending money cannot be announced, and organisations are waiting on it.",
            "follow_up": "Ask which organisations are affected and whether they have been told.",
            "cite": "TRANSPARENCY_RESO",
        })

    largest = (evidence.get("district", {}).get("d49_topical_awards") or [])
    if largest:
        org = largest[0]
        out.append({
            "ask": (f"{org['org']} administers ${(org['amount'] or 0):,.0f} in "
                    f"District {DISTRICT} through {org['agency'] or 'the agency'}. "
                    f"What outcomes is the agency measuring on that contract?"),
            "why": "Ties an oversight question to a named provider in the district.",
            "follow_up": "If there is no outcome measure, ask why the contract renews without one.",
            "cite": org.get("source_id") or "SCHEDULE_C",
        })

    if evidence.get("is_oversight"):
        out.append({
            "ask": (f"What is the agency's staffing level on {topic} today versus "
                    f"three years ago, and how many of those positions serve "
                    f"Staten Island?"),
            "why": ("Headcount is the question agencies answer least comfortably "
                    "and the one that best explains service levels."),
            "follow_up": "Ask for the vacancy rate separately from the budgeted headcount.",
            "cite": VERIFY_TAG,
        })
        out.append({
            "ask": (f"On {topic}, how does Staten Island's provision compare with "
                    f"the other four boroughs, and what is in the ten-year capital "
                    f"plan to close any gap?"),
            "why": ("Borough parity is the Councilmember's standing frame, and "
                    "asking for the comparison puts a figure in the record."),
            "follow_up": ("If they cannot give the borough comparison, ask when the "
                          "committee will receive it in writing."),
            "cite": VERIFY_TAG,
        })

    # Fallbacks, used only to reach the requested count. Each is a different
    # question: three copies of the same one wastes her turn at the microphone.
    fallbacks = [
        {"ask": (f"What does success on {topic} look like twelve months from now, "
                 f"stated as a number this committee can check?"),
         "why": "Forces a measurable commitment where none has been offered.",
         "follow_up": "Write the number down and open the next hearing with it.",
         "cite": VERIFY_TAG},
        {"ask": (f"What did the agency spend on {topic} in Staten Island last "
                 f"fiscal year, and what is budgeted this year?"),
         "why": "A borough-level spend figure is rarely volunteered and easy to check.",
         "follow_up": "If they only have a citywide figure, ask when the borough cut will be provided.",
         "cite": VERIFY_TAG},
        {"ask": (f"Who in the agency is accountable for {topic} on the North Shore, "
                 f"and when did this committee last hear from them?"),
         "why": "Puts a name in the record and makes the next hearing easier to run.",
         "follow_up": "Ask for that person to appear at the next oversight hearing.",
         "cite": VERIFY_TAG},
        {"ask": (f"What is the single biggest constraint on improving {topic} — "
                 f"money, headcount, or authority?"),
         "why": "Separates a funding ask from a management failure before the vote.",
         "follow_up": "Whichever they name, ask what this Council would have to do about it.",
         "cite": VERIFY_TAG},
    ]
    seen = {q["ask"] for q in out}
    for extra in fallbacks:
        if len(out) >= count:
            break
        if extra["ask"] not in seen:
            out.append(extra)
            seen.add(extra["ask"])
    return out[:max(count, len(out))] if evidence.get("is_oversight") else out[:count]


def deflection_playbook() -> list[dict]:
    """The answers an agency gives, and what to say to each."""
    return [{"they_say": a, "she_says": b} for a, b in DEFLECTIONS]


# ----------------------------------------------------------------- render ----
def _bullets(items: Iterable[str]) -> list[str]:
    return [f"- {i}" for i in items if i]


def to_markdown(packet: dict) -> str:
    e = packet["evidence"]
    event = e["event"]
    when = (event.get("start") or "")[:16].replace("T", " ")
    out = [f"# {event.get('summary') or 'Meeting'}",
           "",
           f"*{packet['kind'].title()} packet for {MEMBER_NAME}, {DISTRICT_LABEL} · "
           f"{when}{' · ' + event['location'] if event.get('location') else ''}*",
           ""]

    if packet.get("bottom_line"):
        out += ["## Bottom line", packet["bottom_line"], ""]

    if e.get("agenda"):
        out += ["## On the agenda",
                f"*{e['agenda_confidence']}*", ""]
        for item in e["agenda"][:12]:
            flag = ("**she is prime sponsor**" if item["hanks_position"] == "prime sponsor"
                    else "**she is a co-sponsor**" if item["hanks_position"] == "co-sponsor"
                    else item["status"] or "")
            out.append(f"- [{item['file']}]({item['url']}) — {item['title']} "
                       f"({item['prime'] or 'sponsor unlisted'}; {flag})")
        out.append("")

    stake = e.get("district", {})
    if stake.get("money_by_initiative") or stake.get("d49_largest_awards"):
        out += [f"## What District {DISTRICT} has riding on it"]
        totals = stake.get("money_totals") or {}
        if totals.get("amount"):
            out.append(f"- ${totals['amount']:,.0f} tracked in this area for "
                       f"FY{stake['fiscal_year']} across {totals.get('records', 0):,} "
                       f"lines; ${totals.get('confirmed', 0):,.0f} confirmed, "
                       f"${totals.get('pending', 0):,.0f} pending a modification")
        shown = stake.get("d49_topical_awards") or stake.get("d49_largest_awards") or []
        if stake.get("awards_note"):
            out.append(f"- *{stake['awards_note']}*")
        for row in shown[:5]:
            lines = row.get("lines") or 1
            across = f", {lines} lines" if lines > 1 else ""
            out.append(f"- {row['org']} — ${(row['amount'] or 0):,.0f} "
                       f"({row['agency'] or 'agency unlisted'}{across})")
        out.append("")

    if packet.get("talking_points"):
        out += ["## Talking points"] + \
               [f"{i}. {t}" for i, t in enumerate(packet["talking_points"], 1)] + [""]

    if packet.get("opening"):
        out += ["## Opening statement", packet["opening"], ""]

    label = "Questions" if packet["kind"] == "oversight" else "Three questions"
    out += [f"## {label}"]
    for i, q in enumerate(packet.get("questions", []), 1):
        out.append(f"**{i}. {q['ask']}**")
        out.append(f"   - *Why:* {q['why']}")
        out.append(f"   - *If they deflect:* {q['follow_up']}")
        out.append(f"   - *Source:* {q['cite']}")
    out.append("")

    if packet["kind"] == "oversight":
        out += ["## When they deflect"]
        for row in deflection_playbook():
            out.append(f"- They say *\"{row['they_say']}\"* → "
                       f"**{row['she_says']}**")
        out.append("")

    if packet.get("closing"):
        out += ["## Closing", packet["closing"], ""]

    reg = CitationRegistry(DEFAULT_SOURCES)
    bib = reg.bibliography(e.get("sources", []))
    if bib:
        out += ["## Sources"] + bib + [""]
    out += ["---",
            f"*Mode: {packet['mode']}. Agenda items are {e['agenda_confidence']}. "
            f"Anything marked {VERIFY_TAG} is unconfirmed and must be checked "
            f"before it is said out loud.*"]
    return "\n".join(out)


# ----------------------------------------------------------------- build ----
# The calendar kinds a packet is worth building for.
PACKET_KINDS = ("oversight", "hearing", "stated", "meeting", "caucus", "community")


def build(store: Store, event: dict, fy: int = 2027, use_ai: bool = True,
          save: bool = True, actor: str = "") -> dict:
    """Assemble a packet for one calendar item."""
    init(store)
    e = evidence_for(store, event, fy)
    kind = e["packet_kind"]
    questions = questions_for(e, count=6 if kind == "oversight" else 3)

    packet = {
        "id": f"packet:{event.get('event_id')}:{kind}",
        "event_id": event.get("event_id"),
        "kind": kind,
        "title": event.get("summary") or "Meeting",
        "meeting_at": event.get("start"),
        "committee": e.get("committee"),
        "evidence": e,
        "questions": questions,
        "bottom_line": _default_bottom_line(e),
        "talking_points": _default_points(e),
        "opening": None,
        "closing": None,
        "mode": "evidence-only",
    }

    if use_ai and AI.usable():
        written = _write(packet)
        # Only claim the packet was written if the model actually returned
        # prose. A refused or unparseable call leaves the deterministic
        # scaffold in place, and saying "written" over it would tell a staffer
        # the packet had been read when it had not.
        prose = {k: v for k, v in written.items() if k != "mode" and v}
        packet.update(prose)
        packet["mode"] = "written" if prose else "evidence-only"
        if not prose:
            packet["ai_note"] = ("The model did not return usable prose; this is "
                                 "the evidence scaffold.")

    if save:
        store.upsert("ws_packets", [{
            "id": packet["id"], "event_id": packet["event_id"],
            "kind": kind, "title": packet["title"],
            "meeting_at": packet["meeting_at"], "committee": packet["committee"],
            "body": to_markdown(packet),
            "evidence": json.dumps(e, default=str)[:400000],
            "mode": packet["mode"], "created": _now(), "created_by": actor or "system"}])
        store.conn.commit()
    return packet


def _default_bottom_line(e: dict) -> str:
    committee = e.get("committee") or "the committee"
    n = len(e.get("agenda") or [])
    mine = len(e.get("member_on_agenda") or [])
    totals = (e.get("district", {}).get("money_totals") or {})
    parts = [f"{committee} meets on {(e['event']['start'] or '')[:10]}"]
    if n:
        parts.append(f"with {n} live item{'s' if n != 1 else ''} on its docket")
    if mine:
        parts.append(f"{mine} of which she already sponsors")
    line = ", ".join(parts) + "."
    if totals.get("amount"):
        line += (f" ${totals['amount']:,.0f} in tracked awards sits in this subject "
                 f"area for FY{e['district']['fiscal_year']}")
        if totals.get("pending"):
            line += f", ${totals['pending']:,.0f} of it still pending a modification"
        line += "."
    if e.get("is_oversight"):
        line += (" This is an oversight hearing: the value of her turn is in the "
                 "follow-up, not the first question.")
    return line


def _default_points(e: dict) -> list[str]:
    points = []
    committee = e.get("committee") or "this committee"
    for item in (e.get("member_on_agenda") or [])[:2]:
        points.append(f"She is {item['hanks_position']} on {item['file']}: "
                      f"{item['title']}")
    stake = e.get("district", {})
    if stake.get("pillar"):
        points.append(f"This sits under the office's {stake['pillar']} priority.")
    for row in (stake.get("d49_topical_awards") or [])[:2]:
        points.append(f"{row['org']} delivers ${(row['amount'] or 0):,.0f} of this "
                      f"work inside District {DISTRICT}.")
    if _is_land_use(e.get("committee"), e.get("topic", "")):
        points.append(LAND_USE_DOCTRINE.get(
            "ownership", "Development in District 49 is measured by ownership, "
            "not units."))
    if not points:
        points.append(f"No District {DISTRICT} exposure was found in the lake for "
                      f"{committee}; treat this as a listening posture. {VERIFY_TAG}")
    return points


def _write(packet: dict) -> dict:
    """Have the model write the prose over evidence it is forbidden to add to."""
    e = packet["evidence"]
    facts = json.dumps({
        "event": e["event"], "committee": e["committee"], "topic": e["topic"],
        "agenda": e["agenda"][:10], "member_on_agenda": e["member_on_agenda"],
        "district": {k: e["district"].get(k) for k in
                     ("fiscal_year", "pillar", "money_totals",
                      "money_by_initiative", "d49_largest_awards")},
        "prepared_questions": packet["questions"],
    }, default=str)[:14000]

    want = ("an opening statement of about 120 words, a closing of about 60 words, "
            "and six talking points" if packet["kind"] == "oversight"
            else "four talking points")
    prompt = (
        f"Prepare the {packet['kind']} packet below for {MEMBER_NAME}.\n\n"
        f"Write: a bottom line of two or three sentences, {want}.\n"
        f"Return JSON only, with keys bottom_line (string), talking_points "
        f"(array of strings), opening (string or null), closing (string or null).\n"
        f"Use only the EVIDENCE. Do not add a number, a date, a bill number or "
        f"another official's position that is not in it; write {VERIFY_TAG} instead.\n"
        f"She speaks plainly and gets to the point. Do not write in the first "
        f"person plural. Do not praise the agency.\n\nEVIDENCE:\n{facts}")

    raw = AI.ask_model(prompt, max_tokens=1800)
    if not raw or raw.startswith("[council unavailable"):
        return {"mode": "evidence-only"}
    body = re.search(r"\{.*\}", raw, re.S)
    if not body:
        return {"mode": "evidence-only"}
    try:
        got = json.loads(body.group(0))
    except ValueError:
        return {"mode": "evidence-only"}
    return {k: got[k] for k in ("bottom_line", "talking_points", "opening", "closing")
            if got.get(k)}


# ------------------------------------------------------------------ week ----
def week(store: Store, days: int = 7, fy: int = 2027, use_ai: bool = False,
         limit: int = 12) -> dict:
    """
    Every meeting in the window, each with its packet.

    This is the Friday deliverable: what is coming, what is on it, and what she
    is going to say. Built once, updated when the calendar or the docket moves.
    """
    init(store)
    events = [e for e in upcoming(store, days)
              if e["packet_kind"] in ("oversight", "hearing", "stated")]
    deferred = [e for e in upcoming(store, days) if e["packet_kind"] == "deferred"]
    packets = [build(store, e, fy, use_ai=use_ai) for e in events[:limit]]
    return {
        "window_days": days,
        "meetings": len(events),
        "prepared": len(packets),
        "oversight": sum(1 for p in packets if p["kind"] == "oversight"),
        "packets": [{"id": p["id"], "title": p["title"], "when": p["meeting_at"],
                     "kind": p["kind"], "committee": p["committee"],
                     "agenda_items": len(p["evidence"]["agenda"]),
                     "questions": len(p["questions"]),
                     "bottom_line": p["bottom_line"]} for p in packets],
        "skipped": [e["summary"] for e in events[limit:]],
        "deferred": [{"title": e["summary"], "when": e["start"]} for e in deferred],
    }


def saved(store: Store, event_id: str = "", limit: int = 25) -> list[dict]:
    init(store)
    sql = "SELECT id, event_id, kind, title, meeting_at, committee, mode, created FROM ws_packets"
    params: list = []
    if event_id:
        sql += " WHERE event_id=?"
        params.append(event_id)
    sql += " ORDER BY meeting_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in store.q(sql, params)]


def read(store: Store, packet_id: str) -> dict | None:
    init(store)
    row = store.one("SELECT * FROM ws_packets WHERE id=?", (packet_id,))
    return dict(row) if row else None
