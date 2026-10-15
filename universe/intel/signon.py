#!/usr/bin/env python3
"""
Sign on, watch, or decline — and why.

The office could already list bills touching its priorities. A list is not a
recommendation, and the difference is the whole job: forty candidate bills
arrive every week and the scarce thing is not finding them, it is deciding.

So every live bill gets a verdict and the reasoning behind it, drawn from what
the corpus actually knows:

    Does it serve one of the five pillars, and which?
    Does it reach District 49 — the North Shore specifically, or citywide?
    Where is the Staten Island delegation? Carr and Morano on it, or not?
    Does her name change the arithmetic, or is it already at 26?
    Has she worked with the prime sponsor before?
    What has happened to bills amending this section of the code?

The verdicts are deliberately few, because a scale with seven points is a
scale nobody uses:

    SIGN      the case is clear on the office's own doctrine
    WATCH     it matters, but something is unresolved — usually the district
              effect, or it is too early in committee to spend the name
    DECLINE   it cuts against the doctrine, or the cost outweighs the gain
    LETTER    not hers to sign — another body's bill, or already past the
              point of sponsorship — but a letter of support would land

Nothing here is automatic. Every verdict carries its reasons so the
Councilmember can overrule it in one read, which is the point: a recommender
whose reasoning is hidden gets ignored the first time it is wrong.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from ..core.config import DISTRICT, MEMBER_NAME, PILLARS, SI_DISTRICTS
from ..core.stage import label as stage_label
from ..core.store import Store
from . import legislation as LI

MAJORITY = 26
VETO_PROOF = 34
# Below this the bill is early enough that a name costs little and buys
# position; above it the name is arithmetic rather than signal.
EARLY = 10

# Words in a bill's name or summary that put it inside the office's land-use
# doctrine, or against it. Written down rather than inferred, because a
# recommender that cannot be argued with will not be trusted with land use.
DOCTRINE_FOR = (
    "homeownership", "home ownership", "first-time", "affordable homeowner",
    "condo", "co-op", "community land trust", "senior housing",
    "aging in place", "workforce housing", "mitchell-lama",
)
DOCTRINE_AGAINST = (
    "as-of-right upzoning", "eliminate parking minimum",
    "override local zoning", "preempt community board",
)
SI_WORDS = ("staten island", "north shore", "richmond county", "st. george",
            "stapleton", "tompkinsville", "port richmond", "west brighton",
            "mariners harbor", "clifton", "rosebank", "new brighton")


def _pillars(raw: Any) -> list[str]:
    try:
        return [p for p in json.loads(raw or "[]") if p in PILLARS]
    except (TypeError, ValueError):
        return []


def _pillar_labels(keys: Iterable[str]) -> list[str]:
    return [PILLARS[k].get("label", k) if isinstance(PILLARS.get(k), dict)
            else k.replace("_", " ").title() for k in keys]


def assess(store: Store, matter_id: int, member: str = MEMBER_NAME) -> dict:
    """One bill, one verdict, with everything the verdict rests on."""
    rows = store.q(
        "SELECT m.*, mb.name AS prime_name, mb.district AS prime_district, "
        "mb.party AS prime_party FROM matters m "
        "LEFT JOIN members mb ON mb.person_id = m.prime_id "
        "WHERE m.matter_id = ?", (matter_id,))
    if not rows:
        return {"error": f"No matter {matter_id}."}
    m = dict(rows[0])
    text = store.q("SELECT summary, body FROM matter_text WHERE matter_id = ?",
                   (matter_id,))
    t = dict(text[0]) if text else {}
    blob = " ".join(filter(None, [m.get("name"), t.get("summary"),
                                  (t.get("body") or "")[:4000]])).lower()

    sponsors = [dict(r) for r in store.q(
        "SELECT s.person_id, s.role, mb.name, mb.district, mb.party "
        "FROM sponsorships s LEFT JOIN members mb ON mb.person_id = s.person_id "
        "WHERE s.matter_id = ?", (matter_id,))]
    already = any((r["name"] or "").lower().find(member.split()[-1].lower()) >= 0
                  for r in sponsors)
    si_on = [r for r in sponsors
             if str(r.get("district") or "") in {str(d) for d in SI_DISTRICTS}]
    n = m.get("n_sponsors") or len(sponsors)

    pillars = _pillars(m.get("pillars"))
    si_named = [w for w in SI_WORDS if w in blob]
    doctrine_for = [w for w in DOCTRINE_FOR if w in blob]
    doctrine_against = [w for w in DOCTRINE_AGAINST if w in blob]

    from . import law
    touches = law.for_matter(store, matter_id)
    hardest = None
    for ref in touches.get("amends", [])[:3]:
        p = law.precedent(store, ref["section"])
        if not p.get("attempts") or p.get("rate") is None:
            continue
        # `p["rate"] or 1` reads a 0% success rate as 1.0, because 0.0 is
        # falsy. The effect is exactly inverted: the sections nobody has ever
        # succeeded on -- the ones most worth warning about -- were the only
        # ones that could never be selected as the hardest, or trip the
        # warning below. Compare the number, not its truthiness.
        if hardest is None or p["rate"] < hardest["rate"]:
            hardest = {**p, "section": ref["section"]}

    reasons: list[str] = []
    against: list[str] = []

    if pillars:
        reasons.append(f"Serves {', '.join(_pillar_labels(pillars))}.")
    if si_named:
        reasons.append(f"Names Staten Island or a North Shore neighbourhood "
                       f"({si_named[0]}) in its text.")
    if doctrine_for:
        reasons.append(f"Inside the office's land-use doctrine "
                       f"({doctrine_for[0]}).")
    if doctrine_against:
        against.append(f"Cuts against the office's land-use doctrine "
                       f"({doctrine_against[0]}). Read the text before signing.")

    if si_on and not already:
        who = ", ".join(r["name"] for r in si_on if r.get("name"))
        reasons.append(f"Staten Island is already on it — {who}. "
                       f"{MEMBER_NAME} is not, which is visible.")
    elif not si_on and not already:
        reasons.append("No Staten Island member sponsors this. Signing puts "
                       "the borough on the record first.")

    if n < EARLY:
        reasons.append(f"Early — {n} sponsor(s). A name costs little now and "
                       f"buys position.")
    elif n >= MAJORITY:
        against.append(f"Already at {n} sponsors, past the {MAJORITY} needed. "
                       f"Her name is signal, not arithmetic.")
    else:
        reasons.append(f"{n} of {MAJORITY} sponsors — {MAJORITY - n} short. "
                       f"Her name still moves it.")

    if hardest is not None and hardest["rate"] < 0.25:
        against.append(
            f"§ {hardest['section']} is historically hard: "
            f"{hardest['enacted']} of {hardest['attempts']} attempts became "
            f"law ({hardest['rate']:.0%}).")

    session_now = LI.current_session(store)
    stale_session = (str(m.get("session") or "") != str(session_now)
                     and m.get("stage") == "live")
    if stale_session:
        against.append(
            f"From the {m.get('year')} session, which has closed. Legistar "
            f"still shows it in committee because it applies "
            f"\u201cFiled (End of Session)\u201d in bulk, months late.")

    if already:
        verdict, headline = "ALREADY", f"{MEMBER_NAME} is already a sponsor."
    elif stale_session:
        verdict = "DECLINE"
        headline = ("Died with the last session. Nothing to sign — if the "
                    "policy still matters, the move is to re-introduce it.")
    elif m.get("stage") != "live":
        verdict = "LETTER"
        headline = (f"Not signable — {stage_label(m.get('stage') or 'unknown')}. "
                    f"A letter of support is the remaining lever.")
    elif doctrine_against:
        verdict, headline = "DECLINE", "Cuts against the office's doctrine."
    elif pillars and (si_named or si_on or n < EARLY) and not against:
        verdict, headline = "SIGN", "Clear on the office's own doctrine."
    elif pillars or si_named:
        verdict = "WATCH"
        headline = ("Relevant, but something is unresolved — read the reasons "
                    "against before committing the name.")
    else:
        verdict = "DECLINE"
        headline = ("Touches none of the five pillars and names no District 49 "
                    "interest.")

    return {
        "matter_id": matter_id, "file": m.get("file"), "name": m.get("name"),
        "status": m.get("status"), "stage": m.get("stage"),
        "committee": m.get("committee"), "prime": m.get("prime_name"),
        "session": m.get("session"), "stale_session": stale_session,
        "prime_party": m.get("prime_party"),
        "prime_district": m.get("prime_district"),
        "n_sponsors": n, "pillars": pillars, "already": already,
        "si_sponsors": [r["name"] for r in si_on if r.get("name")],
        "verdict": verdict, "headline": headline,
        "for": reasons, "against": against,
        "law": touches.get("amends", [])[:5], "precedent": hardest,
        "url": ("https://legistar.council.nyc.gov/LegislationDetail.aspx"
                f"?ID={matter_id}"),
        "source_id": m.get("source_id") or "LEGISTAR_MIRROR",
    }


def queue(store: Store, limit: int = 25, verdicts: Iterable[str] = (),
          pillars: Iterable[str] | None = None) -> dict:
    """
    This week's sign-on decisions, ranked by how much the name would matter.

    Ranked rather than listed. A bill four sponsors short of a majority that
    touches the North Shore is a different decision from one at thirty-eight
    sponsors that does not, and presenting them in the same order wastes the
    reader on the second.
    """
    candidates = LI.pending_for_pillars(store, pillars, limit=limit * 6)
    items = candidates.get("items", [])
    out: list[dict] = []
    for row in items:
        got = assess(store, row["matter_id"])
        if "error" in got:
            continue
        got["weight"] = _weight(got)
        out.append(got)
    wanted = {v.upper() for v in verdicts} if verdicts else None
    if wanted:
        out = [o for o in out if o["verdict"] in wanted]
    out.sort(key=lambda o: -o["weight"])
    tally: dict[str, int] = {}
    for o in out:
        tally[o["verdict"]] = tally.get(o["verdict"], 0) + 1
    return {"considered": len(items), "returned": len(out[:limit]),
            "by_verdict": tally, "items": out[:limit],
            "says": _says(tally, len(items))}


def _weight(a: dict) -> float:
    """How much this decision is worth the Councilmember's attention."""
    w = 0.0
    w += 3.0 * len(a["pillars"])
    w += 4.0 if a["si_sponsors"] else 0.0
    w += 5.0 if any("Staten Island" in r or "North Shore" in r
                    for r in a["for"]) else 0.0
    n = a["n_sponsors"] or 0
    if n < MAJORITY:
        # Closest to the line matters most: a bill two short is a decision,
        # one at thirty-eight is a formality.
        w += 6.0 * (n / MAJORITY)
    else:
        w -= 2.0
    w += {"SIGN": 6.0, "WATCH": 3.0, "LETTER": 2.0,
          "DECLINE": 0.0, "ALREADY": -5.0}.get(a["verdict"], 0.0)
    w -= 1.5 * len(a["against"])
    return round(w, 2)


def _says(tally: dict, considered: int) -> str:
    if not tally:
        return ("Nothing live matches the office's pillars right now. That is "
                "a real answer, not an empty one.")
    parts = [f"{v} {k.lower()}" for k, v in sorted(tally.items(),
                                                   key=lambda kv: -kv[1])]
    return (f"{considered} live bills considered: " + ", ".join(parts)
            + ". Ranked by how much her name would change the outcome.")
