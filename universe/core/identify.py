#!/usr/bin/env python3
"""
Recognising an identifier before treating it as prose.

Typing "365-2026" into the search box returned:

    No record contains all of 365, 2026; showing records matching any of
    them, best first.

Two bills carry that number -- Int 0365-2026 and Res 0365-2026 -- and the
search returned neither near the top. It split the identifier on the hyphen,
looked for records containing both "365" and "2026", found none because the
corpus stores the number zero-padded as "0365-2026", and then fell back to
matching *either* token. That fallback is correct behaviour for a sentence and
completely wrong for an identifier: "2026" alone matches every bill introduced
this session, so the one record the user asked for is buried under seventeen
hundred others.

The relevance ladder is not the bug. The bug is that the ladder ran at all. A
bill number is not a phrase to be loosened; it is a key, and a key either
resolves or it does not. So identifiers are recognised first, resolved
exactly, and never degraded.

What the office actually types, all of which must work:

    365-2026        Int 365-2026      Int. 0365-2026    int0365-2026
    Res 365-2026    Res. 365/2026     T2026-1234        20265001 HAM
    LL 42 of 2025   Local Law 42/2025 77661             13-3481845

Zero-padding is the quiet one. Legistar pads the sequence to four digits and
nobody types it that way, so every un-padded number missed. Padding is applied
on the way in, and both forms are tried.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from .store import Store

# Int / Res / land use prefixes as Legistar writes them, and as people type
# them. The trailing dot and the space are both optional because both appear
# in the office's own emails.
PREFIX = {
    "int": "Int", "introduction": "Int", "intro": "Int", "i": "Int",
    "res": "Res", "resolution": "Res", "reso": "Res", "r": "Res",
    "ln": "LU", "lu": "LU", "landuse": "LU", "land": "LU",
    "t": "T", "pre": "Pre", "sli": "SLI", "m": "M",
}

# "Int 0365-2026", "res.365/2026", "365-2026"
BILL = re.compile(
    r"^\s*(?:(?P<pre>[A-Za-z]{1,12})\.?\s*)?"
    r"(?P<num>\d{1,5})\s*[-/]\s*(?P<year>(?:19|20)\d{2})\s*$")
# "LL 42 of 2025", "Local Law 42/2025", "local law 42-2025"
LOCAL_LAW = re.compile(
    r"^\s*(?:local\s+law|ll)\.?\s*(?P<num>\d{1,4})\s*"
    r"(?:of|/|-)?\s*(?P<year>(?:19|20)\d{2})?\s*$", re.I)
# A bare Legistar matter id. Five digits and up; below that it is a year or an
# amount and guessing would be worse than not matching.
MATTER_ID = re.compile(r"^\s*(?P<id>\d{5,7})\s*$")
# "13-3481845" or "133481845"
EIN = re.compile(r"^\s*(?P<a>\d{2})-?(?P<b>\d{7})\s*$")
# Land use: "20265001 HAM", "C 260123 ZMR"
ULURP = re.compile(r"^\s*(?P<num>[A-Z]?\s?\d{6,9})\s*(?P<suf>[A-Z]{3,4})\s*$", re.I)
# The system's own row keys.
ROW_KEY = re.compile(r"^\s*(?P<key>(?:LG|SC)[A-Za-z0-9-]{6,})\s*$")
FY = re.compile(r"^\s*FY\s?(?P<year>(?:20)?\d{2})\s*$", re.I)


def pad(num: str | int, width: int = 4) -> str:
    """Legistar pads the sequence to four digits; nobody types it that way."""
    return str(int(num)).zfill(width)


def parse(text: str) -> dict | None:
    """
    What kind of identifier is this, if any.

    Returns None for ordinary prose, which is the common case and must stay
    cheap -- this runs on every keystroke of every search.
    """
    raw = (text or "").strip()
    # A length cap only. An earlier word-count guard rejected anything over
    # four words and so never let "Local Law 1 of 2025" -- five words, and
    # exactly the form a lawyer writes -- reach the parser at all. Every
    # pattern below is anchored at both ends, so running them on a short
    # string is cheap and a sentence falls through immediately.
    if not raw or len(raw) > 48:
        return None

    m = LOCAL_LAW.match(raw)
    if m:
        return {"kind": "local_law", "number": m.group("num"),
                "year": m.group("year"),
                "display": f"Local Law {int(m.group('num'))}"
                           + (f" of {m.group('year')}" if m.group("year") else "")}

    m = BILL.match(raw)
    if m:
        pre = (m.group("pre") or "").lower().rstrip(".")
        kind = PREFIX.get(pre)
        if pre and kind is None:
            return None                      # a word, not a prefix
        num, year = pad(m.group("num")), m.group("year")
        return {"kind": "bill", "prefix": kind, "number": num, "year": year,
                "file": f"{kind} {num}-{year}" if kind else None,
                "stem": f"{num}-{year}",
                "display": f"{kind} {num}-{year}" if kind
                           else f"bill {int(num)}-{year}"}

    m = ROW_KEY.match(raw)
    if m:
        return {"kind": "row", "key": m.group("key").upper(),
                "display": m.group("key").upper()}

    m = FY.match(raw)
    if m:
        y = m.group("year")
        year = int(y) if len(y) == 4 else 2000 + int(y)
        return {"kind": "fy", "year": year, "display": f"FY{year}"}

    m = ULURP.match(raw)
    if m and not raw.isdigit():
        return {"kind": "ulurp",
                "number": re.sub(r"\s+", "", m.group("num")).upper(),
                "suffix": m.group("suf").upper(),
                "display": f"{m.group('num').strip()} {m.group('suf').upper()}"}

    m = EIN.match(raw)
    if m:
        return {"kind": "ein", "ein": f"{m.group('a')}-{m.group('b')}",
                "display": f"EIN {m.group('a')}-{m.group('b')}"}

    m = MATTER_ID.match(raw)
    if m:
        return {"kind": "matter_id", "id": m.group("id"),
                "display": f"matter {m.group('id')}"}

    return None


# ------------------------------------------------------------ resolving ----
def resolve(store: Store, text: str, limit: int = 25) -> dict | None:
    """
    Turn an identifier into the records it names.

    Returns None when the text is not an identifier, so the caller falls
    through to ordinary search. Returns a result with an empty ``records``
    list when it *is* an identifier that names nothing -- which is a different
    and much more useful answer than a page of loose matches, because it tells
    the office the bill does not exist rather than burying that fact.
    """
    ident = parse(text)
    if ident is None:
        return None
    rows = HANDLERS[ident["kind"]](store, ident, limit)
    return {
        "identifier": ident,
        "records": rows,
        "exact": True,
        "note": _note(ident, rows),
    }


def _note(ident: dict, rows: list[dict]) -> str:
    if not rows:
        if ident["kind"] == "bill":
            return (f"No bill numbered {ident['stem']} is in the corpus. "
                    f"Either it has not been introduced, or the Legistar "
                    f"mirror has not synced since it was. "
                    f"Run `universe repos sync` and look again.")
        return f"Nothing in the corpus matches {ident['display']}."
    if ident["kind"] == "bill" and not ident.get("prefix") and len(rows) > 1:
        kinds = ", ".join(sorted({r.get("file", "").split()[0]
                                  for r in rows if r.get("file")}))
        return (f"{len(rows)} matters share the number {ident['stem']} "
                f"({kinds}). Type the prefix to narrow it — "
                f"for example “Int {ident['stem']}”.")
    return f"Resolved {ident['display']} exactly."


def _matters(store: Store, where: str, params: Iterable,
             limit: int) -> list[dict]:
    rows = store.q(
        f"SELECT matter_id, file, name, type, status, committee, year, "
        f"session, enacted, local_law, n_sponsors, prime_id, source_id "
        f"FROM matters WHERE {where} "
        f"ORDER BY year DESC, file LIMIT ?", (*params, limit))
    out = []
    for r in rows:
        d = dict(r)
        d["kind"] = "matter"
        d["key"] = f"matter:{r['matter_id']}"
        d["url"] = ("https://legistar.council.nyc.gov/LegislationDetail.aspx"
                    f"?ID={r['matter_id']}")
        out.append(d)
    return out


def _by_bill(store: Store, ident: dict, limit: int) -> list[dict]:
    # Both the padded and the typed form, because a corpus loaded from a
    # different export may not be padded at all.
    stem, num, year = ident["stem"], ident["number"], ident["year"]
    bare = f"{int(num)}-{year}"
    if ident.get("prefix"):
        return _matters(store, "file IN (?, ?)",
                        (f"{ident['prefix']} {stem}",
                         f"{ident['prefix']} {bare}"), limit)
    return _matters(store, "file LIKE ? OR file LIKE ?",
                    (f"%{stem}", f"%{bare}"), limit)


def _by_local_law(store: Store, ident: dict, limit: int) -> list[dict]:
    """
    Local Law 42 of 2025 is stored as "2025/042".

    The year in a local law number is the year it was *enacted*, which is
    routinely not the year the bill was introduced -- Local Law 2025/001 is
    Int 1022-2024. Filtering on the matter's year instead of on the law number
    is therefore wrong in the common case, and wrong in a way that returns a
    plausible bill rather than nothing.
    """
    num = pad(ident["number"], 3)
    if ident.get("year"):
        return _matters(store, "local_law = ?",
                        (f"{ident['year']}/{num}",), limit)
    # No year given: every law carrying that number, newest first, so the
    # office can see which one was meant.
    return _matters(store, "local_law LIKE ?", (f"%/{num}",), limit)


def _by_matter_id(store: Store, ident: dict, limit: int) -> list[dict]:
    return _matters(store, "matter_id = ?", (ident["id"],), limit)


def _by_ein(store: Store, ident: dict, limit: int) -> list[dict]:
    rows = store.q(
        "SELECT org, org_key, ein, fy, member, district, amount, program, "
        "agency, source_id, line_id FROM funding WHERE ein = ? "
        "ORDER BY fy DESC, amount DESC LIMIT ?", (ident["ein"], limit))
    return [{**dict(r), "kind": "funding", "key": f"funding:{r['line_id']}"}
            for r in rows]


def _by_row(store: Store, ident: dict, limit: int) -> list[dict]:
    key = ident["key"]
    if key.startswith("LG"):
        rows = store.q(
            "SELECT row_id, book, sheet, row_no, label, amount, kind, locator "
            "FROM ledger WHERE row_id = ? LIMIT ?", (key, limit))
        return [{**dict(r), "kind": "ledger", "key": r["row_id"]} for r in rows]
    rows = store.q(
        "SELECT line_id, fy, member, org, ein, amount, program, agency, "
        "source_id FROM funding WHERE line_id = ? LIMIT ?", (key, limit))
    return [{**dict(r), "kind": "funding", "key": r["line_id"]} for r in rows]


def _by_fy(store: Store, ident: dict, limit: int) -> list[dict]:
    # A fiscal year is not a record, it is a filter. Returning the year's
    # shape is more useful than returning an arbitrary slice of its lines.
    r = store.q(
        "SELECT COUNT(*) lines, SUM(amount) total, COUNT(DISTINCT org_key) orgs "
        "FROM funding WHERE fy = ?", (ident["year"],))
    if not r or not r[0]["lines"]:
        return []
    d = dict(r[0])
    d.update({"kind": "fiscal_year", "key": f"fy:{ident['year']}",
              "fy": ident["year"],
              "title": f"FY{ident['year']} — {d['lines']:,} funding lines"})
    return [d]


def _by_ulurp(store: Store, ident: dict, limit: int) -> list[dict]:
    needle = f"%{ident['number']}%"
    return _matters(store, "file LIKE ? OR name LIKE ?", (needle, needle), limit)


HANDLERS = {
    "bill": _by_bill,
    "local_law": _by_local_law,
    "matter_id": _by_matter_id,
    "ein": _by_ein,
    "row": _by_row,
    "fy": _by_fy,
    "ulurp": _by_ulurp,
}
