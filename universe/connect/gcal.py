#!/usr/bin/env python3
"""
The Google Calendar connector.

The District 49 calendar is the office's real schedule: what the Councilmember
is personally committed to, which staffer owns an event, when an RSVP closes,
and which hearings the office has to be ready for. Pulling it on a timer is
what turns "we have a calendar" into "we have a week's worth of briefings
already drafted".

Four transports, tried in the order that asks least of the user:

  1. ``ics``      -- a secret ICS address. No credential, no consent screen,
                     works for a private calendar. This is the one to use.
  2. ``public``   -- the public ICS address, if the calendar is shared publicly.
  3. ``api_key``  -- Calendar API v3 with an API key. Public calendars only;
                     Google rejects an API key against a private calendar.
  4. ``oauth``    -- Calendar API v3 with an OAuth access token. Full access,
                     including private events and attendee lists.

Whatever answers, the rows come back in Calendar API shape and go through the
same ``ingest.calendar`` normaliser, so the staff-initials convention, the
event kinds and the pillar tagging behave identically on every path.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..core import keys
from ..core.config import D49_CALENDAR_ID
from ..core.store import Store
from ..ingest import calendar as cal_ingest
from . import ics as ics_reader

UA = "D49-Universe/1.0 (+council.nyc.gov District 49)"
API = "https://www.googleapis.com/calendar/v3"
PUBLIC_ICS = "https://calendar.google.com/calendar/ical/{cid}/public/basic.ics"
TIMEOUT = 30


def _fetch(url: str, headers: dict | None = None, timeout: int = TIMEOUT) -> tuple[int, str]:
    """Return (status, body). A network failure is a status, not an exception."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:600]
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def _window(days_back: int, days_ahead: int) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    return ((now - timedelta(days=days_back)).isoformat(timespec="seconds").replace("+00:00", "Z"),
            (now + timedelta(days=days_ahead)).isoformat(timespec="seconds").replace("+00:00", "Z"))


# ------------------------------------------------------------- transports ----
def _from_ics(url: str, horizon: int) -> dict:
    status, body = _fetch(url)
    if status != 200 or "BEGIN:VCALENDAR" not in body:
        return {"ok": False, "status": status,
                "detail": body[:300] if status else body}
    events = ics_reader.parse(body, horizon_days=horizon)
    return {"ok": True, "status": status, "events": events}


def _from_api(calendar_id: str, auth: dict, days_back: int, days_ahead: int) -> dict:
    """Calendar API v3 events.list, paged. `auth` is a key or a bearer token."""
    lo, hi = _window(days_back, days_ahead)
    params = {"timeMin": lo, "timeMax": hi, "singleEvents": "true",
              "orderBy": "startTime", "maxResults": "2500"}
    headers = {}
    if auth.get("token"):
        headers["Authorization"] = f"Bearer {auth['token']}"
    elif auth.get("key"):
        params["key"] = auth["key"]

    events: list[dict] = []
    page = None
    for _ in range(20):                       # a hard stop; no unbounded paging
        if page:
            params["pageToken"] = page
        url = (f"{API}/calendars/{urllib.parse.quote(calendar_id)}/events"
               f"?{urllib.parse.urlencode(params)}")
        status, body = _fetch(url, headers)
        if status != 200:
            return {"ok": False, "status": status, "detail": body[:400],
                    "events": events}
        try:
            payload = json.loads(body)
        except ValueError:
            return {"ok": False, "status": status, "detail": "unparseable JSON"}
        events += payload.get("items", [])
        page = payload.get("nextPageToken")
        if not page:
            break
    return {"ok": True, "status": 200, "events": events}


# ------------------------------------------------------------------ fetch ----
def fetch(calendar_id: str | None = None, days_back: int = 60,
          days_ahead: int = 180, transport: str = "auto") -> dict:
    """
    Pull the calendar by whatever transport is configured.

    Returns the events plus an `attempts` list, so when nothing comes back the
    office can see which door was tried and what the server said, rather than
    an empty screen with no explanation.
    """
    cid = calendar_id or keys.setting("d49_calendar_id", D49_CALENDAR_ID)
    horizon = days_back + days_ahead
    attempts: list[dict] = []

    def note(name: str, result: dict) -> dict | None:
        attempts.append({"transport": name, "ok": result.get("ok", False),
                         "status": result.get("status"),
                         "events": len(result.get("events", []) or []),
                         "detail": result.get("detail")})
        return result if result.get("ok") and result.get("events") is not None else None

    order = ([transport] if transport != "auto"
             else ["ics", "oauth", "api_key", "public"])

    for name in order:
        if name == "ics":
            url = keys.setting("calendar_ics_url")
            if not url:
                attempts.append({"transport": "ics", "ok": False,
                                 "detail": "no calendar_ics_url configured"})
                continue
            got = note("ics", _from_ics(url, horizon))
        elif name == "public":
            got = note("public", _from_ics(PUBLIC_ICS.format(cid=urllib.parse.quote(cid)),
                                           horizon))
        elif name == "oauth":
            token = keys.get("google_oauth_token")
            if not token:
                attempts.append({"transport": "oauth", "ok": False,
                                 "detail": "no google_oauth_token configured"})
                continue
            got = note("oauth", _from_api(cid, {"token": token}, days_back, days_ahead))
        elif name == "api_key":
            key = keys.get("google_api_key")
            if not key:
                attempts.append({"transport": "api_key", "ok": False,
                                 "detail": "no google_api_key configured"})
                continue
            got = note("api_key", _from_api(cid, {"key": key}, days_back, days_ahead))
        else:
            attempts.append({"transport": name, "ok": False,
                             "detail": "unknown transport"})
            continue
        if got:
            return {"ok": True, "transport": name, "calendar_id": cid,
                    "events": got["events"], "attempts": attempts}

    return {"ok": False, "calendar_id": cid, "events": [], "attempts": attempts,
            "how_to_fix": HOW_TO_FIX}


HOW_TO_FIX = (
    "No calendar transport answered. The quickest fix needs no OAuth: in "
    "Google Calendar open Settings for the District 49 calendar, copy the "
    "'Secret address in iCal format', and run "
    "`universe connect calendar --ics-url '<that address>'`. That address is "
    "itself a credential -- it is stored in ~/.d49/config.json with owner-only "
    "permissions and is never written into the repository."
)


# ----------------------------------------------------------------- ingest ----
def sync(store: Store, calendar_id: str | None = None, days_back: int = 60,
         days_ahead: int = 180, transport: str = "auto") -> dict:
    """Fetch, then load through the normal calendar ingest so nothing skips
    the staff-initials, kind and pillar conventions."""
    got = fetch(calendar_id, days_back, days_ahead, transport)
    if not got["ok"]:
        store.journal("connect.calendar.failed", {"attempts": got["attempts"]})
        return got
    loaded = cal_ingest.ingest(store, got["events"])
    store.set_meta("calendar.last_sync", {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "transport": got["transport"], "events": loaded.get("calendar_events", 0)})
    return {**got, **loaded, "events": len(got["events"])}


def sync_file(store: Store, path: Path | str) -> dict:
    """Load a calendar export -- an .ics file or a connector JSON dump."""
    text = Path(path).read_text()
    events = (ics_reader.parse(text) if "BEGIN:VCALENDAR" in text
              else (lambda raw: raw.get("events", raw if isinstance(raw, list) else []))
                   (json.loads(text)))
    loaded = cal_ingest.ingest(store, events)
    store.set_meta("calendar.last_sync", {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "transport": "file", "source": str(path),
        "events": loaded.get("calendar_events", 0)})
    return {"ok": True, "transport": "file", "source": str(path),
            "events": len(events), **loaded}


def last_sync(store: Store) -> dict | None:
    return store.get_meta("calendar.last_sync")
