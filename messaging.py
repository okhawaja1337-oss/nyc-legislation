#!/usr/bin/env python3
"""
messaging.py — the political communications & influence layer.

This is deliberately SEPARATE from the app's neutral-analysis tools. Everything
here writes in the *member's own voice* — it is advocacy from their viewpoint,
not nonpartisan analysis, and the UI labels it that way.

Hard rules, enforced in the system prompt on every call:
  * Never fabricate statistics, dollar figures, dates, or events. If a number
    is needed, use one the user supplied or mark it [verify: source].
  * Never put invented words in another real official's mouth. The user supplies
    any opposing statement being responded to; we do not manufacture quotes.
  * Criticize policy, record, and outcomes — not persons. No personal attacks,
    no defamation. Keep it principled and grounded.
  * The member's own draft statements are fair game; that's normal political
    speech for an elected official.

Takes an `llm.LLM`; returns text/markdown the UI renders. No Streamlit, no
network here.
"""

import json

COMMS_STYLE = (
    "You are a senior communications advisor to a New York City Council Member. "
    "You draft the MEMBER'S OWN public communications — press statements, floor "
    "remarks, quotes for reporters, newsletter blurbs, social posts, and rapid "
    "responses. Voice: principled, plain-spoken, disciplined, and human; firm on "
    "substance without being cruel. STRICT INTEGRITY RULES: (1) never invent "
    "statistics, dollar amounts, dates, or events — use only figures the user "
    "provides, otherwise write the claim qualitatively and tag any needed number "
    "as [verify: source]; (2) never fabricate a quote or position for another "
    "real official — respond only to statements the user supplies; (3) criticize "
    "policy, record, and outcomes, not persons — no personal attacks or "
    "defamation; (4) this is advocacy in the member's voice, clearly not neutral "
    "analysis. Keep it tight and quotable."
)

# The user-provided exemplar sets the register: measured, values-forward, firm,
# no fabricated numbers. Offered as a starting style anchor.
EXEMPLAR = (
    "We delivered meaningful wins in this year's budget, but we also failed to "
    "keep our promise to restore the NYPD headcount. Sexual violence is too "
    "serious to be softened by statistics, reframed by messaging, or explained "
    "away. Every report demands urgency, every survivor deserves justice, and "
    "New Yorkers deserve real action—not better talking points."
)

FORMATS = {
    "Press statement": "a 90–160 word on-the-record press statement with a strong opening line",
    "Reporter quote": "2–3 sentences a spokesperson could give a reporter, highly quotable",
    "Floor / hearing remarks": "short spoken remarks (spoken cadence, ~150–220 words)",
    "Newsletter paragraph": "a warm constituent-newsletter paragraph, plain and local",
    "Social post": "a tight social post (a few lines), punchy but accurate",
    "Talking points": "5–7 crisp bullet talking points the member can speak from",
}

TONES = {
    "Measured": "calm, reasonable, bridge-building",
    "Firm": "principled and pointed but disciplined (the exemplar's register)",
    "Urgent": "high-urgency, demanding action now, still factual",
}

STATEMENT_PROMPT = """Draft {format_desc} for the Council Member.

ISSUE: {issue}
THE MEMBER'S POSITION / WHAT THEY WANT TO CONVEY: {stance}
VERIFIED FACTS THE MEMBER CAN CITE (use ONLY these for any specifics; do not add numbers): {facts}
TONE: {tone_desc}

Mirror the register of this exemplar (style only, don't copy its content unless relevant):
"{exemplar}"

Output the finished text only (Markdown ok). If a compelling specific is missing,
write it qualitatively and add [verify: source] rather than inventing a figure.
End with a one-line "— figures to confirm: …" note ONLY if something needs checking.
"""

REBUTTAL_PROMPT = """The Council Member needs a grounded rapid response.

WHAT WAS SAID (supplied by the user — treat as the opposing claim to answer, do
not add to it): "{claim}"
SAID BY (role, if given): {who}
THE MEMBER'S POSITION: {position}
VERIFIED FACTS THE MEMBER CAN CITE (use ONLY these for specifics): {facts}
TONE: {tone_desc}

Write a {format_desc} that responds on substance: acknowledge what's real,
correct or reframe what's wrong on the policy/record, and pivot to the member's
call to action. Criticize the position and the record — not the person. Do not
invent statistics or restate the opposing claim as if it were the member's fact.
Output the finished text only.
"""

TALKING_POINTS_PROMPT = """Give the Council Member {n} crisp talking points on this issue.

ISSUE: {issue}
POSITION: {stance}
VERIFIED FACTS (use ONLY these for specifics): {facts}
TONE: {tone_desc}

Each bullet: one idea, quotable, grounded. Tag any needed-but-unknown figure as
[verify: source]. Return a Markdown bullet list only.
"""

INFLUENCE_PROMPT = """You advise a NYC Council Member on how to build a majority for a
position. Analyze the political landscape and who to persuade.

ISSUE: {issue}
THE MEMBER'S GOAL: {goal}

NYC COUNCIL STRUCTURE (reference — rosters shift, verify current membership):
{factions}

OFFICIAL COMMITTEE LEADERSHIP (from Legistar — current chairs and members; these are
the real gatekeepers for moving a bill): {committees}

EVIDENCE — co-sponsorship coalitions from the loaded legislation (who actually works
with whom; may be partial): {coalitions}

Write a practical influence memo in Markdown with these sections:
**The math** — what a majority looks like and where this issue likely splits (progressive / moderate / Republican, and cross-cutting caucuses).
**Likely allies** — which blocs/profiles start with you and why (reason from the evidence and general dynamics; name specific members ONLY if the evidence or a web result supports it, else describe by bloc).
**Persuadables** — the swing members/factions to target, and the argument most likely to move each (fiscal, public-safety, borough, constituent).
**Opposition & risks** — who resists, their strongest counter, and how to blunt it.
**Play** — a concrete 3–5 step whip/outreach sequence, plus the message that travels across factions.

Do not fabricate roster membership or vote counts. Where you're inferring, say so.
"""


def _facts_block(facts):
    if not facts:
        return "(none provided — keep all specifics qualitative and tag [verify: source])"
    if isinstance(facts, (list, tuple)):
        return "; ".join(str(f) for f in facts if str(f).strip())
    return str(facts)


def draft_statement(llm, issue, stance, facts=None, fmt="Press statement",
                    tone="Firm", use_exemplar=True):
    if not (llm and llm.ready):
        return ""
    prompt = STATEMENT_PROMPT.format(
        format_desc=FORMATS.get(fmt, fmt), issue=issue, stance=stance or "(not specified)",
        facts=_facts_block(facts), tone_desc=TONES.get(tone, tone),
        exemplar=EXEMPLAR if use_exemplar else "(no exemplar)")
    try:
        return llm.complete(prompt, max_tokens=900, system=COMMS_STYLE)
    except Exception as e:
        return f"_(generation failed: {e})_"


def rebuttal(llm, claim, position, who="", facts=None, fmt="Reporter quote", tone="Firm"):
    if not (llm and llm.ready):
        return ""
    prompt = REBUTTAL_PROMPT.format(
        claim=claim, who=who or "(unspecified)", position=position or "(not specified)",
        facts=_facts_block(facts), tone_desc=TONES.get(tone, tone),
        format_desc=FORMATS.get(fmt, fmt))
    try:
        return llm.complete(prompt, max_tokens=900, system=COMMS_STYLE)
    except Exception as e:
        return f"_(generation failed: {e})_"


def talking_points(llm, issue, stance, facts=None, tone="Firm", n=6):
    if not (llm and llm.ready):
        return ""
    prompt = TALKING_POINTS_PROMPT.format(
        n=int(n), issue=issue, stance=stance or "(not specified)",
        facts=_facts_block(facts), tone_desc=TONES.get(tone, tone))
    try:
        return llm.complete(prompt, max_tokens=800, system=COMMS_STYLE)
    except Exception as e:
        return f"_(generation failed: {e})_"


def influence_memo(llm, issue, goal, factions_ref, coalitions=None, committees=None,
                   allow_web=False):
    if not (llm and llm.ready):
        return ""
    prompt = INFLUENCE_PROMPT.format(
        issue=issue, goal=goal or "(not specified)",
        factions=factions_ref,
        committees=json.dumps(committees or [], ensure_ascii=False)[:3500]
                   or "(not loaded)",
        coalitions=json.dumps(coalitions or {}, ensure_ascii=False)[:4000])
    try:
        return llm.complete(prompt, max_tokens=1600, system=COMMS_STYLE, allow_web=allow_web)
    except Exception as e:
        return f"_(generation failed: {e})_"
