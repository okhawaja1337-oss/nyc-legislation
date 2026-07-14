#!/usr/bin/env python3
"""
profiles.py — rich, single-page member profiles across all three levels.

Pure assembly + formatting; the caller passes in the facts it already fetched
(dossier stats for NYC, the congress-legislators profile for federal, the NY
Senate member record for state) and an optional `llm.LLM` for the AI
"record at a glance" summary. No network, no Streamlit here.
"""

import json

PROFILE_STYLE = (
    "You write neutral, factual one-paragraph staff profiles of elected "
    "officials for internal use. Use ONLY the data provided; never add "
    "biography, endorsements, scandals, or positions not derivable from it. "
    "Mark interpretation as inference. Nonpartisan, plain English."
)

GLANCE_PROMPT = """Write a 4–6 sentence "record at a glance" for this official using
ONLY the DATA. No invented facts. End with one line noting what the data does and
doesn't cover.

LEVEL: {level}
NAME: {name}
DATA (JSON): {data}
"""


def glance(llm, level, name, data):
    """AI 'record at a glance'; empty string if no key (UI hides the section)."""
    if not (llm and llm.ready):
        return ""
    try:
        return llm.complete(
            GLANCE_PROMPT.format(level=level, name=name,
                                 data=json.dumps(data, ensure_ascii=False)[:6000]),
            max_tokens=500, system=PROFILE_STYLE)
    except Exception:
        return ""


def council_facts(name, stats, district=None, committees=None):
    """Shape a NYC Council member's facts from dossier stats + optional extras."""
    s = stats or {}
    top_topics = list((s.get("by_topic") or {}).keys())[:5]
    return {
        "level": "NYC", "name": name, "chamber": "City Council",
        "district": district, "committees": committees or [],
        "bills_on": s.get("bills_on"), "as_prime": s.get("as_prime"),
        "as_cosponsor": s.get("as_cosponsor"),
        "passed": (s.get("by_status") or {}).get("passed"),
        "focus_areas": top_topics,
        "coalition": list((s.get("top_coalition") or {}).keys())[:5],
    }


def federal_facts(profile, committees=None, sponsored=None):
    """Shape a federal delegation member's facts."""
    ex = profile.get("extra", {})
    return {
        "level": "Federal", "name": profile.get("name"),
        "chamber": profile.get("chamber"), "seat": profile.get("seat"),
        "party": profile.get("party"), "since": ex.get("since"),
        "term_ends": profile.get("term_end"), "committees": committees or [],
        "contact": profile.get("contact"), "twitter": ex.get("twitter"),
        "recent_bills": [b.get("File") for b in (sponsored or [])[:8]],
    }


def state_facts(member, sponsored=None):
    return {
        "level": "NY State", "name": member.get("name"),
        "chamber": member.get("chamber"), "district": member.get("district"),
        "party": member.get("party"),
        "recent_bills": [b.get("File") for b in (sponsored or [])[:8]],
    }


def facts_to_rows(facts):
    """Flatten a facts dict into (label, value) rows for a clean table."""
    labels = [
        ("Level", "level"), ("Chamber", "chamber"), ("Seat", "seat"),
        ("District", "district"), ("Party", "party"), ("In office since", "since"),
        ("Term ends", "term_ends"), ("Bills on", "bills_on"),
        ("As prime sponsor", "as_prime"), ("As co-sponsor", "as_cosponsor"),
        ("Passed / enacted", "passed"), ("Contact", "contact"),
    ]
    rows = []
    for label, key in labels:
        v = facts.get(key)
        if v not in (None, "", []):
            rows.append((label, v))
    if facts.get("focus_areas"):
        rows.append(("Focus areas", ", ".join(facts["focus_areas"])))
    if facts.get("committees"):
        rows.append(("Committees", ", ".join(facts["committees"])))
    if facts.get("coalition"):
        rows.append(("Frequent partners", ", ".join(facts["coalition"])))
    if facts.get("twitter"):
        rows.append(("Twitter/X", "@" + facts["twitter"]))
    return rows
