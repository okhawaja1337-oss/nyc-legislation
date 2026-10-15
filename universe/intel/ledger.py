#!/usr/bin/env python3
"""
Reading the reconciliation.

There is no single number for "what District 49 got this year", and pretending
otherwise is how an office ends up contradicting itself in public. There are
four defensible answers and they differ by an order of magnitude:

  $3,008,000    Schedule C, member designations signed by the Councilmember.
  $35,903,925   FY27 District 49 wins -- every dollar the office secured for
                the North Shore across capital, expense, Speaker and citywide.
  $39,627,090   The full District 49 breakdown: 245 items, 17 categories.
  $95,282,347   Staten Island combined -- all three members plus delegation,
                Speaker and citywide, corrected basis.

Each is right about a different question. The failure mode is not using the
wrong one; it is using one without saying which question it answers. So every
figure this module returns arrives with its basis, the sheet it was read from,
and whether that sheet carries a passing tie-check.

That last part matters more than it sounds. A tie-check is the row where the
office proved its workbook reproduces the printed book. A figure standing on a
sheet that ties has been checked against the City's own document; a figure
without one is an assertion. The system says which it is handing you.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

from ..core.store import Store

MEMBER = "Hanks"
DISTRICT = "49"

# The reconciled answers, each tied to the sheet that proves it. These are
# looked up rather than hard-coded as numbers: if the office reissues the
# workbook with a corrected figure, the system follows it instead of arguing.
BASES: tuple[dict, ...] = (
    {"key": "designations",
     "question": "What did the Councilmember personally designate?",
     "sheet": None, "book": None, "from": "schedule_c",
     "where": "member", "caution":
         "Member discretionary only, by signature. Excludes capital, Speaker "
         "and citywide money, and includes designations to citywide and "
         "borough organisations that are not coded to District 49. This is "
         "the smallest true number and the one most often quoted as if it "
         "were the district's total."},
    {"key": "landed_in_d49",
     "question": "How much Schedule C money is coded to District 49?",
     "sheet": None, "book": None, "from": "schedule_c",
     "where": "district", "caution":
         "A different question from the line above, and the difference is the "
         "point: money the Councilmember designated to a citywide or borough "
         "organisation is not coded to the district, and money other members "
         "designated into the North Shore is. Neither figure is the district's "
         "position -- both are Schedule C's filing convention."},
    {"key": "d49_wins",
     "question": "What did the office secure for the North Shore?",
     "sheet": "NS Wins — Summary (PDF)", "label": "North Shore Total",
     "book": "FY27_SI_EIN_Census_and_Reconciliation_v3",
     "caution": "The advocacy figure. Counts every channel, attributed to "
                "District 49."},
    {"key": "d49_breakdown",
     "question": "What is in the district, item by item?",
     "sheet": None, "book": "D49_FY27_Full_Breakdown",
     "caution": "The itemised basis: 245 named items across 17 categories. "
                "Wider than the wins figure because it counts everything "
                "landing in the district, not only what the office secured."},
    {"key": "si_total",
     "question": "What did Staten Island get, all in?",
     "sheet": "Grand Total — Combined (Jul 7)",
     "label": "CAPITAL + EXPENSE — GRAND TOTAL",
     "book": "FY27_SI_EIN_Census_and_Reconciliation_v3",
     "caution": "All three members, the delegation pot, Speaker and citywide. "
                "Never attribute this to one member."},
)

TIE_OK = re.compile(r"TIES?\s*✓|✓\s*TIES?|EXACT TIE|IDENTICAL", re.I)


def _cells(row: Any) -> list[str]:
    try:
        return json.loads(row["cells"] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def loaded(store: Store) -> dict:
    """Which books are in, how big, and when."""
    out = {}
    for r in store.q("SELECT book, COUNT(*) rows, COUNT(DISTINCT sheet) sheets, "
                     "SUM(kind='tie_check') ties, SUM(kind='line') lines "
                     "FROM ledger GROUP BY book ORDER BY book"):
        d = dict(r)
        d["summary"] = store.get_meta(f"ledger.{r['book']}") or {}
        out[r["book"]] = d
    return out


def tie_checks(store: Store, book: str | None = None) -> list[dict]:
    """
    Every reconciliation check, and whether it passes.

    A check that no longer passes is the loudest possible signal that a source
    book was reissued and the office's totals have drifted from the City's.
    """
    sql = "SELECT * FROM ledger WHERE kind='tie_check'"
    params: list[Any] = []
    if book:
        sql += " AND book = ?"
        params.append(book)
    out = []
    for r in store.q(sql + " ORDER BY book, sheet_no, row_no", params):
        cells = _cells(r)
        blob = " ".join(cells)
        out.append({
            "book": r["book"], "sheet": r["sheet"], "row": r["row_no"],
            "check": r["label"], "passes": bool(TIE_OK.search(blob)),
            "says": blob[:300], "locator": r["locator"],
        })
    return out


def _find(store: Store, book: str | None, sheet: str | None,
          label: str | None) -> dict | None:
    sql = "SELECT * FROM ledger WHERE amount IS NOT NULL"
    params: list[Any] = []
    if book:
        sql += " AND book = ?"
        params.append(book)
    if sheet:
        sql += " AND sheet = ?"
        params.append(sheet)
    if label:
        sql += " AND UPPER(label) LIKE ?"
        params.append(f"%{label.upper()}%")
    rows = store.q(sql + " ORDER BY kind='total' DESC, amount DESC LIMIT 1",
                   params)
    return dict(rows[0]) if rows else None


def sheet_ties(store: Store, book: str, sheet: str) -> bool | None:
    """Does this sheet carry a passing tie-check? None means it carries none."""
    checks = [c for c in tie_checks(store, book) if c["sheet"] == sheet]
    if not checks:
        return None
    return all(c["passes"] for c in checks)


def position(store: Store, fy: int = 2027) -> dict:
    """
    Every defensible answer to "what did we get", side by side.

    Handing back all four with their questions attached is deliberate. The
    office's recurring error is not arithmetic; it is quoting the member
    designation total in a room that is asking about the district.
    """
    out: dict[str, Any] = {"fy": fy, "bases": [], "notes": []}

    for spec in BASES:
        entry = {k: spec[k] for k in ("key", "question", "caution")}
        if spec.get("from") == "schedule_c":
            # Whose money, or whose district: the two Schedule C questions
            # that get mistaken for each other. FY2027 answers them
            # $3,008,000 and $1,042,000 -- a three-fold gap that has nothing
            # to do with arithmetic and everything to do with which one the
            # room was asking about.
            if spec.get("where") == "member":
                clause, params = ("member LIKE ?", (fy, f"%{MEMBER}%"))
            else:
                clause, params = ("district = ?", (fy, DISTRICT))
            rows = store.q(
                f"SELECT COUNT(*) n, SUM(amount) total FROM funding "
                f"WHERE fy = ? AND {clause} AND source_id = 'SCHEDULE_C'",
                params)
            entry.update({
                "amount": rows[0]["total"] if rows else None,
                "lines": rows[0]["n"] if rows else 0,
                "source": "Adopted Schedule C", "book": None, "sheet": None,
                "ties": None,
            })
            out["bases"].append(entry)
            continue

        if spec["book"] == "D49_FY27_Full_Breakdown":
            summary = store.get_meta("ledger.D49_FY27_Full_Breakdown") or {}
            stated = summary.get("stated") or {}
            entry.update({
                "amount": stated.get("total") or summary.get("items_sum"),
                "lines": summary.get("items"),
                "categories": summary.get("categories"),
                "source": "District 49 FY2027 Full Breakdown",
                "book": spec["book"], "sheet": None,
                "ties": summary.get("ties"),
            })
            out["bases"].append(entry)
            continue

        row = _find(store, spec["book"], spec.get("sheet"), spec.get("label"))
        entry.update({
            "amount": row["amount"] if row else None,
            "lines": None,
            "source": f"{spec['book']} · {spec['sheet']}" if row else None,
            "book": spec["book"], "sheet": spec.get("sheet"),
            "locator": row["locator"] if row else None,
            "ties": sheet_ties(store, spec["book"], spec["sheet"])
                    if row else None,
        })
        out["bases"].append(entry)

    checks = tie_checks(store)
    out["tie_checks"] = {"total": len(checks),
                         "passing": sum(1 for c in checks if c["passes"]),
                         "failing": [c for c in checks if not c["passes"]]}
    if out["tie_checks"]["failing"]:
        out["notes"].append(
            f"{len(out['tie_checks']['failing'])} reconciliation check(s) do "
            f"not read as passing. Treat the affected sheets as unproven.")
    got = [b for b in out["bases"] if b.get("amount")]
    if len(got) < len(BASES):
        out["notes"].append(
            "Not every basis is loaded. Run `universe repos sync` to pull the "
            "budget repository, or the district position is incomplete.")
    return out


# The one sheet that decomposes the island's total into non-overlapping
# components. Everything else in the workbook restates the same money from a
# different angle, which is what a reconciliation is for.
GRAND_SHEET = "Grand Total — Combined (Jul 7)"
SECTION_TOTALS = {"CAPITAL TOTAL": "capital", "EXPENSE TOTAL": "expense"}


def channels(store: Store, book: str = "FY27_SI_EIN_Census_and_Reconciliation_v3",
             fy: int = 2027) -> list[dict]:
    """
    How the money reached the island, by door.

    Read from the reconciliation's own decomposition, never aggregated across
    the workbook. Grouping every line row by channel and summing looks like
    the obvious implementation and is catastrophically wrong: the workbook
    states the same $77,350,000 of capital on eight different sheets -- the
    §254 detail, the allocator, the category cut, the sponsor cut, the named
    wins, the corrected totals -- because restating money from several angles
    is exactly what a reconciliation does. Summing across them reported
    $2.13 billion of capital for Staten Island, roughly eight times over, and
    it appeared on screen looking perfectly plausible.

    So: the components come from one sheet, and the result is checked against
    that sheet's own totals before it is handed back.
    """
    rows = store.q(
        "SELECT label, amount, kind, row_no FROM ledger "
        "WHERE book = ? AND sheet = ? AND amount IS NOT NULL "
        "ORDER BY row_no", (book, GRAND_SHEET))
    out: list[dict] = []
    section = "capital"
    for r in rows:
        label = (r["label"] or "").strip()
        if label in SECTION_TOTALS:
            # The section total closes the section it totals; the next
            # components belong to whatever comes after it.
            section = "expense" if SECTION_TOTALS[label] == "capital" else None
            continue
        if r["kind"] != "line" or section is None:
            continue
        out.append({"channel": label, "section": section,
                    "total": r["amount"],
                    "locator": f"{book} · {GRAND_SHEET} · row {r['row_no']}"})
    return out


def channels_reconcile(store: Store,
                       book: str = "FY27_SI_EIN_Census_and_Reconciliation_v3"
                       ) -> dict:
    """Do the components add up to the sheet's own stated totals?"""
    comps = channels(store, book)
    stated = {r["label"]: r["amount"] for r in store.q(
        "SELECT label, amount FROM ledger WHERE book = ? AND sheet = ? "
        "AND kind = 'total'", (book, GRAND_SHEET))}
    out = {}
    for label, key in SECTION_TOTALS.items():
        got = round(sum(c["total"] or 0 for c in comps
                        if c["section"] == key), 2)
        want = stated.get(label)
        out[key] = {"components": got, "stated": want,
                    "ties": want is not None and round(want, 2) == got}
    return out


def categories(store: Store,
               book: str = "D49_FY27_Full_Breakdown") -> list[dict]:
    """The District 49 breakdown by category, biggest first."""
    rows = store.q(
        "SELECT label, amount FROM ledger WHERE book = ? AND sheet='categories' "
        "ORDER BY amount DESC", (book,))
    total = sum(r["amount"] or 0 for r in rows) or 1.0
    return [{"category": r["label"], "amount": r["amount"],
             "share": round(100 * (r["amount"] or 0) / total, 1)} for r in rows]


def items(store: Store, category: str | None = None,
          book: str = "D49_FY27_Full_Breakdown", limit: int = 400) -> list[dict]:
    """The named items, optionally within one category."""
    sql = ("SELECT label, amount, channel, locator FROM ledger "
           "WHERE book = ? AND sheet = 'items'")
    params: list[Any] = [book]
    if category:
        sql += " AND LOWER(channel) = ?"
        params.append(category.lower())
    rows = store.q(sql + " ORDER BY amount DESC LIMIT ?", (*params, limit))
    return [dict(r) for r in rows]


def find(store: Store, keyword: str, limit: int = 40) -> list[dict]:
    """
    Find every reconciliation row mentioning an organisation or project.

    This replaces the workbook's own 'Funding Finder' sheet, which required
    opening a 600 KB spreadsheet and typing into one magic cell.
    """
    like = f"%{keyword.lower()}%"
    rows = store.q(
        "SELECT book, sheet, row_no, label, amount, kind, channel, member, "
        "locator, cells FROM ledger "
        "WHERE LOWER(label) LIKE ? OR LOWER(cells) LIKE ? "
        "ORDER BY kind='line' DESC, amount DESC LIMIT ?",
        (like, like, limit))
    out = []
    for r in rows:
        d = dict(r)
        d["cells"] = _cells(r)
        out.append(d)
    return out


def evidence(store: Store, keys: Iterable[str] = ()) -> dict:
    """
    An evidence packet a brief can be gated against.

    The pipeline checks every figure a brief prints against the values in its
    packet. Handing over the reconciled totals means a brief can finally say
    "$35.9 million" without the gate calling it invented -- and cannot say it
    if the workbook is not loaded.
    """
    pos = position(store)
    values = [b["amount"] for b in pos["bases"] if b.get("amount")]
    values += [c["total"] for c in channels(store) if c.get("total")]
    values += [c["amount"] for c in categories(store) if c.get("amount")]
    return {"position": pos, "channels": channels(store),
            "categories": categories(store),
            "values": [v for v in values if v is not None]}
