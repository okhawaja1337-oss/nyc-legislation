#!/usr/bin/env python3
"""
Change detection.

The budget and the legislative calendar do not announce themselves. A bill
moves out of committee, a Transparency Resolution reverses a $100,000 award,
a hearing gets rescheduled three days out -- and the office finds out because
somebody happened to reload a page. That is the failure this module exists to
end.

The mechanism is deliberately dull. Every scan takes a fingerprint of the
fields that matter on every watched record, compares it to the fingerprint
from last time, and writes down exactly what moved. No model is involved: a
diff is arithmetic, and arithmetic does not hallucinate a reversal.

What makes it useful rather than noisy is triage. A typo fixed in an
organisation's name is not the same event as a discretionary award flipping to
REVERSED, and a system that pages the office for both gets muted within a
week. Every change is scored, and the scoring rules are written down here in
the open where they can be argued with.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from ..core.store import Store

WATCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS ws_watch_state (
  key TEXT PRIMARY KEY,
  kind TEXT, entity_id TEXT, label TEXT,
  fingerprint TEXT, snapshot TEXT,
  first_seen TEXT, last_seen TEXT
);
CREATE INDEX IF NOT EXISTS ix_watch_kind ON ws_watch_state(kind);

CREATE TABLE IF NOT EXISTS ws_changes (
  id TEXT PRIMARY KEY,
  at TEXT, kind TEXT, entity_id TEXT, label TEXT,
  change TEXT,               -- appeared | changed | disappeared
  field TEXT, before TEXT, after TEXT,
  delta REAL,                -- signed dollars, when the field is money
  severity TEXT,             -- high | medium | low
  why TEXT,                  -- the rule that set the severity, in words
  url TEXT, source_id TEXT,
  acknowledged INTEGER DEFAULT 0,
  acknowledged_by TEXT, acknowledged_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_changes_at ON ws_changes(at DESC);
CREATE INDEX IF NOT EXISTS ix_changes_sev ON ws_changes(severity, acknowledged);
CREATE INDEX IF NOT EXISTS ix_changes_ent ON ws_changes(kind, entity_id);
"""

MONEY_HIGH = 25_000.0      # a change at or above this is worth a person's attention
MONEY_MED = 5_000.0

# Field renames for readable alerts. A staffer should not have to know that
# `tier` is the office's word for whether money is real yet.
FIELD_LABELS = {
    "tier": "funding tier", "amount": "award amount", "status": "status",
    "committee": "committee", "enacted": "enacted", "local_law": "local law",
    "n_sponsors": "sponsor count", "start": "start time", "end": "end time",
    "location": "location", "reso": "transparency resolution", "org": "recipient",
    "summary": "title", "pot": "funding pot", "agency": "agency",
}


def init(store: Store) -> None:
    store.conn.executescript(WATCH_SCHEMA)
    store.conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fingerprint(fields: dict) -> str:
    canonical = json.dumps({k: fields[k] for k in sorted(fields)},
                           sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


# ------------------------------------------------------------- collectors ----
def watch_matters(store: Store, session_only: bool = True) -> list[dict]:
    sql = ("SELECT matter_id, file, name, status, committee, enacted, local_law, "
           "n_sponsors, year, source_id FROM matters")
    params: list = []
    if session_only:
        from ..intel.legislation import current_session
        latest = current_session(store)
        if latest:
            sql += " WHERE session=?"
            params.append(latest)
    rows = []
    for r in store.q(sql, params):
        rows.append({
            "key": f"matter:{r['matter_id']}",
            "kind": "matter", "entity_id": str(r["matter_id"]),
            "label": f"{r['file'] or r['matter_id']} — {(r['name'] or '')[:90]}",
            "url": f"https://legistar.council.nyc.gov/LegislationDetail.aspx?ID={r['matter_id']}",
            "source_id": r["source_id"] or "LEGISTAR",
            "fields": {"status": r["status"], "committee": r["committee"],
                       "enacted": r["enacted"], "local_law": r["local_law"],
                       "n_sponsors": r["n_sponsors"]},
        })
    return rows


def watch_funding(store: Store, fy: int | None = None) -> list[dict]:
    """
    Every discretionary line, including the Transparency Resolution ledger.

    `tier` is in the fingerprint on purpose. A line moving to REVERSED is the
    single most consequential change in this dataset -- money the office has
    already told a nonprofit it was getting -- and it often moves without the
    amount changing at all.
    """
    sql = ("SELECT line_id, fy, channel, pot, member, org, agency, amount, tier, "
           "status, reso, source_id, district FROM funding")
    params: list = []
    if fy:
        sql += " WHERE fy=?"
        params.append(fy)
    rows = []
    for r in store.q(sql, params):
        rows.append({
            "key": f"funding:{r['line_id']}",
            "kind": "funding", "entity_id": r["line_id"],
            "label": (f"FY{r['fy']} {r['org'] or r['pot'] or 'line'} "
                      f"({r['member'] or r['channel'] or '—'})"),
            "url": None, "source_id": r["source_id"] or "SCHEDULE_C",
            "fields": {"amount": r["amount"], "tier": r["tier"],
                       "status": r["status"], "org": r["org"],
                       "agency": r["agency"], "pot": r["pot"], "reso": r["reso"]},
        })
    return rows


def watch_calendar(store: Store) -> list[dict]:
    rows = []
    for r in store.q("SELECT * FROM calendar"):
        rows.append({
            "key": f"calendar:{r['event_id']}",
            "kind": "calendar", "entity_id": r["event_id"],
            "label": r["summary"] or r["event_id"],
            "url": r["link"], "source_id": "D49_CALENDAR",
            "fields": {"start": r["start"], "end": r["end"],
                       "location": r["location"], "summary": r["summary"]},
        })
    return rows


def watch_ledger(store: Store) -> list[dict]:
    """
    Watch the reconciliation's totals and its tie-checks.

    Only the load-bearing rows: the totals the office quotes in public and the
    checks that prove those totals reproduce the City's printed books. A
    tie-check that stops passing means a source book was reissued underneath
    the office's arithmetic, and every figure on that sheet is unproven until
    somebody looks. There is no louder signal in the whole system.
    """
    rows = []
    for r in store.q("SELECT row_id, book, sheet, label, amount, kind, cells "
                     "FROM ledger WHERE kind IN ('total','tie_check')"):
        rows.append({
            "key": f"ledger:{r['row_id']}",
            "kind": "ledger",
            "entity_id": r["row_id"],
            "label": f"{r['sheet']} — {r['label']}"[:160],
            "fields": {"amount": r["amount"], "kind": r["kind"],
                       "passes": _tie_passes(r["cells"])
                       if r["kind"] == "tie_check" else None},
        })
    return rows


def _tie_passes(cells: Any) -> bool | None:
    if not cells:
        return None
    blob = str(cells).upper()
    if "TIE" not in blob and "✓" not in blob:
        return None
    return "TIES ✓" in blob or "✓ TIES" in blob or "EXACT TIE" in blob \
        or "IDENTICAL" in blob


COLLECTORS: dict[str, Callable[[Store], list[dict]]] = {
    "matter": watch_matters,
    "funding": watch_funding,
    "calendar": watch_calendar,
    "ledger": watch_ledger,
}


# ---------------------------------------------------------------- triage ----
def _money_delta(before: Any, after: Any) -> float | None:
    try:
        return round(float(after or 0) - float(before or 0), 2)
    except (TypeError, ValueError):
        return None


def severity(kind: str, field: str, before: Any, after: Any,
             row: dict) -> tuple[str, str]:
    """
    How loud should this be, and why.

    The "why" is returned with the level because an alert a staffer cannot
    interrogate is an alert they learn to ignore.
    """
    after_s = str(after or "").upper()
    before_s = str(before or "").upper()

    if kind == "funding":
        if field == "tier":
            if after_s == "REVERSED":
                return "high", ("Money already announced has been reversed. The "
                                "recipient may have been told it was coming.")
            if before_s == "ADOPTED-PENDING-MOD" and after_s == "ADOPTED-IMPLEMENTATION":
                return "high", ("Pending money is now confirmed and can be counted "
                                "in a public total.")
            return "medium", "The funding tier moved, which changes what can be said publicly."
        if field == "amount":
            delta = _money_delta(before, after)
            size = abs(delta or 0)
            if size >= MONEY_HIGH:
                return "high", f"The award moved by ${size:,.0f}."
            if size >= MONEY_MED:
                return "medium", f"The award moved by ${size:,.0f}."
            return "low", f"The award moved by ${size:,.0f}."
        if field == "reso":
            return "high", "The line is now tied to a different transparency resolution."
        if field == "org":
            return "medium", "The recipient on this line changed."
        return "low", "A descriptive field on a funding line changed."

    if kind == "matter":
        if field == "enacted" and after:
            return "high", "The bill has been enacted."
        if field == "local_law" and after:
            return "high", f"The bill became Local Law {after}."
        if field == "status":
            moved = any(w in after_s for w in ("PASS", "ADOPT", "ENACT", "APPROV"))
            return ("high" if moved else "medium"), f"Status moved to {after}."
        if field == "committee":
            return "medium", "The bill was referred to a different committee."
        if field == "n_sponsors":
            delta = _money_delta(before, after) or 0
            return ("medium" if abs(delta) >= 5 else "low"), \
                   f"Sponsor count moved by {int(delta):+d}."
        return "low", "A descriptive field on the bill changed."

    if kind == "ledger":
        if field == "passes":
            if before and not after:
                return "high", ("A reconciliation check that used to tie no "
                                "longer does. Every figure on that sheet is "
                                "unproven until somebody re-checks it.")
            if after and not before:
                return "medium", "A reconciliation check now ties."
            return "medium", "A reconciliation check changed state."
        if field == "amount":
            delta = _money_delta(before, after)
            return "high", (f"A published total moved by ${abs(delta or 0):,.0f}. "
                            f"Anything the office already said using the old "
                            f"figure is now wrong.")
        return "medium", "A reconciliation row changed."

    if kind == "calendar":
        if field in ("start", "end"):
            soon = _within_days(after or before, 7)
            return (("high", "A meeting inside the next week was rescheduled.")
                    if soon else ("medium", "A scheduled item moved."))
        if field == "location":
            return "medium", "The location changed."
        return "low", "A calendar detail changed."

    return "low", "A watched field changed."


def _within_days(iso: Any, days: int) -> bool:
    try:
        when = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return now <= when <= now + timedelta(days=days)


def appearance_severity(kind: str, row: dict) -> tuple[str, str]:
    if kind == "funding":
        amount = row["fields"].get("amount") or 0
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            amount = 0.0
        if abs(amount) >= MONEY_HIGH:
            return "high", f"A new funding line of ${amount:,.0f} appeared."
        return "medium", f"A new funding line of ${amount:,.0f} appeared."
    if kind == "matter":
        return "medium", "A new bill was introduced."
    if kind == "calendar":
        return ("high", "A new item was added inside the next week.") \
            if _within_days(row["fields"].get("start"), 7) \
            else ("low", "A new calendar item was added.")
    return "low", "A new watched record appeared."


# ------------------------------------------------------------------ scan ----
def scan(store: Store, kinds: Iterable[str] | None = None,
         baseline: bool = False, record_removals: bool = True) -> dict:
    """
    Compare every watched record to its last fingerprint.

    The first scan is a *baseline*: it records the world as it is without
    reporting 72,000 changes. Everything after it reports only what moved.
    """
    init(store)
    kinds = list(kinds or COLLECTORS.keys())
    stamp = _now()
    changes: list[dict] = []
    counts: dict[str, dict] = {}

    for kind in kinds:
        collector = COLLECTORS.get(kind)
        if collector is None:
            continue
        rows = collector(store)
        previous = {r["key"]: dict(r) for r in store.q(
            "SELECT * FROM ws_watch_state WHERE kind=?", (kind,))}
        first_run = not previous
        seen: set[str] = set()
        new_state: list[dict] = []
        added = moved = 0

        for row in rows:
            seen.add(row["key"])
            fields = row["fields"]
            fingerprint = _fingerprint(fields)
            old = previous.get(row["key"])
            if old is None:
                if not (baseline or first_run):
                    level, why = appearance_severity(kind, row)
                    changes.append(_change(stamp, row, "appeared", None, None,
                                           None, None, level, why))
                    added += 1
            elif old["fingerprint"] != fingerprint:
                try:
                    before = json.loads(old["snapshot"] or "{}")
                except ValueError:
                    before = {}
                for field, after in fields.items():
                    was = before.get(field)
                    if _same(was, after):
                        continue
                    level, why = severity(kind, field, was, after, row)
                    changes.append(_change(stamp, row, "changed", field, was, after,
                                           _money_delta(was, after)
                                           if field == "amount" else None,
                                           level, why))
                    moved += 1
            new_state.append({
                "key": row["key"], "kind": kind, "entity_id": row["entity_id"],
                "label": row["label"], "fingerprint": fingerprint,
                "snapshot": json.dumps(fields, default=str),
                "first_seen": old["first_seen"] if old else stamp,
                "last_seen": stamp,
            })

        gone = [k for k in previous if k not in seen]
        if gone and record_removals and not (baseline or first_run):
            for key in gone:
                old = previous[key]
                changes.append(_change(
                    stamp, {"kind": kind, "entity_id": old["entity_id"],
                            "label": old["label"], "url": None,
                            "source_id": None, "fields": {}},
                    "disappeared", None, old["label"], None, None,
                    "medium" if kind == "funding" else "low",
                    "The record is no longer present in the source."))
        if gone:
            store.conn.executemany("DELETE FROM ws_watch_state WHERE key=?",
                                   [(k,) for k in gone])
        if new_state:
            store.upsert("ws_watch_state", new_state)
        counts[kind] = {"watched": len(rows), "appeared": added, "changed": moved,
                        "disappeared": len(gone), "baseline": baseline or first_run}

    if changes:
        store.upsert("ws_changes", changes)
    store.conn.commit()
    store.set_meta("watch.last_scan", {"at": stamp, "counts": counts,
                                       "changes": len(changes)})
    store.journal("watch.scan", {"kinds": kinds, "changes": len(changes)})
    _publish(store, changes)
    return {"at": stamp, "baseline": baseline, "counts": counts,
            "changes": len(changes),
            "high": sum(1 for c in changes if c["severity"] == "high"),
            "detail": changes[:200]}


def _same(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    if isinstance(a, (int, float)) or isinstance(b, (int, float)):
        try:
            return abs(float(a or 0) - float(b or 0)) < 0.005
        except (TypeError, ValueError):
            pass
    return str(a if a is not None else "") == str(b if b is not None else "")


def _change(stamp: str, row: dict, change: str, field: str | None,
            before: Any, after: Any, delta: float | None,
            level: str, why: str) -> dict:
    ident = hashlib.sha256(
        f"{stamp}|{row['kind']}|{row['entity_id']}|{change}|{field}|{after}"
        .encode()).hexdigest()[:24]
    return {"id": ident, "at": stamp, "kind": row["kind"],
            "entity_id": str(row["entity_id"]),
            "label": row.get("label"), "change": change,
            "field": FIELD_LABELS.get(field or "", field),
            "before": None if before is None else str(before)[:300],
            "after": None if after is None else str(after)[:300],
            "delta": delta, "severity": level, "why": why,
            "url": row.get("url"), "source_id": row.get("source_id"),
            "acknowledged": 0}


def _publish(store: Store, changes: list[dict]) -> None:
    """Push high-severity changes onto the workspace event stream."""
    if not changes:
        return
    try:
        from ..workspace import events as ws_events
    except Exception:
        return
    for change in changes:
        if change["severity"] != "high":
            continue
        summary = (f"{change['label']}: {change['field'] or change['change']} "
                   f"{change['before']} → {change['after']}"
                   if change["change"] == "changed"
                   else f"{change['label']} {change['change']}")
        try:
            ws_events.emit(store, "change.detected", change["kind"],
                           change["entity_id"], summary, actor="watch",
                           payload={"field": change["field"],
                                    "before": change["before"],
                                    "after": change["after"],
                                    "severity": change["severity"],
                                    "why": change["why"], "url": change["url"],
                                    "source_id": change["source_id"]})
        except Exception:
            return          # the stream is a convenience; a diff is the record


# ----------------------------------------------------------------- readers ----
def recent(store: Store, limit: int = 50, severity_at_least: str = "",
           kind: str = "", unacknowledged_only: bool = False) -> list[dict]:
    init(store)
    order = {"high": 3, "medium": 2, "low": 1}
    sql = "SELECT * FROM ws_changes WHERE 1=1"
    params: list = []
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    if unacknowledged_only:
        sql += " AND acknowledged=0"
    if severity_at_least in order:
        allowed = [k for k, v in order.items() if v >= order[severity_at_least]]
        sql += f" AND severity IN ({','.join('?' * len(allowed))})"
        params += allowed
    sql += " ORDER BY at DESC, severity ASC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in store.q(sql, params)]


def acknowledge(store: Store, change_id: str, who: str) -> bool:
    init(store)
    cur = store.conn.execute(
        "UPDATE ws_changes SET acknowledged=1, acknowledged_by=?, acknowledged_at=? "
        "WHERE id=?", (who, _now(), change_id))
    store.conn.commit()
    return cur.rowcount > 0


def digest(store: Store, since_hours: int = 168) -> dict:
    """
    What moved this week, in the shape the Councilmember reads.

    A paragraph of what matters, then the specifics, because a wall of 400
    diffs is the same as no alert at all.
    """
    init(store)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)) \
        .isoformat(timespec="seconds")
    rows = [dict(r) for r in store.q(
        "SELECT * FROM ws_changes WHERE at>=? ORDER BY at DESC", (cutoff,))]
    high = [r for r in rows if r["severity"] == "high"]
    money = round(sum(r["delta"] or 0 for r in rows if r["kind"] == "funding"), 2)
    reversals = [r for r in rows
                 if r["kind"] == "funding" and (r["after"] or "").upper() == "REVERSED"]
    enacted = [r for r in rows if r["kind"] == "matter"
               and (r["field"] or "") in ("enacted", "local law") and r["after"]]

    headline = (f"{len(rows)} tracked change{'s' if len(rows) != 1 else ''} in the "
                f"last {since_hours // 24} day{'s' if since_hours // 24 != 1 else ''}, "
                f"{len(high)} of them worth your attention.")
    if reversals:
        headline += (f" {len(reversals)} funding line"
                     f"{'s were' if len(reversals) != 1 else ' was'} reversed.")
    if money:
        headline += f" Net movement in tracked awards: ${money:,.0f}."
    if not rows:
        headline = (f"Nothing moved in the last {since_hours // 24} days on the "
                    f"records being watched.")

    return {
        "window_hours": since_hours, "headline": headline,
        "total": len(rows), "high": len(high),
        "net_funding_delta": money,
        "reversals": reversals[:20],
        "enacted": enacted[:20],
        "by_kind": {k: sum(1 for r in rows if r["kind"] == k)
                    for k in sorted({r["kind"] for r in rows})},
        "items": high[:40] or rows[:40],
        "sources": sorted({r["source_id"] for r in rows if r["source_id"]}),
    }


def status(store: Store) -> dict:
    init(store)
    last = store.get_meta("watch.last_scan")
    return {
        "last_scan": last,
        "watched": {r["kind"]: r["n"] for r in store.q(
            "SELECT kind, COUNT(*) AS n FROM ws_watch_state GROUP BY kind")},
        "changes_recorded": store.scalar("SELECT COUNT(*) FROM ws_changes") or 0,
        "unacknowledged_high": store.scalar(
            "SELECT COUNT(*) FROM ws_changes WHERE severity='high' AND acknowledged=0") or 0,
    }
