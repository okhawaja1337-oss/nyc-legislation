#!/usr/bin/env python3
"""
Live feeds.

Every connector here is read-only, cached with a TTL, and degrades to the
cached copy when the network is unavailable -- the office must be able to
work on a train to Albany. Nothing here fabricates: a feed either returns
real rows or reports that it could not reach the source.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from ..core.config import (CACHE_DIR, GPP_BASE, LEGISTAR_BASE, OPEN_DATASETS,
                           SOCRATA_APP_TOKEN, SOCRATA_BASE)

UA = "D49-Universe/1.0 (NYC Council District 49; legislative research)"
DEFAULT_TTL = 3600 * 6


@dataclass
class FeedResult:
    ok: bool
    rows: list
    source: str
    url: str
    fetched: str
    cached: bool = False
    error: str | None = None

    def __len__(self) -> int:
        return len(self.rows)


# --------------------------------------------------------------- caching ----
def _cache_path(url: str) -> Path:
    import hashlib
    return CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".json")


def _read_cache(url: str, ttl: int) -> tuple[Any, str] | None:
    p = _cache_path(url)
    if not p.exists():
        return None
    age = time.time() - p.stat().st_mtime
    if ttl >= 0 and age > ttl:
        return None
    try:
        blob = json.loads(p.read_text())
        return blob.get("data"), blob.get("fetched", "")
    except (ValueError, OSError):
        return None


def _write_cache(url: str, data: Any) -> str:
    fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        _cache_path(url).write_text(json.dumps({"fetched": fetched, "data": data}))
    except OSError:
        pass
    return fetched


def get_json(url: str, ttl: int = DEFAULT_TTL, headers: dict | None = None,
             timeout: int = 45) -> tuple[Any, bool, str, str | None]:
    """Fetch JSON with a TTL cache. Returns (data, from_cache, fetched, error)."""
    hit = _read_cache(url, ttl)
    if hit is not None:
        return hit[0], True, hit[1], None

    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json",
                                               **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return data, False, _write_cache(url, data), None
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            TimeoutError, OSError) as exc:
        stale = _read_cache(url, ttl=-1)          # any age beats nothing
        if stale is not None:
            return stale[0], True, stale[1], f"{type(exc).__name__}: {exc}"
        return None, False, "", f"{type(exc).__name__}: {exc}"


# -------------------------------------------------------------- Legistar ----
def legistar(path: str, ttl: int = DEFAULT_TTL, **params) -> FeedResult:
    """Query the NYC Council Legistar Web API."""
    odata = ("filter", "top", "skip", "select", "orderby", "expand")
    qs = {(f"${k}" if k in odata else k): v for k, v in params.items()}
    url = f"{LEGISTAR_BASE}/{path.lstrip('/')}"
    if qs:
        url += "?" + urllib.parse.urlencode(qs)
    data, cached, fetched, err = get_json(url, ttl)
    rows = data if isinstance(data, list) else ([data] if data else [])
    return FeedResult(ok=err is None or bool(rows), rows=rows,
                      source="LEGISTAR", url=url, fetched=fetched,
                      cached=cached, error=err)


def matters_for_year(year: int, top: int = 1000, ttl: int = DEFAULT_TTL) -> FeedResult:
    """Every matter introduced in a given year."""
    flt = (f"MatterIntroDate ge datetime'{year}-01-01' and "
           f"MatterIntroDate lt datetime'{year + 1}-01-01'")
    return legistar("matters", ttl=ttl, filter=flt, top=top,
                    orderby="MatterIntroDate desc")


def matter_sponsors(matter_id: int, ttl: int = DEFAULT_TTL) -> FeedResult:
    return legistar(f"matters/{matter_id}/sponsors", ttl=ttl)


def matter_histories(matter_id: int, ttl: int = DEFAULT_TTL) -> FeedResult:
    return legistar(f"matters/{matter_id}/histories", ttl=ttl)


def upcoming_events(days: int = 30, ttl: int = 1800) -> FeedResult:
    """Committee hearings on the calendar in the next N days."""
    start = date.today().isoformat()
    end = (date.today() + timedelta(days=days)).isoformat()
    flt = f"EventDate ge datetime'{start}' and EventDate le datetime'{end}'"
    return legistar("events", ttl=ttl, filter=flt, orderby="EventDate", top=200)


# --------------------------------------------------------------- Socrata ----
def socrata(dataset: str, ttl: int = DEFAULT_TTL, limit: int = 1000,
            **soql) -> FeedResult:
    """Query a NYC Open Data dataset with SoQL."""
    params = {f"${k}": v for k, v in soql.items()}
    params["$limit"] = limit
    if SOCRATA_APP_TOKEN:
        params["$$app_token"] = SOCRATA_APP_TOKEN
    url = f"{SOCRATA_BASE}/{dataset}.json?" + urllib.parse.urlencode(params)
    data, cached, fetched, err = get_json(url, ttl)
    rows = data if isinstance(data, list) else []
    label = OPEN_DATASETS.get(dataset, (dataset, ""))[0]
    return FeedResult(ok=err is None or bool(rows), rows=rows, source=label,
                      url=url, fetched=fetched, cached=cached, error=err)


def service_requests_311(district: int = 49, days: int = 30,
                         ttl: int = 3600) -> FeedResult:
    """311 complaints in a council district over a recent window.

    This is the closest thing the office has to a live read on what
    constituents are actually angry about this week.
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    where = (f"created_date > '{since}T00:00:00' AND "
             f"city_council_district = '{district}'")
    return socrata("erm2-nwe9", ttl=ttl, limit=5000,
                   select="complaint_type, descriptor, incident_zip, "
                          "created_date, status, agency",
                   where=where)


def complaints_by_type(district: int = 49, days: int = 30,
                       ttl: int = 3600) -> FeedResult:
    """311 grouped by complaint type -- the constituent-demand signal."""
    since = (date.today() - timedelta(days=days)).isoformat()
    where = (f"created_date > '{since}T00:00:00' AND "
             f"city_council_district = '{district}'")
    return socrata("erm2-nwe9", ttl=ttl, limit=500,
                   select="complaint_type, count(unique_key) as n",
                   where=where, group="complaint_type", order="n DESC")


def discretionary_awards(fy: int | None = None, district: int = 49,
                         ttl: int = DEFAULT_TTL) -> FeedResult:
    """The city's own discretionary award tracker, for tie-out against Schedule C."""
    where = f"council_district = '{district}'"
    if fy:
        where += f" AND fiscal_year = '{fy}'"
    return socrata("ujre-m2tj", ttl=ttl, limit=5000, where=where)


def contracts_for_vendor(name: str, ttl: int = DEFAULT_TTL) -> FeedResult:
    """Registered contract awards for a vendor, by name match."""
    safe = name.replace("'", "''")
    return socrata("qyyg-4tf5", ttl=ttl, limit=500,
                   where=f"upper(vendor_name) like upper('%{safe}%')",
                   order="startdate DESC")


# ------------------------------------------------- publications portal ----
def gpp_search(query: str = "", fiscal_years: Iterable[int] = (),
               agencies: Iterable[str] = (), page: int = 1,
               per_page: int = 50, ttl: int = DEFAULT_TTL) -> FeedResult:
    """Search the NYC Government Publications Portal.

    The portal is the city's permanent archive of published agency documents:
    capital project detail by borough, budget books, agency reports. It runs
    Blacklight, so any catalog view also serves JSON.
    """
    params: list[tuple[str, str]] = [
        ("q", query), ("search_field", "all_fields"),
        ("locale", "en"), ("page", str(page)),
        ("per_page", str(per_page)), ("format", "json"),
    ]
    for fy in fiscal_years:
        params.append(("f[fiscal_year_sim][]", str(fy)))
    for ag in agencies:
        params.append(("f[agency_sim][]", str(ag)))

    url = f"{GPP_BASE}/catalog?" + urllib.parse.urlencode(params)
    data, cached, fetched, err = get_json(url, ttl)
    docs: list = []
    if isinstance(data, dict):
        resp = data.get("response") or data
        docs = resp.get("docs") or resp.get("documents") or []
    elif isinstance(data, list):
        docs = data
    return FeedResult(ok=err is None or bool(docs), rows=docs, source="GPP",
                      url=url, fetched=fetched, cached=cached, error=err)


def capital_project_detail(borough: str = "Staten Island",
                           fiscal_years: Iterable[int] = (2026, 2027),
                           ttl: int = DEFAULT_TTL) -> FeedResult:
    """Capital Project Detail Data published for a borough and fiscal years.

    This is where the line-level capital record lives -- the detail behind the
    Section 254 adds and the agency commitments that follow them.
    """
    return gpp_search(f"Capital Project Detail Data {borough}",
                      fiscal_years=fiscal_years, ttl=ttl)


def status() -> list[dict]:
    """Connectivity check across every configured feed."""
    checks = [
        ("Legistar", lambda: legistar("matters", ttl=0, top=1)),
        ("311 Service Requests", lambda: socrata("erm2-nwe9", ttl=0, limit=1)),
        ("Discretionary Awards", lambda: socrata("ujre-m2tj", ttl=0, limit=1)),
        ("Expense Budget", lambda: socrata("mwzb-yiwb", ttl=0, limit=1)),
        ("Capital Commitments", lambda: socrata("2cmn-uidm", ttl=0, limit=1)),
        ("Publications Portal", lambda: gpp_search("capital project detail", ttl=0, per_page=1)),
    ]
    out = []
    for label, fn in checks:
        try:
            res = fn()
            out.append({"feed": label, "ok": bool(res.rows), "rows": len(res.rows),
                        "cached": res.cached, "error": res.error})
        except Exception as exc:                    # a feed must never crash status
            out.append({"feed": label, "ok": False, "rows": 0, "cached": False,
                        "error": f"{type(exc).__name__}: {exc}"})
    return out
