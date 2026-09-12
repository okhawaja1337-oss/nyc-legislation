#!/usr/bin/env python3
"""
Funding pattern intelligence.

Six fiscal years of Schedule C, the in-year Transparency Resolutions, and the
office's own MOCS pipeline, read together. The questions this answers are the
ones that actually decide a discretionary cycle:

  * Who did we fund every year, and who fell off?
  * Where is District 49 concentrated, and is that concentration a risk?
  * How does the North Shore compare per-resident with the rest of the Island
    and the rest of the city?
  * What is still sitting in the MOCS pipeline, and what is exposed?
  * Does our ledger foot to the adopted books?

Every number here is computed from the lake and carries the source it came
from. Nothing is estimated unless the function says ``estimate`` in its name.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..core.config import DISTRICT, FISCAL_YEARS, PILLARS, SI_DISTRICTS
from ..core.store import Store

ADOPTED = "'adopted'"


# ------------------------------------------------------------- utilities ----
def _rows(store: Store, sql: str, params: Iterable = ()) -> list[dict]:
    return [dict(r) for r in store.q(sql, list(params))]


def pct(a: float | None, b: float | None) -> float | None:
    if not b:
        return None
    return round(100.0 * (a or 0) / b, 2)


def growth(first: float | None, last: float | None) -> float | None:
    if not first:
        return None
    return round(100.0 * ((last or 0) - first) / abs(first), 1)


# ------------------------------------------------------- member portfolio ----
def member_portfolio(store: Store, member: str, fy: int | None = None) -> dict:
    """What a member funded, by pot and by pillar, for one year or all years."""
    where = "member = ? AND source_id = 'SCHEDULE_C'"
    params: list[Any] = [member]
    if fy:
        where += " AND fy = ?"
        params.append(fy)

    by_pot = _rows(store, f"""
        SELECT pot, COUNT(*) AS lines, SUM(amount) AS total
        FROM funding WHERE {where}
        GROUP BY pot ORDER BY total DESC
    """, params)
    by_pillar = _rows(store, f"""
        SELECT COALESCE(pillar, 'unclassified') AS pillar,
               COUNT(*) AS lines, SUM(amount) AS total
        FROM funding WHERE {where}
        GROUP BY pillar ORDER BY total DESC
    """, params)
    by_agency = _rows(store, f"""
        SELECT COALESCE(NULLIF(agency,''),'(none)') AS agency,
               COUNT(*) AS lines, SUM(amount) AS total
        FROM funding WHERE {where}
        GROUP BY agency ORDER BY total DESC LIMIT 15
    """, params)
    top_orgs = _rows(store, f"""
        SELECT org_key, MAX(org) AS org, MAX(ein) AS ein,
               COUNT(*) AS lines, SUM(amount) AS total
        FROM funding WHERE {where}
        GROUP BY org_key ORDER BY total DESC LIMIT 20
    """, params)
    total = sum(r["total"] or 0 for r in by_pot)

    for coll in (by_pot, by_pillar, by_agency, top_orgs):
        for r in coll:
            r["share_pct"] = pct(r["total"], total)

    return {
        "member": member, "fy": fy, "total": total,
        "lines": sum(r["lines"] for r in by_pot),
        "by_pot": by_pot, "by_pillar": by_pillar,
        "by_agency": by_agency, "top_orgs": top_orgs,
        "source_id": "SCHEDULE_C",
    }


def pillar_drift(store: Store, member: str,
                 years: Iterable[int] = FISCAL_YEARS) -> dict:
    """How a member's priority mix moved across fiscal years.

    Drift is the story a budget director has to be able to tell in one
    sentence: 'we moved a point and a half out of culture into public safety'.
    """
    years = list(years)
    rows = _rows(store, """
        SELECT fy, COALESCE(pillar,'unclassified') AS pillar, SUM(amount) AS total
        FROM funding
        WHERE member = ? AND source_id = 'SCHEDULE_C'
        GROUP BY fy, pillar
    """, [member])

    totals: dict[int, float] = {}
    grid: dict[str, dict[int, float]] = {}
    for r in rows:
        totals[r["fy"]] = totals.get(r["fy"], 0) + (r["total"] or 0)
        grid.setdefault(r["pillar"], {})[r["fy"]] = r["total"] or 0

    out = []
    for pillar, byfy in grid.items():
        shares = {fy: pct(byfy.get(fy, 0), totals.get(fy)) for fy in years
                  if totals.get(fy)}
        present = [fy for fy in years if fy in shares and shares[fy] is not None]
        if not present:
            continue
        first, last = present[0], present[-1]
        out.append({
            "pillar": pillar,
            "label": PILLARS.get(pillar, {}).get("label", pillar.replace("_", " ").title()),
            "shares": shares,
            "first_fy": first, "last_fy": last,
            "share_change_pts": (None if shares[first] is None or shares[last] is None
                                 else round(shares[last] - shares[first], 2)),
            "dollar_change": round((byfy.get(last, 0) - byfy.get(first, 0)), 2),
        })
    out.sort(key=lambda r: abs(r["share_change_pts"] or 0), reverse=True)
    return {"member": member, "totals_by_fy": totals, "drift": out,
            "source_id": "SCHEDULE_C"}


# ----------------------------------------------------- organization churn ----
def org_trajectories(store: Store, member: str | None = None,
                     district: int | None = None,
                     years: Iterable[int] = FISCAL_YEARS) -> dict:
    """Classify every funded organization: sustained, new, lapsed, growing, cut.

    The lapse list is the one that matters politically -- an organization that
    was funded for four years and is not in this year's book will call, and
    the office should know before they do.
    """
    years = sorted(years)
    last_fy = years[-1]
    where = "source_id = 'SCHEDULE_C'"
    params: list[Any] = []
    if member:
        where += " AND member = ?"
        params.append(member)
    if district is not None:
        where += " AND (district = ? OR in_d49 = 1)" if district == 49 else " AND district = ?"
        params.append(district)

    rows = _rows(store, f"""
        SELECT org_key, MAX(org) AS org, MAX(ein) AS ein, fy, SUM(amount) AS total
        FROM funding WHERE {where}
        GROUP BY org_key, fy
    """, params)

    byorg: dict[str, dict] = {}
    for r in rows:
        o = byorg.setdefault(r["org_key"], {"org": r["org"], "ein": r["ein"],
                                            "by_fy": {}})
        o["by_fy"][r["fy"]] = (o["by_fy"].get(r["fy"], 0) + (r["total"] or 0))

    sustained, new, lapsed, growing, cut = [], [], [], [], []
    for key, o in byorg.items():
        fys = sorted(o["by_fy"])
        rec = {"org_key": key, "org": (o["org"] or "").split(" - ")[0],
               "ein": o["ein"], "years_funded": len(fys),
               "first_fy": fys[0], "last_fy": fys[-1],
               "by_fy": o["by_fy"],
               "total": round(sum(o["by_fy"].values()), 2)}
        latest = o["by_fy"].get(last_fy)
        prior_years = [fy for fy in fys if fy < last_fy]
        prior = o["by_fy"].get(prior_years[-1]) if prior_years else None
        rec["latest"] = latest
        rec["prior"] = prior
        rec["change"] = (None if latest is None or prior is None
                         else round(latest - prior, 2))
        rec["change_pct"] = growth(prior, latest) if prior else None

        if latest is None and prior_years:
            rec["status"] = "lapsed"
            rec["risk"] = "call expected" if len(fys) >= 3 else "low"
            lapsed.append(rec)
        elif len(fys) == 1 and fys[0] == last_fy:
            rec["status"] = "new"
            new.append(rec)
        else:
            rec["status"] = "sustained"
            sustained.append(rec)
            if rec["change"] and rec["change"] > 0:
                growing.append(rec)
            elif rec["change"] and rec["change"] < 0:
                cut.append(rec)

    key_total = lambda r: -(r["total"] or 0)
    return {
        "member": member, "district": district, "last_fy": last_fy,
        "counts": {"sustained": len(sustained), "new": len(new),
                   "lapsed": len(lapsed), "growing": len(growing), "cut": len(cut)},
        "lapsed": sorted(lapsed, key=key_total)[:40],
        "new": sorted(new, key=key_total)[:40],
        "growing": sorted(growing, key=lambda r: -(r["change"] or 0))[:25],
        "cut": sorted(cut, key=lambda r: (r["change"] or 0))[:25],
        "source_id": "SCHEDULE_C",
    }


def concentration(store: Store, member: str, fy: int) -> dict:
    """How concentrated a member's money is, via Herfindahl-Hirschman index.

    A high HHI means a handful of organizations carry the district's whole
    program: efficient to administer, fragile if one loses its 501(c)(3) or
    fails a MOCS review.
    """
    rows = _rows(store, """
        SELECT org_key, MAX(org) AS org, SUM(amount) AS total
        FROM funding
        WHERE member = ? AND fy = ? AND source_id = 'SCHEDULE_C'
        GROUP BY org_key ORDER BY total DESC
    """, [member, fy])
    total = sum(r["total"] or 0 for r in rows) or 1
    shares = [(r["total"] or 0) / total for r in rows]
    hhi = round(sum(s * s for s in shares) * 10000, 1)
    top5 = round(100 * sum(shares[:5]), 1)
    top10 = round(100 * sum(shares[:10]), 1)
    return {
        "member": member, "fy": fy, "orgs": len(rows), "total": total,
        "hhi": hhi, "top5_share_pct": top5, "top10_share_pct": top10,
        "reading": ("highly concentrated" if hhi > 2500 else
                    "moderately concentrated" if hhi > 1500 else "diffuse"),
        "effective_orgs": round(1 / sum(s * s for s in shares), 1) if shares else 0,
        "top": [{"org": (r["org"] or "").split(" - ")[0], "total": r["total"],
                 "share_pct": pct(r["total"], total)} for r in rows[:10]],
        "source_id": "SCHEDULE_C",
    }


# ------------------------------------------------------- district compare ----
def district_equity(store: Store, fy: int,
                    focus: int = DISTRICT) -> dict:
    """Per-resident discretionary funding by council district.

    The parity argument Staten Island makes has to survive contact with the
    denominator. This computes dollars per resident using the district
    population the Council Record carries, and ranks every district.
    """
    denoms = store.get_meta("council_record.district_denoms") or {}
    pop: dict[int, int] = {}
    if isinstance(denoms, dict) and "cols" in denoms:
        cols = denoms["cols"]
        di, pi = cols.index("district"), cols.index("population")
        for row in denoms["rows"]:
            pop[int(row[di])] = row[pi]

    rows = _rows(store, """
        SELECT district, COUNT(*) AS lines, SUM(amount) AS total
        FROM funding
        WHERE fy = ? AND source_id = 'SCHEDULE_C' AND district IS NOT NULL
        GROUP BY district
    """, [fy])

    table = []
    for r in rows:
        d = r["district"]
        p = pop.get(d)
        table.append({"district": d, "lines": r["lines"], "total": r["total"],
                      "population": p,
                      "per_resident": round((r["total"] or 0) / p, 2) if p else None})
    ranked = sorted([t for t in table if t["per_resident"] is not None],
                    key=lambda t: -t["per_resident"])
    for i, t in enumerate(ranked, 1):
        t["rank"] = i

    vals = [t["per_resident"] for t in ranked]
    median = (sorted(vals)[len(vals) // 2] if vals else None)
    me = next((t for t in ranked if t["district"] == focus), None)
    si = [t for t in ranked if t["district"] in SI_DISTRICTS]

    return {
        "fy": fy, "focus_district": focus, "focus": me,
        "median_per_resident": median,
        "focus_vs_median_pct": (round(100 * (me["per_resident"] / median - 1), 1)
                                if me and median else None),
        "staten_island": si,
        "si_mean_per_resident": (round(sum(t["per_resident"] for t in si) / len(si), 2)
                                 if si else None),
        "top5": ranked[:5], "bottom5": ranked[-5:],
        "all": ranked,
        "source_id": "SCHEDULE_C",
        "population_source": "COUNCIL_RECORD district_denoms",
        "caveat": ("Per-resident figures cover member designations that name a "
                   "council district in the Schedule C line. Citywide initiative "
                   "dollars that land in a district are not attributed here."),
    }


# --------------------------------------------------------- pipeline risk ----
def pipeline_risk(store: Store, fy: int, member: str | None = None) -> dict:
    """What is still moving through MOCS, and what is exposed.

    A designation is not money until MOCS clears it. This is the office's
    exposure report: what is pending, what was defunded, and which
    organizations have a history of clearing late.
    """
    where = "source_id = 'D49_MOCS_TRACKER' AND fy = ?"
    params: list[Any] = [fy]
    if member:
        where += " AND member = ?"
        params.append(member)

    by_status = _rows(store, f"""
        SELECT status, COUNT(*) AS lines, SUM(amount) AS total
        FROM funding WHERE {where} GROUP BY status ORDER BY total DESC
    """, params)
    pending = _rows(store, f"""
        SELECT org, ein, pot, amount, analyst, mocs_id, purpose
        FROM funding WHERE {where} AND status IN ('pending MOCS','in pipeline','on hold')
        ORDER BY amount DESC
    """, params)
    defunded = _rows(store, f"""
        SELECT org, ein, pot, amount, analyst, mocs_id
        FROM funding WHERE {where} AND status IN ('defunded','denied','withdrawn')
        ORDER BY amount DESC
    """, params)

    # An organization that was defunded or denied in any prior year is a
    # standing risk on a new designation.
    prior_trouble = _rows(store, """
        SELECT DISTINCT org_key, MAX(org) AS org, MAX(fy) AS fy, MAX(status) AS status
        FROM funding
        WHERE source_id = 'D49_MOCS_TRACKER'
          AND status IN ('defunded','denied','withdrawn')
        GROUP BY org_key
    """)
    trouble_keys = {r["org_key"] for r in prior_trouble}
    flagged = [p for p in pending
               if store.scalar("SELECT org_key FROM funding WHERE org = ? LIMIT 1",
                               [p["org"]]) in trouble_keys]

    by_analyst = _rows(store, f"""
        SELECT COALESCE(NULLIF(analyst,''),'(unassigned)') AS analyst,
               COUNT(*) AS lines, SUM(amount) AS total
        FROM funding WHERE {where} GROUP BY analyst ORDER BY lines DESC
    """, params)

    total = sum(r["total"] or 0 for r in by_status)
    pend_total = sum(r["total"] or 0 for r in by_status
                     if r["status"] in ("pending MOCS", "in pipeline", "on hold"))
    return {
        "fy": fy, "member": member, "tracked_total": total,
        "pending_total": pend_total,
        "pending_share_pct": pct(pend_total, total),
        "by_status": by_status, "by_analyst": by_analyst,
        "pending": pending[:60], "defunded": defunded,
        "repeat_risk": flagged[:20],
        "source_id": "D49_MOCS_TRACKER",
        "caveat": ("Tracker rows reflect the office's own workbook export and "
                   "may lag MOCS. Confirm any status before acting on it."),
    }


# --------------------------------------------------------- reconciliation ----
def reconcile_si(store: Store, fy: int = 2027) -> dict:
    """Tie the Staten Island ledger out against the adopted books.

    Reports coverage honestly: if the loaded detail does not foot to the
    stated total, it says by how much rather than quietly reporting the part
    it happens to have.
    """
    meta = store.get_meta(f"si_rollup.fy{fy}") or {}
    totals = meta.get("totals") or {}
    rollup = meta.get("rollup") or []

    loaded = store.one("""
        SELECT COUNT(*) AS lines, SUM(amount) AS total
        FROM funding WHERE source_id = 'SI_ROLLUP' AND fy = ?
    """, [fy])
    loaded_lines = loaded["lines"] if loaded else 0
    loaded_total = loaded["total"] if loaded else 0

    stated_lines = totals.get("lines")
    stated_total = totals.get("adopted")

    rollup_total = sum((r.get("adopted") or 0) for r in rollup)
    rollup_lines = sum(int(r.get("lines") or 0) for r in rollup)

    return {
        "fy": fy,
        "stated": {"lines": stated_lines, "adopted": stated_total,
                   "tr_movement": totals.get("tr_movement"),
                   "note": totals.get("note")},
        "rollup_sum": {"lines": rollup_lines, "adopted": rollup_total,
                       "pots": len(rollup)},
        "loaded_detail": {"lines": loaded_lines, "total": loaded_total},
        "rollup_vs_stated": {
            "lines_delta": (rollup_lines - stated_lines) if stated_lines else None,
            "dollar_delta": (round(rollup_total - stated_total, 2)
                             if stated_total else None),
            "foots": (abs(rollup_total - (stated_total or 0)) < 1
                      if stated_total else None),
        },
        "detail_coverage": {
            "lines_pct": pct(loaded_lines, stated_lines),
            "dollars_pct": pct(loaded_total, stated_total),
            "missing_lines": (stated_lines - loaded_lines) if stated_lines else None,
            "missing_dollars": (round((stated_total or 0) - (loaded_total or 0), 2)
                                if stated_total else None),
        },
        "by_channel": _rows(store, """
            SELECT channel, COUNT(*) AS lines, SUM(amount) AS total
            FROM funding WHERE source_id = 'SI_ROLLUP' AND fy = ?
            GROUP BY channel
        """, [fy]),
        "source_id": "SI_ROLLUP",
        "reading": ("Detail is partial -- the rollup totals are authoritative; "
                    "reload the full ledger export before citing line counts."
                    if stated_lines and loaded_lines < stated_lines
                    else "Detail foots to the stated total."),
    }


# ------------------------------------------------------------- citywide ----
def citywide_context(store: Store, years: Iterable[int] = FISCAL_YEARS) -> dict:
    """The trend line every budget conversation starts from."""
    summary = store.get_meta("schedule_c.summary") or {}
    series = _rows(store, """
        SELECT series, fy, value, unit, label FROM fiscal_series
        ORDER BY series, fy
    """)
    rows = []
    for fy in sorted(int(k) for k in summary):
        s = summary[str(fy)] if str(fy) in summary else summary.get(fy, {})
        rows.append({"fy": fy, **{k: s.get(k) for k in
                                  ("n", "total", "member_total", "citywide_total",
                                   "roster_size")}})
    first = rows[0] if rows else {}
    last = rows[-1] if rows else {}
    return {
        "by_fy": rows,
        "growth": {
            "total_pct": growth(first.get("total"), last.get("total")),
            "member_pct": growth(first.get("member_total"), last.get("member_total")),
            "citywide_pct": growth(first.get("citywide_total"),
                                   last.get("citywide_total")),
            "span": f"FY{first.get('fy')}-FY{last.get('fy')}" if rows else None,
        },
        "series": series,
        "forecast": store.get_meta("forecast.tax") or {},
        "transparency": store.get_meta("transparency.summary") or {},
        "source_id": "SCHEDULE_C",
    }
