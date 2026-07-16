#!/usr/bin/env python3
"""
lawwiki.py — a "wiki page" for a single bill/law.

Goes past a summary to the questions a Council office actually asks: has this been
tried somewhere else and how did it go, what are the alternative designs, where in
the city does it bite hardest, what would it actually take to make it work, and
would it move the needle. Web-search-enabled so precedents are real, not
imagined.

Takes an `llm.LLM`; returns Markdown. No Streamlit, no network here directly.
"""

import json

WIKI_STYLE = (
    "You are a nonpartisan NYC legislative policy analyst writing an internal "
    "wiki entry on a bill. Ground every claim in the bill text or the provided "
    "data; when you use general knowledge or web results, say so and cite the "
    "place/source. Never invent NYC statistics, dollar figures, or bill numbers "
    "— name the dataset or source to check instead. Mark predictions as "
    "predictions. Be concrete and practical, not generic."
)

WIKI_PROMPT = """Write a wiki entry for this NYC bill. Use web search for real
precedents and comparisons. Output Markdown with EXACTLY these sections:

## {file} — what it is
**In plain English:** 1–2 sentences.
- 3–4 bullets on the actual mechanism: what legally changes, who's covered, triggers/dates if stated.

## Tried elsewhere
- Real examples of similar laws/programs in other cities or states (name the place and, where known, the year and result). Search the web. If you can't verify a case, say so rather than inventing one.

## Alternatives & variations
- 3–4 concrete alternative designs or amendments (stronger, weaker, cheaper, phased, pilot-first) and the trade-off of each.

## Where it hits hardest in NYC
- Which boroughs/neighborhoods/districts are most affected and why (use the bill's named boroughs/topic; reason from the data). Note if it's uniform citywide.

## What it takes to work
- The lead agency; the funding, staffing, data, and enforcement needed; and the biggest implementation risk. Name the NYC Open Data / agency source that would size each need — do not fabricate figures.

## Would it move the needle
- A candid, labeled prediction of impact (high/medium/low) with the reasoning and the 2–3 metrics to watch afterward.

## Open questions
- The 2–3 sharpest things staff should resolve before the sponsor commits.

BILL
File: {file} | Type: {type} | Status: {status} | Committee: {committee}
Title: {title}
Boroughs named: {boroughs} | Topics: {topics}
Sponsors: {sponsors}
Text (excerpt): {text}

REAL DATA CONTEXT (may be empty): {data}
"""


def law_wiki(llm, row, text="", data_ctx="", allow_web=True):
    if not (llm and llm.ready):
        return ""
    sponsors = ", ".join(s.get("MatterSponsorName", "") for s in (row.get("_sponsor_objs") or [])) or \
        row.get("Prime Sponsor", "") or "(not loaded)"
    prompt = WIKI_PROMPT.format(
        file=row.get("File", ""), type=row.get("Type", ""), status=row.get("Status", ""),
        committee=row.get("Committee/Body") or row.get("Committee", ""),
        title=row.get("Title") or row.get("Summary", ""),
        boroughs=row.get("Boroughs named", "") or "—", topics=row.get("Topic tags", "") or "—",
        sponsors=sponsors, text=(text or row.get("Name", ""))[:7000], data=data_ctx or "(none)")
    try:
        return llm.complete(prompt, max_tokens=2200, system=WIKI_STYLE, allow_web=allow_web)
    except Exception as e:
        return f"_(couldn't build the wiki entry: {e})_"


# Sub-generators (used for on-demand, section-at-a-time speed if desired).
_SECTION_PROMPTS = {
    "precedents": ("Has anything like this NYC bill been tried in other U.S. cities or states? "
                   "Search the web. List real examples with place, year, and outcome; flag anything unverified. "
                   "Bill: {file} — {title}. Topics: {topics}."),
    "alternatives": ("Give 4 concrete alternative designs or amendments to this NYC bill "
                     "(stronger/weaker/cheaper/phased/pilot), each with its trade-off. "
                     "Bill: {file} — {title}."),
    "resources": ("What would it take to implement this NYC bill effectively — lead agency, funding, staffing, "
                  "data, enforcement, and the biggest risk? Name the NYC data/agency source to size each; do not "
                  "invent figures. Bill: {file} — {title}. Text: {text}"),
}


def section(llm, key, row, text="", allow_web=True):
    if not (llm and llm.ready):
        return ""
    tpl = _SECTION_PROMPTS.get(key)
    if not tpl:
        return ""
    prompt = tpl.format(file=row.get("File", ""), title=row.get("Title") or row.get("Summary", ""),
                        topics=row.get("Topic tags", "") or "—", text=(text or "")[:4000])
    try:
        return llm.complete(prompt, max_tokens=900, system=WIKI_STYLE,
                            allow_web=allow_web and key == "precedents")
    except Exception as e:
        return f"_(failed: {e})_"
