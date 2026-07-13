#!/usr/bin/env python3
"""
briefing.py — "Bulletpoints for Bureaucrats".

Turns a bill, a member's record, or a policy topic into a tight, plain-language,
copy-paste-ready briefing built for a busy elected official (the running example
is CM Hanks). Two layers, mirroring the rest of the app:

  * a TEMPLATE skeleton that always works with zero AI — it arranges the hard
    facts we already have into clean bullets, so you're never blocked; and
  * an AI layer that rewrites those facts into press-ready, plain-English
    bullets and talking points when an Anthropic key is present.

Nothing here touches Streamlit or the network directly — it takes an `llm.LLM`
instance for the AI calls, and returns Markdown strings the UI renders/exports.
"""

import json

# House style, injected as the system prompt so every briefing reads the same.
BRIEFING_STYLE = (
    "You write internal briefings for a busy New York City elected official and "
    "their communications staff. Your house style is 'Bulletpoints for "
    "Bureaucrats': short, concrete, plain-English bullets a non-expert can read "
    "in 30 seconds and repeat out loud. Rules: lead with the bottom line; one "
    "idea per bullet; no jargon (or define it in three words); never invent "
    "numbers, sponsors, dates, or quotes — if a figure is unknown, say what to "
    "look up and where; stay strictly nonpartisan and factual; mark any "
    "prediction as a prediction. Ground everything in the data provided."
)

AUDIENCES = {
    "Staff brief (neutral)": "internal policy staff who need it straight and complete",
    "Press-ready (PR)": "communications staff drafting a statement or press note; "
                        "punchy, quotable, still accurate and nonpartisan",
    "Constituent-friendly": "a resident newsletter; warm, jargon-free, explains why "
                            "it matters to everyday New Yorkers",
    "One-pager (leave-behind)": "a single-page leave-behind for a meeting; the "
                                "tightest possible version, headline facts only",
}

BILL_BRIEFING_PROMPT = """Write a briefing on this legislation for {audience}.

Use ONLY the facts in DATA below plus general knowledge of how government works;
do not invent specifics about THIS bill. Output GitHub-flavored Markdown with
exactly these sections (keep the headers):

## {file} — {title_short}
**Bottom line (1 sentence):** …

**What it does**
- 3–5 tight bullets, plain English, one idea each.

**Why it matters to NYC**
- 2–4 bullets on who is affected and how it lands across the city/boroughs.

**Where it stands**
- 2–3 bullets: current status, the committee/path, and the realistic next step.

**Talking points**
- 3 quotable, nonpartisan lines the member could say out loud.

**Press one-liner:** one sentence a spokesperson could give a reporter.

**Watch for / open questions**
- 2–3 things staff should confirm before the member commits (name the source to check).

DATA (JSON):
{data}
"""

MEMBER_BRIEFING_PROMPT = """Write a briefing on this elected official's legislative
record for {audience}. Use ONLY the DATA below (their sponsorship record, topic
mix, outcomes, coalition). Do NOT add biography, party, district, quotes, or
positions not derivable from the data; if something isn't in the data, say so.

Output Markdown with these sections:

## {name} — legislative record
**Bottom line:** one sentence on what their record shows.

**Focus areas**
- 3–4 bullets: the leading policy topics, from the topic mix.

**Productivity & outcomes**
- 3 bullets: volume, prime vs. co-sponsor balance, and passed vs. stalled.

**Who they work with**
- 2–3 bullets on frequent co-sponsors (coalition).

**Talking points**
- 3 nonpartisan lines summarizing the record.

**Caveat:** one line noting this reflects sponsorship activity only (not floor
votes) and is generated from public data.

DATA (JSON):
{data}
"""

TOPIC_BRIEFING_PROMPT = """Write a topic briefing for {audience} on the policy area
"{topic}" as it appears in the loaded NYC legislation. Use the DATA (a sample of
matching bills and counts) as evidence and cite bill file numbers like Int
0220-2026. You may use general knowledge of NYC government for context, but do
not invent bill specifics. Output Markdown with:

## Briefing — {topic}
**Bottom line:** one sentence.

**The landscape**
- 3–5 bullets: what's moving in this area, with bill numbers as evidence.

**Why it matters to NYC**
- 2–3 bullets.

**Angles for the member**
- 3 bullets: where the member could lead, co-sponsor, or hold a hearing.

**Talking points**
- 3 quotable lines.

DATA (JSON):
{data}
"""


# ---------------------------------------------------------------------------
# Template skeletons (no AI needed — always available)
# ---------------------------------------------------------------------------
def template_bill_briefing(row, detail=None):
    """Build a factual Markdown skeleton straight from the data we hold."""
    detail = detail or {}
    file = row.get("File", "—")
    title = (row.get("Title") or row.get("Summary") or "").strip()
    level = row.get("level", "NYC")
    typ = row.get("Type", "")
    status = row.get("Status", "") or "—"
    committee = row.get("Committee/Body") or row.get("Committee") or "—"
    sponsor = row.get("Prime Sponsor") or row.get("Sponsor") or "—"
    nspon = row.get("Sponsors (#)", "")
    topics = row.get("Topic tags", "")
    boroughs = row.get("Boroughs named", "")
    link = row.get("Web Link", "")

    lines = [f"## {file} — {title[:110]}", ""]
    lines.append(f"**Bottom line:** {typ or 'Legislation'} currently **{status}**"
                 + (f" in {committee}." if committee and committee != '—' else "."))
    lines.append("")
    lines.append("**Key facts**")
    lines.append(f"- **Level:** {level}")
    lines.append(f"- **Type:** {typ or '—'}")
    lines.append(f"- **Status:** {status}")
    lines.append(f"- **Committee / body:** {committee}")
    lines.append(f"- **Prime sponsor:** {sponsor}")
    if nspon not in ("", None):
        lines.append(f"- **Total sponsors:** {nspon}")
    if topics:
        lines.append(f"- **Policy topics:** {topics}")
    if boroughs:
        lines.append(f"- **Boroughs named:** {boroughs}")
    if link:
        lines.append(f"- **Official record:** {link}")
    lines.append("")
    if detail.get("history"):
        lines.append("**Recent action**")
        for h in detail["history"][-4:]:
            lines.append(f"- {h}")
        lines.append("")
    lines.append("_This is the fact skeleton. Add your Anthropic key to generate "
                 "the plain-language 'Bulletpoints for Bureaucrats' version._")
    return "\n".join(lines)


def bill_briefing(llm, row, text="", data_ctx="", audience="Staff brief (neutral)",
                  detail=None):
    """AI-written briefing; falls back to the template if no key / on error."""
    if not (llm and llm.ready):
        return template_bill_briefing(row, detail)
    data = {
        "file": row.get("File"), "level": row.get("level", "NYC"),
        "type": row.get("Type"), "title": row.get("Title") or row.get("Summary"),
        "status": row.get("Status"), "committee": row.get("Committee/Body") or row.get("Committee"),
        "prime_sponsor": row.get("Prime Sponsor") or row.get("Sponsor"),
        "sponsor_count": row.get("Sponsors (#)"),
        "sponsors": [s.get("MatterSponsorName") for s in (row.get("_sponsor_objs") or [])][:25]
                    or (detail or {}).get("sponsors", []),
        "topics": row.get("Topic tags"), "boroughs": row.get("Boroughs named"),
        "recent_actions": (detail or {}).get("history", []),
        "text_excerpt": (text or "")[:6000],
        "real_data_context": data_ctx or "",
    }
    prompt = BILL_BRIEFING_PROMPT.format(
        audience=AUDIENCES.get(audience, audience),
        file=row.get("File", ""),
        title_short=((row.get("Title") or row.get("Summary") or "")[:70]),
        data=json.dumps(data, ensure_ascii=False)[:9000])
    try:
        return llm.complete(prompt, max_tokens=1700, system=BRIEFING_STYLE)
    except Exception as e:
        return template_bill_briefing(row, detail) + f"\n\n> _(AI step failed: {e})_"


def member_briefing(llm, name, stats, audience="Staff brief (neutral)"):
    if not (llm and llm.ready):
        return _template_member(name, stats)
    prompt = MEMBER_BRIEFING_PROMPT.format(
        audience=AUDIENCES.get(audience, audience), name=name,
        data=json.dumps(stats, ensure_ascii=False)[:7000])
    try:
        return llm.complete(prompt, max_tokens=1400, system=BRIEFING_STYLE)
    except Exception as e:
        return _template_member(name, stats) + f"\n\n> _(AI step failed: {e})_"


def topic_briefing(llm, topic, evidence, audience="Staff brief (neutral)"):
    if not (llm and llm.ready):
        return f"## Briefing — {topic}\n\nAdd your Anthropic key to generate this."
    prompt = TOPIC_BRIEFING_PROMPT.format(
        audience=AUDIENCES.get(audience, audience), topic=topic,
        data=json.dumps(evidence, ensure_ascii=False)[:8000])
    try:
        return llm.complete(prompt, max_tokens=1500, system=BRIEFING_STYLE)
    except Exception as e:
        return f"## Briefing — {topic}\n\n> _(AI step failed: {e})_"


def _template_member(name, stats):
    s = stats or {}
    lines = [f"## {name} — legislative record", ""]
    lines.append(f"**Key facts**")
    lines.append(f"- Bills on: {s.get('bills_on', '—')}")
    lines.append(f"- As prime sponsor: {s.get('as_prime', '—')}")
    lines.append(f"- As co-sponsor: {s.get('as_cosponsor', '—')}")
    bystat = s.get("by_status") or {}
    if bystat:
        lines.append(f"- Passed/enacted: {bystat.get('passed', 0)} · "
                     f"in committee: {bystat.get('committee', bystat.get('alive', 0))}")
    top = s.get("by_topic") or {}
    if top:
        lead = ", ".join(list(top.keys())[:4])
        lines.append(f"- Leading topics: {lead}")
    coal = s.get("top_coalition") or {}
    if coal:
        lines.append(f"- Frequent co-sponsors: {', '.join(list(coal.keys())[:4])}")
    lines.append("")
    lines.append("_Add your Anthropic key for the plain-language write-up._")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------
def briefing_to_rows(md):
    """Flatten a Markdown briefing into (Section, Content) rows for Excel."""
    rows, section = [], "Header"
    for ln in (md or "").splitlines():
        ln = ln.rstrip()
        if not ln:
            continue
        if ln.startswith("## "):
            section = ln[3:].strip()
        elif ln.startswith("**") and ln.endswith("**"):
            section = ln.strip("*").strip()
        elif ln.startswith("- "):
            rows.append((section, ln[2:].strip()))
        else:
            rows.append((section, ln.strip("*").strip()))
    return rows


def md_to_html(md):
    """Minimal, dependency-free Markdown -> HTML for the subset briefings use
    (##/### headers, **bold**, - bullets, paragraphs, bare links)."""
    import html as _html
    import re as _re
    out, in_ul = [], False

    def inline(s):
        s = _html.escape(s)
        s = _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = _re.sub(r"(?<!\w)(https?://[^\s)]+)",
                    r'<a href="\1">\1</a>', s)
        return s

    for raw in (md or "").splitlines():
        ln = raw.rstrip()
        if not ln.strip():
            if in_ul:
                out.append("</ul>"); in_ul = False
            continue
        if ln.startswith("- "):
            if not in_ul:
                out.append("<ul>"); in_ul = True
            out.append(f"<li>{inline(ln[2:])}</li>"); continue
        if in_ul:
            out.append("</ul>"); in_ul = False
        if ln.startswith("### "):
            out.append(f"<h3>{inline(ln[4:])}</h3>")
        elif ln.startswith("## "):
            out.append(f"<h2>{inline(ln[3:])}</h2>")
        elif ln.startswith("# "):
            out.append(f"<h1>{inline(ln[2:])}</h1>")
        else:
            out.append(f"<p>{inline(ln)}</p>")
    if in_ul:
        out.append("</ul>")
    return "\n".join(out)


def print_html(md_html_body, title="Briefing"):
    """Wrap rendered briefing HTML in a clean, printable page."""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ font: 15px/1.55 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
          color:#0f172a; max-width:760px; margin:32px auto; padding:0 20px; }}
  h1,h2 {{ color:#0b3b8c; }} h2 {{ border-bottom:2px solid #dbe6fb; padding-bottom:4px; }}
  ul {{ margin:.3rem 0 1rem 1.1rem; }} li {{ margin:.25rem 0; }}
  .tag {{ display:inline-block; background:#eaf1fd; color:#1d4ed8; border-radius:999px;
          padding:2px 10px; font-size:12px; font-weight:600; }}
  @media print {{ a {{ color:#0b3b8c; text-decoration:none; }} }}
</style></head><body>{md_html_body}</body></html>"""
