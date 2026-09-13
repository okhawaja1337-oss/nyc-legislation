#!/usr/bin/env python3
"""
The workspace assistant.

Ask a question in plain language and get an answer built from the office's own
record, with the evidence attached. Ask for a deliverable -- talking points, a
press statement, a quote, a hearing question set, a constituent reply, a
newsletter item -- and get a draft in the house style that a staffer can edit
and the Councilmember can read.

The order is the whole design, and it is the same order the rest of the system
uses: **retrieve, then write.** The assistant searches the index, pulls the
matching funding lines, bills, hearings and clips, and hands that packet to the
model. The model writes over evidence it was given; it never supplies the
facts. With no API key configured the assistant still answers -- with the
evidence, the figures and the structure, minus the prose.

Two rules are enforced in code rather than asked for in a prompt:

* **A quote must come from a recording.** ``quote`` only ever returns verbatim
  excerpts from a stored transcript, with the clip's URL. The assistant cannot
  manufacture something the Member never said, because it has no path to.
* **A figure must come from the lake.** Numbers in a draft are computed here
  and passed in; anything the model adds that is not in the packet is flagged
  by the same verification the briefing engine uses.
"""
from __future__ import annotations

import json
import re
import textwrap
from datetime import date, datetime, timezone
from typing import Any, Iterable

from ..ai.council import BASE_CONTEXT, ask_model, available, deliberate
from ..core.citations import VERIFY_TAG
from ..core.config import DISTRICT, DISTRICT_LABEL, HOUSE_STYLE, MEMBER_NAME, PILLARS
from . import events, media, search

# What the assistant can be asked to produce.
DELIVERABLES = {
    "answer": "A direct answer to a staff question, with the evidence behind it.",
    "talking_points": "Six sayable lines for a hearing, a reporter or a walk-through.",
    "quote": "A quotation for release — drawn only from a recorded transcript.",
    "press_statement": "A short statement for press, in the Member's register.",
    "press_release": "A full release: headline, dateline, body, quote, boilerplate.",
    "hearing_questions": "Three questions the Member can ask out loud in a hearing.",
    "constituent_reply": "A plain-English reply to a constituent, with the referral.",
    "newsletter": "A short newsletter item for the district list.",
    "memo": "An internal memo in the office's brief format.",
    "social": "Two or three short posts, plain and factual.",
}

REGISTERS = ("measured", "firm", "urgent", "warm")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------- retrieval --
def gather(store, question: str, limit: int = 24) -> dict:
    """Assemble the evidence packet for a question. This runs first, always."""
    hits = search.search_nl(store, question, limit=limit, with_facets=True)
    rows = hits.get("rows", [])

    by_kind: dict[str, list[dict]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)

    # A total is only computed when retrieval was precise. A loose any-word
    # match returns related records, not matching ones -- summing those
    # produces a figure that looks authoritative and is not, which is the one
    # kind of mistake this system exists to prevent.
    strategy = hits.get("strategy", "exact")
    money = [r for r in rows if r["kind"] == "funding" and r.get("amount")]
    totals = {}
    if money and strategy == "any-word":
        totals = {
            "suppressed": True,
            "matched_lines": len(money),
            "why": ("No total is shown. This search matched records containing "
                    "any of the search words, not all of them, so the lines "
                    "returned are related rather than matching. Narrow the "
                    "question — or use a field filter such as "
                    "`kind:funding org:\"…\" fy:2027` — before quoting a figure."),
        }
    elif money:
        totals = {
            "matched_lines": len(money),
            "matched_total": round(sum(r["amount"] or 0 for r in money), 2),
            "by_fy": {},
            "caveat": ("This is the sum of the funding lines this search "
                       "matched. It is not a reconciled budget total and must "
                       "not be published as one."),
        }
        for r in money:
            fy = r.get("fy")
            if fy:
                totals["by_fy"][fy] = round(
                    totals["by_fy"].get(fy, 0) + (r["amount"] or 0), 2)

    clips = media.quotable(store, topic=_topic(question), limit=6)
    return {
        "question": question,
        "hit_count": hits.get("total", 0),
        "search_ms": hits.get("ms"),
        "strategy": hits.get("strategy"),
        "retrieval_note": hits.get("note"),
        "facets": {k: v for k, v in hits.get("facets", {}).items()
                   if not k.startswith("_")},
        "by_kind": {k: v[:8] for k, v in by_kind.items()},
        "funding_totals": totals,
        "quotable_clips": clips,
        "gaps": _gaps(by_kind, clips),
    }


def _topic(question: str) -> str:
    """The longest meaningful phrase in the question, for clip matching."""
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", question or "")
             if w.lower() not in {"what", "which", "where", "when", "does",
                                  "have", "has", "with", "that", "this",
                                  "from", "about", "they", "been", "were"}]
    return " ".join(words[:2])


def _gaps(by_kind: dict, clips: list) -> list[str]:
    gaps = []
    if not by_kind:
        gaps.append("Nothing in the loaded record matched this question.")
    if not clips:
        gaps.append("No recorded clip with a transcript matched, so no "
                    "quotation can be drawn for release.")
    if "funding" not in by_kind:
        gaps.append("No funding lines matched; any dollar figure must be "
                    "sourced separately.")
    return gaps


def _packet(evidence: dict, limit: int = 9000) -> str:
    trimmed = dict(evidence)
    trimmed["quotable_clips"] = [
        {k: v for k, v in c.items() if k != "excerpts"} | {
            "excerpts": c.get("excerpts", [])[:2]}
        for c in evidence.get("quotable_clips", [])]
    return json.dumps(trimmed, indent=1, default=str)[:limit]


# ------------------------------------------------------------- generation --
def ask(store, question: str, kind: str = "answer", register: str = "measured",
        audience: str = "", council: bool = False, actor: str = "") -> dict:
    """The one entry point. Retrieve, then write, then record."""
    if kind not in DELIVERABLES:
        raise ValueError(f"Unknown deliverable. Choose one of: "
                         f"{', '.join(DELIVERABLES)}.")
    if register not in REGISTERS:
        register = "measured"

    evidence = gather(store, question)

    if kind == "quote":
        result = _quote(evidence)
    else:
        result = _write(store, question, kind, register, audience, evidence,
                        council)

    result.update(question=question, kind=kind, register=register,
                  evidence=evidence, created=now(),
                  ai=available()["mode"])
    events.emit(store, "assistant.answer", "assistant", kind,
                f"{DELIVERABLES[kind].split('.')[0]} — {question[:60]}", actor,
                {"hits": evidence["hit_count"], "mode": result["ai"]})
    return result


def _quote(evidence: dict) -> dict:
    """Quotations come only from recordings. No transcript, no quote."""
    clips = evidence.get("quotable_clips") or []
    if not clips:
        return {
            "body": "",
            "quotes": [],
            "blocked": True,
            "why": ("No recorded clip with a stored transcript matched this "
                    "topic, so there is nothing the Member is on record saying "
                    "about it. Attach a transcript to a clip in the media "
                    "library, or write a new statement for approval instead of "
                    "quoting one."),
        }
    quotes = []
    for c in clips:
        for ex in c.get("excerpts", []):
            quotes.append({"text": ex, "source": c["title"], "url": c["url"],
                           "published": c.get("published"),
                           "committee": c.get("committee")})
    return {"quotes": quotes[:6], "blocked": False,
            "body": "\n\n".join(f'"{q["text"]}"\n— {MEMBER_NAME}, {q["source"]}'
                                for q in quotes[:3]),
            "why": "Verbatim from stored transcripts. Confirm against the "
                   "recording before release."}


SHAPES = {
    "talking_points": "Six numbered lines. Each is one sentence the Member can "
                      "say out loud. No preamble.",
    "press_statement": "Three to five sentences in the Member's voice. Lead "
                       "with what she is doing, not what she thinks.",
    "press_release": "FOR IMMEDIATE RELEASE, a headline, a Staten Island "
                     "dateline, three short body paragraphs, one quote clearly "
                     "marked [DRAFT QUOTE — REQUIRES MEMBER APPROVAL], and a "
                     "one-line boilerplate.",
    "hearing_questions": "Exactly three questions, each answerable by the "
                         "witness and each following from the evidence.",
    "constituent_reply": "A short, warm reply in plain English. Say what the "
                         "office will do, by when, and who to contact.",
    "newsletter": "A 120-word item: what happened, what it means for the North "
                  "Shore, and one link.",
    "memo": "The house brief: bottom line, bullets, District 49 impact, three "
            "questions, recommendation.",
    "social": "Two or three posts under 280 characters. Factual, no hashtags "
              "beyond one.",
    "answer": "A direct answer of two to five sentences, then the bullets that "
              "support it.",
}


def _write(store, question: str, kind: str, register: str, audience: str,
           evidence: dict, council: bool) -> dict:
    shape = SHAPES.get(kind, SHAPES["answer"])
    rules = "\n".join(f"- {r}" for r in HOUSE_STYLE["rules"])
    prompt = textwrap.dedent(f"""
        Produce: {DELIVERABLES[kind]}

        REQUEST
        {question}

        SHAPE
        {shape}

        REGISTER: {register}{f" · AUDIENCE: {audience}" if audience else ""}

        HOUSE RULES
        {rules}

        RETRIEVAL: {evidence.get('strategy', 'exact')}
        {evidence.get('retrieval_note') or ''}

        EVIDENCE (the only facts you may use)
        {_packet(evidence)}

        If RETRIEVAL is "any-word", the records below are related to the
        question rather than answers to it: do not total them, and say plainly
        that the question needs narrowing.

        Every figure must appear in the evidence above. If a number is not
        there, write {VERIFY_TAG} instead of supplying one. Do not write a
        quotation attributed to {MEMBER_NAME} or to anyone else -- a quote must
        come from a recording, and this is not one. Where the evidence is thin,
        say so in one line at the end under "Gaps".
    """).strip()

    body = ask_model(prompt, max_tokens=1800)
    deliberation = None
    if council:
        deliberation = deliberate(
            f"{DELIVERABLES[kind]} — {question}", _packet(evidence))
        if deliberation.synthesis:
            body = deliberation.synthesis

    if not body:
        body = _scaffold(question, kind, evidence)
        mode = "evidence-only"
    else:
        mode = "written"

    return {"body": body, "mode": mode, "blocked": False,
            "council": deliberation.to_dict() if deliberation else None,
            "unverified": body.count(VERIFY_TAG)}


def _scaffold(question: str, kind: str, evidence: dict) -> str:
    """What the assistant returns with no model configured: the evidence.

    This is deliberately still useful. The staffer gets the matching records,
    the computed totals with their caveat, and the structure to write into --
    which is most of the work, and none of the risk.
    """
    out = [f"# {DELIVERABLES[kind]}", "", f"**Request.** {question}", "",
           f"*{evidence['hit_count']:,} records matched in "
           f"{evidence.get('search_ms', 0)} ms. No language model is configured, "
           f"so this is the evidence and the shape to write into — not a draft.*",
           ""]
    if evidence.get("strategy") == "any-word":
        out += [f"> **Loose match.** {evidence.get('retrieval_note', '')} "
                f"Treat these as related records, not as the answer.", ""]
    totals = evidence.get("funding_totals") or {}
    if totals.get("suppressed"):
        out += ["## Money matched", "",
                f"- {totals['matched_lines']:,} funding lines were returned, "
                f"but **no total is shown**.",
                f"- {totals['why']}", ""]
    elif totals:
        out += ["## Money matched", "",
                f"- {totals['matched_lines']:,} funding lines, "
                f"${totals['matched_total']:,.0f} in total",
                *[f"- FY{fy}: ${amt:,.0f}"
                  for fy, amt in sorted(totals.get("by_fy", {}).items())],
                f"- {totals['caveat']}", ""]
    for kindname, rows in (evidence.get("by_kind") or {}).items():
        out.append(f"## {kindname.title()} ({len(rows)} shown)")
        out += [f"- {r.get('title', '')[:110]}"
                + (f" — ${r['amount']:,.0f}" if r.get("amount") else "")
                for r in rows]
        out.append("")
    clips = evidence.get("quotable_clips") or []
    out.append("## On the record")
    out += ([f"- {c['title']} — {c.get('url') or 'no link'}" for c in clips]
            or ["- No recorded clip with a transcript matched. Nothing can be "
                "quoted for release on this topic."])
    if evidence.get("gaps"):
        out += ["", "## Gaps", *[f"- {g}" for g in evidence["gaps"]]]
    out += ["", "---",
            f"*Prepared for {MEMBER_NAME}, {DISTRICT_LABEL}. Figures trace to "
            f"the loaded record; confirm before release.*"]
    return "\n".join(out)


# ------------------------------------------------------------- suggestions --
def prompts(store) -> list[dict]:
    """Starter questions, grounded in what is actually loaded."""
    return [
        {"label": "Who did we fund for seniors last year, and for how much?",
         "kind": "answer"},
        {"label": "Talking points on the FY2027 Staten Island funding position",
         "kind": "talking_points"},
        {"label": "Three hearing questions on the MOCS pipeline backlog",
         "kind": "hearing_questions"},
        {"label": "Press statement on a Bay Street development approval",
         "kind": "press_statement"},
        {"label": "Newsletter item on lapsed North Shore grantees",
         "kind": "newsletter"},
        {"label": "Constituent reply: no heat and hot water for a week",
         "kind": "constituent_reply"},
    ]


def history(store, limit: int = 40) -> list[dict]:
    return [dict(r) for r in store.q(
        "SELECT * FROM ws_events WHERE kind='assistant.answer' "
        "ORDER BY id DESC LIMIT ?", [limit])]
