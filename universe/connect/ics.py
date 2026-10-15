#!/usr/bin/env python3
"""
An iCalendar reader.

Google, Outlook, Legistar and Granicus all publish ICS. It is the one calendar
format every system the office deals with can emit, and reading it needs no
credential -- a secret ICS address is itself the credential. That makes it the
cheapest transport we have, so it is the one we do properly.

RFC 5545 in the parts that matter here: line unfolding, parameter parsing,
the DATE/DATE-TIME split (an all-day event has no time and must not be given
one), text unescaping, and enough RRULE to expand the weekly and monthly
repeats a Council office actually uses. Anything more exotic is returned as a
single occurrence with its rule attached rather than silently dropped.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Iterator

# "DTSTART;TZID=America/New_York:20260113T100000"
LINE = re.compile(r"^(?P<name>[A-Za-z0-9-]+)(?P<params>(?:;[^:]*)?):(?P<value>.*)$")
UNESCAPE = ((r"\n", "\n"), (r"\N", "\n"), (r"\,", ","), (r"\;", ";"), ("\\\\", "\\"))
WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
MAX_OCCURRENCES = 200          # a runaway RRULE must not fill the lake


def unfold(text: str) -> Iterator[str]:
    """RFC 5545 folds long lines; a continuation starts with a space or tab."""
    current = ""
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t"):
            current += raw[1:]
            continue
        if current:
            yield current
        current = raw
    if current:
        yield current


def _unescape(value: str) -> str:
    for token, char in UNESCAPE:
        value = value.replace(token, char)
    return value


def _params(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.upper()] = v.strip('"')
    return out


def parse_when(value: str, params: dict[str, str]) -> tuple[str | None, bool]:
    """
    Return (ISO string, all_day).

    An all-day event is a DATE with no time. Giving it midnight would make it
    sort against timed events and show up as "12:00 AM" on a briefing sheet,
    which is wrong in a way people notice.
    """
    value = value.strip()
    if params.get("VALUE") == "DATE" or (len(value) == 8 and value.isdigit()):
        try:
            return date(int(value[:4]), int(value[4:6]), int(value[6:8])).isoformat(), True
        except ValueError:
            return None, True
    naive = value.rstrip("Z")
    try:
        moment = datetime.strptime(naive[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        return value or None, False
    if value.endswith("Z"):
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.isoformat(), False


def _to_dt(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


# ------------------------------------------------------------ recurrence ----
def _rrule(raw: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for part in raw.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.upper()] = v
    return out


def expand(start_iso: str, end_iso: str | None, rule: str,
           horizon_days: int = 400) -> list[tuple[str, str | None]]:
    """
    Expand an RRULE into concrete occurrences.

    Handles DAILY, WEEKLY (with BYDAY) and MONTHLY by day-of-month -- the
    shapes a Council calendar uses for a standing committee, a weekly staff
    meeting, or a monthly community board. An unsupported FREQ returns the
    single original occurrence, because a wrong date on a hearing sheet is
    worse than a missing one.
    """
    spec = _rrule(rule)
    freq = spec.get("FREQ", "").upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY"):
        return [(start_iso, end_iso)]

    begin = _to_dt(start_iso)
    if begin is None:
        return [(start_iso, end_iso)]
    finish = _to_dt(end_iso)
    span = (finish - begin) if finish else None

    interval = max(1, int(spec.get("INTERVAL", 1) or 1))
    count = int(spec["COUNT"]) if spec.get("COUNT", "").isdigit() else None
    until = None
    if spec.get("UNTIL"):
        until_iso, _ = parse_when(spec["UNTIL"], {})
        until = _to_dt(until_iso)
        if until and until.tzinfo and not begin.tzinfo:
            until = until.replace(tzinfo=None)

    limit = begin + timedelta(days=horizon_days)
    if until:
        limit = min(limit, until)

    days = [WEEKDAYS[d[-2:]] for d in spec.get("BYDAY", "").split(",")
            if d[-2:] in WEEKDAYS]

    out: list[tuple[str, str | None]] = []
    cursor = begin
    guard = 0
    while cursor <= limit and len(out) < MAX_OCCURRENCES and guard < 5000:
        guard += 1
        if freq == "WEEKLY" and days:
            week_start = cursor - timedelta(days=cursor.weekday())
            for offset in sorted(days):
                moment = week_start + timedelta(days=offset)
                if moment < begin or moment > limit:
                    continue
                out.append((moment.isoformat(),
                            (moment + span).isoformat() if span else None))
            cursor += timedelta(weeks=interval)
        else:
            out.append((cursor.isoformat(),
                        (cursor + span).isoformat() if span else None))
            if freq == "DAILY":
                cursor += timedelta(days=interval)
            elif freq == "WEEKLY":
                cursor += timedelta(weeks=interval)
            else:                                  # MONTHLY, same day number
                month = cursor.month - 1 + interval
                year = cursor.year + month // 12
                month = month % 12 + 1
                day = min(cursor.day, [31, 29 if year % 4 == 0 and
                                       (year % 100 != 0 or year % 400 == 0) else 28,
                                       31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
                cursor = cursor.replace(year=year, month=month, day=day)
        if count and len(out) >= count:
            out = out[:count]
            break
    return out or [(start_iso, end_iso)]


# ----------------------------------------------------------------- parse ----
def parse(text: str, expand_recurrence: bool = True,
          horizon_days: int = 400) -> list[dict]:
    """
    ICS text to event dicts shaped like the Google Calendar API, so the rest
    of the system does not have to care which transport delivered them.
    """
    events: list[dict] = []
    current: dict[str, Any] | None = None
    for line in unfold(text or ""):
        upper = line.strip().upper()
        if upper == "BEGIN:VEVENT":
            current = {"_raw": {}}
            continue
        if upper == "END:VEVENT":
            if current is not None:
                events.extend(_build(current, expand_recurrence, horizon_days))
            current = None
            continue
        if current is None:
            continue
        m = LINE.match(line)
        if not m:
            continue
        name = m.group("name").upper()
        params = _params(m.group("params") or "")
        value = m.group("value")
        if name in ("DTSTART", "DTEND"):
            iso, all_day = parse_when(value, params)
            current[name] = iso
            current["_all_day"] = current.get("_all_day") or all_day
            if params.get("TZID"):
                current["_tzid"] = params["TZID"]
        else:
            current["_raw"].setdefault(name, []).append(_unescape(value))
    return events


def _one(node: dict, name: str) -> str | None:
    got = node.get("_raw", {}).get(name)
    return got[0] if got else None


def _build(node: dict, expand_recurrence: bool, horizon_days: int) -> list[dict]:
    start, end = node.get("DTSTART"), node.get("DTEND")
    if not start:
        return []
    uid = _one(node, "UID") or f"ics-{abs(hash((start, _one(node, 'SUMMARY'))))}"
    rule = _one(node, "RRULE")
    occurrences = ([(start, end)] if not (rule and expand_recurrence)
                   else expand(start, end, rule, horizon_days))
    all_day = bool(node.get("_all_day"))
    out = []
    for index, (began, ended) in enumerate(occurrences):
        out.append({
            "id": uid if index == 0 else f"{uid}::{began[:10]}",
            "summary": _one(node, "SUMMARY") or "",
            "location": _one(node, "LOCATION"),
            "description": _one(node, "DESCRIPTION"),
            "status": (_one(node, "STATUS") or "").lower() or None,
            "organizer": _one(node, "ORGANIZER"),
            "attendees": node.get("_raw", {}).get("ATTENDEE", []),
            "htmlLink": _one(node, "URL"),
            "updated": node.get("_raw", {}).get("LAST-MODIFIED", [None])[0]
                       or _one(node, "DTSTAMP"),
            "recurrence": [f"RRULE:{rule}"] if rule else None,
            "start": {"date": began} if all_day else {"dateTime": began,
                                                      "timeZone": node.get("_tzid")},
            "end": ({"date": ended} if all_day else {"dateTime": ended}) if ended else None,
        })
    return out
