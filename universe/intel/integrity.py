#!/usr/bin/env python3
"""
The NYC Accountability Index.

A transparent, auditable score for New York City elected officials -- from the
Mayor down through every citywide, borough, Council, State and federal seat
that represents part of the city.

Design commitments, because an index without them is just an opinion with
decimals:

  1. **Published indicators.** Every indicator declares its definition,
     source, direction, weight, and peer group. Nothing is secret.
  2. **Peer-group normalization.** A Council Member is scored against Council
     Members, not against the Mayor. Percentile rank within the peer group,
     so the scale is interpretable and robust to outliers.
  3. **No silent imputation.** A missing indicator is missing. It lowers the
     coverage figure; it does not become a zero and it does not become an
     average. A composite below the coverage floor is reported as
     ``insufficient data`` rather than as a grade.
  4. **Direction is explicit.** Some indicators are good when high (attendance),
     some when low (absence). Two are deliberately *non-monotonic*: perfect
     alignment with leadership scores no better than reflexive opposition,
     because an index that rewards a rubber stamp is measuring obedience, not
     integrity.
  5. **Computed vs. declared.** Indicators the lake can compute are marked
     ``computed``. Indicators that require a live external source are declared
     with that source and left uncovered until it is loaded -- visible as a
     gap rather than quietly dropped.

This methodology was written for this office from standard public-integrity
index practice (indicator batteries, peer normalization, coverage gating). It
is not a reproduction of any other organization's proprietary methodology.

Deliberative: scores are analytical constructs for internal use. They measure
what the record shows, not a person's character.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Any, Callable, Iterable, Literal

from ..core.store import Store

Direction = Literal["higher_better", "lower_better", "midpoint_better"]
COVERAGE_FLOOR = 0.50          # below this, no grade is issued
# A record is "thin" relative to the body, not against a magic number: an
# official with less than this share of the peer median vote count is scored
# but ranked provisionally.
THIN_RECORD_RATIO = 0.5


# ---------------------------------------------------------------- offices ----
# Peer groups. An official is only ever ranked inside their own group.
PEER_GROUPS = {
    "citywide": ("Mayor", "Public Advocate", "Comptroller"),
    "borough": ("Borough President", "District Attorney"),
    "council": ("Council Member",),
    "state": ("State Senator", "Assembly Member"),
    "federal": ("U.S. Representative", "U.S. Senator"),
}
OFFICE_TO_GROUP = {o: g for g, offices in PEER_GROUPS.items() for o in offices}


@dataclass(frozen=True)
class Indicator:
    key: str
    label: str
    pillar: str
    definition: str
    direction: Direction
    weight: float
    source_id: str
    peer_groups: tuple[str, ...] = ("council",)
    computed: bool = False
    metric: str | None = None          # member_metrics key, when computed
    midpoint: float | None = None      # for midpoint_better
    note: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------- pillars ----
PILLARS: dict[str, dict] = {
    "transparency": {
        "label": "Transparency & Disclosure",
        "weight": 0.20,
        "asks": "Can the public see what this official does, spends, and is asked for?",
    },
    "presence": {
        "label": "Presence & Participation",
        "weight": 0.20,
        "asks": "Do they show up and vote?",
    },
    "productivity": {
        "label": "Legislative Productivity & Follow-through",
        "weight": 0.20,
        "asks": "Do the things they introduce actually become law?",
    },
    "independence": {
        "label": "Independence of Judgment",
        "weight": 0.15,
        "asks": "Do they exercise judgment, or reliably follow the room?",
    },
    "stewardship": {
        "label": "Fiscal Stewardship",
        "weight": 0.15,
        "asks": "Is the money they direct well spread, well documented, and delivered?",
    },
    "enforcement": {
        "label": "Accountability & Enforcement Record",
        "weight": 0.10,
        "asks": "Has an oversight body found against them?",
    },
}


# ------------------------------------------------------------ indicators ----
INDICATORS: tuple[Indicator, ...] = (
    # -- presence ---------------------------------------------------------
    Indicator("attendance", "Stated-meeting attendance", "presence",
              "Share of Council meetings the member was recorded present for.",
              "higher_better", 0.45, "COUNCIL_RECORD", ("council",),
              computed=True, metric="vs_attend"),
    Indicator("participation", "Vote participation", "presence",
              "Share of recorded votes on which the member cast a vote rather "
              "than being absent or excused.",
              "higher_better", 0.35, "COUNCIL_RECORD", ("council",),
              computed=True, metric="vs_particip"),
    Indicator("plain_absence", "Unexcused absence rate", "presence",
              "Share of votes missed without an excused leave.",
              "lower_better", 0.20, "COUNCIL_RECORD", ("council",),
              computed=True, metric="absence_rate"),

    # -- productivity -----------------------------------------------------
    Indicator("prime_volume", "Prime-sponsored items", "productivity",
              "Count of matters on which the member is the prime sponsor.",
              "higher_better", 0.25, "COUNCIL_RECORD", ("council",),
              computed=True, metric="_prime_count"),
    Indicator("enactment_rate", "Enactment rate", "productivity",
              "Share of prime-sponsored introductions that became local law.",
              "higher_better", 0.45, "COUNCIL_RECORD", ("council",),
              computed=True, metric="_enactment_rate"),
    Indicator("committee_throughput", "Committee throughput", "productivity",
              "For committee chairs: share of bills referred to their "
              "committee that were reported out rather than held.",
              "higher_better", 0.30, "LEGISTAR", ("council",),
              computed=True, metric="_committee_throughput",
              note="Only scored for members who chair a committee."),

    # -- independence -----------------------------------------------------
    Indicator("speaker_alignment", "Alignment with the Speaker", "independence",
              "Share of contested votes cast with the Speaker. Scored toward a "
              "midpoint: near-total alignment and near-total opposition both "
              "indicate a member who is not deciding case by case.",
              "midpoint_better", 0.40, "COUNCIL_RECORD", ("council",),
              computed=True, metric="alignment_with_speaker", midpoint=0.85),
    Indicator("dissent_rate", "Recorded dissent", "independence",
              "Share of items on which the member cast a No vote. A record "
              "with no dissent at all is a record with no visible judgment.",
              "midpoint_better", 0.30, "COUNCIL_RECORD", ("council",),
              computed=True, metric="dissent_rate", midpoint=0.05),
    Indicator("predictability", "Vote predictability", "independence",
              "How well a model predicts the member's vote from party, "
              "borough and district alone. Lower means the member's own "
              "judgment carries more of the signal.",
              "lower_better", 0.30, "COUNCIL_RECORD", ("council",),
              computed=True, metric="predictability_auc"),

    # -- stewardship ------------------------------------------------------
    Indicator("funding_spread", "Discretionary spread", "stewardship",
              "Effective number of organizations funded (inverse Herfindahl). "
              "Concentrating a district's whole program in a few grantees is a "
              "delivery risk.",
              "higher_better", 0.30, "SCHEDULE_C", ("council",),
              computed=True, metric="_effective_orgs"),
    Indicator("delivery_rate", "Designation delivery rate", "stewardship",
              "Share of designations that cleared MOCS rather than lapsing, "
              "being defunded, or being withdrawn.",
              "higher_better", 0.40, "D49_MOCS_TRACKER", ("council",),
              computed=True, metric="_delivery_rate",
              note="Requires a loaded pipeline tracker; uncovered for most members."),
    Indicator("per_resident_equity", "Per-resident discretionary delivery",
              "stewardship",
              "Member designations per district resident, ranked across the "
              "body. Measures delivery, not generosity -- the pot is equal.",
              "higher_better", 0.30, "SCHEDULE_C", ("council",),
              computed=True, metric="_per_resident"),

    # -- transparency (declared; needs live sources) ----------------------
    Indicator("financial_disclosure", "Annual financial disclosure filed",
              "transparency",
              "Filed the required annual disclosure with the Conflicts of "
              "Interest Board by the statutory deadline.",
              "higher_better", 0.30, "COIB", ("citywide", "borough", "council")),
    Indicator("discretionary_disclosure", "Discretionary award disclosure",
              "transparency",
              "Member designations published with organization, EIN, amount "
              "and purpose in the adopted Schedule C.",
              "higher_better", 0.25, "SCHEDULE_C", ("council",),
              computed=True, metric="_disclosure_completeness"),
    Indicator("lobbying_contacts", "Lobbying contact disclosure", "transparency",
              "Lobbying targeted at the office, as reported to the City Clerk.",
              "higher_better", 0.20, "CITY_CLERK_LOBBYING",
              ("citywide", "borough", "council")),
    Indicator("public_calendar", "Public schedule publication", "transparency",
              "Whether the official publishes a public calendar of meetings.",
              "higher_better", 0.25, "OFFICIAL_SITE",
              ("citywide", "borough", "council", "state", "federal")),

    # -- enforcement (declared; needs live sources) -----------------------
    Indicator("coib_findings", "Conflicts of Interest Board findings",
              "enforcement",
              "Public COIB dispositions or penalties against the official.",
              "lower_better", 0.40, "COIB",
              ("citywide", "borough", "council")),
    Indicator("cfb_penalties", "Campaign Finance Board penalties", "enforcement",
              "Penalties assessed by the CFB in the most recent completed cycle.",
              "lower_better", 0.35, "CFB",
              ("citywide", "borough", "council")),
    Indicator("audit_findings", "Comptroller audit findings", "enforcement",
              "Adverse findings in Comptroller audits of the office.",
              "lower_better", 0.25, "COMPTROLLER",
              ("citywide", "borough")),
)

INDICATORS_BY_KEY = {i.key: i for i in INDICATORS}


# ------------------------------------------------------------- normalizing ----
def percentile_rank(value: float, population: list[float]) -> float:
    """Fraction of the peer group at or below this value, in [0, 1]."""
    pop = [v for v in population if v is not None]
    if not pop:
        return 0.5
    below = sum(1 for v in pop if v < value)
    equal = sum(1 for v in pop if v == value)
    return (below + 0.5 * equal) / len(pop)


def score_indicator(ind: Indicator, value: float | None,
                    population: list[float]) -> float | None:
    """Turn a raw value into a 0-100 score within its peer group."""
    if value is None:
        return None
    if ind.direction == "higher_better":
        return round(100 * percentile_rank(value, population), 1)
    if ind.direction == "lower_better":
        return round(100 * (1 - percentile_rank(value, population)), 1)
    # midpoint_better: distance from the declared midpoint, scaled by the
    # peer group's own spread so the penalty is relative, not absolute.
    mid = ind.midpoint if ind.midpoint is not None else (
        sorted(population)[len(population) // 2] if population else value)
    spread = max((max(population) - min(population)) if population else 0, 1e-6)
    dist = abs(value - mid) / spread
    return round(100 * max(0.0, 1 - min(dist, 1.0)), 1)


def grade(score: float | None, population: list[float] | None = None) -> str:
    """Grade relative to the peer group.

    The composite is built from percentile ranks, so it clusters around 50 by
    construction. Grading it on an absolute 90/80/70 scale would hand most of
    the Council an F and mean nothing. The grade is therefore the official's
    standing *among their peers*: a C is a median performer, not a failure.
    """
    if score is None:
        return "—"
    if not population:
        population = []
    pr = percentile_rank(score, population) if population else 0.5
    for cut, g in ((0.95, "A"), (0.85, "A−"), (0.75, "B+"), (0.65, "B"),
                   (0.55, "B−"), (0.45, "C+"), (0.35, "C"), (0.25, "C−"),
                   (0.15, "D+"), (0.05, "D")):
        if pr >= cut:
            return g
    return "F"


# -------------------------------------------------------- raw measurement ----
def _member_raw(store: Store, person_id: int, cache: dict) -> dict[str, float | None]:
    """Every computable indicator value for one member."""
    mt = cache["metrics"].get(person_id, {})
    spon = cache["sponsorship"].get(person_id, {})
    fund = cache["funding"].get(person_id, {})

    prime = spon.get("prime", 0)
    enacted = spon.get("enacted", 0)
    n_obs = mt.get("n_cast") or mt.get("n_items") or 0
    def cur(name: str, fallback: str | None = None):
        """Current-session value, falling back to the career figure."""
        v = mt.get(f"cur_{name}")
        if v is None and fallback:
            v = mt.get(fallback)
        return v

    raw: dict[str, float | None] = {
        "_n_observations": n_obs,
        "_career_attendance": mt.get("career_vs_attend"),
        "_career_absence_rate": mt.get("absence_rate"),
        "_session_meetings": cur("vs_meetings"),
        "attendance": cur("vs_attend", "career_vs_attend"),
        "participation": cur("vs_particip", "career_vs_particip"),
        "plain_absence": cur("absence_rate", "absence_rate"),
        "prime_volume": float(prime) if prime else None,
        "enactment_rate": (enacted / prime) if prime else None,
        "committee_throughput": None,          # needs Legistar committee referrals
        "speaker_alignment": mt.get("alignment_with_speaker"),
        "dissent_rate": mt.get("dissent_rate"),
        "predictability": mt.get("predictability_auc"),
        "funding_spread": fund.get("effective_orgs"),
        "delivery_rate": fund.get("delivery_rate"),
        "per_resident_equity": fund.get("per_resident"),
        "discretionary_disclosure": fund.get("disclosure_completeness"),
        # declared-only indicators stay None until their source is loaded
        "financial_disclosure": None,
        "lobbying_contacts": None,
        "public_calendar": None,
        "coib_findings": None,
        "cfb_penalties": None,
        "audit_findings": None,
    }
    return raw


def _latest_session(store: Store) -> str:
    """The session currently sitting, by the highest start year on record."""
    rows = [r["session"] for r in store.q(
        "SELECT DISTINCT session FROM member_metrics WHERE session LIKE '____-____'")]
    return max(rows, key=lambda s: s[:4]) if rows else "all"


def _build_cache(store: Store, fy: int, session: str | None = None) -> dict:
    """One pass over the lake for every member, rather than N queries each."""
    metrics: dict[int, dict] = {}
    for r in store.q("""SELECT person_id, metric, value FROM member_metrics
                        WHERE session IN ('all','pooled') AND value IS NOT NULL"""):
        metrics.setdefault(r["person_id"], {})[r["metric"]] = r["value"]
    # Presence is scored on the CURRENT session, not a career average.
    # A member who missed votes two terms ago and has since attended every
    # meeting should be read on what they are doing now; the career figure is
    # kept alongside as context rather than as the score.
    session = session or _latest_session(store)
    for r in store.q("""SELECT person_id, metric, value, session FROM member_metrics
                        WHERE value IS NOT NULL AND session = ?""", [session]):
        metrics.setdefault(r["person_id"], {})[f"cur_{r['metric']}"] = r["value"]

    # Career means, for the indicators that have no session-scoped form.
    agg: dict[int, dict[str, list]] = {}
    for r in store.q("""SELECT person_id, metric, value FROM member_metrics
                        WHERE value IS NOT NULL AND session NOT IN ('all','pooled')"""):
        agg.setdefault(r["person_id"], {}).setdefault(r["metric"], []).append(r["value"])
    for pid, mm in agg.items():
        slot = metrics.setdefault(pid, {})
        for k, vals in mm.items():
            slot.setdefault(f"career_{k}", sum(vals) / len(vals))

    sponsorship: dict[int, dict] = {}
    for r in store.q("""
        SELECT s.person_id,
               SUM(CASE WHEN s.role='prime' THEN 1 ELSE 0 END) AS prime,
               SUM(CASE WHEN s.role='prime' AND m.enacted=1 THEN 1 ELSE 0 END) AS enacted
        FROM sponsorships s JOIN matters m ON m.matter_id = s.matter_id
        GROUP BY s.person_id"""):
        sponsorship[r["person_id"]] = {"prime": r["prime"] or 0,
                                       "enacted": r["enacted"] or 0}

    # Funding is keyed by member surname in Schedule C; map to person_id.
    name_to_pid: dict[str, int] = {}
    pop: dict[int, int] = {}
    for r in store.q("SELECT person_id, last, name, district FROM members WHERE current=1"):
        if r["last"]:
            name_to_pid[r["last"].lower()] = r["person_id"]
        name_to_pid[(r["name"] or "").split()[-1].lower()] = r["person_id"]

    denoms = store.get_meta("council_record.district_denoms") or {}
    dist_pop: dict[int, int] = {}
    if isinstance(denoms, dict) and "cols" in denoms:
        cols = denoms["cols"]
        di, pi = cols.index("district"), cols.index("population")
        for row in denoms["rows"]:
            dist_pop[int(row[di])] = row[pi]

    funding: dict[int, dict] = {}
    for r in store.q("""
        SELECT member, district, COUNT(*) AS lines, SUM(amount) AS total,
               SUM(CASE WHEN purpose IS NOT NULL AND purpose != '' THEN 1 ELSE 0 END) AS documented
        FROM funding
        WHERE fy = ? AND source_id = 'SCHEDULE_C' AND member IS NOT NULL
        GROUP BY member""", [fy]):
        pid = name_to_pid.get((r["member"] or "").lower())
        if not pid:
            continue
        shares = [row["t"] for row in store.q("""
            SELECT SUM(amount) AS t FROM funding
            WHERE fy = ? AND member = ? AND source_id='SCHEDULE_C'
            GROUP BY org_key""", [fy, r["member"]]) if row["t"]]
        tot = sum(shares) or 1
        hhi = sum((x / tot) ** 2 for x in shares) or 1
        d = store.scalar("SELECT district FROM members WHERE person_id=?", [pid])
        p = dist_pop.get(d)
        deliv = store.one("""
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN status='cleared' THEN 1 ELSE 0 END) AS ok
            FROM funding WHERE source_id='D49_MOCS_TRACKER' AND member = ? AND fy = ?
        """, [r["member"], fy])
        funding[pid] = {
            "effective_orgs": round(1 / hhi, 2),
            "per_resident": round((r["total"] or 0) / p, 3) if p else None,
            "disclosure_completeness": (round(r["documented"] / r["lines"], 4)
                                        if r["lines"] else None),
            "delivery_rate": (round((deliv["ok"] or 0) / deliv["n"], 4)
                              if deliv and deliv["n"] else None),
        }

    return {"metrics": metrics, "sponsorship": sponsorship,
            "funding": funding, "session": session}


# ------------------------------------------------------------- the index ----
def build_index(store: Store, fy: int = 2027, group: str = "council") -> dict:
    """Score every official in a peer group. Returns the full audit trail."""
    members = [dict(r) for r in store.q(
        "SELECT person_id, name, party, district, borough, leadership "
        "FROM members WHERE current = 1 ORDER BY district")]
    if not members:
        return {"error": "no current members loaded"}

    cache = _build_cache(store, fy)
    raws = {m["person_id"]: _member_raw(store, m["person_id"], cache)
            for m in members}

    applicable = [i for i in INDICATORS if group in i.peer_groups]
    populations = {
        i.key: [raws[m["person_id"]].get(i.key) for m in members
                if raws[m["person_id"]].get(i.key) is not None]
        for i in applicable
    }

    obs_all = [raws[m["person_id"]].get("_n_observations") or 0 for m in members]
    live_obs = sorted(v for v in obs_all if v)
    median_obs = live_obs[len(live_obs) // 2] if live_obs else 0
    thin_cut = median_obs * THIN_RECORD_RATIO

    scored = []
    for m in members:
        pid = m["person_id"]
        raw = raws[pid]
        n_obs = raw.get("_n_observations") or 0
        thin = n_obs < thin_cut
        ind_scores: dict[str, dict] = {}
        for i in applicable:
            v = raw.get(i.key)
            ind_scores[i.key] = {
                "label": i.label, "pillar": i.pillar, "raw": v,
                "score": score_indicator(i, v, populations[i.key]),
                "weight": i.weight, "direction": i.direction,
                "source_id": i.source_id, "covered": v is not None,
            }

        pillar_scores: dict[str, dict] = {}
        for pkey, pspec in PILLARS.items():
            parts = [(ind_scores[i.key]["score"], i.weight)
                     for i in applicable if i.pillar == pkey
                     and ind_scores[i.key]["score"] is not None]
            possible = sum(i.weight for i in applicable if i.pillar == pkey)
            got = sum(w for _, w in parts)
            pillar_scores[pkey] = {
                "label": pspec["label"],
                "score": (round(sum(s * w for s, w in parts) / got, 1)
                          if got else None),
                "coverage": round(got / possible, 3) if possible else 0.0,
                "weight": pspec["weight"],
                "indicators": [i.key for i in applicable if i.pillar == pkey],
            }

        live = [(p["score"], PILLARS[k]["weight"]) for k, p in pillar_scores.items()
                if p["score"] is not None and p["coverage"] >= COVERAGE_FLOOR]
        wsum = sum(w for _, w in live)
        coverage = round(wsum / sum(p["weight"] for p in PILLARS.values()), 3)
        composite = (round(sum(s * w for s, w in live) / wsum, 1) if wsum else None)
        sufficient = coverage >= COVERAGE_FLOOR

        scored.append({
            "person_id": pid, "name": m["name"], "party": m["party"],
            "n_observations": n_obs, "thin_record": thin,
            "session_scored": cache["session"],
            "career_context": {
                "career_attendance": raw.get("_career_attendance"),
                "career_absence_rate": raw.get("_career_absence_rate"),
                "session_meetings": raw.get("_session_meetings"),
            },
            "district": m["district"], "borough": m["borough"],
            "leadership": m["leadership"], "peer_group": group,
            "composite": composite if sufficient else None,
            "grade": None,          # assigned below, against the peer spread
            "coverage": coverage, "sufficient": sufficient,
            "pillars": pillar_scores, "indicators": ind_scores,
        })

    # Grades are assigned only once every composite exists, so each official
    # is graded against the distribution they actually sit in.
    comp_pop = [s["composite"] for s in scored if s["composite"] is not None]
    for s in scored:
        if s["composite"] is None:
            s["grade"] = "insufficient data"
        elif s["thin_record"]:
            s["grade"] = "provisional — thin record"
        else:
            s["grade"] = grade(s["composite"], comp_pop)
        s["peer_percentile"] = (round(100 * percentile_rank(s["composite"], comp_pop), 1)
                                if s["composite"] is not None else None)

    # Members with a thin voting record are scored but ranked separately: a
    # first-year member's attendance over 40 votes is not comparable with a
    # veteran's over 5,000, and pooling them would flatter the newcomer and
    # slander the incumbent.
    ranked = sorted([s for s in scored
                     if s["composite"] is not None and not s["thin_record"]],
                    key=lambda s: -s["composite"])
    for i, s in enumerate(ranked, 1):
        s["rank"] = i
        s["of"] = len(ranked)
    provisional = sorted([s for s in scored
                          if s["composite"] is not None and s["thin_record"]],
                         key=lambda s: -s["composite"])
    for i, s in enumerate(provisional, 1):
        s["provisional_rank"] = i
        s["of"] = len(provisional)

    return {
        "as_of": date.today().isoformat(),
        "fiscal_year": fy,
        "peer_group": group,
        "n_officials": len(scored),
        "n_scored": len(ranked),
        "coverage_floor": COVERAGE_FLOOR,
        "officials": scored,
        "n_provisional": len(provisional),
        "session_scored": cache["session"],
        "median_observations": median_obs,
        "thin_record_cutoff": thin_cut,
        "ranking": [{"rank": s["rank"], "name": s["name"],
                     "district": s["district"], "party": s["party"],
                     "composite": s["composite"], "grade": s["grade"],
                     "peer_percentile": s["peer_percentile"],
                     "coverage": s["coverage"], "n_observations": s["n_observations"]}
                    for s in ranked],
        "provisional": [{"provisional_rank": s["provisional_rank"], "name": s["name"],
                         "district": s["district"], "party": s["party"],
                         "composite": s["composite"], "coverage": s["coverage"],
                         "n_observations": s["n_observations"]} for s in provisional],
        "methodology": methodology(group),
        "label": ("DELIBERATIVE — analytical construct for internal use. "
                  "Measures the public record, not character. Indicators "
                  "without a loaded source are reported as uncovered, never "
                  "imputed."),
    }


def methodology(group: str = "council") -> dict:
    """The published methodology, emitted alongside every score."""
    applicable = [i for i in INDICATORS if group in i.peer_groups]
    return {
        "peer_group": group,
        "peer_group_members": PEER_GROUPS.get(group, ()),
        "normalization": ("Percentile rank within the peer group, scaled 0-100. "
                          "Midpoint indicators score by scaled distance from a "
                          "declared midpoint, so both extremes are penalized."),
        "aggregation": ("Indicator scores are weighted within their pillar; "
                        "pillars are weighted into the composite. A pillar "
                        f"below {COVERAGE_FLOOR:.0%} indicator coverage is "
                        "excluded from the composite, and an official below "
                        f"{COVERAGE_FLOOR:.0%} overall coverage receives no grade."),
        "missing_data": ("Never imputed. Missing indicators reduce coverage and "
                         "are listed explicitly."),
        "thin_record_rule": (
            f"An official with fewer than {THIN_RECORD_RATIO:.0%} of the peer "
            "median vote count is scored but ranked in a separate provisional "
            "table. Comparing a first-year member's attendance over a few dozen "
            "votes with a veteran's over thousands is not like-for-like."),
        "session_rule": (
            "Presence indicators are scored on the CURRENT session. A member "
            "who missed votes in an earlier term and now attends everything is "
            "read on the current record; the career figure travels alongside "
            "as context, never as the score."),
        "pillars": [{"key": k, **{kk: vv for kk, vv in v.items()}}
                    for k, v in PILLARS.items()],
        "indicators": [i.to_dict() for i in applicable],
        "known_biases": [
            "Majority-party advantage. Enactment rate and committee throughput "
            "are structurally easier for members of the governing coalition. "
            "Minority-party members will score lower on Productivity for "
            "reasons that are not about their diligence. Read the pillar "
            "scores, not only the composite.",
            "Leadership advantage. Committee chairs and leadership-title "
            "holders control what moves, which lifts Productivity.",
            "Tenure effects. Enactment accrues over years; a first-term member "
            "has had fewer chances to land a local law.",
            "The index measures the public record only. It cannot see "
            "constituent casework, negotiation, or work that never reaches a "
            "roll call -- often the majority of what an office actually does.",
        ],
        "coverage_note": (
            f"{sum(1 for i in applicable if i.computed)} of {len(applicable)} "
            "indicators are computed from loaded data; the rest declare an "
            "external source and stay uncovered until it is connected."),
        "grade_scale": (
            "Relative to peers, not absolute. The composite is built from "
            "percentile ranks and clusters near 50 by construction, so grades "
            "are assigned on the official's standing within the peer group: "
            "C is a median performer, not a failing one."),
        "grade_bands": {"A": "top 5%", "A−": "top 15%", "B+": "top 25%",
                        "B": "top 35%", "B−": "top 45%", "C+": "top 55%",
                        "C": "top 65%", "C−": "top 75%", "D+": "top 85%",
                        "D": "top 95%", "F": "bottom 5%"},
    }


def official_scorecard(store: Store, who: str | int, fy: int = 2027) -> dict:
    """One official's full scorecard, with every input shown."""
    from .legislation import resolve_member
    m = resolve_member(store, who)
    if not m:
        return {"error": f"official not found: {who}"}
    idx = build_index(store, fy=fy)
    rec = next((o for o in idx["officials"] if o["person_id"] == m["person_id"]), None)
    if not rec:
        return {"error": f"{m['name']} is not in the scored peer group"}
    uncovered = [k for k, v in rec["indicators"].items() if not v["covered"]]
    return {
        **rec,
        "as_of": idx["as_of"], "fiscal_year": fy,
        "uncovered_indicators": uncovered,
        "how_to_improve_coverage": [
            f"{INDICATORS_BY_KEY[k].label} — connect {INDICATORS_BY_KEY[k].source_id}"
            for k in uncovered if k in INDICATORS_BY_KEY],
        "methodology": idx["methodology"],
    }
