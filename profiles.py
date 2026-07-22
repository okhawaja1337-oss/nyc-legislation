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


PERSONA_PROMPT = """Write a short "legislative persona" of this NYC Council Member for
an internal wiki — a characterization of their LEGISLATIVE STYLE ONLY, drawn
strictly from the DATA (their sponsorship volume, prime-vs-cosponsor balance,
topic mix, outcomes, and frequent partners).

HARD RULES:
- Characterize legislative behavior, not the person. NO personal traits, no
  temperament, no biography, no motives, no party/ideology labels unless the
  data shows it, no praise or criticism.
- Use ONLY the data. If it's thin, say so. Invent nothing.
- Mark interpretation as inference ("the record suggests…").

Write ~120 words, plus a final line: "**In three words:** w1 · w2 · w3" capturing
their legislative style (e.g., "coalition-builder", "housing-focused",
"prolific", "committee-anchored"). Nonpartisan, plain.

MEMBER: {name}
DATA (JSON): {data}
"""


def persona(llm, name, stats):
    """AI legislative-persona for the wiki; empty string if no key."""
    if not (llm and llm.ready):
        return ""
    import json as _json
    try:
        return llm.complete(PERSONA_PROMPT.format(name=name,
                            data=_json.dumps(stats, ensure_ascii=False)[:6000]),
                            max_tokens=420, system=PROFILE_STYLE)
    except Exception:
        return ""


WHY_PROMPT = """Based ONLY on this member's legislative record (topic mix, what they
prime-sponsor vs. co-sponsor, outcomes, frequent partners) — and, via web search,
their own public statements and district — explain what appears to drive this NYC
Council Member's priorities. Frame as INFERENCE from the record and cited public
sources, never as a claim about private motives. ~120 words, 3–4 bullets, then a
one-line caveat that this is analysis of a public record, not a statement of the
member's views.

MEMBER: {name}
RECORD (JSON): {data}
"""

ENRICH_PROMPT = """Using web search, gather quick reference facts about NYC Council
Member {name}. Return ONLY a JSON object with keys (use "" / [] when unknown, never
guess): "district" (number as string), "party", "committees" (array of committee
names), "background" (<=25 words on prior career/roots), "interests" (array of
policy interests), "social" (object like {{"x":"handle","instagram":"...","website":"..."}}),
"sources" (array of URLs used). Do not invent handles or facts."""


STANCE_PROMPT = """Based on web search of this NYC Council Member's PUBLIC statements,
press, and record, estimate their stance on each policy topic below. Return ONLY a
JSON object mapping each topic to a number from -1 (publicly opposed) through 0
(neutral / no clear public stance) to +1 (publicly supportive). Use 0 when you
can't find a clear public position — do NOT guess from party. Topics: {topics}
MEMBER: {name}"""


def topic_stances(llm, name, topics, allow_web=True):
    """Web-sourced per-topic public-statement lean (-1..1). {} if unavailable."""
    if not (llm and llm.ready) or not topics:
        return {}
    import llm as _llmmod
    try:
        txt = llm.complete(STANCE_PROMPT.format(name=name, topics=", ".join(topics)),
                           max_tokens=500, allow_web=allow_web)
        data = _llmmod.extract_json(txt)
        out = {}
        if isinstance(data, dict):
            for k, v in data.items():
                try:
                    out[str(k)] = max(-1.0, min(1.0, float(v)))
                except (TypeError, ValueError):
                    continue
        return out
    except Exception:
        return {}


def why_support(llm, name, stats, allow_web=True):
    if not (llm and llm.ready):
        return ""
    import json as _json
    try:
        return llm.complete(WHY_PROMPT.format(name=name, data=_json.dumps(stats, ensure_ascii=False)[:5000]),
                            max_tokens=500, system=PROFILE_STYLE, allow_web=allow_web)
    except Exception:
        return ""


def enrichment(llm, name, allow_web=True):
    """Web-sourced reference facts (committees, interests, socials). {} if unavailable."""
    if not (llm and llm.ready):
        return {}
    import llm as _llmmod
    try:
        txt = llm.complete(ENRICH_PROMPT.format(name=name), max_tokens=700, allow_web=allow_web)
        data = _llmmod.extract_json(txt)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


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
