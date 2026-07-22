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


# Council discretionary ("member item") funding — the clearest read on a member's
# fiscal footprint. NYC Open Data publishes it; the resource id and field names have
# shifted across fiscal years, so this is best-effort and degrades to links.
DISCRETIONARY = "https://data.cityofnewyork.us/resource/nsr4-355a.json"
FISCAL_LINKS = {
    "Checkbook NYC (spending)": "https://www.checkbooknyc.com/",
    "Council Finance Division": "https://council.nyc.gov/budget/",
    "Adopted Budget (OMB)": "https://www.nyc.gov/site/omb/publications/publications.page",
    "IBO (independent budget office)": "https://ibo.nyc.ny.us/",
    "Discretionary funding data": "https://data.cityofnewyork.us/City-Government/Local-Law-15-Council-Discretionary-Funding/nsr4-355a",
}


def discretionary_funding(member, token=None, dataset_url=None, sponsor_field=None):
    """Best-effort discretionary-funding rows for a Council member. [] on any failure.

    Returns [{organization, amount, purpose, agency, fiscal_year}] where available.
    Pin an exact source by passing `dataset_url` (a Socrata resource .json URL) and
    `sponsor_field` (the column holding the member's name); otherwise it tries the
    default dataset and a few common field names, since NYC's IDs/fields drift.
    """
    last = (member or "").split()[-1] if member else ""
    if not last:
        return []
    url = (dataset_url or DISCRETIONARY).strip()
    fields = [sponsor_field] if sponsor_field else ["council_member", "sponsor", "councilmember", "member"]
    for field in fields:
        params = {"$where": f"upper({field}) like upper('%{last}%')", "$limit": 200}
        data = _get(url, params, token=token)
        if not data:
            continue
        out = []
        for r in data:
            amt = r.get("amount") or r.get("funding_amount") or r.get("award_amount")
            try:
                amt = float(amt) if amt is not None else None
            except (TypeError, ValueError):
                amt = None
            out.append({
                "organization": r.get("organization_name") or r.get("organization") or r.get("vendor") or "",
                "amount": amt,
                "purpose": r.get("purpose_of_funds") or r.get("purpose") or r.get("description") or "",
                "agency": r.get("agency") or r.get("administering_agency") or "",
                "fiscal_year": r.get("fiscal_year") or r.get("fy") or "",
            })
        if out:
            return out
    return []


# 311 Service Requests — constituent demand by complaint type.
NYC311 = "https://data.cityofnewyork.us/resource/erm2-nwe9.json"


def nyc311_by_type(since, borough=None, token=None, limit=250):
    """311 complaint counts by type since a date (optionally one borough).

    Returns {complaint_type: count}. Empty on failure. `since` is 'YYYY-MM-DD',
    `borough` one of MANHATTAN/BRONX/BROOKLYN/QUEENS/STATEN ISLAND (case-insensitive).
    """
    where = f"created_date > '{since}T00:00:00'"
    if borough:
        where += f" and upper(borough)='{borough.strip().upper()}'"
    params = {"$select": "complaint_type,count(1)", "$where": where,
              "$group": "complaint_type", "$order": "count_1 DESC", "$limit": limit}
    data = _get(NYC311, params, token=token)
    out = {}
    for row in data or []:
        t = (row.get("complaint_type") or "").strip()
        c = row.get("count_1") or row.get("count")
        if not t or c is None:
            continue
        try:
            out[t] = int(c)
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
