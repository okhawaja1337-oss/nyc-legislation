#!/usr/bin/env python3
"""
The briefing engine.

Turns a subject -- a bill, a member, an organization, a fiscal question, a
land-use application -- into a deliverable in the Councilmember's house style:

    a short paragraph that states the answer
    bullets, one fact or one decision each
    what it means for Staten Island and District 49
    exactly three questions she could ask out loud in a hearing
    a recommendation, or an explicit "no recommendation and why"

The evidence packet is assembled from the lake *first*, deterministically, and
carries its sources. The AI layer writes over that packet; it never supplies
the facts. That ordering is the whole design: with no API key the brief is
thinner in prose but identical in evidence, and it is never wrong because a
model guessed.
"""
from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable

from ..core.citations import CitationRegistry, DEFAULT_SOURCES, Fact, VERIFY_TAG
from ..core.config import (DISTRICT, DISTRICT_LABEL, HOUSE_STYLE, LAND_USE_DOCTRINE,
                           MEMBER_NAME, PILLARS)
from ..core.store import Store
from ..intel import funding as FI
from ..intel import integrity as II
from ..intel import legislation as LI
from .council import BASE_CONTEXT, Deliberation, ask_model, deliberate


# ----------------------------------------------------------------- model ----
@dataclass
class Brief:
    subject: str
    kind: str
    bottom_line: str = ""
    details: list[str] = field(default_factory=list)
    d49_impact: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    recommendation: str = ""
    talking_points: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    sources_used: list[str] = field(default_factory=list)
    council: Deliberation | None = None
    created: str = field(default_factory=
                         lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    mode: str = "evidence-only"

    # ------------------------------------------------------------ render --
    def to_markdown(self, registry: CitationRegistry | None = None) -> str:
        reg = registry or CitationRegistry(DEFAULT_SOURCES)
        out = [f"# {self.subject}", "",
               f"*{self.kind} · prepared for {MEMBER_NAME}, {DISTRICT_LABEL} · "
               f"{self.created[:10]}*", ""]
        if self.bottom_line:
            out += ["## Bottom line", self.bottom_line, ""]
        if self.details:
            out += ["## What you need to know"] + [f"- {d}" for d in self.details] + [""]
        if self.d49_impact:
            out += [f"## Staten Island / District {DISTRICT} impact"] + \
                   [f"- {d}" for d in self.d49_impact] + [""]
        if self.talking_points:
            out += ["## Talking points"] + \
                   [f"{i}. {t}" for i, t in enumerate(self.talking_points, 1)] + [""]
        if self.questions:
            out += ["## Three questions"] + \
                   [f"{i}. {q}" for i, q in enumerate(self.questions, 1)] + [""]
        if self.recommendation:
            out += ["## Recommendation", self.recommendation, ""]
        if self.council and self.council.synthesis:
            out += ["## Council deliberation", self.council.synthesis, ""]
        bib = reg.bibliography(self.sources_used)
        if bib:
            out += ["## Sources"] + bib + [""]
        out += ["---",
                f"*Mode: {self.mode}. Figures trace to the sources above; any "
                f"item marked {VERIFY_TAG} is unconfirmed and must be checked "
                f"before use.*"]
        return "\n".join(out)

    def to_dict(self) -> dict:
        d = {k: v for k, v in vars(self).items() if k != "council"}
        d["council"] = self.council.to_dict() if self.council else None
        return d


# ------------------------------------------------- evidence assembly ----
def _fmt(n: Any, unit: str = "") -> str:
    if n is None:
        return VERIFY_TAG
    if unit == "usd":
        return f"${n:,.0f}"
    if unit == "pct":
        return f"{n:.1f}%"
    if isinstance(n, float):
        return f"{n:,.3g}"
    if isinstance(n, int):
        return f"{n:,}"
    return str(n)


def evidence_for_member(store: Store, who: str, fy: int = 2027) -> dict:
    """Everything the lake knows about an official, assembled once."""
    rec = LI.legislative_record(store, who)
    if "error" in rec:
        return rec
    name = rec["member"]["name"]
    last = name.split()[-1]
    return {
        "member": rec["member"],
        "record": {k: rec[k] for k in ("totals", "enactment_rate", "by_pillar",
                                       "by_committee", "recent_prime", "behavior")},
        "benchmark": LI.peer_benchmark(store, who),
        "coalition": LI.coalition(store, who, limit=10),
        "portfolio": FI.member_portfolio(store, last, fy),
        "drift": FI.pillar_drift(store, last),
        "concentration": FI.concentration(store, last, fy),
        "churn": FI.org_trajectories(store, last),
        "equity": FI.district_equity(store, fy),
        "scorecard": II.official_scorecard(store, who, fy),
        "sources": ["COUNCIL_RECORD", "SCHEDULE_C", "LEGISTAR"],
    }


def evidence_for_matter(store: Store, matter_id: int) -> dict:
    whip = LI.whip_count(store, matter_id)
    if "error" in whip:
        return whip
    return {"whip": whip, "sources": ["COUNCIL_RECORD", "LEGISTAR"]}


def evidence_for_fiscal(store: Store, fy: int = 2027) -> dict:
    return {
        "citywide": FI.citywide_context(store),
        "d49": FI.member_portfolio(store, "Hanks", fy),
        "equity": FI.district_equity(store, fy),
        "pipeline": FI.pipeline_risk(store, fy),
        "reconciliation": FI.reconcile_si(store, fy),
        "churn": FI.org_trajectories(store, "Hanks"),
        "sources": ["SCHEDULE_C", "TRANSPARENCY_RESO", "COUNCIL_BUDGET",
                    "D49_MOCS_TRACKER", "SI_ROLLUP"],
    }


def packet_text(evidence: dict, limit: int = 9000) -> str:
    """Flatten an evidence packet into the block the model is grounded on."""
    return json.dumps(evidence, indent=1, default=str)[:limit]


# ------------------------------------------------------- brief builders ----
def member_brief(store: Store, who: str, fy: int = 2027,
                 with_council: bool = False) -> Brief:
    ev = evidence_for_member(store, who, fy)
    if "error" in ev:
        return Brief(subject=str(who), kind="Member brief",
                     bottom_line=ev["error"])
    m = ev["member"]
    rec, bench, port = ev["record"], ev["benchmark"], ev["portfolio"]
    sc, eq = ev["scorecard"], ev["equity"]
    t = rec["totals"]

    b = Brief(subject=f"{m['name']} — member brief", kind="Member brief")
    b.evidence = ev
    b.sources_used = ["COUNCIL_RECORD", "SCHEDULE_C"]

    b.bottom_line = (
        f"{m['name']} ({m['party']}, District {m['district']}, {m['borough']}) has "
        f"prime-sponsored {_fmt(t.get('prime'))} matters and co-sponsored "
        f"{_fmt(t.get('cosponsor'))}, with {_fmt(t.get('prime_enacted'))} enacted "
        f"into local law — an enactment rate of "
        f"{_fmt((rec['enactment_rate'] or 0) * 100, 'pct')}. "
        f"They rank {bench['prime_sponsored']['rank']} of "
        f"{bench['prime_sponsored']['of']} on prime sponsorship against a body "
        f"median of {_fmt(bench['prime_sponsored']['median'])}. "
        f"In FY{fy} they directed {_fmt(port['total'], 'usd')} across "
        f"{_fmt(port['lines'])} discretionary lines.")

    top_cmte = rec["by_committee"][0] if rec["by_committee"] else None
    beh = rec["behavior"]
    b.details = [
        f"Legislative centre of gravity: "
        f"{top_cmte['committee'] if top_cmte else VERIFY_TAG}"
        + (f" ({top_cmte['n']} prime-sponsored items)" if top_cmte else ""),
        f"Priority mix by prime sponsorship: " + ", ".join(
            f"{PILLARS[k]['label']} {v['prime']}" for k, v in
            list(rec["by_pillar"].items())[:4] if k in PILLARS) or VERIFY_TAG,
        f"Discretionary concentration: {_fmt(ev['concentration']['effective_orgs'])} "
        f"effective organizations ({ev['concentration']['reading']}); top five take "
        f"{_fmt(ev['concentration']['top5_share_pct'], 'pct')}.",
        f"Voting behaviour (model-derived): dissent rate "
        f"{_fmt((beh.get('dissent_rate') or 0) * 100, 'pct')}, alignment with the "
        f"Speaker {_fmt((beh.get('alignment_with_speaker') or 0) * 100, 'pct')}, "
        f"profile '{beh.get('profile_type') or VERIFY_TAG}'.",
        f"Accountability Index: composite {_fmt(sc.get('composite'))} "
        f"(grade {sc.get('grade')}, peer percentile {_fmt(sc.get('peer_percentile'))}), "
        f"on {_fmt((sc.get('coverage') or 0) * 100, 'pct')} indicator coverage.",
        f"Organization churn: {ev['churn']['counts']['sustained']} sustained, "
        f"{ev['churn']['counts']['new']} new, {ev['churn']['counts']['lapsed']} lapsed "
        f"since FY{min(ev['portfolio'].get('fy') or fy, 2022)}.",
    ]

    focus = eq.get("focus") or {}
    b.d49_impact = [
        f"District {DISTRICT} received {_fmt(focus.get('total'), 'usd')} in FY{fy} "
        f"member designations — {_fmt(focus.get('per_resident'))} per resident, "
        f"ranked {focus.get('rank')} of {len(eq.get('all', []))} districts "
        f"({_fmt(eq.get('focus_vs_median_pct'), 'pct')} against the median).",
        "Staten Island districts: " + ", ".join(
            f"D{t['district']} ${t['per_resident']}/resident (rank {t['rank']})"
            for t in eq.get("staten_island", [])) or VERIFY_TAG,
    ]
    lapsed = ev["churn"]["lapsed"][:3]
    if lapsed:
        b.d49_impact.append(
            "Lapsed grantees most likely to call: " + "; ".join(
                f"{l['org']} (funded {l['years_funded']}y, "
                f"{_fmt(l['total'], 'usd')} lifetime)" for l in lapsed))

    b.questions = [
        f"Our enactment rate is {_fmt((rec['enactment_rate'] or 0) * 100, 'pct')} "
        f"against a body median of {_fmt(bench['enacted']['median'])} enacted — "
        "are we introducing the wrong instruments, or losing them in committee?",
        f"We fund {_fmt(ev['concentration']['orgs'])} organizations and the top "
        f"five hold {_fmt(ev['concentration']['top5_share_pct'], 'pct')} — is that "
        "the delivery capacity we want, or a single point of failure?",
        f"District {DISTRICT} sits at rank {focus.get('rank')} on per-resident "
        "delivery — what is the specific ask that moves us, and who has to say yes?",
    ]
    b.talking_points = [
        f"Since FY2022 the Council's discretionary pot grew "
        f"{_fmt(FI.citywide_context(store)['growth']['total_pct'], 'pct')}; "
        f"District {DISTRICT}'s job is to convert that growth into North Shore "
        "capacity, not just North Shore grants.",
        f"We sustain {ev['churn']['counts']['sustained']} organizations year over "
        "year — that continuity is the district's delivery infrastructure.",
    ]
    b.recommendation = (
        "No recommendation — this is a descriptive brief. Decisions on "
        "sponsorship, funding or public posture should run through the Council "
        "deliberation before they are made.")

    if with_council:
        b.council = deliberate(
            f"What should {m['name']}'s office prioritise this cycle, given this record?",
            packet_text(ev))
        b.mode = b.council.mode
    return b


def fiscal_brief(store: Store, fy: int = 2027, with_council: bool = False) -> Brief:
    ev = evidence_for_fiscal(store, fy)
    cw, d49, eq = ev["citywide"], ev["d49"], ev["equity"]
    pipe, rec = ev["pipeline"], ev["reconciliation"]
    g = cw["growth"]

    b = Brief(subject=f"FY{fy} fiscal position — District {DISTRICT}",
              kind="Fiscal brief")
    b.evidence = ev
    b.sources_used = ev["sources"]

    latest = cw["by_fy"][-1] if cw["by_fy"] else {}
    b.bottom_line = (
        f"The Council's FY{fy} Schedule C totals {_fmt(latest.get('total'), 'usd')} "
        f"across {_fmt(latest.get('n'))} designations — up {_fmt(g['total_pct'], 'pct')} "
        f"since {g['span'].split('-')[0] if g.get('span') else 'FY2022'}, with member "
        f"designations up {_fmt(g['member_pct'], 'pct')}. District {DISTRICT} directed "
        f"{_fmt(d49['total'], 'usd')} across {_fmt(d49['lines'])} lines. "
        f"{_fmt(pipe['pending_total'], 'usd')} of tracked District {DISTRICT} awards "
        f"is still sitting in the MOCS pipeline.")

    b.details = [
        f"Citywide initiatives now carry {_fmt(latest.get('citywide_total'), 'usd')} "
        f"against {_fmt(latest.get('member_total'), 'usd')} in member designations — "
        "the growth is disproportionately in pots the member does not control directly.",
        f"Priority mix in FY{fy}: " + ", ".join(
            f"{PILLARS.get(r['pillar'], {}).get('label', r['pillar'].replace('_', ' ').title())} "
            f"{_fmt(r['total'], 'usd')} ({_fmt(r['share_pct'], 'pct')})"
            for r in d49["by_pillar"][:4]),
        f"Pipeline: {_fmt(pipe['pending_total'], 'usd')} pending "
        f"({_fmt(pipe['pending_share_pct'], 'pct')} of tracked), "
        f"{len(pipe['defunded'])} defunded or withdrawn lines on record.",
        f"Staten Island FY{fy}, as two separate measures that are never merged: "
        f"capital {_fmt(rec['capital']['adopted'], 'usd')} over "
        f"{_fmt(rec['capital']['lines'])} lines, expense "
        f"{_fmt(rec['expense']['adopted'], 'usd')} over "
        f"{_fmt(rec['expense']['lines'])} lines.",
        f"Transparency Resolution movement: the headline net is "
        f"{_fmt(rec['tr_movement']['stated_net'], 'usd')}, but only "
        f"{_fmt(rec['tr_movement']['confirmed'], 'usd')} is confirmed money. "
        f"{_fmt(rec['tr_movement']['pending_mod'], 'usd')} sits in "
        f"pending-modification lines that do not take effect until a budget "
        f"modification passes, and "
        f"{_fmt(rec['tr_movement']['reversed_and_excluded'], 'usd')} was "
        f"designated and then rescinded.",
    ]
    hl = (cw.get("forecast") or {}).get("highlights") or []
    b.details += [f"Revenue outlook: {h}" for h in hl[:2]]

    focus = eq.get("focus") or {}
    b.d49_impact = [
        f"District {DISTRICT} per-resident delivery {_fmt(focus.get('per_resident'))}, "
        f"rank {focus.get('rank')} of {len(eq.get('all', []))} "
        f"({_fmt(eq.get('focus_vs_median_pct'), 'pct')} vs median).",
        "Island comparison: " + ", ".join(
            f"D{t['district']} ${t['per_resident']} (rank {t['rank']})"
            for t in eq.get("staten_island", [])),
        f"Borough-wide capital §254 is {_fmt(rec['capital']['adopted'], 'usd')}; "
        f"borough-wide expense is {_fmt(rec['expense']['adopted'], 'usd')}. "
        "Cite them separately — they are different measures.",
    ]
    b.questions = [
        f"Citywide initiative dollars grew {_fmt(g['citywide_pct'], 'pct')} while "
        f"member designations grew {_fmt(g['member_pct'], 'pct')} — what share of "
        f"the citywide pots is actually reaching District {DISTRICT} organizations?",
        f"{_fmt(pipe['pending_total'], 'usd')} is unreleased in the pipeline — which "
        "of those grantees cannot absorb a late clearance, and who is calling them?",
        f"The press line is {_fmt(rec['tr_movement']['stated_net'], 'usd')} but "
        f"confirmed money is {_fmt(rec['tr_movement']['confirmed'], 'usd')} — "
        "do we say the confirmed number and hold the pending line back until "
        "the modification passes?",
    ]
    b.talking_points = [
        f"Staten Island's FY{fy} capital position is "
        f"{_fmt(rec['capital']['adopted'], 'usd')} and its expense position is "
        f"{_fmt(rec['expense']['adopted'], 'usd')} — the Island is not a "
        "rounding error in this budget.",
        f"District {DISTRICT} sustains "
        f"{ev['churn']['counts']['sustained']} organizations year over year; that "
        "continuity is what turns an annual grant into a standing service.",
    ]
    b.recommendation = (
        f"Use {_fmt(rec['tr_movement']['confirmed'], 'usd')} as the Transparency "
        f"Resolution figure in any public statement, not the "
        f"{_fmt(rec['tr_movement']['stated_net'], 'usd')} headline. The difference "
        f"is a {_fmt(rec['tr_movement']['pending_mod'], 'usd')} line awaiting a "
        f"budget modification and a "
        f"{_fmt(rec['tr_movement']['reversed_and_excluded'], 'usd')} designation "
        f"that was reversed four weeks later. Capital and expense stay separate "
        f"in every citation.")

    if with_council:
        b.council = deliberate(
            f"What should District {DISTRICT} prioritise in the FY{fy + 1} budget cycle?",
            packet_text(ev))
        b.mode = b.council.mode
    return b


def matter_brief(store: Store, matter_id: int, with_council: bool = False) -> Brief:
    ev = evidence_for_matter(store, matter_id)
    if "error" in ev:
        return Brief(subject=str(matter_id), kind="Bill brief",
                     bottom_line=ev["error"])
    w = ev["whip"]
    m = w["matter"]
    b = Brief(subject=f"{m['file']} — {m['name']}", kind="Bill brief")
    b.evidence = ev
    b.sources_used = ["COUNCIL_RECORD", "LEGISTAR"]

    b.bottom_line = (
        f"{m['file']} ({m['type']}) is in {m['committee'] or VERIFY_TAG} with status "
        f"'{m['status']}'. It carries {w['have']} current-member sponsors: "
        + ("already at a majority" if w["at_majority"] else
           f"{w['need_majority']} short of the 26 needed")
        + ("; veto-proof" if w["veto_proof"] else
           f", {w['need_veto_proof']} short of 34 for a veto-proof margin") + ".")

    b.details = [
        f"Prime sponsor: {m.get('prime_name') or VERIFY_TAG}.",
        f"Sponsor composition: " + ", ".join(f"{k} {v}" for k, v in w["by_party"].items()),
        f"Borough spread: " + ", ".join(f"{k} {v}" for k, v in w["by_borough"].items()),
        f"Modeled passage odds: "
        f"{_fmt((m.get('pass_prob') or 0) * 100, 'pct')} (model estimate, not a "
        "vote count).",
        f"Office priorities touched: " + (", ".join(
            PILLARS[p]["label"] for p in w["pillars"] if p in PILLARS) or "none tagged"),
    ]
    si = [s for s in w["signed"] if s["district"] in (49, 50, 51)]
    b.d49_impact = [
        ("Staten Island members already signed: " +
         ", ".join(f"{s['name']} (D{s['district']})" for s in si))
        if si else "No Staten Island member has signed on.",
        f"District {DISTRICT} position: " + (
            "signed" if any(s["district"] == DISTRICT for s in w["signed"])
            else "not signed — this is a live decision."),
    ]
    warm = [t for t in w["targets"] if t["lean"] == "warm"][:5]
    if warm:
        b.details.append("Warmest unsigned targets: " + ", ".join(
            f"{t['name']} (D{t['district']})" for t in warm))
    b.questions = [
        "What does this actually change for a North Shore resident, and when?",
        f"We are {w['need_majority']} votes from a majority — which of the warm "
        "targets does this office have standing to ask, and what do they want back?",
        "What is the implementing agency, and does it have the headcount to do this?",
    ]
    b.recommendation = (
        "Decision required: sign on, hold, or seek an amendment. Sponsorship "
        "counts are not vote counts — confirm any lean by call before relying on it.")

    if with_council:
        b.council = deliberate(
            f"Should the office sign on to {m['file']}: {m['name']}?",
            packet_text(ev))
        b.mode = b.council.mode
    return b


def talking_points(store: Store, subject: str, evidence: dict,
                   n: int = 6) -> list[str]:
    """Short, sayable lines grounded in a packet. Model-written when available."""
    prompt = textwrap.dedent(f"""
        Write {n} talking points for {MEMBER_NAME} on: {subject}

        Each is one sentence she could say out loud at a hearing or to a
        reporter. Plain English. Every figure must come from the evidence; if
        a number is not there, do not use one.

        EVIDENCE
        {packet_text(evidence, 6000)}
    """).strip()
    out = ask_model(prompt, max_tokens=900)
    if not out:
        return []
    return [ln.lstrip("-•*0123456789. ").strip()
            for ln in out.splitlines() if ln.strip()][:n]
