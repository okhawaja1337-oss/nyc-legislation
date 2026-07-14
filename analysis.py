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
