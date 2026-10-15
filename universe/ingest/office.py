#!/usr/bin/env python3
"""
The office's own working papers.

Schedule C tells you what the Council adopted. It does not tell you what
District 49 actually got, because a dollar reaches the North Shore through
five different doors: the member's own discretionary pot, the Speaker's
initiatives, citywide initiatives that happen to land here, the Staten Island
delegation pot, and Section 254 capital. Schedule C files them under whoever
signed, not under where the money landed.

So the office did the reconciliation by hand, across 56 worksheets, and tied
it out against the printed books page by page. That work produced the only
defensible answer to "what did we get this year" -- $35,903,925 in FY27 wins
against a $95,282,347 Staten Island total -- and until now the system could
not see a single row of it. It quoted $3,008,000, the Schedule C member layer,
and called that the district's position. It was not wrong; it was a twelfth of
the truth.

This module reads those workbooks with nothing but the standard library,
because the system has to run on a laptop with nothing installed. An .xlsx is
a zip of XML and so is a .docx; neither needs a dependency, only care.

Every row keeps its sheet, its row number and its full cell contents, so any
figure the system later prints can be walked back to the cell it came from.
The derived columns -- amount, member, channel -- are conveniences layered on
top and are recorded with the header they were read from. Nothing is
collapsed, because a reconciliation you cannot re-check is just an assertion.
"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Iterator
from xml.etree import ElementTree as ET

from ..core.store import Store

XL = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# Columns whose header says the number underneath it is money. Checked in
# order, so a sheet with both "FY 2027" and "Amount" prefers the explicit one.
MONEY_HEADERS = (
    "amount", "corrected", "adopted", "total $", "$", "award", "designated",
    "fy 2027", "fy2027", "fy27", "value",
)
SI_MEMBERS = {"hanks": "Kamillah Hanks", "carr": "David M. Carr",
              "morano": "Frank Morano"}
# Where a dollar came through. The office argues about these labels in every
# reconciliation meeting, so they are written down rather than inferred.
CHANNELS = (
    ("capital", ("capital", "§254", "sec 254", "section 254", "sec i")),
    ("speaker", ("speaker",)),
    ("citywide", ("citywide", "city-wide")),
    ("delegation", ("delegation", "si deleg")),
    ("expense", ("mdi", "expense", "designation", "schedule c")),
)
NUMERIC = re.compile(r"^-?\$?\s*[\d,]+(\.\d+)?%?$")
MONEY_IN_TEXT = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)")


# ------------------------------------------------------------ xlsx ----
def _col(ref: str) -> int:
    """'AB7' -> 27. Sheets skip empty cells, so the letter is the position."""
    letters = re.match(r"([A-Z]+)", ref or "A")
    n = 0
    for ch in (letters.group(1) if letters else "A"):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    # A string can be split across several runs (a bolded word mid-cell), so
    # every <t> under the item has to be joined or the text arrives shredded.
    return ["".join(t.text or "" for t in si.iter(XL + "t")) for si in root]


def _sheet_parts(z: zipfile.ZipFile) -> list[tuple[str, str]]:
    book = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    target = {r.get("Id"): r.get("Target") for r in rels}
    out = []
    for s in book.iter(XL + "sheet"):
        part = target.get(s.get(REL + "id"), "") or ""
        part = part[1:] if part.startswith("/xl/") else part
        out.append((s.get("name") or "", part if part.startswith("xl/")
                    else "xl/" + part))
    return out


def _sheet_rows(z: zipfile.ZipFile, part: str,
                sst: list[str]) -> Iterator[list[str]]:
    root = ET.fromstring(z.read(part))
    for r in root.iter(XL + "row"):
        cells: dict[int, str] = {}
        for c in r.iter(XL + "c"):
            i = _col(c.get("r") or "A")
            kind = c.get("t")
            v = c.find(XL + "v")
            if kind == "s":
                val = sst[int(v.text)] if v is not None and v.text else ""
            elif kind == "inlineStr":
                val = "".join(t.text or "" for t in c.iter(XL + "t"))
            else:
                val = (v.text or "") if v is not None else ""
            if val not in ("", None):
                cells[i] = val
        if cells:
            yield [cells.get(i, "") for i in range(max(cells) + 1)]


def read_xlsx(path: Path | str) -> list[dict]:
    """Every sheet as {name, headers, rows}, headers detected per sheet."""
    z = zipfile.ZipFile(str(path))
    sst = _shared_strings(z)
    out = []
    for name, part in _sheet_parts(z):
        try:
            rows = list(_sheet_rows(z, part, sst))
        except KeyError:
            continue
        head_at = _header_row(rows)
        out.append({
            "name": name,
            "header_row": head_at,
            "headers": [str(c).strip() for c in rows[head_at]] if head_at >= 0 else [],
            "preamble": [r for r in rows[:max(head_at, 0)]],
            "rows": rows[head_at + 1:] if head_at >= 0 else rows,
            "all": rows,
        })
    return out


def _header_row(rows: list[list[str]]) -> int:
    """
    Which row is the header.

    These workbooks open with one to three title rows -- a banner, a basis
    note, sometimes a stray label -- before the real column names. Guessing
    row 0 puts "FY 2027 STATEN ISLAND — COMBINED GRAND TOTAL" in as a column
    name and every row underneath lands in the wrong field. So: the header is
    the first wide, wholly non-numeric row that has data under it.
    """
    for i, row in enumerate(rows[:12]):
        cells = [str(c).strip() for c in row if str(c).strip()]
        if len(cells) < 3 or any(_is_number(c) for c in cells):
            continue
        for later in rows[i + 1:i + 8]:
            if len(later) >= len(row) - 1 and any(_is_number(c) for c in later):
                return i
    return 0 if rows else -1


def _is_number(cell: Any) -> bool:
    s = str(cell).strip()
    return bool(s) and bool(NUMERIC.match(s))


def _number(cell: Any) -> float | None:
    s = str(cell).strip().replace("$", "").replace(",", "").replace("%", "")
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------- derivation ----
def _amount_column(headers: list[str]) -> int | None:
    lowered = [h.lower() for h in headers]
    for want in MONEY_HEADERS:
        for i, h in enumerate(lowered):
            if want in h:
                return i
    return None


def _amount(row: list[str], headers: list[str],
            preferred: int | None) -> tuple[float | None, str]:
    """The row's money, and the header it was read from."""
    if preferred is not None and preferred < len(row):
        v = _number(row[preferred])
        if v is not None:
            return v, (headers[preferred] if preferred < len(headers) else "")
    for i, cell in enumerate(row):
        v = _number(cell)
        # A page number, a line count and a fiscal year are all numbers. Money
        # in these books is never under $100, and a bare year is not a total.
        if v is not None and abs(v) >= 100 and not (1990 <= v <= 2100):
            return v, (headers[i] if i < len(headers) else "")
    return None, ""


def _member(blob: str) -> str | None:
    low = blob.lower()
    for key, full in SI_MEMBERS.items():
        if re.search(rf"\b{key}\b", low):
            return full
    return None


def _channel(sheet: str, blob: str) -> str | None:
    hay = (sheet + " " + blob).lower()
    for label, needles in CHANNELS:
        if any(n in hay for n in needles):
            return label
    return None


def _kind(label: str, row: list[str], amount: float | None) -> str:
    """
    What sort of row this is.

    A tie-check is the most important row in a reconciliation and the easiest
    to lose: it is the line that proves the sheet reproduces the printed book.
    It gets its own kind so the system can report "this figure sits on a sheet
    that ties" rather than quoting a number with no standing.
    """
    up = label.upper()
    joined = " ".join(str(c) for c in row).upper()
    if "TIE CHECK" in up or "TIES ✓" in joined or "TIE-OUT" in up:
        return "tie_check"
    if "GRAND TOTAL" in up or re.search(r"\bTOTAL\b", up):
        return "total"
    if amount is None:
        filled = [c for c in row if str(c).strip()]
        return "section" if len(filled) <= 1 else "note"
    return "line"


def _row_id(book: str, sheet: str, n: int) -> str:
    raw = f"{book}|{sheet}|{n}"
    return "LG" + hashlib.sha1(raw.encode()).hexdigest()[:16]


# ------------------------------------------------------------ ingest ----
def ingest_workbook(store: Store, path: Path | str,
                    source_id: str = "OFFICE-WB") -> dict:
    """Load one reconciliation workbook, sheet by sheet, row by row."""
    path = Path(path)
    book = path.stem
    sheets = read_xlsx(path)
    rows_out: list[dict] = []
    index: list[tuple] = []
    per_sheet: dict[str, dict] = {}

    for pos, sheet in enumerate(sheets):
        name = sheet["name"]
        headers = sheet["headers"]
        prefer = _amount_column(headers)
        counts = {"rows": 0, "lines": 0, "totals": 0, "ties": 0, "sum": 0.0}
        for n, row in enumerate(sheet["rows"], start=sheet["header_row"] + 2):
            cells = [str(c).strip() for c in row]
            if not any(cells):
                continue
            label = next((c for c in cells if c and not _is_number(c)), "")
            amount, amount_col = _amount(cells, headers, prefer)
            blob = " ".join(cells)
            kind = _kind(label, cells, amount)
            rid = _row_id(book, name, n)
            rows_out.append({
                "row_id": rid, "book": book, "sheet": name, "sheet_no": pos,
                "row_no": n, "label": label[:400], "amount": amount,
                "amount_col": amount_col, "kind": kind,
                "member": _member(blob), "channel": _channel(name, blob),
                "fy": _fiscal_year(name, blob),
                "cells": json.dumps(cells, ensure_ascii=False),
                "headers": json.dumps(headers, ensure_ascii=False),
                "source_id": source_id,
                "locator": f"{book} · {name} · row {n}",
            })
            counts["rows"] += 1
            if kind == "line":
                counts["lines"] += 1
                counts["sum"] += amount or 0.0
            elif kind == "total":
                counts["totals"] += 1
            elif kind == "tie_check":
                counts["ties"] += 1
            index.append(("ledger", rid, f"{name} — {label[:120]}",
                          blob[:2000], f"{book} {name} {kind}"))
        per_sheet[name] = counts

    _write(store, rows_out, book)
    store.index_many(index)
    summary = {"book": book, "sheets": len(sheets), "rows": len(rows_out),
               "by_sheet": per_sheet}
    store.set_meta(f"ledger.{book}", summary)
    store.journal("ingest.workbook", {"book": book, "rows": len(rows_out)})
    return summary


def _fiscal_year(sheet: str, blob: str) -> int | None:
    m = re.search(r"\bFY\s?(20)?(\d{2})\b", sheet, re.I) or \
        re.search(r"\bFY\s?(20)?(\d{2})\b", blob[:200], re.I)
    if m:
        return 2000 + int(m.group(2))
    m = re.search(r"\b(20[2-3]\d)\b", sheet)
    return int(m.group(1)) if m else None


def _write(store: Store, rows: list[dict], book: str) -> None:
    cols = ("row_id", "book", "sheet", "sheet_no", "row_no", "label", "amount",
            "amount_col", "kind", "member", "channel", "fy", "cells",
            "headers", "source_id", "locator")
    with store.tx() as c:
        # Re-ingesting a workbook replaces it wholesale. A partial overwrite
        # would leave rows from a sheet the newer version deleted, and a
        # reconciliation with orphan rows ties to nothing.
        c.execute("DELETE FROM ledger WHERE book = ?", (book,))
        c.executemany(
            f"INSERT INTO ledger ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})",
            [tuple(r.get(k) for k in cols) for r in rows])


# -------------------------------------------------------------- docx ----
def read_docx(path: Path | str) -> dict:
    """Paragraphs in order and every table, flattened to text."""
    z = zipfile.ZipFile(str(path))
    body = ET.fromstring(z.read("word/document.xml")).find(W + "body")
    paragraphs: list[str] = []
    tables: list[list[list[str]]] = []
    if body is None:
        return {"paragraphs": [], "tables": []}
    for el in body:
        if el.tag == W + "p":
            text = "".join(t.text or "" for t in el.iter(W + "t")).strip()
            if text:
                paragraphs.append(text)
        elif el.tag == W + "tbl":
            table = []
            for tr in el.findall(W + "tr"):
                table.append([
                    "".join(t.text or "" for t in tc.iter(W + "t")).strip()
                    for tc in tr.findall(W + "tc")])
            if table:
                tables.append(table)
    return {"paragraphs": paragraphs, "tables": tables}


# "$7,000,000  North Shore Action Plan — Tompkinsville — description"
ITEM = re.compile(r"^\$\s?([\d,]+(?:\.\d{2})?)\s+(.+)$")
# "  CULTURALS   $14,115,000  ·  21 items"
CATEGORY = re.compile(r"^\s*([A-Z][A-Z &/'\-]{2,40})\s+\$\s?([\d,]+)\s*·\s*(\d+)\s*items?",
                      re.I)
# "$39,627,090  ·  245 items  ·  17 categories" -- the document's own headline.
#
# This one has to be recognised before ITEM, and the reason is worth keeping.
# It begins with a dollar figure and so matched the item pattern perfectly,
# which filed the grand total as a 246th line item. The parse then reported
# $79,254,180 for District 49: exactly twice the truth, arrived at honestly,
# and the kind of figure that survives a read-through because it is not
# obviously absurd. A total is never a line.
HEADLINE = re.compile(
    r"^\$\s?([\d,]+(?:\.\d{2})?)\s*·\s*([\d,]+)\s*items?\s*·\s*(\d+)\s*categor",
    re.I)


def ingest_breakdown(store: Store, path: Path | str,
                     source_id: str = "OFFICE-D49") -> dict:
    """
    Load the District 49 full breakdown: every item, by category.

    This is the document the Councilmember's own total comes from. It is
    ordered prose, not a table, so the parse is line-shaped: a category
    heading opens a section and every "$amount  Name — note" line under it
    belongs to that category until the next heading.
    """
    path = Path(path)
    book = path.stem
    doc = read_docx(path)
    rows: list[dict] = []
    index: list[tuple] = []
    category = ""
    n = 0
    stated: dict[str, Any] = {}

    for para in doc["paragraphs"]:
        head = CATEGORY.match(para)
        if head:
            category = head.group(1).strip().title()
            n += 1
            rid = _row_id(book, "categories", n)
            rows.append(_doc_row(rid, book, "categories", n, category,
                                 _number(head.group(2)), "total", category,
                                 para, source_id))
            index.append(("ledger", rid, f"D49 category — {category}",
                          para, f"{book} category"))
            continue
        top = HEADLINE.match(para)
        if top:
            stated = {"total": _number(top.group(1)),
                      "items": int(top.group(2).replace(",", "")),
                      "categories": int(top.group(3)),
                      "text": para}
            continue
        item = ITEM.match(para)
        if item:
            n += 1
            rid = _row_id(book, "items", n)
            name = item.group(2).strip()
            rows.append(_doc_row(rid, book, "items", n, name,
                                 _number(item.group(1)), "line", category,
                                 para, source_id))
            index.append(("ledger", rid, name[:120], para,
                          f"{book} {category} D49 Hanks"))
            continue

    for t, table in enumerate(doc["tables"]):
        headers = table[0] if table else []
        for r, tr in enumerate(table[1:], start=2):
            if not any(tr):
                continue
            n += 1
            rid = _row_id(book, f"table{t}", r)
            amount, _ = _amount(tr, headers, _amount_column(headers))
            label = tr[0] if tr else ""
            rows.append(_doc_row(rid, book, f"summary table {t + 1}", r, label,
                                 amount, "total" if amount else "note",
                                 label, " | ".join(tr), source_id,
                                 headers=headers))
            index.append(("ledger", rid, f"D49 summary — {label}",
                          " | ".join(tr), f"{book} summary"))

    _write(store, rows, book)
    store.index_many(index)
    lines = [r for r in rows if r["kind"] == "line"]
    parsed_sum = round(sum(r["amount"] or 0 for r in lines), 2)
    categories = len({r["label"] for r in rows
                      if r["sheet"] == "categories"})
    # The document states its own totals. Checking the parse against them is
    # the whole reason those totals are worth reading in: a silent parse is a
    # parse nobody can trust with the Councilmember's headline number.
    summary = {"book": book, "rows": len(rows), "items": len(lines),
               "items_sum": parsed_sum, "categories": categories,
               "stated": stated,
               "ties": bool(stated) and (
                   round(stated.get("total") or -1, 2) == parsed_sum
                   and stated.get("items") == len(lines)
                   and stated.get("categories") == categories)}
    store.set_meta(f"ledger.{book}", summary)
    store.journal("ingest.breakdown", {"book": book, "rows": len(rows)})
    return summary


def _int_in(text: str, word: str) -> int | None:
    m = re.search(rf"([\d,]+)\s*{word}", text, re.I)
    return int(m.group(1).replace(",", "")) if m else None


def _doc_row(rid, book, sheet, n, label, amount, kind, category, raw,
             source_id, headers: list[str] | None = None) -> dict:
    return {
        "row_id": rid, "book": book, "sheet": sheet, "sheet_no": 0,
        "row_no": n, "label": str(label)[:400], "amount": amount,
        "amount_col": "Amount", "kind": kind,
        "member": _member(raw) or "Kamillah Hanks",
        "channel": category or None, "fy": _fiscal_year(book, raw),
        "cells": json.dumps([raw], ensure_ascii=False),
        "headers": json.dumps(headers or [], ensure_ascii=False),
        "source_id": source_id, "locator": f"{book} · {sheet} · {n}",
    }


# ----------------------------------------------------------- sources ----
SOURCE_LINK = re.compile(r"\*\*(.+?):\*\*\s*(https?://\S+)")


def ingest_sources_md(store: Store, path: Path | str,
                      prefix: str = "REPO") -> dict:
    """
    Register the source directory that ships with the budget repository.

    Every citation the system prints has to resolve to something a staffer can
    open. SOURCES.md is the office's own list of where each figure comes from,
    so it belongs in the citation registry rather than in a file nobody reads.
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    found = []
    section = ""
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].split("—")[0].strip()
        for name, url in SOURCE_LINK.findall(line):
            found.append((name.strip(), url.rstrip(").,"), section))
    rows = []
    for i, (name, url, section) in enumerate(found, start=1):
        rows.append((f"{prefix}{i:02d}", name, section or "NYC fiscal sources",
                     url, None, section, "OFFICIAL_DERIVED", None,
                     f"Listed in SOURCES.md under '{section}'."))
    with store.tx() as c:
        c.executemany(
            "INSERT OR REPLACE INTO sources (source_id, name, publisher, url, "
            "accessed, coverage, tier, locator, notes) VALUES (?,?,?,?,?,?,?,?,?)",
            rows)
    store.journal("ingest.sources_md", {"registered": len(rows)})
    return {"registered": len(rows), "file": str(path)}


# --------------------------------------------------------------- all ----
def ingest_office_docs(store: Store, repo_dir: Path | str) -> dict:
    """
    Load every working paper in the budget repository that is not raw data.

    Only the newest version of the reconciliation is loaded. The repository
    keeps v1 alongside v3 for audit, but loading both puts two different
    answers to the same question into one lake, and the office would then have
    to guess which one the system was quoting.
    """
    repo_dir = Path(repo_dir)
    out: dict[str, Any] = {}
    books = sorted(repo_dir.glob("**/*Reconciliation*.xlsx"))
    if books:
        out["reconciliation"] = ingest_workbook(store, books[-1])
    for docx in sorted(repo_dir.glob("**/*Full_Breakdown*.docx")):
        out["breakdown"] = ingest_breakdown(store, docx)
    smd = repo_dir / "SOURCES.md"
    if smd.exists():
        out["sources"] = ingest_sources_md(store, smd)
    return out
