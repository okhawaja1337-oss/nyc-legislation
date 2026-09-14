#!/usr/bin/env python3
"""
The legislative record, kept current from upstream.

The corpus was built once from a static export of the Council Record. It holds
21,537 matters and a great deal of derived intelligence over them -- pass
probabilities, topic tags, pillar assignments -- and it is four days stale the
moment it is built, because bills are introduced every week the Council sits.

It is also missing the two fields an office actually reads. The export carried
each bill's *name*, a single truncated line, and nothing else. Not the official
summary. Not the text. So the corpus could tell you that Int 1068-2026 exists
and what committee it sits in, and could not tell you what it does.

This module reads the upstream Legistar mirror -- the same flat-file mirror the
Council Record was built from, but live -- and enriches rather than replaces.
The derived intelligence stays; status, committee, enactment and sponsorship
are refreshed from upstream; and the summary, the full text and the attachment
list are loaded for the first time.

The enrichment is the point. A search for "Legionnaires" should find the bill
that establishes the hotline whether or not those words survived into the
truncated name -- and until the text is loaded, it cannot.

Incremental by LastModified. A full pass over 21,628 files takes a few minutes;
a daily pass touches the handful that moved.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..core.store import Store

# Where each Legistar type lives in the mirror, and the code the corpus uses.
SECTIONS = (("introduction", "I"), ("resolution", "R"), ("land_use", "LU"))

# The matter_text table lives in the base schema (core/store.py): a lake
# that has never synced still has to answer a question about a bill.
# Legistar writes an unset date as year zero rather than null.
NULL_DATE = re.compile(r"^0001-01-01")
# Text arrives with hard line wrapping and RTF residue.
WS = re.compile(r"[ \t]*\n[ \t]*")
LEGAL_NOISE = re.compile(
    r"\b(?:Local Law Enacted|LS ?#?\d+|Int\.? No\.? \d+)\b", re.I)
TEXT_CAP = 60_000      # a handful of bills run to novellas; index the front


def _date(v: Any) -> str | None:
    s = str(v or "").strip()
    if not s or NULL_DATE.match(s):
        return None
    return s[:10]


def _clean(text: Any, cap: int = TEXT_CAP) -> str:
    s = str(text or "")
    if not s.strip():
        return ""
    s = WS.sub("\n", s).strip()
    return s[:cap]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def files(root: Path, years: set[int] | None = None) -> Iterator[tuple[Path, str]]:
    """Every matter file in the mirror, newest year first."""
    for section, code in SECTIONS:
        base = root / section
        if not base.is_dir():
            continue
        for ydir in sorted(base.iterdir(), reverse=True):
            if not ydir.is_dir() or not ydir.name.isdigit():
                continue
            if years and int(ydir.name) not in years:
                continue
            for path in sorted(ydir.glob("*.json")):
                yield path, code


def read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        # One malformed file must not stop the sync. It is recorded in the
        # result rather than raised, because an office that cannot sync
        # because of one bad bill has no sync at all.
        return None


def session_of(year: int | None) -> str | None:
    """
    Which four-year Council session a year belongs to.

    Sessions run 2022-2025 on the current numbering, so the arithmetic is
    fixed rather than read off the corpus -- deriving it from the data would
    put a new session's first bill in the previous session, which is exactly
    when it matters.
    """
    if not year:
        return None
    if year >= 2026:
        return str(10 + (year - 2026) // 4)
    if year >= 2024:
        return "9"
    if year >= 2022:
        return "8"
    if year >= 2018:
        return "7"
    if year >= 2014:
        return "6"
    if year >= 2010:
        return "5"
    if year >= 2006:
        return "4"
    if year >= 2004:
        return "3"
    if year >= 2002:
        return "2"
    if year >= 1998:
        return "1"
    return "0"


def _committee(body_name: Any) -> str | None:
    """'Committee on Transportation and Infrastructure' -> the subject."""
    s = str(body_name or "").strip()
    m = re.match(r"^Committee on\s+(.+)$", s, re.I)
    if m:
        return m.group(1).strip()
    return s or None


def _local_law(row: dict) -> str | None:
    for key in ("EnactmentNumber", "LocalLaw", "EnactmentNum"):
        v = str(row.get(key) or "").strip()
        if v and v not in ("0", "None"):
            return v
    return None


def ingest(store: Store, root: Path | str, years: set[int] | None = None,
           since: str | None = None, limit: int | None = None) -> dict:
    """
    Load the mirror, enriching what is already here.

    ``since`` is a LastModified cut-off: pass the previous run's high-water
    mark and only the files that moved are read. ``years`` narrows to a
    session, which is what a weekly refresh actually wants.
    """
    root = Path(root)
    if not root.is_dir():
        return {"error": f"no Legistar mirror at {root}", "loaded": 0}

    known = {int(r["matter_id"]): dict(r) for r in store.q(
        "SELECT matter_id, status, committee, enacted, local_law, n_sponsors "
        "FROM matters")}
    counts = {"seen": 0, "new": 0, "updated": 0, "text": 0, "skipped": 0,
              "unreadable": 0, "superseded": 0, "sponsorships": 0}
    # Keyed by matter id, not appended. Upstream files one matter under two
    # sections at least once -- Int 1142-2023 sits in both introduction/ and
    # resolution/ with different statuses -- and the sponsorship table is
    # keyed on (matter, person), so a straight append fails the whole load on
    # a single upstream filing error. Last modified wins, because that is
    # upstream's own statement of which copy is current.
    matters: dict[int, tuple] = {}
    texts: dict[int, tuple] = {}
    sponsor_rows: dict[int, list[tuple]] = {}
    index: dict[int, tuple] = {}
    stamps: dict[int, str] = {}
    high_water = since or ""

    for path, code in files(root, years):
        if limit and counts["seen"] >= limit:
            break
        row = read(path)
        if not row or not row.get("ID"):
            counts["unreadable"] += 1
            continue
        modified = str(row.get("LastModified") or "")
        if since and modified and modified <= since:
            counts["skipped"] += 1
            continue
        mid = int(row["ID"])
        if mid in stamps and modified <= stamps[mid]:
            counts["superseded"] += 1
            continue
        if mid in stamps:
            counts["superseded"] += 1
        else:
            counts["seen"] += 1
        stamps[mid] = modified
        high_water = max(high_water, modified)
        file_no = str(row.get("File") or "").strip()
        year = None
        m = re.search(r"-((?:19|20)\d{2})", file_no)
        if m:
            year = int(m.group(1))
        status = str(row.get("StatusName") or "").strip() or None
        committee = _committee(row.get("BodyName"))
        law = _local_law(row)
        enacted = 1 if (_date(row.get("EnactmentDate")) or law) else 0
        sponsors = row.get("Sponsors") or []
        prime = sponsors[0].get("ID") if sponsors else None

        n_sponsors = len({sp["ID"] for sp in sponsors if sp.get("ID")})
        prior = known.get(mid)
        if prior is None:
            counts["new"] += 1
        elif (prior.get("status") != status
              or prior.get("enacted") != enacted
              or (prior.get("local_law") or None) != law
              or prior.get("committee") != committee
              or prior.get("n_sponsors") != n_sponsors):
            counts["updated"] += 1

        matters[mid] = (mid, file_no, str(row.get("Name") or "").strip(),
                        code, status, committee, year, session_of(year),
                        enacted, law, prime, n_sponsors,
                        "LEGISTAR_MIRROR", _now())

        summary = _clean(row.get("Summary"), 8000)
        body = _clean(row.get("Text"))
        if summary or body:
            counts["text"] += 1
        texts[mid] = (
            mid, file_no, str(row.get("Title") or "").strip(), summary, body,
            _date(row.get("IntroDate")), _date(row.get("AgendaDate")),
            _date(row.get("PassedDate")), _date(row.get("EnactmentDate")),
            str(row.get("Version") or "").strip(),
            # Name and link only, and at most eight. The full attachment
            # blocks are 22 MB across the corpus -- more than the bill text --
            # and nothing reads past the first few.
            json.dumps([{"name": a.get("Name"), "link": a.get("Link")}
                        for a in (row.get("Attachments") or [])[:8]],
                       ensure_ascii=False),
            # The last twelve actions. A bill's full history runs to
            # hundreds of committee-laid-over entries and nothing reads past
            # the recent ones.
            json.dumps([{"date": _date(h.get("Date")), "action": h.get("Action"),
                         "body": h.get("BodyName")}
                        for h in (row.get("History") or [])[-12:]],
                       ensure_ascii=False),
            modified, "LEGISTAR_MIRROR",
            f"https://legistar.council.nyc.gov/LegislationDetail.aspx?ID={mid}")

        # Upstream occasionally lists the same person twice on one matter --
        # usually a prime sponsor who also appears in the co-sponsor block.
        # The table is keyed on (matter, person), so the duplicate is not a
        # second sponsorship to record; it is the same one, and the first
        # listing is the authoritative role.
        seen_sponsors: set[int] = set()
        rows_for_matter: list[tuple] = []
        for n, sp in enumerate(sponsors):
            pid = sp.get("ID")
            if not pid or int(pid) in seen_sponsors:
                continue
            seen_sponsors.add(int(pid))
            rows_for_matter.append((mid, int(pid),
                                    "prime" if n == 0 else "cosponsor"))
        sponsor_rows[mid] = rows_for_matter

        # The file number goes in the body as typed *and* unpadded, so that
        # "365-2026" and "0365-2026" both reach the record through ordinary
        # search as well as through the identifier resolver.
        bare = re.sub(r"\b0+(\d)", r"\1", file_no)
        index[mid] = (
            "matter", str(mid),
            f"{file_no} — {str(row.get('Name') or '')[:160]}",
            " ".join(filter(None, [file_no, bare, row.get("Name") or "",
                                   summary, body[:4000]]))[:20000],
            " ".join(filter(None, [code, status or "", committee or "",
                                   str(year or ""), law or ""])))

    flat_sponsors = [r for rows in sponsor_rows.values() for r in rows]
    _write(store, list(matters.values()), list(texts.values()), flat_sponsors)
    counts["sponsorships"] = len(flat_sponsors)
    if index:
        store.index_many(list(index.values()))
    store.set_meta("legistar.last_ingest", {
        "at": _now(), "root": str(root), "high_water": high_water,
        "counts": counts, "years": sorted(years) if years else None})
    store.journal("ingest.legistar", counts)
    return {**counts, "high_water": high_water}


def _write(store: Store, matters: list[tuple], texts: list[tuple],
           sponsors: list[tuple]) -> None:
    if not matters:
        return
    with store.tx() as c:
        # Upsert, never replace: the corpus carries derived intelligence --
        # pass probabilities, topic tags, pillar assignments, the D49 score --
        # that upstream knows nothing about. Overwriting the row would throw
        # all of it away on every sync.
        c.executemany(
            "INSERT INTO matters (matter_id, file, name, type, status, "
            "committee, year, session, enacted, local_law, prime_id, "
            "n_sponsors, source_id, updated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(matter_id) DO UPDATE SET "
            "  file=excluded.file, name=excluded.name, type=excluded.type, "
            "  status=excluded.status, committee=excluded.committee, "
            "  year=COALESCE(excluded.year, matters.year), "
            "  session=COALESCE(excluded.session, matters.session), "
            "  enacted=excluded.enacted, "
            "  local_law=COALESCE(excluded.local_law, matters.local_law), "
            "  prime_id=COALESCE(excluded.prime_id, matters.prime_id), "
            "  n_sponsors=excluded.n_sponsors, updated=excluded.updated, "
            # The mirror is now authoritative for every fact a citation
            # prints -- file number, name, status, committee, sponsors -- so
            # the row must say so. Leaving source_id at COUNCIL_RECORD cites a
            # static export for a status that was refreshed an hour ago.
            "  source_id=excluded.source_id",
            matters)
        c.executemany(
            "INSERT OR REPLACE INTO matter_text (matter_id, file, title, "
            "summary, body, intro_date, agenda_date, passed_date, "
            "enacted_date, version, attachments, history, last_modified, "
            "source_id, url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", texts)
        if sponsors:
            # Replace a matter's sponsor set wholesale. A bill picks up and
            # occasionally loses co-sponsors, and merging would leave a member
            # credited with a bill they withdrew from.
            ids = sorted({m for m, _, _ in sponsors})
            c.executemany("DELETE FROM sponsorships WHERE matter_id = ?",
                          [(i,) for i in ids])
            c.executemany(
                "INSERT INTO sponsorships (matter_id, person_id, role) "
                "VALUES (?,?,?)", sponsors)


def trim_text(store: Store, keep_from: int = 2022) -> dict:
    """
    Drop bill text older than a session, keeping every summary.

    The full text of 21,627 matters is 77 MB; the summaries are 3.6 MB. For a
    package the office downloads, that is the difference between a minute and
    ten. The text of a 1999 bill is almost never searched, and when it is,
    `universe repos sync --force` brings it back.

    Summaries are never dropped. They are what makes a bill findable by what
    it does rather than by its truncated name, they cost almost nothing, and
    an office that cannot read a summary has lost the thing this data was
    loaded for.
    """
    before = store.q("SELECT COUNT(*) n FROM matter_text WHERE body != ''")[0]["n"]
    with store.tx() as c:
        c.execute(
            "UPDATE matter_text SET body = '' WHERE matter_id IN "
            "(SELECT matter_id FROM matters WHERE year < ?)", (keep_from,))
    after = store.q("SELECT COUNT(*) n FROM matter_text WHERE body != ''")[0]["n"]
    store.set_meta("legistar.text_trimmed", {
        "keep_from": keep_from, "dropped": before - after, "kept": after,
        "restore": "universe repos sync --repo legistar --force"})
    return {"dropped": before - after, "kept": after, "keep_from": keep_from}


def coverage(store: Store) -> dict:
    """What the mirror has given us, and how fresh it is."""
    last = store.get_meta("legistar.last_ingest") or {}
    try:
        totals = dict(store.q(
            "SELECT COUNT(*) rows, SUM(summary != '') summaries, "
            "SUM(body != '') texts, MAX(last_modified) newest "
            "FROM matter_text")[0])
    except Exception:                                   # noqa: BLE001
        totals = {"rows": 0, "summaries": 0, "texts": 0, "newest": None}
    matters = store.q("SELECT COUNT(*) n FROM matters")[0]["n"]
    trimmed = store.get_meta("legistar.text_trimmed") or {}
    return {"matters": matters, "with_text": totals,
            "last_ingest": last.get("at"),
            "high_water": last.get("high_water"),
            "text_trimmed": trimmed or None,
            "says": (
                f"Full bill text is loaded for sessions from "
                f"{trimmed['keep_from']} onward. Older text was left out of "
                f"this package to keep it small — every summary is here, and "
                f"`{trimmed['restore']}` brings the rest back."
                if trimmed else
                f"{totals['texts'] or 0:,} of {matters:,} matters carry their "
                f"full text."),
            "share_with_summary": (round(100 * (totals["summaries"] or 0)
                                         / matters, 1) if matters else 0)}
