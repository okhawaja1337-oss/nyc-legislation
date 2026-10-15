#!/usr/bin/env python3
"""
Ingest the money.

Six fiscal years of adopted Schedule C (every discretionary designation the
Council made, by member and initiative), the Section 254 capital adds, the
in-year Transparency Resolutions that move money after adoption, and the
Council's own tax revenue forecast.

Schedule C is the single most important dataset in the office: it is the
authoritative record of who funded whom, for how much, through which pot.
Everything the funding-pattern engine knows, it knows from here.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any, Iterable

from ..core.config import (D49_ZIPS, FISCAL_YEARS, PILLARS, SI_DISTRICTS)
from ..core.store import Store

# Initiative names carry district numbers in the org string, e.g.
# "Access Justice Brooklyn, Inc. - Legal Assistance - Council District 40".
DISTRICT_IN_ORG = re.compile(r"Council District\s+(\d+)", re.I)
ZIP_RE = re.compile(r"\b(1\d{4})\b")


def open_any(path: Path):
    """Open a .json or .json.gz transparently."""
    path = Path(path)
    if path.suffix == ".gz":
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8")
    return path.open(encoding="utf-8")


def find(src_dir: Path, *names: str) -> Path | None:
    for n in names:
        for cand in (src_dir / n, src_dir / (n + ".gz")):
            if cand.exists():
                return cand
    return None


# ----------------------------------------------------------- normalizing ----
def org_key(name: str, ein: str | None = None) -> str:
    """A stable identity for an organization across years and spellings."""
    if ein:
        digits = re.sub(r"\D", "", str(ein))
        if len(digits) == 9:
            return "EIN" + digits
    base = re.sub(r"[^a-z0-9]+", "", (name or "").lower().split(" - ")[0])
    base = re.sub(r"(inc|llc|ltd|corp|thecorporation|the)$", "", base)
    return "ORG" + hashlib.sha1(base.encode()).hexdigest()[:10]


def clean_ein(ein: Any) -> str | None:
    if not ein:
        return None
    digits = re.sub(r"\D", "", str(ein))
    return f"{digits[:2]}-{digits[2:]}" if len(digits) == 9 else None


def pillar_for(*texts: str) -> str | None:
    """Assign a funding line to one of the office's priority pillars."""
    blob = " ".join(t for t in texts if t).lower()
    best, score = None, 0
    for key, spec in PILLARS.items():
        n = sum(1 for k in spec["keywords"] if k.lower() in blob)
        n += 3 * sum(1 for i in spec["initiatives"] if i.lower()[:22] in blob)
        if n > score:
            best, score = key, n
    return best


def _line_id(fy: int, *parts: Any) -> str:
    raw = "|".join(str(p) for p in parts)
    return f"SC{fy}-" + hashlib.sha1(raw.encode()).hexdigest()[:14]


# -------------------------------------------------------------- schedule C ----
def ingest_schedule_c(store: Store, src_dir: Path | str,
                      years: Iterable[int] = FISCAL_YEARS) -> dict:
    """Load every adopted discretionary designation for the given years."""
    src_dir = Path(src_dir)
    rows: list[dict] = []
    series: list[dict] = []
    summary: dict[int, dict] = {}

    with store.tx() as c:
        c.execute("DELETE FROM funding WHERE source_id = 'SCHEDULE_C'")

    for fy in years:
        path = find(src_dir, f"fy{str(fy)[-2:]}_schedule_c.json")
        if not path:
            continue
        with open_any(path) as fh:
            doc = json.load(fh)
        summary[fy] = {k: doc.get(k) for k in
                       ("n", "total", "member_total", "citywide_total", "roster_size")}
        for k, label in (("total", "Schedule C total"),
                         ("member_total", "Member designations"),
                         ("citywide_total", "Citywide initiatives")):
            if doc.get(k) is not None:
                series.append({"series": f"schedule_c.{k}", "label": label,
                               "fy": fy, "value": doc[k], "unit": "usd",
                               "source_id": "SCHEDULE_C"})

        # Schedule C legitimately carries repeated rows -- the same
        # organization can appear twice in one initiative for the same amount.
        # The row ordinal keeps those distinct instead of collapsing them.
        for ordinal, it in enumerate(doc.get("items", [])):
            org = it.get("org") or ""
            ein = clean_ein(it.get("ein"))
            member = (it.get("member") or "").strip()
            dm = DISTRICT_IN_ORG.search(org)
            district = int(dm.group(1)) if dm else None
            zm = ZIP_RE.search(org)
            zipc = zm.group(1) if zm else None
            boro = (it.get("borough") or "").strip().title() or None
            in_d49 = 1 if (zipc in D49_ZIPS or district == 49) else 0
            rows.append({
                "line_id": _line_id(fy, ordinal, member, org, it.get("amount"),
                                    it.get("agency"), it.get("section")),
                "fy": fy,
                "channel": "expense",
                "pot": (it.get("section") or "").strip() or "Local",
                "member": member or None,
                "person_id": None,
                "district": district,
                "borough": boro,
                "org": org,
                "org_key": org_key(org, ein),
                "ein": ein,
                "program": org.split(" - ")[1] if " - " in org else None,
                "agency": it.get("agency"),
                "amount": it.get("amount"),
                "section": it.get("section"),
                "purpose": it.get("purpose"),
                "status": "adopted",
                "mocs_id": None,
                "analyst": None,
                "in_d49": in_d49,
                "pillar": pillar_for(org, it.get("purpose"), it.get("section")),
                "source_id": "SCHEDULE_C",
                "locator": f"FY{fy} Adopted Schedule C",
            })

    n = store.upsert("funding", rows)
    store.upsert("fiscal_series", series)
    store.set_meta("schedule_c.summary", summary)
    store.journal("ingest.schedule_c", {"rows": n, "years": list(summary)})
    return {"funding_rows": n, "years": sorted(summary)}


# ------------------------------------------------------ transparency resos ----
def ingest_transparency(store: Store, src_dir: Path | str) -> dict:
    """Load in-year Transparency Resolution movement (adds, cuts, transfers)."""
    src_dir = Path(src_dir)
    rows: list[dict] = []
    metas: dict[str, Any] = {}

    with store.tx() as c:
        c.execute("DELETE FROM funding WHERE source_id = 'TRANSPARENCY_RESO'")

    for path in sorted(src_dir.glob("fy*_transparency_reso_*.json*")):
        with open_any(path) as fh:
            doc = json.load(fh)
        fy = doc.get("fy")
        reso = doc.get("reso")
        metas[f"TR{reso}"] = {k: doc.get(k) for k in
                              ("title", "reso", "fy", "n", "adds", "cuts", "net", "by_fy")}
        for ordinal, r in enumerate(doc.get("rows", [])):
            org = r.get("org") or ""
            ein = clean_ein(r.get("ein"))
            amt = r.get("amount") or 0
            dm = DISTRICT_IN_ORG.search(org)
            district = int(dm.group(1)) if dm else None
            zm = ZIP_RE.search(org)
            rows.append({
                "line_id": _line_id(r.get("fy") or fy, "TR", reso, ordinal,
                                    r.get("member"), org, amt, r.get("chart")),
                "fy": r.get("fy") or fy,
                "channel": "expense",
                "pot": r.get("chart") or "Local Initiatives",
                "member": (r.get("member") or "").strip() or None,
                "person_id": None,
                "district": district,
                "borough": (r.get("boro") or "").strip().title() or None,
                "org": org,
                "org_key": org_key(org, ein),
                "ein": ein,
                "program": org.split(" - ")[1] if " - " in org else None,
                "agency": r.get("agency"),
                "amount": amt,
                "section": f"Transparency Reso #{reso}",
                "purpose": r.get("flag") or None,
                "status": "TR-add" if amt >= 0 else "TR-cut",
                "mocs_id": None,
                "analyst": None,
                "in_d49": 1 if ((zm and zm.group(1) in D49_ZIPS) or district == 49) else 0,
                "pillar": pillar_for(org, r.get("chart")),
                "source_id": "TRANSPARENCY_RESO",
                "locator": f"FY{fy} Transparency Reso #{reso}, chart {r.get('chart_no')}",
            })

    n = store.upsert("funding", rows)
    store.set_meta("transparency.summary", metas)
    store.journal("ingest.transparency", {"rows": n, "resos": list(metas)})
    return {"tr_rows": n, "resos": list(metas)}


# --------------------------------------------------------------- forecast ----
def ingest_forecast(store: Store, src_dir: Path | str) -> dict:
    """Load the Council Finance Division tax revenue forecast."""
    src_dir = Path(src_dir)
    path = find(src_dir, "nyc_council_tax_forecast_feb2026.json")
    if not path:
        return {"forecast": 0}
    with open_any(path) as fh:
        doc = json.load(fh)

    years = doc.get("years", [])
    series = []
    for i, fy in enumerate(years):
        tot = doc.get("total_forecast", [])
        if i < len(tot) and tot[i] is not None:
            series.append({"series": "revenue.council_forecast",
                           "label": doc.get("label", "Council tax forecast"),
                           "fy": fy, "value": tot[i], "unit": doc.get("unit", "millions"),
                           "source_id": "COUNCIL_BUDGET"})
        vs = doc.get("total_vsomb", [])
        if i < len(vs) and vs[i] is not None:
            series.append({"series": "revenue.council_vs_omb",
                           "label": "Council forecast vs OMB",
                           "fy": fy, "value": vs[i], "unit": doc.get("unit", "millions"),
                           "source_id": "COUNCIL_BUDGET"})

    store.upsert("fiscal_series", series)
    store.set_meta("forecast.tax", {
        "published": doc.get("published"), "label": doc.get("label"),
        "unit": doc.get("unit"), "years": years,
        "highlights": doc.get("highlights", []),
        "by_source": doc.get("forecast", []),
        "vs_omb": doc.get("vsomb", []),
    })
    store.journal("ingest.forecast", {"series": len(series)})
    return {"forecast_series": len(series), "years": years}


# ------------------------------------------------------------------ orgs ----
def rebuild_orgs(store: Store) -> int:
    """Roll funding lines up into an organization ledger."""
    rows = store.q("""
        SELECT org_key,
               MAX(org)           AS name,
               MAX(ein)           AS ein,
               MAX(borough)       AS borough,
               MIN(fy)            AS first_fy,
               MAX(fy)            AS last_fy,
               COUNT(*)           AS n_awards,
               SUM(amount)        AS total_awarded,
               MAX(in_d49)        AS in_d49,
               GROUP_CONCAT(DISTINCT pillar) AS pillars
        FROM funding
        WHERE org_key IS NOT NULL
        GROUP BY org_key
    """)
    orgs = []
    for r in rows:
        name = (r["name"] or "").split(" - ")[0].strip()
        zm = ZIP_RE.search(r["name"] or "")
        orgs.append({
            "org_key": r["org_key"], "name": name, "ein": r["ein"],
            "borough": r["borough"], "zip": zm.group(1) if zm else None,
            "in_d49": r["in_d49"] or 0, "first_fy": r["first_fy"],
            "last_fy": r["last_fy"], "n_awards": r["n_awards"],
            "total_awarded": r["total_awarded"],
            "pillars": r["pillars"], "notes": None,
        })
    n = store.upsert("orgs", orgs)
    store.journal("ingest.orgs", {"orgs": n})
    return n


def ingest_all(store: Store, src_dir: Path | str) -> dict:
    out = {}
    out.update(ingest_schedule_c(store, src_dir))
    out.update(ingest_transparency(store, src_dir))
    out.update(ingest_forecast(store, src_dir))
    out["orgs"] = rebuild_orgs(store)
    return out
