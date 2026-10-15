#!/usr/bin/env python3
"""
A citation for anything the system can find.

The registry already knows how to cite a *source* — the Schedule C book, the
Legistar mirror, the BOE recap. What it could not do is cite a *record*: the
one bill, the one funding line, the one reconciliation row a staffer just
found and is about to put in front of the Councilmember.

That gap shows up at exactly the wrong moment. A staffer finds Int 0365-2026,
pastes the title into a memo, and the memo now asserts something with no way
back to the document. Nobody did anything careless; the system simply never
offered the citation, so there was nothing to paste.

So: every record kind renders four ways.

    inline      (Int 0365-2026, NYC Council, accessed 2026-09-14)
    footnote    [3] Int 0365-2026 — Requiring the department of housing … —
                NYC Council Legistar — https://legistar…?ID=77661 — accessed …
    locator     the precise position: a sheet and row, a page, a line id
    url         the page a reader opens to check it

The locator is the one that matters most and is the easiest to drop. "Schedule
C FY2027" is not a citation; "Schedule C FY2027, line SC2027-8f3a…, Greenbelt
Conservancy, $40,000" is one, because a reader can land on the row.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

from .store import Store

LEGISTAR = "https://legistar.council.nyc.gov/LegislationDetail.aspx?ID="

# Publisher of record per source family. A citation that names the system
# rather than the publisher is worthless in a hearing room.
PUBLISHERS = {
    "LEGISTAR_MIRROR": ("NYC Council Legislative Research Center (Legistar)",
                        "New York City Council"),
    "COUNCIL_RECORD": ("The Council Record", "New York City Council"),
    "SCHEDULE_C": ("Adopted Budget — Schedule C (member designations)",
                   "New York City Council, Finance Division"),
    "TRANSPARENCY_RESO": ("Transparency Resolution report",
                          "New York City Council, Finance Division"),
    "SI_TR_LEDGER": ("Staten Island Transparency Resolution ledger",
                     "Office of Council Member Kamillah Hanks"),
    "SI_ROLLUP": ("Staten Island funding rollup",
                  "Office of Council Member Kamillah Hanks"),
    "D49_MOCS_TRACKER": ("District 49 MOCS designation tracker",
                         "Office of Council Member Kamillah Hanks"),
    "OFFICE-WB": ("FY27 Staten Island EIN Census and Reconciliation",
                  "Office of Council Member Kamillah Hanks"),
    "OFFICE-D49": ("District 49 FY2027 Full Breakdown",
                   "Office of Council Member Kamillah Hanks"),
}


def today() -> str:
    return date.today().isoformat()


def _publisher(source_id: str | None) -> tuple[str, str]:
    return PUBLISHERS.get(source_id or "", (source_id or "the corpus", ""))


def for_record(store: Store, record: dict) -> dict:
    """
    Build a full citation for one record.

    Takes whatever shape the caller has — a workspace record, a raw matter
    row, a ledger row — because the office's own search returns all three and
    a citation that only works on one of them will be the one nobody uses.
    """
    kind = (record.get("kind") or "").lower()
    builder = BUILDERS.get(kind, _generic)
    cite = builder(store, record)
    name, publisher = _publisher(cite.get("source_id"))
    cite.setdefault("source", name)
    cite.setdefault("publisher", publisher)
    cite["accessed"] = today()
    cite["inline"] = _inline(cite)
    cite["footnote"] = _footnote(cite)
    return cite


def _inline(c: dict) -> str:
    bits = [b for b in (c.get("label"), c.get("locator"),
                        c.get("publisher") or c.get("source")) if b]
    return "(" + "; ".join(bits) + f"; accessed {c['accessed']})"


def _footnote(c: dict) -> str:
    parts = [c.get("label") or c.get("source") or "record"]
    for key in ("detail", "locator", "source", "publisher", "url"):
        v = c.get(key)
        if v and v not in parts:
            parts.append(str(v))
    parts.append(f"accessed {c['accessed']}")
    return " — ".join(parts)


# ------------------------------------------------------------ builders ----
def _matter(store: Store, r: dict) -> dict:
    mid = str(r.get("entity_id") or r.get("matter_id") or "").strip()
    file_no = r.get("file")
    name = r.get("name")
    status = r.get("status")
    law = r.get("local_law")
    if (not file_no or not status) and mid:
        got = store.q("SELECT file, name, status, local_law, committee, year "
                      "FROM matters WHERE matter_id = ?", (mid,))
        if got:
            d = dict(got[0])
            file_no = file_no or d["file"]
            name = name or d["name"]
            status = status or d["status"]
            law = law or d["local_law"]
    # A title like "Int 0365-2026 — Requiring the department…" already carries
    # the file number; splitting it back out avoids printing it twice.
    if not file_no and r.get("title") and "—" in str(r["title"]):
        file_no, _, name = str(r["title"]).partition("—")
        file_no, name = file_no.strip(), name.strip()
    detail = status or ""
    if law:
        year, _, num = str(law).partition("/")
        detail = f"enacted as Local Law {int(num)} of {year}" if num else detail
    return {"kind": "matter", "label": file_no or f"Matter {mid}",
            "detail": (name or "")[:160] or None,
            "locator": detail or None,
            "source_id": r.get("source_id") or "LEGISTAR_MIRROR",
            "url": r.get("url") or (LEGISTAR + mid if mid else None),
            "entity": mid}


# What to call a funding line, by where it came from. A line from the Staten
# Island rollup labelled "Schedule C" is a small lie that puts the office's
# own working ledger in a citation as though it were the adopted budget.
BOOK_LABEL = {
    "SCHEDULE_C": "Adopted Schedule C",
    "TRANSPARENCY_RESO": "Transparency Resolution",
    "SI_TR_LEDGER": "SI Transparency Resolution ledger",
    "SI_ROLLUP": "SI funding rollup",
    "D49_MOCS_TRACKER": "D49 MOCS tracker",
}


def _funding(store: Store, r: dict) -> dict:
    org = r.get("org") or r.get("title") or "a designation"
    fy = r.get("fy") or r.get("year")
    amount = r.get("amount")
    # The workspace key is "funding:SC2027-…"; the citable line id is the part
    # after the colon. Printing the prefix puts an internal table name into a
    # citation a reporter may read.
    line = str(r.get("line_id") or r.get("key") or "").split(":", 1)[-1]
    source_id = r.get("source_id") or "SCHEDULE_C"
    book = BOOK_LABEL.get(source_id, source_id.replace("_", " ").title())
    bits = [str(org)[:90]]
    if amount is not None:
        bits.append(f"${float(amount):,.0f}")
    if r.get("member"):
        bits.append(f"designated by {r['member']}")
    if line:
        bits.append(f"line {line}")
    return {"kind": "funding",
            "label": f"{book} FY{fy}" if fy else book,
            "detail": None,
            "locator": ", ".join(b for b in bits if b),
            "source_id": source_id,
            "url": r.get("url"), "entity": line}


def _ledger(store: Store, r: dict) -> dict:
    # The reconciliation's own locator is already exact — book, sheet, row —
    # and is the whole reason those columns are kept.
    loc = r.get("locator")
    if not loc and r.get("row_id"):
        got = store.q("SELECT locator, book, sheet, row_no FROM ledger "
                      "WHERE row_id = ?", (r["row_id"],))
        loc = dict(got[0])["locator"] if got else None
    return {"kind": "ledger",
            "label": (r.get("label") or r.get("title") or "reconciliation row")[:120],
            "detail": (f"${float(r['amount']):,.0f}"
                       if r.get("amount") is not None else None),
            "locator": loc,
            "source_id": r.get("source_id") or "OFFICE-WB",
            "url": None, "entity": r.get("row_id") or r.get("key")}


def _org(store: Store, r: dict) -> dict:
    ein = r.get("ein")
    return {"kind": "org", "label": r.get("org") or r.get("title") or "organisation",
            "detail": f"EIN {ein}" if ein else None,
            "locator": "Schedule C recipient register",
            "source_id": r.get("source_id") or "SCHEDULE_C",
            "url": (f"https://projects.propublica.org/nonprofits/"
                    f"organizations/{str(ein).replace('-', '')}" if ein else None),
            "entity": r.get("org_key") or ein}


def _calendar(store: Store, r: dict) -> dict:
    return {"kind": "calendar",
            "label": r.get("title") or r.get("summary") or "calendar item",
            "detail": r.get("start") or r.get("year"),
            "locator": r.get("location") or "District 49 office calendar",
            "source_id": r.get("source_id") or "OFFICE_CALENDAR",
            "source": "District 49 office calendar",
            "publisher": "Office of Council Member Kamillah Hanks",
            "url": r.get("url"), "entity": r.get("entity_id")}


def _contact(store: Store, r: dict) -> dict:
    return {"kind": "contact",
            "label": r.get("title") or r.get("name") or "contact",
            "detail": r.get("org"),
            "locator": (f"confirmed {r['verified']}" if r.get("verified")
                        else "unverified — confirm before use"),
            "source_id": r.get("source_id") or "SI_DIRECTORY",
            "source": "Staten Island directory",
            "publisher": r.get("verified_at") or "published agency listing",
            "url": r.get("verified_at") or r.get("url"),
            "entity": r.get("entity_id")}


def _generic(store: Store, r: dict) -> dict:
    return {"kind": r.get("kind") or "record",
            "label": r.get("title") or r.get("label") or str(r.get("key") or "record"),
            "detail": None,
            "locator": r.get("locator"),
            "source_id": r.get("source_id"),
            "url": r.get("url"), "entity": r.get("key")}


BUILDERS = {
    "matter": _matter, "funding": _funding, "ledger": _ledger,
    "org": _org, "calendar": _calendar, "contact": _contact,
}


def for_results(store: Store, rows: list[dict]) -> list[dict]:
    """Attach a citation to every row of a search result, in place."""
    for r in rows:
        try:
            r["citation"] = for_record(store, r)
        except Exception as exc:                        # noqa: BLE001
            # A citation that throws must not take the search result with it.
            # A row that cannot be cited is still a row worth showing, as long
            # as it says so rather than appearing quotable.
            r["citation"] = {"kind": "error", "label": r.get("title") or "",
                             "note": f"could not build a citation: {exc}"}
    return rows


def bibliography(store: Store, rows: list[dict]) -> list[str]:
    """Numbered footnotes for a set of records, deduplicated, in order."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for r in rows:
        cite = r.get("citation") or for_record(store, r)
        key = cite.get("footnote") or ""
        if not key or key in seen:
            continue
        seen[key] = len(out) + 1
        out.append(f"[{len(out) + 1}] {key}")
    return out
