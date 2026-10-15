#!/usr/bin/env python3
"""
Ingest The Council Record.

The Council Record is a single self-contained HTML file carrying a ~7MB JSON
payload: every Council member since 1992, their voting fingerprints, ideal
points, election history, career transitions, and the full 21k-row matter
table. This module lifts that payload out and normalizes it into the lake.

It reads the file; it never depends on the page's markup beyond the one
``<script id="site-data">`` island.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator

from ..core.config import PILLARS
from ..core.store import Store

SITE_DATA_RE = re.compile(
    r'<script id="site-data" type="application/json">(.*?)</script>', re.S)


# --------------------------------------------------------------- reading ----
def load_payload(path: Path | str) -> dict:
    """Pull the embedded JSON island out of a Council Record HTML file."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    m = SITE_DATA_RE.search(text)
    if not m:
        raise ValueError(f"no site-data island found in {path}")
    return json.loads(m.group(1))


def table(payload: dict, key: str) -> Iterator[dict]:
    """Iterate a cols/rows table as dicts."""
    block = payload.get(key)
    if not isinstance(block, dict) or "cols" not in block:
        return iter(())
    cols = block["cols"]
    return ({c: r[i] if i < len(r) else None for i, c in enumerate(cols)}
            for r in block.get("rows", []))


# ------------------------------------------------------------ classifying ----
def pillars_for(text: str) -> list[str]:
    """Tag arbitrary text against the office's five-plus-one priority pillars."""
    low = (text or "").lower()
    hits = []
    for key, spec in PILLARS.items():
        if any(k.lower() in low for k in spec["keywords"]):
            hits.append(key)
    return hits


TOPIC_TO_PILLAR = {
    "arts_culture": "arts_culture",
    "land_use": "neighborhood_development",
    "housing": "neighborhood_development",
    "waterfront": "neighborhood_development",
    "parks": "neighborhood_development",
    "economic_development": "economic_development",
    "small_business": "economic_development",
    "labor": "economic_development",
    "technology": "economic_development",
    "public_safety": "public_safety",
    "civil_rights": "public_safety",
    "health": "health_hospitals",
    "social_services": "health_hospitals",
    "aging": "health_hospitals",
    "youth": "health_hospitals",
    "transportation": "si_parity",
}


# ------------------------------------------------------------- ingesting ----
def ingest(store: Store, path: Path | str, source_id: str = "COUNCIL_RECORD") -> dict:
    """Load a Council Record file into the lake. Returns a count summary."""
    payload = load_payload(path)
    counts: dict[str, int] = {}

    # This ingest owns these tables entirely. Matter ids and sponsorship pairs
    # are not stable across Council Record builds, so appending a newer build
    # on top of an older one silently doubles the record. Replace, don't merge.
    with store.tx() as c:
        c.execute("DELETE FROM sponsorships")
        c.execute("DELETE FROM votes")

    # ---- members -----------------------------------------------------
    members = []
    for r in table(payload, "members"):
        members.append({
            "person_id": r.get("id"),
            "name": r.get("name"),
            "last": r.get("last"),
            "party": r.get("party"),
            "borough": r.get("borough"),
            "district": r.get("district"),
            "current": 1 if r.get("current") else 0,
            "first_seen": r.get("first"),
            "last_seen": r.get("last"),
            "sessions": r.get("sessions"),
            "leadership": r.get("lead_now") or r.get("leadership"),
            "wiki": r.get("wiki"),
            "qid": r.get("qid"),
            "dob": r.get("dob"),
            "gender": r.get("gender"),
            "extra": json.dumps({"lead_hist": r.get("lead_hist"),
                                 "districts": r.get("districts")}),
        })
    counts["members"] = store.upsert("members", members)

    # ---- member metrics: fingerprints, ideal points, forecasts --------
    metrics: list[dict] = []

    def add_metric(pid, metric, value=None, text=None, session="all"):
        if pid is None or (value is None and text is None):
            return
        metrics.append({"person_id": pid, "metric": metric,
                        "value": value if isinstance(value, (int, float)) else None,
                        "text_value": text, "session": session,
                        "source_id": source_id})

    for r in table(payload, "fingerprints"):
        pid = r.get("person_id")
        for k, v in r.items():
            if k == "person_id":
                continue
            if isinstance(v, (int, float)):
                add_metric(pid, k, value=v)
            elif isinstance(v, str) and v:
                add_metric(pid, k, text=v)

    for r in table(payload, "fingerprints_session"):
        pid, sess = r.get("person_id"), r.get("session") or "all"
        for k in ("nay_rate_contested", "alignment_with_speaker", "absence_rate",
                  "n_contested_votes"):
            if isinstance(r.get(k), (int, float)):
                add_metric(pid, k, value=r[k], session=sess)

    for r in table(payload, "ideal_points"):
        pid, era = r.get("person_id"), r.get("era") or "pooled"
        for k in ("dim1", "dim1_lo", "dim1_hi", "dim2", "n_votes_used"):
            if isinstance(r.get(k), (int, float)):
                add_metric(pid, f"ideal_{k}", value=r[k], session=era)

    for r in table(payload, "vote_stats"):
        pid, sess = r.get("id"), r.get("session") or "all"
        for k in ("aye", "nay", "dissent", "particip", "attend", "sponsored",
                  "prim_sponsored", "enacted", "committee", "meetings"):
            if isinstance(r.get(k), (int, float)):
                add_metric(pid, f"vs_{k}", value=r[k], session=sess)

    for r in table(payload, "legis_stats"):
        pid, sess = r.get("id"), r.get("session") or "all"
        for k in ("n_primary", "n_cosponsor", "n_primary_enacted", "rate"):
            if isinstance(r.get(k), (int, float)):
                add_metric(pid, f"leg_{k}", value=r[k], session=sess)

    for r in table(payload, "forecast"):
        pid = r.get("person_id")
        add_metric(pid, "forecast_prob", value=r.get("logit_prob"))
        add_metric(pid, "forecast_base_rate", value=r.get("base_rate_prob"))
        add_metric(pid, "forecast_next_office", text=r.get("most_likely_next_office"))
        add_metric(pid, "forecast_top3", text=r.get("top_3_next_offices"))
        add_metric(pid, "term_limited_year", value=r.get("term_limited_year"))
        add_metric(pid, "tenure_years", value=r.get("tenure_years"))

    for r in table(payload, "publicity"):
        pid = r.get("person_id")
        for k in ("wiki_mean_monthly", "wiki_last12mo", "wiki_total_views",
                  "press_release_mentions_total", "press_release_mentions_last12mo"):
            if isinstance(r.get(k), (int, float)):
                add_metric(pid, k, value=r[k])

    counts["member_metrics"] = store.upsert("member_metrics", metrics)

    # ---- matters (bills, resolutions, land use) -----------------------
    mdict = payload.get("matters_dict", {}) or {}
    status_names = mdict.get("status", [])
    cmte_names = mdict.get("cmte", [])
    type_names = mdict.get("type", []) or []
    topic_names = mdict.get("topics", []) or []

    def deref(idx, names):
        if isinstance(idx, int) and 0 <= idx < len(names):
            return names[idx]
        return idx

    matters = []
    pos_to_id: list[int] = []          # sponsorship_index stores row positions
    for r in table(payload, "matters"):
        pos_to_id.append(r.get("id"))
        topics_raw = r.get("topics")
        topics: list[str] = []
        if isinstance(topics_raw, int) and topic_names:
            # bitmask or index depending on build; handle both defensively
            if 0 <= topics_raw < len(topic_names):
                topics = [topic_names[topics_raw]]
        elif isinstance(topics_raw, list):
            topics = [deref(t, topic_names) for t in topics_raw]
        elif isinstance(topics_raw, str):
            topics = [t.strip() for t in topics_raw.split("|") if t.strip()]

        name = r.get("name") or ""
        pil = sorted({TOPIC_TO_PILLAR[t] for t in topics
                      if isinstance(t, str) and t in TOPIC_TO_PILLAR}
                     | set(pillars_for(name)))
        matters.append({
            "matter_id": r.get("id"),
            "file": r.get("file"),
            "name": name,
            "type": deref(r.get("type"), type_names),
            "status": deref(r.get("status"), status_names),
            "committee": deref(r.get("cmte"), cmte_names),
            "year": r.get("year"),
            "session": r.get("session"),
            "enacted": 1 if r.get("enacted") else 0,
            "local_law": r.get("ll"),
            "prime_id": r.get("pri"),
            "n_sponsors": r.get("ns"),
            "topics": json.dumps(topics),
            "pending": 1 if r.get("pending") else 0,
            "pass_prob": r.get("p"),
            "pillars": json.dumps(pil),
            "source_id": source_id,
        })
    counts["matters"] = store.upsert("matters", matters)

    # ---- sponsorships -------------------------------------------------
    # The Record stores sponsorships as *row positions* into the matter table,
    # not matter ids. Resolve them through the position map built above.
    def resolve(pos: Any) -> int | None:
        if isinstance(pos, int) and 0 <= pos < len(pos_to_id):
            return pos_to_id[pos]
        return None

    spons = []
    for pid, blk in (payload.get("sponsorship_index") or {}).items():
        try:
            pid_i = int(pid)
        except (TypeError, ValueError):
            continue
        for role, key in (("prime", "p"), ("cosponsor", "c")):
            for pos in blk.get(key, []) or []:
                mid = resolve(pos)
                if mid is not None:
                    spons.append({"matter_id": mid, "person_id": pid_i, "role": role})
    # de-dup: prime wins over cosponsor on the same pair
    seen: dict[tuple, dict] = {}
    for s in spons:
        key = (s["matter_id"], s["person_id"])
        if key not in seen or s["role"] == "prime":
            seen[key] = s
    counts["sponsorships"] = store.upsert("sponsorships", list(seen.values()))

    # ---- dissent votes (the contested record) -------------------------
    vrows = []
    for pid, items in (payload.get("dissents") or {}).items():
        try:
            pid_i = int(pid)
        except (TypeError, ValueError):
            continue
        for it in items or []:
            if not isinstance(it, list) or len(it) < 6:
                continue
            ref, title, vdate, aye, nay, topic = it[:6]
            vrows.append({"matter_ref": ref, "person_id": pid_i, "vote": "Nay",
                          "vote_date": vdate, "aye": aye, "nay": nay,
                          "topic": topic, "title": title})
    counts["votes"] = store.upsert("votes", vrows)

    # ---- sources declared by the Record itself ------------------------
    srcs = []
    for r in table(payload, "sources"):
        srcs.append({
            "source_id": r.get("source_id"),
            "name": r.get("name"),
            "publisher": r.get("publisher"),
            "url": r.get("url"),
            "accessed": r.get("accessed"),
            "coverage": r.get("coverage"),
            "tier": _tier(r.get("provenance_tier")),
            "locator": None,
            "notes": r.get("notes"),
        })
    counts["sources"] = store.upsert("sources", srcs)

    # ---- headline aggregates + analytical blocks kept whole -----------
    for key in ("headlines", "decomposition", "base_rates", "bill_model",
                "district_covariates", "district_denoms", "crosswalk",
                "leadership_slate", "si_pairs", "elections", "transitions",
                "votes_needed", "careers"):
        blk = payload.get(key)
        if blk is not None:
            store.set_meta(f"council_record.{key}",
                           blk if not isinstance(blk, dict) or "cols" not in blk
                           else {"cols": blk["cols"], "rows": blk["rows"]})

    store.set_meta("council_record.loaded", {
        "path": str(path), "counts": counts,
        "headlines": payload.get("headlines", {}),
    })
    store.journal("ingest.council_record", {"path": str(path), "counts": counts})
    return counts


def _tier(raw: Any) -> str:
    s = str(raw or "").upper()
    for t in ("OFFICIAL_PRIMARY", "OFFICIAL_DERIVED", "INSTITUTIONAL",
              "PRESS", "INTERNAL", "DELIBERATIVE"):
        if t in s:
            return t
    if "OFFICIAL" in s:
        return "OFFICIAL_DERIVED"
    return "INSTITUTIONAL"
