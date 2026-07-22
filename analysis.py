#!/usr/bin/env python3
"""
analysis.py — data-driven political analysis over the loaded NYC legislation.

Everything here is a PURE function over the row dicts the app already builds
(each row carries File, Type, Status, Prime Sponsor, Topic tags, and the private
_sponsor_names list). No network, no Streamlit, no AI — so it's unit-testable and
its outputs are transparent, auditable numbers rather than a black box.

Two sensitive things live here and are handled carefully:

  * "Estimated issue lean" — a guess at where a member sits on a topic, inferred
    ONLY from what they've sponsored. It is explicitly NOT a prediction of their
    floor vote or a statement of their position; the labels and the returned
    `caveat` say so, and the evidence (which bills) travels with the estimate.
  * "Swing / persuadable members" — a heuristic ranking to seed a whip strategy,
    not a claim about anyone's private intentions.

Both are framed as starting points for a human, grounded in public records.
"""

from collections import Counter

LEAN_CAVEAT = ("Estimated from public sponsorship activity only — NOT a floor "
               "vote, a prediction, or a statement of the member's position. "
               "Sponsorship shows interest/alignment, not how someone will vote.")


def _last(name):
    parts = (name or "").split()
    return parts[-1].lower() if parts else (name or "").lower()


def member_names(rows):
    """Every member who appears as a sponsor in the loaded set, with bill counts."""
    counts = {}
    for r in rows:
        for n in r.get("_sponsor_names", []) or []:
            if n:
                counts[n] = counts.get(n, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


def _member_rows(rows, member):
    last = _last(member)
    return [r for r in rows if any(last in (n or "").lower() for n in r.get("_sponsor_names", []) or [])]


def topic_match(row, topic):
    """Does a row relate to a free-text topic? Matches Topic tags + Title + type."""
    t = (topic or "").strip().lower()
    if not t:
        return False
    blob = " ".join([row.get("Topic tags", "") or "", row.get("Title", "") or "",
                     row.get("Type", "") or ""]).lower()
    # match on any word of the topic (so "public safety" hits either word)
    words = [w for w in t.replace("/", " ").split() if len(w) > 2]
    return any(w in blob for w in words) if words else t in blob


def member_issue_signal(rows, member, topic):
    """Counts of a member's engagement with a topic (prime vs co-sponsor)."""
    last = _last(member)
    mine = _member_rows(rows, member)
    on_topic = [r for r in mine if topic_match(r, topic)]
    prime = [r for r in on_topic if last in (r.get("Prime Sponsor", "") or "").lower()]
    return {"member": member, "topic": topic, "on_topic": len(on_topic),
            "as_prime": len(prime), "as_cosponsor": len(on_topic) - len(prime),
            "examples": [r.get("File") for r in on_topic[:6]], "total_bills": len(mine)}


def estimate_lean(rows, member, topic):
    """A labeled, evidence-carrying estimate of a member's lean on a topic."""
    s = member_issue_signal(rows, member, topic)
    if s["as_prime"] >= 1:
        lean, conf = "Likely supportive — leads on it", "moderate"
    elif s["as_cosponsor"] >= 2:
        lean, conf = "Leans supportive", "low–moderate"
    elif s["on_topic"] == 1:
        lean, conf = "Some engagement", "low"
    else:
        lean, conf = "No record on this issue", "n/a"
    return {"member": member, "topic": topic, "lean": lean, "confidence": conf,
            "signal": s, "caveat": LEAN_CAVEAT}


def _partner_breadth(rows, member):
    """How many distinct colleagues a member co-sponsors with (coalition breadth)."""
    last = _last(member)
    partners = set()
    for r in _member_rows(rows, member):
        for n in r.get("_sponsor_names", []) or []:
            if n and last not in n.lower():
                partners.add(n)
    return len(partners)


def _passed(status):
    return any(w in (status or "").lower() for w in ("enact", "adopt", "approv", "passed"))


def committee_stats(rows):
    """Per-committee throughput from the loaded bills.

    Returns rows of {committee, total, enacted, in_committee, other, pass_rate}
    sorted by volume — a quick read on which committees move bills vs. sit on them.
    """
    d = {}
    for r in rows:
        c = (r.get("Committee/Body") or "").strip()
        if not c:
            continue
        v = d.setdefault(c, {"committee": c, "total": 0, "enacted": 0, "in_committee": 0, "other": 0})
        v["total"] += 1
        s = (r.get("Status") or "").lower()
        if _passed(s):
            v["enacted"] += 1
        elif "committee" in s:
            v["in_committee"] += 1
        else:
            v["other"] += 1
    out = list(d.values())
    for v in out:
        v["pass_rate"] = round(100 * v["enacted"] / v["total"]) if v["total"] else 0
    out.sort(key=lambda x: -x["total"])
    return out


def committee_row(rows, committee):
    """Throughput for one committee (or None)."""
    committee = (committee or "").strip()
    for v in committee_stats(rows):
        if v["committee"] == committee:
            return v
    return None


def policy_topics(rows):
    """Distinct policy topics present in the loaded set (from Topic tags)."""
    s = set()
    for r in rows:
        for p in (r.get("Topic tags") or "").split("; "):
            if p.strip():
                s.add(p.strip())
    return sorted(s)


def engagement_matrix(rows, top_members=25, topics=None):
    """Member × topic engagement counts (bills a member sponsored per topic).

    Returns (members, topics, matrix{member:{topic:count}}). Members are the most
    active first so the grid leads with the people who legislate the most.
    """
    topics = topics or policy_topics(rows)
    members = list(member_names(rows).keys())[:top_members]
    mat = {m: {t: 0 for t in topics} for m in members}
    for m in members:
        for r in _member_rows(rows, m):
            for p in (r.get("Topic tags") or "").split("; "):
                p = p.strip()
                if p in mat[m]:
                    mat[m][p] += 1
    return members, topics, mat


def vote_signal(vote_events, member):
    """Tally how a member actually voted across roll-call events (from fetch_votes).

    vote_events is a list of {votes:[{Member,Vote}], ...}. Returns aye/nay/other.
    """
    last = _last(member)
    aye = nay = other = 0
    for ev in vote_events or []:
        for v in ev.get("votes", []) or []:
            nm = (v.get("Member") or "")
            if last not in nm.lower():
                continue
            val = (v.get("Vote") or "").lower()
            if any(w in val for w in ("affirm", "aye", "yes")):
                aye += 1
            elif any(w in val for w in ("negativ", "nay", "no ")):
                nay += 1
            else:
                other += 1
    return {"aye": aye, "nay": nay, "other": other, "total": aye + nay + other}


def blend_lean(sponsor_estimate, vote_counts):
    """Combine the sponsorship estimate with any ACTUAL votes (votes win when present).

    Returns an updated estimate dict with a `blended` lean + `vote_counts`.
    """
    est = dict(sponsor_estimate)
    est["vote_counts"] = vote_counts
    v = vote_counts or {}
    total = v.get("total", 0)
    if total:
        if v["aye"] and not v["nay"]:
            blended = f"Supportive on the record — voted YES on {v['aye']} related bill(s)"
        elif v["nay"] and not v["aye"]:
            blended = f"Opposed on the record — voted NO on {v['nay']} related bill(s)"
        else:
            blended = f"Mixed voting record — {v['aye']} yes / {v['nay']} no on related bills"
        est["blended"] = blended
        est["confidence"] = "higher (includes actual votes)"
    else:
        est["blended"] = est.get("lean")
    return est


SIGNON_CAVEAT = ("Predicts CO-SPONSORSHIP propensity from historical patterns "
                 "(topic focus + who a member usually signs on with) — not a floor "
                 "vote, a stated position, or a commitment. A whip/drafting aid.")


def member_topic_weights(rows, member):
    """Normalized topic focus for a member (fraction of their bills per topic)."""
    mine = _member_rows(rows, member)
    c = Counter()
    for r in mine:
        for p in (r.get("Topic tags") or "").split("; "):
            p = p.strip()
            if p:
                c[p] += 1
    total = sum(c.values()) or 1
    return {k: v / total for k, v in c.items()}, len(mine)


def member_partners_w(rows, member):
    """Normalized coalition weights — how often each colleague co-sponsors with them."""
    last = _last(member)
    c = Counter()
    for r in _member_rows(rows, member):
        for n in r.get("_sponsor_names", []) or []:
            if n and last not in n.lower():
                c[n] += 1
    total = sum(c.values()) or 1
    return {k: v / total for k, v in c.items()}


def aggregate_vote_lean(items):
    """Per-topic aye-rate (0..1) from a member's actual votes on similar bills.

    items: [{"topics": [...], "aye": bool}]. Topics with no votes are omitted.
    """
    agg = {}
    for it in items or []:
        a = it.get("aye")
        if a is None:
            continue
        for tp in it.get("topics", []) or []:
            d = agg.setdefault(tp, {"aye": 0, "n": 0})
            d["aye"] += 1 if a else 0
            d["n"] += 1
    return {tp: d["aye"] / d["n"] for tp, d in agg.items() if d["n"]}


# 311 complaint types that stand in for each policy topic (constituent demand).
TOPIC_311 = {
    "housing": ["HEAT/HOT WATER", "PLUMBING", "PAINT/PLASTER", "UNSANITARY CONDITION",
                "GENERAL", "DOOR/WINDOW", "WATER LEAK"],
    "sanitation": ["Dirty Condition", "Sanitation Condition", "Missed Collection",
                   "Overflowing Litter Baskets", "Electronics Waste"],
    "transportation": ["Street Condition", "Illegal Parking", "Traffic Signal Condition",
                       "Broken Muni Meter", "Street Light Condition", "Blocked Driveway"],
    "environment": ["Air Quality", "Water System", "Water Quality"],
    "parks": ["Maintenance or Facility", "Damaged Tree", "Overgrown Tree/Branches"],
    "health": ["Rodent", "Food Establishment", "Unsanitary Animal Pvt Property", "Standing Water"],
    "public safety": ["Noise - Residential", "Noise - Street/Sidewalk", "Blocked Driveway",
                      "Drug Activity", "Graffiti"],
    "consumer": ["Consumer Complaint"],
}


def demand_from_311(counts_by_type):
    """Normalized per-topic constituent demand (0..1) from a {complaint_type: count} dict."""
    if not counts_by_type:
        return {}
    raw = {topic: sum(counts_by_type.get(t, 0) for t in types)
           for topic, types in TOPIC_311.items()}
    mx = max(raw.values()) or 1
    return {t: c / mx for t, c in raw.items() if c > 0}


# Base weights for each signal; the composite renormalizes over whichever are present.
_SIGNON_WEIGHTS = {"topic": 0.28, "coalition": 0.20, "votes": 0.20,
                   "demand": 0.14, "stance": 0.12, "momentum": 0.06}


def signon_score(row, topic_w, partner_w, vote_lean=None, demand=None,
                 stance=None, momentum_max=None):
    """0–100 propensity that a member signs THIS bill — a MULTI-FACTOR composite.

    Combines whichever signals are available, each scored 0..1, then a weighted
    average renormalized over the present factors (so it's never "just one thing"):
      - topic     : sponsorship focus overlap
      - coalition : overlap with who the member usually co-sponsors with
      - votes     : aye-rate on similar roll-calls (behavior)         [if vote_lean]
      - demand    : 311 constituent-demand on the bill's topics       [if demand]
      - stance    : public-statement lean on the topic, -1..1 -> 0..1 [if stance]
      - momentum  : how many have already signed (bandwagon)          [if momentum_max]
    Returns (score, breakdown) where breakdown carries every factor used.
    """
    tags = [p.strip() for p in (row.get("Topic tags") or "").split("; ") if p.strip()]
    sponsors = row.get("_sponsor_names") or []
    factors = {}
    factors["topic"] = min(sum(topic_w.get(x, 0.0) for x in tags), 1.0)
    factors["coalition"] = min(sum(partner_w.get(n, 0.0) for n in sponsors), 1.0)
    if vote_lean:
        vs = [vote_lean[x] for x in tags if x in vote_lean]
        if vs:
            factors["votes"] = sum(vs) / len(vs)
    if demand:
        ds = [demand[x] for x in tags if x in demand]
        if ds:
            factors["demand"] = sum(ds) / len(ds)
    if stance:
        ss = [stance[x] for x in tags if x in stance]
        if ss:
            factors["stance"] = max(0.0, min(1.0, (sum(ss) / len(ss) + 1) / 2))
    if momentum_max:
        try:
            n = int(row.get("Sponsors (#)") or len(sponsors))
        except (TypeError, ValueError):
            n = len(sponsors)
        factors["momentum"] = min(n / max(momentum_max, 1), 1.0)
    wsum = sum(_SIGNON_WEIGHTS[k] for k in factors) or 1
    comp = sum(v * _SIGNON_WEIGHTS[k] for k, v in factors.items()) / wsum
    shared = [n for n in sponsors if partner_w.get(n, 0) > 0]
    why = {k: round(v, 2) for k, v in factors.items()}
    why.update({"matched_topics": [x for x in tags if topic_w.get(x, 0) > 0],
                "shared_sponsors": shared[:5], "factors_used": list(factors.keys())})
    return round(100 * comp), why


def _momentum_max(rows):
    best = 1
    for r in rows:
        try:
            n = int(r.get("Sponsors (#)") or len(r.get("_sponsor_names") or []))
        except (TypeError, ValueError):
            n = len(r.get("_sponsor_names") or [])
        best = max(best, n)
    return best


def predict_signons(rows, member, top=12, vote_lean=None, demand=None, stance=None):
    """For a member: bills they're most / least likely to co-sponsor (excludes ones they're on).

    Multi-factor: topic + coalition + optional votes/311-demand/statements + momentum.
    """
    tw, nbills = member_topic_weights(rows, member)
    pw = member_partners_w(rows, member)
    mmax = _momentum_max(rows)
    last = _last(member)
    scored = []
    for r in rows:
        if any(last in (n or "").lower() for n in (r.get("_sponsor_names") or [])):
            continue  # already a sponsor
        s, why = signon_score(r, tw, pw, vote_lean=vote_lean, demand=demand,
                              stance=stance, momentum_max=mmax)
        scored.append({"File": r["File"], "Title": (r.get("Title") or "")[:90],
                       "Status": r.get("Status", ""), "Committee": r.get("Committee/Body", ""),
                       "Prime": r.get("Prime Sponsor", ""), "score": s, "why": why})
    scored.sort(key=lambda x: -x["score"])
    used = sorted({k for x in scored for k in x["why"].get("factors_used", [])})
    return {"member": member, "record_size": nbills, "total_candidates": len(scored),
            "likely": [x for x in scored if x["score"] > 0][:top],
            "unlikely": scored[::-1][:top], "caveat": SIGNON_CAVEAT,
            "vote_blended": bool(vote_lean), "signals_used": used}


def predict_supporters(rows, bill_row, top=15, demand=None):
    """For a bill: members most / least likely to sign on (excludes current sponsors)."""
    on = {(n or "").lower() for n in (bill_row.get("_sponsor_names") or [])}
    mmax = _momentum_max(rows)
    results = []
    for m in member_names(rows):
        if any(_last(m) in n for n in on):
            continue
        tw, _n = member_topic_weights(rows, m)
        pw = member_partners_w(rows, m)
        s, why = signon_score(bill_row, tw, pw, demand=demand, momentum_max=mmax)
        results.append({"member": m, "score": s, "why": why})
    results.sort(key=lambda x: -x["score"])
    return {"bill": bill_row.get("File", ""), "total": len(results),
            "likely": results[:top], "unlikely": results[::-1][:top], "caveat": SIGNON_CAVEAT}


def relevant_committee_members(committees, topic):
    """Members of committees whose name relates to the topic (from get_committees())."""
    t = (topic or "").lower()
    words = [w for w in t.replace("/", " ").split() if len(w) > 2]
    hit = set()
    matched = []
    for c in committees or []:
        name = (c.get("committee") or "").lower()
        if any(w in name for w in words):
            matched.append(c.get("committee"))
            for m in c.get("members", []) or []:
                hit.add(m)
    return hit, matched


def swing_members(rows, topic, committees=None, top=12):
    """Rank persuadable members for an issue. Returns scored rows + the reasons.

    Heuristic (transparent): a member is more 'persuadable' when they sit on the
    relevant committee (real leverage), have *some but not heavy* engagement with
    the topic (interested, not locked in), and bridge many colleagues (movable
    coalition-builder). Members already leading the issue score lower — they're
    already with you.
    """
    committees = committees or []
    on_committee, matched_coms = relevant_committee_members(committees, topic)
    names = member_names(rows)
    max_breadth = max((_partner_breadth(rows, m) for m in names), default=1) or 1
    scored = []
    for m in names:
        sig = member_issue_signal(rows, m, topic)
        reasons = []
        score = 0.0
        if m in on_committee:
            score += 2.5; reasons.append("on a relevant committee")
        eng = sig["on_topic"]
        if sig["as_prime"] >= 1:
            score -= 1.0; reasons.append("already leads on the issue (likely already with you)")
        elif eng == 0:
            score += 1.0; reasons.append("no fixed record here — open to persuasion")
        elif eng <= 2:
            score += 2.0; reasons.append(f"light engagement ({eng} bill{'s' if eng != 1 else ''}) — interested, not locked in")
        else:
            score += 0.5; reasons.append("engaged but not leading")
        breadth = _partner_breadth(rows, m)
        b = breadth / max_breadth
        if b >= 0.5:
            score += 1.0; reasons.append("broad cross-member coalition (movable)")
        scored.append({"member": m, "score": round(score, 2), "on_committee": m in on_committee,
                       "topic_bills": eng, "coalition_breadth": breadth, "reasons": reasons})
    scored.sort(key=lambda x: (-x["score"], -x["coalition_breadth"]))
    return {"topic": topic, "matched_committees": matched_coms,
            "candidates": scored[:top],
            "note": "Heuristic starting list from sponsorship + committee data — a whip aid, "
                    "not a claim about anyone's private intentions. Confirm directly."}
