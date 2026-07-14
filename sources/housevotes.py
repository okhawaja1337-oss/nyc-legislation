#!/usr/bin/env python3
"""
sources/housevotes.py — U.S. House roll-call votes from the House Clerk.

The Clerk publishes every recorded floor vote as XML at a stable, key-less URL:
    https://clerk.house.gov/evs/{year}/roll{NNN}.xml
Each vote lists every member's position, and — usefully — the `name-id`
attribute IS the member's bioguide id, so we can filter straight to NYC's
delegation without any name-matching guesswork.

Given a year + roll number we return the vote's question, result, totals, and
each recorded position. `delegation_positions` then narrows to NYC's members.

Everything is defensive: unreachable or malformed XML returns None/empty rather
than raising, matching the rest of the app.
"""

import xml.etree.ElementTree as ET

try:
    import requests
except ImportError:
    requests = None


def _fetch_xml(url, timeout=25):
    if not requests:
        return None
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code == 200 and r.text.strip().startswith("<"):
            return r.text
    except Exception:
        return None
    return None


def roll_vote(year, number):
    """Fetch + parse one House roll-call. Returns a dict or None."""
    try:
        n = int(number)
    except (TypeError, ValueError):
        return None
    url = f"https://clerk.house.gov/evs/{int(year)}/roll{n:03d}.xml"
    xml = _fetch_xml(url)
    if not xml:
        return None
    return parse_roll_vote(xml, source_url=url)


def parse_roll_vote(xml_text, source_url=""):
    """Parse House Clerk roll-call XML text into a normalized dict."""
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return None
    meta = root.find("vote-metadata")
    if meta is None:
        return None

    def mt(tag):
        el = meta.find(tag)
        return (el.text or "").strip() if el is not None and el.text else ""

    totals = {}
    tv = meta.find("vote-totals/totals-by-vote")
    if tv is not None:
        for child in tv:
            label = child.tag.replace("-total", "").replace("-", " ").title()
            totals[label] = (child.text or "").strip()

    positions = []
    for rv in root.findall("vote-data/recorded-vote"):
        leg = rv.find("legislator")
        vote = rv.find("vote")
        if leg is None:
            continue
        positions.append({
            "bioguide": leg.get("name-id", ""),
            "name": (leg.text or leg.get("unaccented-name", "")).strip(),
            "party": leg.get("party", ""),
            "state": leg.get("state", ""),
            "vote": (vote.text or "").strip() if vote is not None else "",
        })

    return {
        "level": "U.S. House",
        "congress": mt("congress"),
        "session": mt("session"),
        "roll": mt("rollcall-num"),
        "bill": mt("legis-num"),
        "question": mt("vote-question"),
        "result": mt("vote-result"),
        "date": mt("action-date"),
        "description": mt("vote-desc"),
        "totals": totals,
        "positions": positions,
        "source_url": source_url,
    }


def delegation_positions(vote, delegation):
    """Filter a parsed vote's positions to NYC's delegation (by bioguide).

    `delegation` is a list of federal profile dicts (each with extra.bioguide).
    Returns (rows, tally) where rows are per-member and tally counts the votes.
    """
    if not vote:
        return [], {}
    bios = {}
    for d in delegation or []:
        bg = (d.get("extra") or {}).get("bioguide") or d.get("bioguide")
        if bg:
            bios[bg] = d
    rows, tally = [], {}
    for p in vote.get("positions", []):
        d = bios.get(p["bioguide"])
        if not d:
            continue
        rows.append({"Member": d.get("name", p["name"]), "Seat": d.get("seat", ""),
                     "Party": d.get("party", p["party"]), "Vote": p["vote"]})
        tally[p["vote"]] = tally.get(p["vote"], 0) + 1
    rows.sort(key=lambda r: (r["Seat"] or "", r["Member"]))
    return rows, tally
