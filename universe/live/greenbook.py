#!/usr/bin/env python3
"""
The Green Book — the city's official directory.

When a constituent calls about a collapsed sidewalk, a stalled Section 8
recertification, or a father who cannot get a death certificate, the office's
job is to route them to the person who can actually act. The Green Book is the
authoritative map of who that is: every city agency, its senior officials,
addresses and phone numbers, plus the borough, state and federal offices that
serve the city.

This module pulls that directory into the lake, tags each office with the
constituent problems it owns, and answers the real question -- "who do I send
this to" -- as a ranked referral rather than a search box.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from typing import Any, Iterable

from ..core.config import DISTRICT, D49_ZIPS
from ..core.store import Store
from .feeds import UA, FeedResult, get_json

GREENBOOK_BASE = "https://a856-gbol.nyc.gov/GBOLWebsite/GreenBook"
SECTIONS = ("City", "Borough", "State", "Federal", "Public", "Community")

# What each agency actually gets called about. This is the routing table the
# office works from; the Green Book supplies the contact, this supplies the
# reason a staffer would reach for it.
SERVICE_AREAS: dict[str, tuple[str, ...]] = {
    "HPD": ("housing code violations", "heat and hot water", "Section 8",
            "affordable housing lotteries", "landlord harassment"),
    "DOB": ("construction permits", "illegal conversions", "stop work orders",
            "facade and scaffolding", "elevator complaints"),
    "DOT": ("potholes", "street resurfacing", "traffic signals", "bus stops",
            "sidewalk repair", "street lights", "parking regulations"),
    "DSNY": ("missed collection", "illegal dumping", "street cleaning",
             "containerization", "snow removal"),
    "DPR": ("park maintenance", "street trees", "playgrounds", "tree pruning",
            "greenstreets"),
    "NYPD": ("precinct concerns", "quality of life enforcement",
             "traffic safety", "community affairs"),
    "FDNY": ("fire safety inspections", "EMS response", "hydrants"),
    "DOE": ("school placement", "IEP and special education", "busing",
            "school capital projects"),
    "DFTA": ("older adult centers", "home-delivered meals", "caregiver support",
             "benefits screening"),
    "HRA": ("SNAP", "cash assistance", "Medicaid", "rental arrears",
            "one-shot deals"),
    "DHS": ("shelter placement", "street homelessness outreach"),
    "DOHMH": ("restaurant grades", "rodent complaints", "lead paint",
              "mental health services", "immunization records"),
    "ACS": ("child welfare", "child care vouchers", "foster care"),
    "SBS": ("small business support", "commercial leases", "MWBE certification",
            "workforce1"),
    "DCLA": ("cultural organization funding", "public art"),
    "DEP": ("water bills", "sewer backups", "catch basins", "noise complaints"),
    "TLC": ("for-hire vehicle licensing", "driver complaints"),
    "DCWP": ("consumer complaints", "paid sick leave", "licensing"),
    "MOIA": ("immigration legal services", "IDNYC"),
    "DORIS": ("vital records", "municipal archives"),
    "BOE": ("voter registration", "poll sites", "ballot questions"),
}


def _cid(*parts: Any) -> str:
    return "GB" + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:14]


def service_area_for(agency: str, office: str = "") -> str | None:
    """Tag an office with the constituent problems it owns."""
    blob = f"{agency} {office}".upper()
    for code, areas in SERVICE_AREAS.items():
        if re.search(rf"\b{re.escape(code)}\b", blob):
            return "; ".join(areas)
    low = blob.lower()
    for code, areas in SERVICE_AREAS.items():
        if any(a.split()[0] in low for a in areas):
            return "; ".join(areas)
    return None


# ------------------------------------------------------------- fetching ----
def fetch_section(section: str = "City", ttl: int = 86400) -> FeedResult:
    """Pull one Green Book section.

    The Green Book is an ASP.NET application whose listing views also serve
    JSON. Where it does not, this returns the error rather than scraping
    speculative HTML -- a wrong phone number is worse than no phone number.
    """
    url = f"{GREENBOOK_BASE}/{urllib.parse.quote(section)}"
    data, cached, fetched, err = get_json(url, ttl,
                                          headers={"Accept": "application/json"})
    rows: list = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in ("results", "data", "items", "agencies", "d"):
            if isinstance(data.get(key), list):
                rows = data[key]
                break
    return FeedResult(ok=err is None and bool(rows), rows=rows, source="GREENBOOK",
                      url=url, fetched=fetched, cached=cached, error=err)


FIELD_ALIASES = {
    "agency": ("agency", "agencyName", "AgencyName", "organization", "Department"),
    "office": ("office", "division", "OfficeName", "unit", "Bureau"),
    "title": ("title", "Title", "position", "jobTitle"),
    "person": ("name", "Name", "official", "contactName", "fullName"),
    "email": ("email", "Email", "emailAddress"),
    "phone": ("phone", "Phone", "telephone", "phoneNumber"),
    "address": ("address", "Address", "address1", "streetAddress"),
    "borough": ("borough", "Borough"),
    "zip": ("zip", "Zip", "zipCode", "postalCode"),
    "url": ("url", "Url", "website", "link"),
}


def _pick(row: dict, key: str) -> Any:
    for alias in FIELD_ALIASES.get(key, ()):
        if row.get(alias) not in (None, ""):
            return row[alias]
    return None


def normalize(rows: Iterable[dict], level: str = "city") -> list[dict]:
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        agency = _pick(r, "agency")
        office = _pick(r, "office")
        if not (agency or office):
            continue
        zipc = str(_pick(r, "zip") or "")[:5] or None
        out.append({
            "contact_id": _cid(level, agency, office, _pick(r, "person"),
                               _pick(r, "title")),
            "level": level,
            "agency": agency, "office": office,
            "title": _pick(r, "title"), "person": _pick(r, "person"),
            "email": _pick(r, "email"), "phone": _pick(r, "phone"),
            "address": _pick(r, "address"),
            "borough": _pick(r, "borough"),
            "district": DISTRICT if zipc in D49_ZIPS else None,
            "zip": zipc,
            "service_area": service_area_for(str(agency or ""), str(office or "")),
            "parent": None,
            "url": _pick(r, "url"),
            "source_id": "GREENBOOK",
            "updated": None,
        })
    return out


def ingest(store: Store, sections: Iterable[str] = SECTIONS) -> dict:
    """Load the Green Book into the contacts table."""
    total, errors = 0, {}
    for sec in sections:
        res = fetch_section(sec)
        if not res.ok:
            errors[sec] = res.error or "no rows returned"
            continue
        rows = normalize(res.rows, level=sec.lower())
        total += store.upsert("contacts", rows)
        store.index_many([
            ("contact", r["contact_id"],
             " — ".join(x for x in (r["agency"], r["office"], r["person"]) if x),
             " ".join(str(x) for x in (r["title"], r["address"], r["phone"],
                                       r["email"]) if x),
             r["service_area"] or "")
            for r in rows])
    store.journal("ingest.greenbook", {"contacts": total, "errors": errors})
    return {"contacts": total, "errors": errors,
            "note": ("Sections that returned no rows are listed in errors; the "
                     "Green Book must be reachable from this network.")}


# ------------------------------------------------------------- referral ----
def refer(store: Store, problem: str, limit: int = 8) -> dict:
    """Answer 'who do I send this constituent to'.

    Searches the service-area routing table first -- because a caller says
    "there's no heat", not "I need HPD" -- then falls back to full text over
    the whole directory.
    """
    low = problem.lower()
    matches: list[dict] = []
    for code, areas in SERVICE_AREAS.items():
        score = sum(1 for a in areas if a in low)
        score += sum(1 for a in areas for w in a.split() if len(w) > 4 and w in low)
        if score:
            rows = [dict(r) for r in store.q(
                "SELECT * FROM contacts WHERE agency LIKE ? OR office LIKE ? LIMIT 5",
                [f"%{code}%", f"%{code}%"])]
            matches.append({"agency_code": code, "why": areas, "score": score,
                            "contacts": rows})
    matches.sort(key=lambda m: -m["score"])

    fts = [dict(r) for r in store.search(problem, "contact", limit=limit)]
    return {
        "problem": problem,
        "routes": matches[:limit],
        "directory_hits": fts,
        "source_id": "GREENBOOK",
        "fallback": ("If no route matches, 311 is the system of record: "
                     "https://portal.311.nyc.gov/ — open a service request and "
                     "track the SR number so the office can escalate it."),
        "note": ("Routing is from the office's own service-area table; the "
                 "contact details come from the Green Book. Confirm a direct "
                 "line before giving it to a constituent."),
    }


def directory(store: Store, level: str | None = None,
              agency: str | None = None, limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM contacts WHERE 1=1"
    params: list[Any] = []
    if level:
        sql += " AND level = ?"
        params.append(level)
    if agency:
        sql += " AND (agency LIKE ? OR office LIKE ?)"
        params += [f"%{agency}%", f"%{agency}%"]
    sql += " ORDER BY agency, office LIMIT ?"
    params.append(limit)
    return [dict(r) for r in store.q(sql, params)]
