#!/usr/bin/env python3
"""
A brief on whatever you just found.

The system could brief three things: a Councilmember, the fiscal position, and
one bill by its Legistar id. Everything else a staffer found in search --- an
organisation with eleven years of awards, a funding line that reversed, a
reconciliation row, a whole fiscal year --- they had to write up by hand, from
a table on a screen, retyping the figures.

Retyping is where the errors come from. Not carelessness: the number is on one
screen and the memo is on another, and somewhere between them a 5 becomes a 6
or last year's total gets pasted under this year's heading. The system knew the
right figure the whole time and had no way to hand it over in the shape the
Councilmember reads.

So: any record can be briefed, every brief is built from an evidence packet
assembled by query rather than by hand, and every figure in it carries the
citation that proves it. The house shape holds throughout --- a short bottom
line, the details as bullets, what it means for District 49, and three
questions --- because that is what gets read.

Where a model is configured it writes the prose from the packet and never
supplies the facts. Where one is not, the brief still stands: the evidence,
the figures and the structure are all assembled here, in Python, from the
corpus. A brief that degrades to a scaffold and says so is worth more than one
that reads well and cannot be traced.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..core import reference as REF
from ..core.config import DISTRICT, MEMBER_NAME
from ..core.store import Store
from .brief import Brief

MONEY = 1000.0


def _money(n: Any) -> str:
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "—"
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:,.2f}M"
    return f"${v:,.0f}"


def _kind_of(key: str) -> str:
    return (key or "").split(":", 1)[0] or "record"


# ------------------------------------------------------------ evidence ----
def evidence(store: Store, key: str) -> dict:
    """
    Assemble everything the corpus knows about one record.

    The packet is the brief's whole factual basis: the figure gate later
    checks that every number in the prose appears here, so anything the brief
    is allowed to say has to be put in here first, by query.
    """
    kind = _kind_of(key)
    builder = PACKETS.get(kind)
    if builder is None:
        return {"error": f"Nothing knows how to brief a {kind!r}.",
                "key": key}
    packet = builder(store, key.split(":", 1)[-1])
    packet["key"] = key
    packet["kind"] = kind
    return packet


def _matter_packet(store: Store, mid: str) -> dict:
    from ..intel import legislation as LI

    rows = store.q("SELECT * FROM matters WHERE matter_id = ?", (mid,))
    if not rows:
        return {"error": f"No matter {mid} in the corpus."}
    m = dict(rows[0])
    text = store.q("SELECT * FROM matter_text WHERE matter_id = ?",
                   (int(mid),))
    t = dict(text[0]) if text else {}
    sponsors = [dict(r) for r in store.q(
        "SELECT s.person_id, s.role, mb.name, mb.district, mb.party "
        "FROM sponsorships s LEFT JOIN members mb ON mb.person_id = s.person_id "
        "WHERE s.matter_id = ? ORDER BY s.role = 'prime' DESC, mb.name", (mid,))]
    si = [s for s in sponsors if str(s.get("district")) in ("49", "50", "51")]
    whip = LI.whip_count(store, int(mid))
    history = json.loads(t.get("history") or "[]")
    return {
        "matter": m,
        "summary": t.get("summary") or "",
        "title": t.get("title") or "",
        "text_excerpt": (t.get("body") or "")[:4000],
        "has_text": bool(t.get("body")),
        "intro_date": t.get("intro_date"),
        "attachments": json.loads(t.get("attachments") or "[]")[:8],
        "history": history[-8:],
        "sponsors": sponsors,
        "si_sponsors": si,
        "hanks_sponsors": any(str(s.get("district")) == str(DISTRICT)
                              for s in sponsors),
        "whip": whip if "error" not in whip else None,
        "url": t.get("url") or
               f"https://legistar.council.nyc.gov/LegislationDetail.aspx?ID={mid}",
        "sources": ["LEGISTAR_MIRROR", "COUNCIL_RECORD"],
    }


def _funding_packet(store: Store, line: str) -> dict:
    rows = store.q("SELECT * FROM funding WHERE line_id = ?", (line,))
    if not rows:
        return {"error": f"No funding line {line}."}
    f = dict(rows[0])
    key = f.get("org_key")
    history = [dict(r) for r in store.q(
        "SELECT fy, member, district, amount, program, agency, tier, source_id, "
        "line_id FROM funding WHERE org_key = ? ORDER BY fy, amount DESC",
        (key,))] if key else []
    by_year: dict[int, float] = {}
    for h in history:
        by_year[h["fy"]] = by_year.get(h["fy"], 0.0) + (h["amount"] or 0.0)
    years = sorted(by_year)
    return {
        "line": f, "org": f.get("org"), "ein": f.get("ein"),
        "history": history, "by_year": {str(y): by_year[y] for y in years},
        "first_year": years[0] if years else None,
        "last_year": years[-1] if years else None,
        "years_funded": len(years),
        "lifetime": round(sum(by_year.values()), 2),
        "latest": by_year[years[-1]] if years else None,
        "previous": by_year[years[-2]] if len(years) > 1 else None,
        "sources": sorted({h["source_id"] for h in history if h.get("source_id")})
                   or ["SCHEDULE_C"],
    }


def _org_packet(store: Store, org_key: str) -> dict:
    rows = [dict(r) for r in store.q(
        "SELECT fy, member, district, amount, program, agency, tier, org, ein, "
        "source_id, line_id FROM funding WHERE org_key = ? ORDER BY fy DESC, "
        "amount DESC", (org_key,))]
    if not rows:
        return {"error": f"Nothing funded under {org_key}."}
    by_year: dict[int, float] = {}
    by_member: dict[str, float] = {}
    for r in rows:
        by_year[r["fy"]] = by_year.get(r["fy"], 0.0) + (r["amount"] or 0.0)
        if r.get("member"):
            by_member[r["member"]] = by_member.get(r["member"], 0.0) + (r["amount"] or 0.0)
    years = sorted(by_year)
    return {
        "org": rows[0]["org"], "ein": rows[0].get("ein"), "lines": rows[:60],
        "by_year": {str(y): by_year[y] for y in years},
        "by_member": dict(sorted(by_member.items(), key=lambda kv: -kv[1])[:10]),
        "years_funded": len(years), "lifetime": round(sum(by_year.values()), 2),
        "latest": by_year[years[-1]] if years else None,
        "previous": by_year[years[-2]] if len(years) > 1 else None,
        "in_d49": any(str(r.get("district")) == str(DISTRICT) for r in rows),
        "sources": sorted({r["source_id"] for r in rows if r.get("source_id")}),
    }


def _ledger_packet(store: Store, row_id: str) -> dict:
    rows = store.q("SELECT * FROM ledger WHERE row_id = ?", (row_id,))
    if not rows:
        return {"error": f"No reconciliation row {row_id}."}
    r = dict(rows[0])
    siblings = [dict(x) for x in store.q(
        "SELECT label, amount, kind, row_no FROM ledger WHERE book = ? AND "
        "sheet = ? AND kind IN ('total','tie_check') ORDER BY row_no LIMIT 12",
        (r["book"], r["sheet"]))]
    from ..intel import ledger as LG
    return {
        "row": r, "cells": json.loads(r.get("cells") or "[]"),
        "headers": json.loads(r.get("headers") or "[]"),
        "sheet_totals": siblings,
        "sheet_ties": LG.sheet_ties(store, r["book"], r["sheet"]),
        "position": LG.position(store),
        "sources": [r.get("source_id") or "OFFICE-WB"],
    }


def _fy_packet(store: Store, year: str) -> dict:
    from ..intel import funding as FI
    from ..intel import ledger as LG
    fy = int(year)
    return {
        "fy": fy,
        "d49": FI.member_portfolio(store, "Hanks", fy),
        "equity": FI.district_equity(store, fy),
        "pipeline": FI.pipeline_risk(store, fy),
        "position": LG.position(store, fy),
        "sources": ["SCHEDULE_C", "TRANSPARENCY_RESO", "OFFICE-WB"],
    }


PACKETS = {
    "matter": _matter_packet,
    "funding": _funding_packet,
    "org": _org_packet,
    "ledger": _ledger_packet,
    "fy": _fy_packet,
}


# -------------------------------------------------------------- briefs ----
def brief(store: Store, key: str, ask: str = "",
          with_council: bool = False) -> Brief:
    """Brief any record the office can find."""
    ev = evidence(store, key)
    if "error" in ev:
        return Brief(subject=key, kind="record",
                     bottom_line=ev["error"], evidence=ev)
    writer = WRITERS[_kind_of(key)]
    out = writer(store, ev)
    out.evidence = ev
    out.sources_used = ev.get("sources", [])
    if ask:
        out.questions.insert(0, ask)
    if with_council:
        from .council import deliberate
        from .brief import packet_text
        out.council = deliberate(
            question=ask or f"What should District {DISTRICT} do about "
                            f"{out.subject}?",
            evidence=packet_text(ev))
        out.mode = "council"
    return out


def _matter_brief(store: Store, ev: dict) -> Brief:
    m = ev["matter"]
    file_no = m.get("file") or f"Matter {m.get('matter_id')}"
    name = (m.get("name") or "").strip()
    summary = (ev.get("summary") or "").strip()
    status = m.get("status") or "unknown status"
    whip = ev.get("whip") or {}
    n = m.get("n_sponsors") or 0

    # The bottom line says what the bill does and where it stands. Where the
    # Council wrote its own summary, that is the sentence -- it is the
    # official description, and paraphrasing it only introduces drift.
    lead = summary.split("\n")[0][:420] if summary else name
    bottom = (f"{file_no} — {lead} It is currently **{status}** "
              f"in the Committee on {m.get('committee')}."
              if m.get("committee") else
              f"{file_no} — {lead} It is currently **{status}**.")

    details = []
    if summary and name and name[:60] not in summary:
        details.append(f"Short title: {name}")
    details.append(f"{n} sponsor{'s' if n != 1 else ''} of 51. "
                   + (f"Prime sponsor: {ev['sponsors'][0].get('name')}."
                      if ev.get("sponsors") else "No prime sponsor recorded."))
    if m.get("enacted"):
        law = m.get("local_law")
        if law and "/" in str(law):
            y, _, num = str(law).partition("/")
            details.append(f"Enacted as Local Law {int(num)} of {y}.")
        else:
            details.append("Enacted.")
    elif m.get("pass_prob") is not None:
        details.append(
            f"Modelled chance of enactment {float(m['pass_prob']) * 100:.1f}% "
            f"— from sponsor count, re-introduction and committee, not a "
            f"reading of the politics.")
    if ev.get("intro_date"):
        details.append(f"Introduced {ev['intro_date']}.")
    if ev.get("history"):
        last = ev["history"][-1]
        details.append(f"Last action: {last.get('action')} on {last.get('date')}.")
    if not ev.get("has_text"):
        details.append("No bill text has synced from Legistar yet — read the "
                       "official page before relying on the substance.")

    impact = []
    si = ev.get("si_sponsors") or []
    if ev.get("hanks_sponsors"):
        impact.append(f"{MEMBER_NAME} is already a sponsor.")
    elif si:
        who = ", ".join(s.get("name") or "" for s in si if s.get("name"))
        impact.append(f"Staten Island is on it through {who} — "
                      f"{MEMBER_NAME} is not.")
    else:
        impact.append("No Staten Island member sponsors this. Signing on "
                      "would put the borough on the record first.")
    if whip.get("needed") is not None:
        impact.append(f"{whip.get('have', 0)} of 26 votes committed; "
                      f"{whip['needed']} more needed to pass.")

    questions = [
        f"Does {file_no} help or hurt the North Shore specifically, or is it "
        f"citywide with no district effect?",
        "Is there a Staten Island carve-out or implementation cost the "
        "sponsor has not accounted for?",
        ("Sign on now, or hold until the committee report?" if not
         ev.get("hanks_sponsors") else
         "Should we seek to be heard at the hearing, or is sponsorship enough?"),
    ]
    b = Brief(subject=f"{file_no} — {name[:80]}", kind="Bill brief",
              bottom_line=bottom, details=details, d49_impact=impact,
              questions=questions)
    b.talking_points = _matter_points(ev)
    return b


def _matter_points(ev: dict) -> list[str]:
    m, out = ev["matter"], []
    summary = (ev.get("summary") or "").strip()
    if summary:
        out.append(summary.split(".")[0].strip() + ".")
    n = m.get("n_sponsors") or 0
    if n >= 26:
        out.append(f"It already has {n} sponsors — a majority of the Council.")
    elif n:
        out.append(f"It has {n} sponsors; 26 are needed to pass.")
    if ev.get("si_sponsors"):
        out.append("Staten Island is represented on the sponsor list.")
    return out


def _funding_brief(store: Store, ev: dict) -> Brief:
    f = ev["line"]
    org = f.get("org") or "this designation"
    fy = f.get("fy")
    amount = f.get("amount")
    latest, prev = ev.get("latest"), ev.get("previous")

    trend = ""
    if latest is not None and prev:
        delta = latest - prev
        pct = (delta / prev * 100) if prev else 0
        trend = (f" Across all sources the organisation's total moved from "
                 f"{_money(prev)} to {_money(latest)}, {pct:+.1f}%.")

    bottom = (f"{org} holds {_money(amount)} in FY{fy}"
              + (f", designated by {f['member']}" if f.get("member") else "")
              + f". It has been funded in {ev['years_funded']} of the years "
                f"the corpus covers, {_money(ev['lifetime'])} in total."
              + trend)

    details = [f"Programme: {f.get('program') or '—'}"
               + (f" · Agency: {f['agency']}" if f.get("agency") else ""),
               f"Tier: {f.get('tier') or 'adopted'}"
               + (" — this line is not yet confirmed money and must not be "
                  "announced." if str(f.get("tier", "")).upper().startswith(
                      "ADOPTED-PENDING") else "")]
    if f.get("ein"):
        details.append(f"EIN {f['ein']} — Form 990 at "
                       f"projects.propublica.org/nonprofits")
    if ev.get("by_year"):
        series = " · ".join(f"FY{y} {_money(v)}"
                            for y, v in list(ev["by_year"].items())[-6:])
        details.append(f"By year: {series}")
    if str(f.get("tier", "")).upper() == "REVERSED":
        details.append("REVERSED — money that was announced and then pulled "
                       "back. Check what the recipient was told.")

    impact = []
    if str(f.get("district")) == str(DISTRICT):
        impact.append(f"Coded to District {DISTRICT}.")
    elif f.get("district"):
        impact.append(f"Coded to District {f['district']}, not {DISTRICT} — "
                      f"it may still serve North Shore residents.")
    else:
        impact.append("Citywide or borough-coded, so it does not appear in "
                      f"District {DISTRICT}'s Schedule C total even if the "
                      f"service is delivered here.")

    return Brief(
        subject=f"{org} — FY{fy}", kind="Funding brief",
        bottom_line=bottom, details=details, d49_impact=impact,
        questions=[
            f"Is {_money(amount)} the right level for FY{int(fy) + 1}, given "
            f"the trend above?",
            "What did this money actually buy — how many people served, and "
            "can the organisation show it?",
            "If this lapsed, who else in the district delivers this service?"])


def _org_brief(store: Store, ev: dict) -> Brief:
    org = ev["org"]
    latest, prev = ev.get("latest"), ev.get("previous")
    move = ""
    if latest is not None and prev:
        move = (f" Its most recent year moved {(latest - prev) / prev * 100:+.1f}% "
                f"on the year before.")
    funders = ", ".join(f"{k} ({_money(v)})"
                        for k, v in list(ev.get("by_member", {}).items())[:4])
    bottom = (f"{org} has received {_money(ev['lifetime'])} across "
              f"{ev['years_funded']} fiscal years, most recently "
              f"{_money(latest)}.{move}")
    details = [f"Principal funders: {funders or '—'}",
               f"By year: " + " · ".join(f"FY{y} {_money(v)}"
                                         for y, v in ev["by_year"].items())]
    if ev.get("ein"):
        details.append(f"EIN {ev['ein']}")
    impact = [f"Funded in District {DISTRICT}." if ev.get("in_d49") else
              f"Not coded to District {DISTRICT} in any year — confirm whether "
              f"it serves North Shore residents before citing it as ours."]
    return Brief(
        subject=org, kind="Organisation brief", bottom_line=bottom,
        details=details, d49_impact=impact,
        questions=[
            "Is the funding level rising, flat or falling in real terms?",
            f"Does District {DISTRICT} get a share proportionate to what it "
            f"contributes to this organisation's case?",
            "What is the reporting obligation, and has it been met?"])


def _ledger_brief(store: Store, ev: dict) -> Brief:
    r = ev["row"]
    ties = ev.get("sheet_ties")
    standing = ("This sheet carries a passing tie-check, so the figure has "
                "been checked against the City's printed book."
                if ties else
                "This sheet carries no passing tie-check, so the figure is the "
                "office's own working number and has not been proved against "
                "the printed book.")
    bottom = (f"{r.get('label')} — {_money(r.get('amount'))}. "
              f"Read from {r.get('locator')}. {standing}")
    details = [f"Row type: {r.get('kind')}"]
    if ev.get("headers") and ev.get("cells"):
        pairs = [f"{h}: {c}" for h, c in zip(ev["headers"], ev["cells"]) if c]
        details.append(" · ".join(pairs[:6]))
    for t in ev.get("sheet_totals", [])[:4]:
        details.append(f"On the same sheet — {t['label']}: {_money(t['amount'])}")
    impact = [b["caution"] for b in ev.get("position", {}).get("bases", [])
              if b.get("key") == "d49_wins"][:1]
    return Brief(
        subject=str(r.get("label"))[:90], kind="Reconciliation brief",
        bottom_line=bottom, details=details,
        d49_impact=impact or ["Part of the District 49 reconciliation."],
        questions=[
            "Which basis is the room asking about — designations, district "
            "wins, or the island total?",
            "Has the source book been reissued since this sheet was tied out?",
            "Is this figure safe to say in public, or does it need the "
            "caveat above?"])


def _fy_brief(store: Store, ev: dict) -> Brief:
    pos = ev.get("position", {})
    bases = {b["key"]: b for b in pos.get("bases", [])}
    wins = bases.get("d49_wins", {}).get("amount")
    desig = bases.get("designations", {}).get("amount")
    fy = ev["fy"]
    bottom = (f"In FY{fy} the office secured {_money(wins)} for the North "
              f"Shore, of which {_money(desig)} was designated directly by "
              f"{MEMBER_NAME}. The rest came through capital, the Speaker's "
              f"initiatives, the delegation pot and citywide programmes.")
    details = [f"{b['question']} {_money(b['amount'])}"
               for b in pos.get("bases", []) if b.get("amount")]
    tc = pos.get("tie_checks", {})
    if tc:
        details.append(f"{tc.get('passing')} of {tc.get('total')} "
                       f"reconciliation checks passing.")
    return Brief(
        subject=f"FY{fy} — the District 49 position", kind="Fiscal year brief",
        bottom_line=bottom, details=details,
        d49_impact=["Never quote one of these figures without the question it "
                    "answers — the gap between them is a factor of twelve."],
        questions=[
            f"Which FY{fy} commitments are still pending a budget modification?",
            "What lapsed or reversed since adoption?",
            f"What is the ask for FY{fy + 1}, and what is the argument for it?"])


WRITERS = {
    "matter": _matter_brief, "funding": _funding_brief, "org": _org_brief,
    "ledger": _ledger_brief, "fy": _fy_brief,
}


def can_brief(key: str) -> bool:
    return _kind_of(key) in PACKETS


def citations(store: Store, ev: dict) -> list[str]:
    """The bibliography for a packet, ready to append to a brief."""
    rows: list[dict] = []
    for field in ("matter", "line", "row"):
        if isinstance(ev.get(field), dict):
            rows.append({**ev[field], "kind": ev.get("kind")})
    rows += [{**r, "kind": "funding"} for r in (ev.get("lines") or [])[:5]]
    return REF.bibliography(store, rows)
