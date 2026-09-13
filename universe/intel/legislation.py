#!/usr/bin/env python3
"""
Legislative pattern intelligence.

Twenty-one thousand matters, a hundred and sixty thousand sponsorships, and
every contested vote since the roll-call record begins. The questions this
answers are the ones that decide whether to sign on, hold, or push:

  * What does this member's record actually consist of, by policy area?
  * Who moves with them, and who never does?
  * Which pending bills touch District 49's priorities and are likely to pass?
  * If we need 26 votes, where do they come from?
  * Is the Staten Island delegation a bloc, or three separate operations?

Model output is labeled as inference and never presented as fact.
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from typing import Any, Iterable

from ..core.config import DISTRICT, PILLARS, SI_DISTRICTS
from ..core.store import Store

MAJORITY = 26          # of 51
VETO_PROOF = 34


def _rows(store: Store, sql: str, params: Iterable = ()) -> list[dict]:
    return [dict(r) for r in store.q(sql, list(params))]


def current_session(store: Store) -> str | None:
    """
    The Council session currently sitting.

    `session` is stored as text, so MAX() sorts it lexically and answers '9'
    for a corpus that runs through session 10. Every "what is live right now"
    query depends on this, so it is compared numerically and returned in the
    exact spelling the column uses.
    """
    rows = [r["session"] for r in store.q(
        "SELECT DISTINCT session FROM matters WHERE session IS NOT NULL AND session<>''")]
    numeric = [r for r in rows if str(r).strip().lstrip("-").isdigit()]
    if numeric:
        return max(numeric, key=lambda v: int(v))
    return max(rows) if rows else None


def resolve_member(store: Store, who: str | int) -> dict | None:
    """Find a member by id, last name, or full name."""
    if isinstance(who, int) or str(who).isdigit():
        r = store.one("SELECT * FROM members WHERE person_id = ?", [int(who)])
        return dict(r) if r else None
    r = store.one("""
        SELECT * FROM members
        WHERE current = 1 AND (last = ? COLLATE NOCASE OR name = ? COLLATE NOCASE
                               OR name LIKE ?)
        ORDER BY current DESC LIMIT 1
    """, [who, who, f"%{who}%"])
    if not r:
        r = store.one("SELECT * FROM members WHERE name LIKE ? ORDER BY current DESC "
                      "LIMIT 1", [f"%{who}%"])
    return dict(r) if r else None


def metrics(store: Store, person_id: int) -> dict:
    """All stored metrics for a member, keyed by metric name."""
    out: dict[str, Any] = {}
    for r in store.q("""SELECT metric, value, text_value, session
                        FROM member_metrics WHERE person_id = ?""", [person_id]):
        key = r["metric"] if r["session"] in (None, "all") else f"{r['metric']}@{r['session']}"
        out[key] = r["value"] if r["value"] is not None else r["text_value"]
    return out


# ------------------------------------------------------ record & portfolio ----
def legislative_record(store: Store, who: str | int) -> dict:
    """A member's whole legislative footprint, by topic and outcome."""
    m = resolve_member(store, who)
    if not m:
        return {"error": f"member not found: {who}"}
    pid = m["person_id"]

    totals = store.one("""
        SELECT
          SUM(CASE WHEN s.role='prime' THEN 1 ELSE 0 END) AS prime,
          SUM(CASE WHEN s.role='cosponsor' THEN 1 ELSE 0 END) AS cosponsor,
          SUM(CASE WHEN s.role='prime' AND m.enacted=1 THEN 1 ELSE 0 END) AS prime_enacted,
          COUNT(*) AS total
        FROM sponsorships s JOIN matters m ON m.matter_id = s.matter_id
        WHERE s.person_id = ?
    """, [pid])

    by_pillar = _rows(store, """
        SELECT m.pillars, s.role, m.enacted, m.status, m.matter_id
        FROM sponsorships s JOIN matters m ON m.matter_id = s.matter_id
        WHERE s.person_id = ?
    """, [pid])
    pillar_counts: dict[str, dict] = defaultdict(
        lambda: {"prime": 0, "cosponsor": 0, "enacted": 0})
    for r in by_pillar:
        for p in json.loads(r["pillars"] or "[]"):
            pillar_counts[p][r["role"]] += 1
            if r["enacted"]:
                pillar_counts[p]["enacted"] += 1

    by_committee = _rows(store, """
        SELECT m.committee, COUNT(*) AS n,
               SUM(m.enacted) AS enacted
        FROM sponsorships s JOIN matters m ON m.matter_id = s.matter_id
        WHERE s.person_id = ? AND s.role = 'prime' AND m.committee IS NOT NULL
        GROUP BY m.committee ORDER BY n DESC LIMIT 12
    """, [pid])

    recent = _rows(store, """
        SELECT m.file, m.name, m.type, m.status, m.year, m.enacted, m.committee
        FROM sponsorships s JOIN matters m ON m.matter_id = s.matter_id
        WHERE s.person_id = ? AND s.role = 'prime'
        ORDER BY m.year DESC, m.matter_id DESC LIMIT 25
    """, [pid])

    prime = (totals["prime"] or 0) if totals else 0
    enacted = (totals["prime_enacted"] or 0) if totals else 0
    mt = metrics(store, pid)

    return {
        "member": {k: m[k] for k in ("person_id", "name", "party", "borough",
                                     "district", "current", "leadership")},
        "totals": dict(totals) if totals else {},
        "enactment_rate": round(enacted / prime, 4) if prime else None,
        "by_pillar": {k: dict(v) for k, v in sorted(
            pillar_counts.items(), key=lambda kv: -(kv[1]["prime"] + kv[1]["cosponsor"]))},
        "by_committee": by_committee,
        "recent_prime": recent,
        "behavior": {
            "dissent_rate": mt.get("dissent_rate"),
            "nay_rate_contested": mt.get("nay_rate_contested"),
            "absence_rate": mt.get("absence_rate"),
            "alignment_with_speaker": mt.get("alignment_with_speaker"),
            "alignment_with_party_majority": mt.get("alignment_with_party_majority"),
            "ideal_dim1": mt.get("ideal_dim1@pooled") or mt.get("ideal_dim1_latest_era"),
            "profile_type": mt.get("profile_type"),
            "cluster": mt.get("cluster_label_latest_session"),
            "predictability_auc": mt.get("predictability_auc"),
        },
        "source_id": "COUNCIL_RECORD",
        "label": "Counts are from the record; behavior scores are model-derived "
                 "(labeled inference, not fact).",
    }


def peer_benchmark(store: Store, who: str | int) -> dict:
    """Where a member sits against the current body on the measures that matter."""
    m = resolve_member(store, who)
    if not m:
        return {"error": f"member not found: {who}"}
    pid = m["person_id"]

    current = [dict(r) for r in store.q(
        "SELECT person_id, name, district, party FROM members WHERE current = 1")]
    ids = [c["person_id"] for c in current]
    if pid not in ids:
        ids.append(pid)
    qmarks = ",".join("?" * len(ids))

    rows = _rows(store, f"""
        SELECT s.person_id,
               SUM(CASE WHEN s.role='prime' THEN 1 ELSE 0 END) AS prime,
               SUM(CASE WHEN s.role='cosponsor' THEN 1 ELSE 0 END) AS cospon,
               SUM(CASE WHEN s.role='prime' AND m.enacted=1 THEN 1 ELSE 0 END) AS enacted
        FROM sponsorships s JOIN matters m ON m.matter_id = s.matter_id
        WHERE s.person_id IN ({qmarks})
        GROUP BY s.person_id
    """, ids)

    def rank_of(key: str) -> dict:
        vals = sorted(((r["person_id"], r.get(key) or 0) for r in rows),
                      key=lambda t: -t[1])
        pos = next((i + 1 for i, (p, _) in enumerate(vals) if p == pid), None)
        mine = next((v for p, v in vals if p == pid), 0)
        arr = [v for _, v in vals]
        med = sorted(arr)[len(arr) // 2] if arr else 0
        return {"value": mine, "rank": pos, "of": len(vals), "median": med}

    return {
        "member": m["name"], "district": m["district"],
        "prime_sponsored": rank_of("prime"),
        "cosponsored": rank_of("cospon"),
        "enacted": rank_of("enacted"),
        "source_id": "COUNCIL_RECORD",
    }


# ---------------------------------------------------------------- coalition ----
def coalition(store: Store, who: str | int, limit: int = 20,
              min_shared: int = 15) -> dict:
    """Who this member actually legislates with, measured by co-sponsorship.

    Uses Jaccard overlap so a prolific member does not dominate purely by
    volume. Also reports the *cold* list: current colleagues with the least
    shared record, which is where a whip operation has to start.
    """
    m = resolve_member(store, who)
    if not m:
        return {"error": f"member not found: {who}"}
    pid = m["person_id"]

    mine = {r["matter_id"] for r in store.q(
        "SELECT matter_id FROM sponsorships WHERE person_id = ?", [pid])}
    if not mine:
        return {"member": m["name"], "partners": [], "cold": []}

    others = _rows(store, """
        SELECT s.person_id, mb.name, mb.party, mb.district, mb.borough,
               COUNT(*) AS shared
        FROM sponsorships s
        JOIN members mb ON mb.person_id = s.person_id
        WHERE s.matter_id IN (SELECT matter_id FROM sponsorships WHERE person_id = ?)
          AND s.person_id != ? AND mb.current = 1
        GROUP BY s.person_id ORDER BY shared DESC
    """, [pid, pid])

    sizes = {r["person_id"]: r["n"] for r in _rows(store, """
        SELECT person_id, COUNT(*) AS n FROM sponsorships GROUP BY person_id
    """)}

    partners = []
    for o in others:
        theirs = sizes.get(o["person_id"], 0)
        union = len(mine) + theirs - o["shared"]
        partners.append({
            **{k: o[k] for k in ("person_id", "name", "party", "district", "borough")},
            "shared": o["shared"], "their_total": theirs,
            "jaccard": round(o["shared"] / union, 4) if union else 0,
            "share_of_mine": round(o["shared"] / len(mine), 4),
        })
    strong = sorted([p for p in partners if p["shared"] >= min_shared],
                    key=lambda p: -p["jaccard"])[:limit]

    seen = {p["person_id"] for p in partners}
    cold = [{"person_id": c["person_id"], "name": c["name"], "party": c["party"],
             "district": c["district"], "shared": 0}
            for c in store.q("SELECT person_id, name, party, district FROM members "
                             "WHERE current = 1 AND person_id != ?", [pid])
            if c["person_id"] not in seen]
    cold += sorted([p for p in partners if p["shared"] < min_shared],
                   key=lambda p: p["shared"])
    return {
        "member": m["name"], "my_items": len(mine),
        "partners": strong, "cold": cold[:limit],
        "source_id": "COUNCIL_RECORD",
    }


def si_delegation(store: Store) -> dict:
    """Is the Staten Island delegation a bloc?

    The Council Record carries pairwise agreement for the three Island
    members. A delegation that votes together has leverage disproportionate to
    its three seats; one that does not has none.
    """
    pairs = store.get_meta("council_record.si_pairs") or []
    members = _rows(store, """
        SELECT person_id, name, party, district FROM members
        WHERE current = 1 AND district IN (49, 50, 51) ORDER BY district
    """)
    rows = []
    for p in pairs:
        if isinstance(p, list) and len(p) >= 3:
            rows.append({"a": p[0], "b": p[1], "agreement": p[2],
                         "n": p[3] if len(p) > 3 else None})
    vals = [r["agreement"] for r in rows if isinstance(r["agreement"], (int, float))]
    cohesion = round(sum(vals) / len(vals), 3) if vals else None
    return {
        "members": members, "pairs": rows, "mean_pairwise_agreement": cohesion,
        "reading": (
            "The delegation does not vote as a bloc: the two Republican members "
            "move together far more closely than either moves with District 49."
            if rows and max(vals or [0]) - min(vals or [0]) > 0.4 else
            "Pairwise agreement is comparable across the delegation."),
        "implication": (
            "Island-wide leverage has to be built issue by issue rather than "
            "assumed. On a borough-parity vote the delegation is worth three "
            "votes only if the ask is framed so all three can sign it."),
        "source_id": "COUNCIL_RECORD",
        "label": "Agreement scores are model-derived from the roll-call record.",
    }


# --------------------------------------------------------- pending pipeline ----
def pending_for_pillars(store: Store, pillars: Iterable[str] | None = None,
                        year: int | None = None, limit: int = 50,
                        min_prob: float = 0.0, live_only: bool = True) -> dict:
    """Bills that touch the office's priorities and are still moving.

    ``live_only`` restricts to matters the Record marks pending. A bill from a
    closed session cannot be signed on to, so recommending one is worse than
    recommending nothing.
    """
    want = list(pillars) if pillars else list(PILLARS)
    clauses = " OR ".join("m.pillars LIKE ?" for _ in want)
    params: list[Any] = [f'%"{p}"%' for p in want]
    sql = f"""
        SELECT m.matter_id, m.file, m.name, m.type, m.status, m.committee,
               m.year, m.n_sponsors, m.pass_prob, m.pillars, m.prime_id,
               mb.name AS prime_name, mb.district AS prime_district, mb.party AS prime_party
        FROM matters m LEFT JOIN members mb ON mb.person_id = m.prime_id
        WHERE ({clauses}) AND m.enacted = 0
    """
    if live_only:
        sql += " AND m.pending = 1"
    if year:
        sql += " AND m.year = ?"
        params.append(year)
    if min_prob:
        sql += " AND COALESCE(m.pass_prob, 0) >= ?"
        params.append(min_prob)
    sql += " ORDER BY COALESCE(m.pass_prob,0) DESC, m.n_sponsors DESC LIMIT ?"
    params.append(limit)

    rows = _rows(store, sql, params)
    for r in rows:
        r["pillars"] = json.loads(r["pillars"] or "[]")
    return {"pillars": want, "year": year, "live_only": live_only,
            "count": len(rows), "items": rows,
            "source_id": "COUNCIL_RECORD",
            "label": "pass_prob is a model estimate from the Council Record's "
                     "bill model, not a prediction of any member's vote."}


def signon_candidates(store: Store, who: str | int, year: int | None = None,
                      limit: int = 25, live_only: bool = True) -> dict:
    """Bills the member is not on that fit the office's priorities.

    A recommendation engine for sign-ons: pillar fit, momentum (sponsor count),
    modeled passage odds, and whether the prime is someone this member already
    works with. Every candidate carries its reasons, so the decision stays the
    Councilmember's.
    """
    m = resolve_member(store, who)
    if not m:
        return {"error": f"member not found: {who}"}
    pid = m["person_id"]

    coal = coalition(store, pid, limit=60, min_shared=5)
    affinity = {p["person_id"]: p["jaccard"] for p in coal.get("partners", [])}

    mine = {r["matter_id"] for r in store.q(
        "SELECT matter_id FROM sponsorships WHERE person_id = ?", [pid])}

    pend = pending_for_pillars(store, year=year, limit=800,
                               live_only=live_only)
    out = []
    for it in pend["items"]:
        if it["matter_id"] in mine:
            continue
        reasons = []
        score = 0.0
        pil = it["pillars"]
        if pil:
            labels = [PILLARS[p]["label"] for p in pil if p in PILLARS]
            score += 1.2 * len(pil)
            reasons.append(f"hits {', '.join(labels)}")
        prob = it.get("pass_prob") or 0
        if prob:
            score += 2.0 * prob
            reasons.append(f"modeled passage odds {prob:.0%}")
        ns = it.get("n_sponsors") or 0
        if ns >= MAJORITY:
            score += 1.5
            reasons.append(f"{ns} sponsors — already at or past a majority")
        elif ns >= 15:
            score += 0.8
            reasons.append(f"{ns} sponsors — building")
        aff = affinity.get(it.get("prime_id"), 0)
        if aff > 0.15:
            score += 1.0
            reasons.append(f"prime is a frequent partner ({it.get('prime_name')})")
        if it.get("prime_district") in SI_DISTRICTS:
            score += 1.2
            reasons.append("prime is a Staten Island colleague")
        if it.get("prime_party") and m.get("party") and it["prime_party"] != m["party"]:
            reasons.append("cross-party sign-on opportunity")
            score += 0.4
        out.append({**it, "score": round(score, 3), "reasons": reasons})

    out.sort(key=lambda r: -r["score"])
    return {
        "member": m["name"], "year": year, "live_only": live_only,
        "candidates": out[:limit], "considered": len(out),
        "source_id": "COUNCIL_RECORD",
        "label": ("Ranked suggestion, not advice: fit and momentum only. "
                  "Read the bill text before signing on."),
    }


# ------------------------------------------------------------- whip math ----
def whip_count(store: Store, matter_id: int) -> dict:
    """Where a bill stands against 26 and 34, and who is left to ask."""
    m = store.one("""SELECT m.*, mb.name AS prime_name FROM matters m
                     LEFT JOIN members mb ON mb.person_id = m.prime_id
                     WHERE m.matter_id = ?""", [matter_id])
    if not m:
        return {"error": f"matter {matter_id} not found"}

    signed = _rows(store, """
        SELECT mb.person_id, mb.name, mb.party, mb.district, mb.borough, s.role
        FROM sponsorships s JOIN members mb ON mb.person_id = s.person_id
        WHERE s.matter_id = ? AND mb.current = 1
        ORDER BY s.role DESC, mb.district
    """, [matter_id])
    signed_ids = {s["person_id"] for s in signed}

    unsigned = _rows(store, """
        SELECT person_id, name, party, district, borough FROM members
        WHERE current = 1 AND person_id NOT IN (
            SELECT person_id FROM sponsorships WHERE matter_id = ?)
        ORDER BY district
    """, [matter_id])

    pillars = json.loads(m["pillars"] or "[]")

    # How close each unsigned member already is to the bill's coalition,
    # measured in one pass: how many matters they share with people who have
    # already signed. Nested per-member queries here cost 50x for no gain.
    overlap: dict[int, int] = {}
    if signed_ids and unsigned:
        qs = ",".join("?" * len(signed_ids))
        qu = ",".join("?" * len(unsigned))
        for r in store.q(f"""
            SELECT a.person_id AS pid, COUNT(DISTINCT a.matter_id) AS shared
            FROM sponsorships a
            JOIN sponsorships b ON b.matter_id = a.matter_id
            WHERE a.person_id IN ({qu}) AND b.person_id IN ({qs})
            GROUP BY a.person_id
        """, [u["person_id"] for u in unsigned] + list(signed_ids)):
            overlap[r["pid"]] = r["shared"]

    scale = max(overlap.values()) if overlap else 0
    for u in unsigned:
        n = overlap.get(u["person_id"], 0)
        u["coalition_overlap"] = n
        u["lean"] = ("warm" if scale and n >= 0.6 * scale
                     else "cold" if n == 0
                     else "reachable")

    have = len(signed)
    return {
        "matter": {k: m[k] for k in ("matter_id", "file", "name", "type",
                                     "status", "committee", "year",
                                     "n_sponsors", "pass_prob", "prime_name")},
        "pillars": pillars,
        "signed": signed, "have": have,
        "need_majority": max(0, MAJORITY - have),
        "need_veto_proof": max(0, VETO_PROOF - have),
        "at_majority": have >= MAJORITY,
        "veto_proof": have >= VETO_PROOF,
        "targets": sorted(unsigned, key=lambda u: -u["coalition_overlap"])[:20],
        "by_party": dict(Counter(s["party"] for s in signed)),
        "by_borough": dict(Counter(s["borough"] for s in signed)),
        "source_id": "COUNCIL_RECORD",
        "label": ("Sponsorship is not a vote count. 'Lean' is inferred from "
                  "co-sponsorship overlap and must be confirmed by a call."),
    }


def find_matters(store: Store, query: str, limit: int = 25) -> list[dict]:
    """Full-text plus field search across the matter table."""
    like = f"%{query}%"
    return _rows(store, """
        SELECT matter_id, file, name, type, status, committee, year,
               enacted, n_sponsors, pass_prob
        FROM matters
        WHERE name LIKE ? OR file LIKE ?
        ORDER BY year DESC, n_sponsors DESC LIMIT ?
    """, [like, like, limit])
