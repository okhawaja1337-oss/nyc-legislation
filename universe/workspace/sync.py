#!/usr/bin/env python3
"""
Keep the workspace current.

Legislation and hearings move without asking the office first, so the
workspace pulls them on a schedule and records what changed. Three properties
matter more than freshness:

* **A failed refresh never destroys good data.** If Legistar is unreachable,
  the last successful pull stays exactly as it was and the failure is recorded
  against the source, visible in the interface. An empty response is a failure,
  not an instruction to delete the record.
* **Changes are diffed, not overwritten.** When a bill's status or sponsor
  count moves, the old and new values are written to the change feed, which is
  what makes a saved search able to say "three of your watched bills moved".
* **Every sync is attributed.** Each source carries its last attempt, last
  success, row count and error, so "is this current?" has an answer.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from ..live import feeds
from . import events, indexer, media, schema

WATCHED = ("status", "committee", "n_sponsors", "enacted", "local_law", "pending")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _mark(store, source: str, status: str, rows: int = 0,
          error: str = "", cursor: str = "") -> None:
    prior = store.one("SELECT * FROM workspace_sync WHERE source=?", [source])
    store.upsert("workspace_sync", [{
        "source": source, "attempted": now(),
        "succeeded": now() if status == "Current"
                     else (dict(prior).get("succeeded") if prior else None),
        "status": status, "rows": rows, "error": error[:500],
        "cursor": cursor or (dict(prior).get("cursor") if prior else ""),
    }])


def source_health(store) -> list[dict]:
    return [dict(r) for r in store.q(
        "SELECT * FROM workspace_sync ORDER BY source")]


# ------------------------------------------------------------- legislation --
def sync_legistar(store, year: int | None = None, full: bool = False) -> dict:
    """Pull this year's matters and diff them against what we hold."""
    schema.apply(store.conn)
    year = year or date.today().year
    res = feeds.matters_for_year(year, top=1000, ttl=0 if full else 900)
    if not res.rows:
        _mark(store, "LEGISTAR", "Error", 0,
              res.error or "No rows returned; keeping the last good copy.")
        return {"source": "LEGISTAR", "ok": False,
                "error": res.error or "No rows returned",
                "kept": "Previous records are unchanged."}

    changed, added = [], 0
    for m in res.rows:
        mid = m.get("MatterId")
        if mid is None:
            continue
        new = {
            "matter_id": mid,
            "file": m.get("MatterFile"), "name": m.get("MatterName"),
            "type": m.get("MatterTypeName"), "status": m.get("MatterStatusName"),
            "committee": m.get("MatterBodyName"), "year": year,
            "enacted": 1 if (m.get("MatterEnactmentNumber") or "") else 0,
            "local_law": m.get("MatterEnactmentNumber"),
            "n_sponsors": m.get("MatterSponsorCount"),
            "pending": 0 if (m.get("MatterStatusName") or "") in
                       ("Enacted", "Filed", "Withdrawn") else 1,
            "source_id": "LEGISTAR", "updated": m.get("MatterLastModifiedUtc") or now(),
        }
        old = store.one("SELECT * FROM matters WHERE matter_id=?", [mid])
        if old:
            old = dict(old)
            diff = [k for k in WATCHED
                    if str(old.get(k) or "") != str(new.get(k) or "")]
            if diff:
                changed.append({"matter_id": mid, "file": new["file"],
                                "fields": diff})
                store.conn.execute(
                    "INSERT INTO workspace_changes (record_key, kind, title, at, "
                    "fields, before_json, after_json) VALUES (?,?,?,?,?,?,?)",
                    (f"matter:{mid}", "matter",
                     f"{new['file']} — {new['name']}", now(), json.dumps(diff),
                     json.dumps({k: old.get(k) for k in diff}, default=str),
                     json.dumps({k: new.get(k) for k in diff}, default=str)))
            store.conn.execute(
                "UPDATE matters SET status=?, committee=?, n_sponsors=?, "
                "enacted=?, local_law=?, pending=?, updated=? WHERE matter_id=?",
                (new["status"], new["committee"], new["n_sponsors"],
                 new["enacted"], new["local_law"], new["pending"],
                 new["updated"], mid))
        else:
            store.upsert("matters", [new])
            added += 1
    store.conn.commit()

    _mark(store, "LEGISTAR", "Current", len(res.rows), "", str(year))
    events.emit(store, "sync.legistar", "source", "LEGISTAR",
                f"Legistar: {len(res.rows)} matters, {added} new, "
                f"{len(changed)} changed", "",
                {"changed": changed[:40]})
    return {"source": "LEGISTAR", "ok": True, "fetched": len(res.rows),
            "added": added, "changed": len(changed), "detail": changed[:40]}


def sync_events(store, days: int = 60) -> dict:
    """Committee hearings for the coming weeks, into the calendar and media."""
    res = feeds.upcoming_events(days=days, ttl=900)
    if not res.rows:
        _mark(store, "LEGISTAR_EVENTS", "Error", 0,
              res.error or "No events returned; keeping the last good copy.")
        return {"source": "LEGISTAR_EVENTS", "ok": False,
                "error": res.error or "No events returned"}

    rows = []
    for e in res.rows:
        eid = e.get("EventId")
        start = (e.get("EventDate") or "")[:10]
        t = (e.get("EventTime") or "").strip()
        rows.append({
            "event_id": f"legistar-{eid}",
            "start": f"{start}T{_24h(t)}" if start and t else start,
            "end": None,
            "summary": f"{e.get('EventBodyName') or 'Council meeting'}",
            "location": e.get("EventLocation"),
            "kind": "hearing", "owners": json.dumps([]),
            "pillar": None, "link": e.get("EventInSiteURL"),
            "updated": now(),
        })
    store.upsert("calendar", rows)
    media.collect_hearings(store, days=days)
    _mark(store, "LEGISTAR_EVENTS", "Current", len(rows))
    events.emit(store, "sync.events", "source", "LEGISTAR_EVENTS",
                f"{len(rows)} hearings in the next {days} days")
    return {"source": "LEGISTAR_EVENTS", "ok": True, "fetched": len(rows)}


def _24h(t: str) -> str:
    """Legistar publishes '10:00 AM'. Calendars want 10:00."""
    try:
        return datetime.strptime(t.upper().replace(".", ""), "%I:%M %p").strftime("%H:%M")
    except ValueError:
        return "09:00"


def sync_311(store, district: int = 49, days: int = 30) -> dict:
    """Live constituent demand, as complaint counts by type."""
    res = feeds.complaints_by_type(district=district, days=days, ttl=1800)
    if not res.rows:
        _mark(store, "OPEN_DATA_311", "Error", 0, res.error or "No rows")
        return {"source": "OPEN_DATA_311", "ok": False, "error": res.error}
    store.set_meta("workspace.311", {"fetched": now(), "days": days,
                                     "rows": res.rows[:60]})
    _mark(store, "OPEN_DATA_311", "Current", len(res.rows))
    return {"source": "OPEN_DATA_311", "ok": True, "types": len(res.rows)}


def refresh_sources(store, full: bool = False) -> dict:
    """One pass over every live source. A failure in one never stops the rest."""
    out: dict[str, Any] = {}
    for name, fn in (("legislation", lambda: sync_legistar(store, full=full)),
                     ("hearings", lambda: sync_events(store)),
                     ("311", lambda: sync_311(store)),
                     ("media", lambda: media.refresh_all(store))):
        try:
            out[name] = fn()
        except Exception as exc:
            out[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    out["health"] = source_health(store)
    return out
