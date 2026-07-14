#!/usr/bin/env python3
"""
sources/opendata.py — grounded figures from NYC Open Data (Socrata).

The point of this module is *sourced facts for political communications*: a
statement should be able to cite "N rape complaints reported citywide, Jan 1–Jun
30 2026 (source: NYPD Complaint Data, NYC Open Data)" rather than a number
someone half-remembers. Every figure comes back with the dataset and date window
attached so it can be cited honestly — and so staff can verify it.

Scope note: these are POLICE COMPLAINT/REPORT counts (what was reported to NYPD),
not convictions or victimization surveys, and recent periods are provisional and
revised. The UI and the citation string say so. No key required; an optional
Socrata app token raises the rate limit.

Defensive throughout — any failure returns empty, never raises into the app.
"""

import time

try:
    import requests
except ImportError:
    requests = None

# NYPD Complaint Data — Historic (all years, report-date field `rpt_dt`).
HISTORIC = "https://data.cityofnewyork.us/resource/qgea-i56i.json"
# NYPD Complaint Data — Current Year To Date (fresher for the current year).
CURRENT_YTD = "https://data.cityofnewyork.us/resource/5uac-w243.json"

# A friendly label -> the substring(s) that appear in the dataset's `ofns_desc`.
CATEGORY_MATCH = {
    "Rape": ["RAPE"],
    "Sex crimes (other)": ["SEX CRIMES"],
    "Murder / manslaughter": ["MURDER", "HOMICIDE"],
    "Felony assault": ["FELONY ASSAULT"],
    "Robbery": ["ROBBERY"],
    "Burglary": ["BURGLARY"],
    "Grand larceny": ["GRAND LARCENY"],
    "Grand larceny of vehicle": ["GRAND LARCENY OF MOTOR VEHICLE"],
}


def _get(url, params, token=None, timeout=40):
    if not requests:
        return None
    headers = {"X-App-Token": token} if token else {}
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            time.sleep(min(2 ** attempt, 8))
        except requests.exceptions.RequestException:
            time.sleep(min(2 ** attempt, 8))
    return None


def complaint_counts(since, until, dataset="historic", token=None):
    """Counts by offense description for report-dates in [since, until].

    since/until are 'YYYY-MM-DD'. Returns {ofns_desc: count} (may be empty).
    """
    url = CURRENT_YTD if dataset == "current" else HISTORIC
    where = (f"rpt_dt between '{since}T00:00:00' and '{until}T23:59:59'")
    params = {"$select": "ofns_desc,count(1)", "$where": where,
              "$group": "ofns_desc", "$order": "count_1 DESC", "$limit": 500}
    data = _get(url, params, token=token)
    out = {}
    for row in data or []:
        desc = (row.get("ofns_desc") or "").strip()
        cnt = row.get("count_1") or row.get("count") or row.get("count_ofns_desc")
        if not desc or cnt is None:
            continue
        try:
            out[desc] = int(cnt)
        except (TypeError, ValueError):
            continue
    return out


def crime_snapshot(since, until, categories=None, dataset="historic", token=None):
    """Roll raw offense counts up into friendly categories, each with a citation.

    Returns a list of {category, count, window, source, citation} dicts. Empty on
    failure. `categories` optionally restricts to a subset of CATEGORY_MATCH keys.
    """
    raw = complaint_counts(since, until, dataset=dataset, token=token)
    if not raw:
        return []
    src = ("NYPD Complaint Data (Year To Date)" if dataset == "current"
           else "NYPD Complaint Data (Historic)")
    wanted = categories or list(CATEGORY_MATCH.keys())
    rows = []
    for cat in wanted:
        needles = CATEGORY_MATCH.get(cat, [cat.upper()])
        total = sum(c for desc, c in raw.items()
                    if any(n in desc.upper() for n in needles))
        window = f"{since} to {until}"
        rows.append({
            "category": cat, "count": total, "window": window, "source": src,
            "citation": (f"{total:,} {cat.lower()} complaints reported citywide, "
                         f"{since} to {until} (source: {src}, NYC Open Data; "
                         f"provisional, subject to revision)"),
        })
    return rows
