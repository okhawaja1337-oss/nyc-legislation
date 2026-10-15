#!/usr/bin/env python3
"""
Breakdowns: any search, cut by any dimension.

A search that returns 9,634 budget lines has answered nothing. The question a
budget director actually asks is a cut: *this* money, by member; by initiative;
by agency; by committee; by fiscal year; by whether it is confirmed or still
pending a modification. The answer is only useful if it also says how much,
how many, and where the number came from.

Three rules govern everything here:

  * **A total is a claim.** Every total carries its line count and its sources.
    A number that cannot be traced back to a book is not quotable, and this
    module marks it rather than leaving a staffer to find out in a hearing.
  * **Tiers are not decoration.** Confirmed money, money pending a budget
    modification, and reversed money are reported separately and never summed
    into one headline figure.
  * **Cuts compose.** Filter by member, break by initiative, then break that
    initiative by agency. Each step narrows the same query rather than starting
    a new one, so the arithmetic stays consistent all the way down.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from ..core.store import Store
from . import search as S

# The dimensions a user may break by: label -> (column, what it means)
DIMENSIONS: dict[str, tuple[str, str]] = {
    "member": ("sponsor_key", "Councilmember — the sponsor of a bill or the funder of a line"),
    "cm": ("sponsor_key", "Councilmember (alias of member)"),
    "sponsor": ("sponsor_key", "Councilmember — the sponsor of a bill or the funder of a line"),
    "initiative": ("initiative", "Funding initiative or pot — the programme the money sits in"),
    "pot": ("initiative", "Funding initiative or pot"),
    "tier": ("tier", "Whether the money is confirmed, pending a budget modification, or reversed"),
    "committee": ("committee", "Council committee of referral"),
    "agency": ("agency", "Administering city agency"),
    "pillar": ("pillar", "Office priority the item falls under"),
    "fy": ("fy", "Fiscal year"),
    "year": ("year", "Calendar or session year"),
    "status": ("status", "Status in its own source system"),
    "kind": ("kind", "Record type"),
    "channel": ("channel", "Expense or capital"),
    "district": ("district", "Council district"),
    "org": ("org", "Recipient organisation"),
    "source": ("source_id", "Source book the record came from"),
}

# Dimensions whose totals are money. Everything else counts records.
# Where a bucket key is a normalised code, the column holding its readable form.
DISPLAY_OF = {"sponsor_key": "sponsor"}

# Kinds whose `amount` is money. A sorted tuple, not a set: these bind as
# SQL parameters and the order has to match the statement every time.
MONEY_KINDS = ("funding",)
TOP_DEFAULT = 25
SCAN_CAP = 120_000          # rows a single breakdown will read before sampling


def dimensions() -> list[dict]:
    """What can be broken by, for a UI to offer without guessing."""
    seen, out = set(), []
    for name, (column, note) in DIMENSIONS.items():
        if column in seen:
            continue
        seen.add(column)
        out.append({"name": name, "column": column, "means": note})
    return out


def _resolve(by: str) -> str:
    key = (by or "").strip().lower()
    if key in DIMENSIONS:
        return DIMENSIONS[key][0]
    if key in {c for c, _ in DIMENSIONS.values()}:
        return key
    raise ValueError(f"cannot break by {by!r}; try one of "
                     f"{sorted({n for n in DIMENSIONS})}")


def _base(q: str, filters: dict | None) -> tuple[str, list]:
    """The WHERE clause and parameters for a query, shared with search()."""
    query = S.Query(q or "")
    where, bound = query.where()
    clauses = [where] if where else []
    for key, value in (filters or {}).items():
        column = S.FIELDS.get(key, key)
        if value in (None, "", []):
            continue
        if isinstance(value, (list, tuple)):
            clauses.append(f"{column} IN ({','.join('?' * len(value))})")
            bound += list(value)
        elif column in S.EXACT or column in S.NUMERIC:
            clauses.append(f"{column} = ?")
            bound.append(value)
        else:
            clauses.append(f"{column} LIKE ?")
            bound.append(f"%{value}%")
    fts = query.fts()
    if fts:
        clauses.append(
            "key IN (SELECT key FROM workspace_records WHERE rowid IN "
            "(SELECT rowid FROM workspace_fts WHERE workspace_fts MATCH ?))")
        bound.append(fts)
    return (" AND ".join(clauses) if clauses else "1=1"), bound


def break_by(store: Store, by: str, q: str = "", filters: dict | None = None,
             top: int = TOP_DEFAULT, include_empty: bool = False,
             order: str = "amount") -> dict:
    """
    One cut of one query.

    Returns a row per bucket with its record count, its money, and -- for
    funding -- confirmed, pending and reversed kept apart, because publishing
    a total that quietly includes money a budget modification has not passed
    yet is the mistake this office cannot afford twice.

    The arithmetic runs in SQL. Pulling 50,000 rows into Python to add them up
    is the difference between a breakdown a staffer re-runs six ways during a
    hearing and one they run once and stop using.
    """
    column = _resolve(by)
    where, bound = _base(q, filters)

    # Tier decides which total a dollar belongs in. Where a line predates the
    # tier vocabulary, its status carries the same signal, so fall back to it
    # rather than silently counting reversed money as confirmed.
    money = (f"CASE WHEN kind IN ({','.join('?' * len(MONEY_KINDS))}) "
             f"THEN COALESCE(amount, 0) ELSE 0 END")
    bucket_sql = (f"CASE WHEN {column} IS NULL OR {column}='' THEN ? ELSE {column} END")
    grade = ("CASE WHEN UPPER(COALESCE(tier,''))='REVERSED' "
             "       OR LOWER(COALESCE(status,'')) LIKE '%tr-cut%' THEN 'reversed' "
             "     WHEN UPPER(COALESCE(tier,'')) LIKE '%PENDING%' "
             "       OR LOWER(COALESCE(status,'')) LIKE '%pending%' THEN 'pending' "
             "     ELSE 'confirmed' END")

    sql = (f"SELECT {bucket_sql} AS bucket, {grade} AS grade, "
           f"COUNT(*) AS records, SUM({money}) AS amount, "
           f"SUM(CASE WHEN kind IN ({','.join('?' * len(MONEY_KINDS))}) "
           f"         AND amount IS NOT NULL THEN 1 ELSE 0 END) AS money_records, "
           f"GROUP_CONCAT(DISTINCT kind) AS kinds, "
           f"GROUP_CONCAT(DISTINCT source_id) AS sources, "
           f"MIN(NULLIF({DISPLAY_OF.get(column, column)}, '')) AS sample "
           f"FROM workspace_records WHERE {where} GROUP BY bucket, grade")
    # Parameters bind in the order they appear in the statement: the bucket
    # label, then the two money CASEs, then the WHERE clause.
    params = ["(not stated)"] + list(MONEY_KINDS) + list(MONEY_KINDS) + bound

    buckets: dict[str, dict] = {}
    for row in store.q(sql, params):
        label = str(row["bucket"])
        if label == "(not stated)" and include_empty:
            label = ""
        entry = buckets.setdefault(label, {
            "value": label, "display": label, "records": 0, "amount": 0.0,
            "confirmed": 0.0, "pending": 0.0, "reversed": 0.0,
            "money_records": 0, "kinds": {}, "sources": set()})
        entry["records"] += row["records"] or 0
        entry["money_records"] += row["money_records"] or 0
        entry["amount"] += row["amount"] or 0.0
        entry[row["grade"]] += row["amount"] or 0.0
        for k in (row["kinds"] or "").split(","):
            if k:
                # Count records, not grade-groups. GROUP_CONCAT gives the
                # distinct kinds in this group; the group's size is `records`.
                entry["kinds"][k] = entry["kinds"].get(k, 0) + (row["records"] or 0)
        entry["sources"].update(s for s in (row["sources"] or "").split(",") if s)
        if row["sample"]:
            entry["display"] = str(row["sample"])

    out = []
    for entry in buckets.values():
        entry["sources"] = sorted(entry["sources"])
        for field in ("amount", "confirmed", "pending", "reversed"):
            entry[field] = round(entry[field], 2)
        out.append(entry)

    keyfn = {"amount": lambda r: (-r["amount"], -r["records"]),
             "records": lambda r: (-r["records"], -r["amount"]),
             "name": lambda r: r["value"].lower()}.get(order, lambda r: -r["amount"])
    out.sort(key=keyfn)
    shown, rest = out[:top], out[top:]

    return {
        "by": by, "column": column, "query": q, "filters": filters or {},
        "buckets": shown,
        "other": {"buckets": len(rest),
                  "records": sum(r["records"] for r in rest),
                  "amount": round(sum(r["amount"] for r in rest), 2)} if rest else None,
        "totals": {
            "buckets": len(out),
            "records": sum(r["records"] for r in out),
            "amount": round(sum(r["amount"] for r in out), 2),
            "confirmed": round(sum(r["confirmed"] for r in out), 2),
            "pending": round(sum(r["pending"] for r in out), 2),
            "reversed": round(sum(r["reversed"] for r in out), 2),
        },
        "sources": sorted({s for r in out for s in r["sources"]}),
        "sampled": False,
        "caveat": None,
    }


def cross(store: Store, rows_by: str, cols_by: str, q: str = "",
          filters: dict | None = None, top_rows: int = 15,
          top_cols: int = 10, measure: str = "amount") -> dict:
    """
    Two dimensions at once -- member by initiative, agency by fiscal year.

    This is the table a budget hearing actually runs on: who got what, out of
    which pot. One scan builds the whole grid.
    """
    row_col, col_col = _resolve(rows_by), _resolve(cols_by)
    where, bound = _base(q, filters)
    rows = store.q(
        f"SELECT {row_col} AS r, {col_col} AS c, kind, amount "
        f"FROM workspace_records WHERE {where} LIMIT {SCAN_CAP + 1}", bound)
    sampled = len(rows) > SCAN_CAP
    rows = rows[:SCAN_CAP]

    grid: dict[str, dict[str, float]] = {}
    row_totals: dict[str, float] = {}
    col_totals: dict[str, float] = {}
    for row in rows:
        r = str(row["r"] if row["r"] not in (None, "") else "(not stated)")
        c = str(row["c"] if row["c"] not in (None, "") else "(not stated)")
        value = (row["amount"] or 0.0) if measure == "amount" and row["kind"] in MONEY_KINDS else 0.0
        if measure == "records":
            value = 1.0
        grid.setdefault(r, {})[c] = grid.setdefault(r, {}).get(c, 0.0) + value
        row_totals[r] = row_totals.get(r, 0.0) + value
        col_totals[c] = col_totals.get(c, 0.0) + value

    keep_rows = [r for r, _ in sorted(row_totals.items(), key=lambda kv: -kv[1])][:top_rows]
    keep_cols = [c for c, _ in sorted(col_totals.items(), key=lambda kv: -kv[1])][:top_cols]
    return {
        "rows_by": rows_by, "cols_by": cols_by, "measure": measure,
        "columns": keep_cols,
        "rows": [{"value": r,
                  "cells": [round(grid.get(r, {}).get(c, 0.0), 2) for c in keep_cols],
                  "total": round(row_totals[r], 2)} for r in keep_rows],
        "column_totals": [round(col_totals[c], 2) for c in keep_cols],
        "grand_total": round(sum(row_totals.values()), 2),
        "sampled": sampled,
    }


def profile(store: Store, q: str = "", filters: dict | None = None,
            dims: Iterable[str] = ("member", "initiative", "committee", "agency",
                                   "fy", "pillar"),
            top: int = 8) -> dict:
    """
    Every standard cut of one query at once -- the answer to "break this down".

    One query in, a full picture out: who, which programme, which committee,
    which agency, which year, which priority.
    """
    cuts = {}
    for name in dims:
        try:
            cuts[name] = break_by(store, name, q, filters, top=top)
        except ValueError as exc:
            cuts[name] = {"error": str(exc)}
    first = next((c for c in cuts.values() if "totals" in c), None)
    return {
        "query": q, "filters": filters or {},
        "matched": first["totals"]["records"] if first else 0,
        "money": first["totals"] if first else {},
        "cuts": cuts,
        "sources": sorted({s for c in cuts.values() for s in c.get("sources", [])}),
    }


# ----------------------------------------------------------------- render ----
def _money(n: float) -> str:
    return f"${n:,.0f}"


def to_markdown(cut: dict) -> str:
    """A breakdown as a table a staffer can paste into a memo."""
    if "error" in cut:
        return f"*{cut['error']}*"
    head = DIMENSIONS.get(cut["by"], (cut["column"], cut["column"]))[1]
    out = [f"**By {cut['by']}** — {head}",
           f"*{cut['totals']['records']:,} records across "
           f"{cut['totals']['buckets']:,} buckets; "
           f"{_money(cut['totals']['amount'])} in tracked awards*", "",
           "| " + cut["by"].title() + " | Records | Amount | Confirmed | Pending | Reversed |",
           "|---|---:|---:|---:|---:|---:|"]
    for bucket in cut["buckets"]:
        out.append(f"| {bucket.get('display') or bucket['value']} | {bucket['records']:,} | "
                   f"{_money(bucket['amount'])} | {_money(bucket['confirmed'])} | "
                   f"{_money(bucket['pending'])} | {_money(bucket['reversed'])} |")
    if cut.get("other"):
        out.append(f"| *{cut['other']['buckets']} more* | "
                   f"{cut['other']['records']:,} | {_money(cut['other']['amount'])} | | | |")
    out += ["",
            "*Confirmed is money with nothing outstanding against it. Pending is "
            "money that needs a budget modification before it can be announced. "
            "Reversed is the signed value of lines a later resolution undid -- "
            "run `universe funding reconcile` to pair a reversal with the award "
            "it cancels before quoting a net figure.*",
            "", f"Sources: {', '.join(cut['sources']) or '—'}"]
    if cut.get("caveat"):
        out += ["", f"> {cut['caveat']}"]
    return "\n".join(out)
