#!/usr/bin/env python3
"""
The event log and the live stream.

Every mutation appends one row to ``ws_events``. Browsers hold an EventSource
on ``/api/stream`` and receive events as they land, so a second staffer sees
an assignment the moment it is made rather than up to fifteen seconds later.

The log is the transport *and* the audit trail: the stream is just a tail of a
table that is durable on its own. A client that was offline reconnects with
the last id it saw and receives exactly what it missed -- no lost updates, no
full reload.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

# Wakes every waiting stream thread when a new event lands.
_pulse = threading.Condition()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def emit(store, kind: str, entity: str, entity_id: str, summary: str,
         actor: str = "", payload: dict | None = None) -> int:
    """Append one event and wake every listening stream."""
    cur = store.conn.execute(
        "INSERT INTO ws_events (at, actor, kind, entity, entity_id, summary, payload) "
        "VALUES (?,?,?,?,?,?,?)",
        (now(), actor or "workspace", kind, entity, str(entity_id),
         summary[:500], json.dumps(payload or {}, default=str)))
    store.conn.commit()
    with _pulse:
        _pulse.notify_all()
    return cur.lastrowid


def since(store, cursor: int, limit: int = 200) -> list[dict]:
    """Events after ``cursor``, oldest first."""
    return [dict(r) for r in store.q(
        "SELECT * FROM ws_events WHERE id > ? ORDER BY id LIMIT ?",
        (int(cursor or 0), limit))]


def latest(store) -> int:
    return store.scalar("SELECT COALESCE(MAX(id), 0) FROM ws_events") or 0


def wait(timeout: float = 25.0) -> None:
    """Block until an event lands or the timeout expires.

    The timeout matters: it bounds how long a proxy sees an idle connection,
    and it gives the stream a moment to send a keepalive so the browser does
    not decide the connection died.
    """
    with _pulse:
        _pulse.wait(timeout)


def format_sse(event: dict) -> str:
    """One event, as a Server-Sent Events frame."""
    body = {
        "id": event["id"], "at": event["at"], "actor": event["actor"],
        "kind": event["kind"], "entity": event["entity"],
        "entity_id": event["entity_id"], "summary": event["summary"],
    }
    try:
        body["payload"] = json.loads(event.get("payload") or "{}")
    except (TypeError, ValueError):
        body["payload"] = {}
    return (f"id: {event['id']}\n"
            f"event: {event['kind']}\n"
            f"data: {json.dumps(body, default=str)}\n\n")


def keepalive() -> str:
    """A comment frame. Keeps proxies from closing an idle stream."""
    return f": keepalive {now()}\n\n"
