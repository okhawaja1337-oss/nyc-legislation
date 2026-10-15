#!/usr/bin/env python3
"""
What law each bill actually touches.

A legislative office drafting a bill asks three questions before it writes a
word: has anyone legislated on this section before, what happened to them, and
what else lives in this part of the code. The corpus could not answer any of
them. It knew a bill existed and what committee it sat in; the law the bill
amends was buried in prose nobody could query.

It is all there. Every Local Law opens by naming its target precisely --

    Section 1. Subchapter 6 of chapter 2 of title 24 of the administrative
    code of the city of New York is amended by adding a new section 24-244.1

-- and 59% of bills carry at least one such citation. This module reads them
out into a table, so "who else has touched § 27-2005" is a query rather than
an afternoon.

Four bodies of law are tracked, because those are the four a City Council bill
can reach: the Administrative Code, the City Charter, the New York City Rules,
and State law where a bill depends on or conforms to it.

The extraction is deliberately conservative. A bare "24-244" pattern also
matches bill numbers, dates and dollar ranges, so a section is only recorded
when its title number is a real Administrative Code title (1-34) and its
suffix is not a year. Over-collecting here would be worse than under-
collecting: an office that searches the code index and gets bill numbers back
stops using it, and a citation index nobody trusts is a citation index nobody
opens.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from ..core.store import Store

# The Administrative Code runs to title 34. A "section" in it is written
# title-number, e.g. 27-2005 in title 27, sometimes with a decimal suffix for
# an inserted section: 24-244.1.
MAX_TITLE = 34
SECTION = re.compile(r"(?<![\d.\-])(\d{1,2})-(\d{2,5}(?:\.\d+)?)(?![\d\-])")
TITLE_OF = re.compile(
    r"title\s+(\d{1,2})\s+of\s+the\s+administrative\s+code", re.I)
CHARTER = re.compile(
    r"section\s+(\d{1,4}(?:\.\d+)?)\s+of\s+the\s+(?:new\s+york\s+)?"
    r"(?:city\s+)?charter", re.I)
RULES = re.compile(
    r"(?:section|§)\s*([\d\-.]+)\s+of\s+title\s+(\d{1,2})\s+of\s+the\s+rules"
    r"\s+of\s+the\s+city\s+of\s+new\s+york", re.I)
STATE_LAW = re.compile(
    r"(?:section|§)\s*([\w.\-]+)\s+of\s+the\s+([a-z][a-z\- ]{3,40}?)\s+law\b",
    re.I)
# "is amended by adding", "is REPEALED", "is renumbered"
ACTION = re.compile(
    r"\b(?:is|are)\s+(amended|REPEALED|repealed|renumbered|added)\b", re.I)
ADDING = re.compile(r"amended\s+by\s+adding", re.I)
# The opening recital names the bill's principal target.
SUBJECT = re.compile(
    r"a\s+local\s+law\s+to\s+amend\s+the\s+([a-z][a-z \-,]{3,60}?)"
    r"(?:\s+of\s+the\s+city\s+of\s+new\s+york)?\s*,\s*in\s+relation\s+to", re.I)

# The code_refs table lives in the base schema (core/store.py), so a
# fresh lake can be queried before anything has been ingested into it.
SCHEMA = """"""  # kept for callers that used to apply it
# Every Administrative Code title, so a section number can be named rather
# than left as a bare number. Source: NYC Administrative Code, title listing.
TITLES: dict[str, str] = {
    "1": "General Provisions", "2": "City Clerk", "3": "Elected officials",
    "4": "Property of the City", "5": "Budget", "6": "Contracts and purchases",
    "7": "Legal affairs", "8": "Civil rights", "9": "Criminal justice",
    "10": "Public safety", "11": "Taxation and finance",
    "12": "Personnel and labor", "13": "Retirement and pensions",
    "14": "Police", "15": "Fire prevention", "16": "Sanitation",
    "17": "Health", "18": "Parks", "19": "Transportation",
    "20": "Consumer and worker protection", "21": "Social services",
    "22": "Economic affairs", "23": "Communications",
    "24": "Environmental protection and utilities", "25": "Land use",
    "26": "Housing and buildings", "27": "Construction and maintenance",
    "28": "New York city construction codes", "29": "New York city fire code",
    "30": "Emergency management", "31": "Department of veterans' services",
    "32": "Office of nightlife", "33": "Investigations",
    "34": "Office of urban agriculture",
}


def _ref_id(matter_id: Any, body: str, section: str) -> str:
    return f"{matter_id}:{body}:{section}"[:200]


def _plausible(title: str, suffix: str) -> bool:
    """
    Is this a code section, or a bill number that happens to look like one?

    "0365-2026" and "27-2005" have the same shape, and the tell is the title:
    an Administrative Code title is 1-34 and never zero-padded, so a bill's
    four-digit sequence cannot be one.

    A tempting second rule -- reject a suffix that looks like a year -- is
    wrong and was in here. Titles 26, 27 and 28 number their sections in the
    2000s, so it silently discarded § 27-2005, the housing maintenance code,
    along with every other section in the most-legislated part of the code.
    The title check alone is sufficient: "0365-2026" fails it, because the
    regex cannot read "0365" as a one-or-two-digit title, and "2020-2021"
    fails because the character before the match is a digit.
    """
    if title.startswith("0") or not title.isdigit():
        return False
    return 1 <= int(title) <= MAX_TITLE


def _action_near(text: str, at: int, window: int = 220) -> str:
    """What is being done to the section named at this position."""
    around = text[max(0, at - window):at + window]
    if ADDING.search(around):
        return "added"
    m = ACTION.search(around)
    if not m:
        return "cited"
    word = m.group(1).lower()
    return "repealed" if word == "repealed" else word


def extract(text: str) -> list[dict]:
    """Every law reference in one bill's text, with what is done to it."""
    out: dict[str, dict] = {}
    if not text:
        return []

    for m in SECTION.finditer(text):
        title, suffix = m.group(1), m.group(2)
        if not _plausible(title, suffix):
            continue
        section = f"{title}-{suffix}"
        key = f"admin_code:{section}"
        action = _action_near(text, m.start())
        prior = out.get(key)
        # A section mentioned once as amended and ten times in passing is an
        # amended section. The operative verb wins over the count.
        if prior is None or (prior["action"] == "cited" and action != "cited"):
            out[key] = {"body_of_law": "admin_code", "title": title,
                        "section": section, "action": action}

    for m in CHARTER.finditer(text):
        section = m.group(1)
        key = f"charter:{section}"
        out.setdefault(key, {
            "body_of_law": "charter", "title": "City Charter",
            "section": section, "action": _action_near(text, m.start())})

    for m in RULES.finditer(text):
        section, title = m.group(1), m.group(2)
        key = f"rules:{title}-{section}"
        out.setdefault(key, {
            "body_of_law": "rules", "title": title, "section": section,
            "action": _action_near(text, m.start())})

    for m in STATE_LAW.finditer(text):
        section, law = m.group(1), " ".join(m.group(2).split()).lower()
        if len(law) < 4 or law in ("such", "this", "the"):
            continue
        key = f"state:{law}:{section}"
        out.setdefault(key, {
            "body_of_law": "state", "title": law.title(), "section": section,
            "action": "cited"})

    return list(out.values())


def subject_of(title_text: str) -> str | None:
    """Which body of law the bill's own recital says it amends."""
    m = SUBJECT.search(title_text or "")
    return " ".join(m.group(1).split()).lower() if m else None


def ingest(store: Store, limit: int | None = None) -> dict:
    """Build the code index over every bill whose text is loaded."""
    sql = ("SELECT t.matter_id, t.file, t.body, m.year, m.stage "
           "FROM matter_text t JOIN matters m ON m.matter_id = t.matter_id "
           "WHERE t.body != ''")
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = store.q(sql)

    refs: list[tuple] = []
    counts = {"bills": 0, "with_refs": 0, "refs": 0}
    for r in rows:
        counts["bills"] += 1
        found = extract(r["body"])
        if found:
            counts["with_refs"] += 1
        for f in found:
            refs.append((
                _ref_id(r["matter_id"], f["body_of_law"], f["section"]),
                r["matter_id"], r["file"], f["body_of_law"], f["title"],
                f["section"], f["action"], r["year"], r["stage"],
                "LEGISTAR_MIRROR"))
    counts["refs"] = len(refs)

    with store.tx() as c:
        c.execute("DELETE FROM code_refs")
        c.executemany(
            "INSERT OR REPLACE INTO code_refs (ref_id, matter_id, file, "
            "body_of_law, title, section, action, year, stage, source_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", refs)
    store.set_meta("codes.last_ingest", counts)
    store.journal("ingest.codes", counts)
    return counts
