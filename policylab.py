#!/usr/bin/env python3
"""
policylab.py — brainstorm new laws, policies, and legislative ideas.

Give it a problem ("rats in my district", "e-bike fires", "childcare deserts")
and it returns structured, staff-ready legislative concepts: the problem, a
concrete mechanism, who would sponsor/oppose it, fiscal and legal flags,
precedents to copy, and a PR angle — the raw material for a real Introduction.

Uses an `llm.LLM` for generation. Returns structured dicts the UI renders; on
no key it returns a scaffold the user can fill in by hand.
"""

import json

LAB_STYLE = (
    "You are a senior legislative policy advisor to a New York City Council "
    "member. You turn problems into concrete, workable legislative ideas that "
    "fit NYC's actual legal toolkit — Local Laws, resolutions (including "
    "state/federal home-rule messages), oversight hearings, budget asks, and "
    "agency rulemaking. You are creative but grounded: every idea names a real "
    "mechanism and the agency that would run it. You are strictly nonpartisan, "
    "you never invent statutes or numbers, and you flag legal limits candidly "
    "(e.g., state preemption). Ideas are proposals for discussion, not legal advice."
)

IDEATE_PROMPT = """Generate {n} distinct legislative/policy ideas for a NYC Council
member to address this goal:

GOAL: {goal}
{context}

Return ONLY a JSON array. Each element is an object with these keys:
- "title": short, memorable name for the idea (like a bill nickname).
- "one_liner": one plain-English sentence on what it does.
- "instrument": the legal vehicle — one of "Local Law", "Resolution",
  "Home-rule message", "Oversight hearing", "Budget ask", "Agency rulemaking".
- "mechanism": 1–2 sentences on exactly what legally changes and how it works.
- "lead_agency": the NYC agency that would implement or be affected.
- "who_benefits": who it helps, concretely.
- "who_pushes_back": likely opponents and their strongest objection.
- "fiscal_flag": qualitative cost/revenue note; name the figure to verify (never invent one).
- "legal_flag": any preemption, Charter, or authority limit to check ("" if none obvious).
- "precedent": a comparable law/program in NYC's past or another city, marked illustrative.
- "pr_angle": one quotable, nonpartisan line the member could lead with.
- "first_step": the single next action staff should take (e.g., "request an IBO fiscal note").
- "boldness": integer 1–5 (1 = incremental/safe, 5 = ambitious/novel).

Make the {n} ideas genuinely different from each other — vary the instrument and
the boldness. Do not fabricate specific NYC statistics or existing bill numbers.

JSON:"""

REFINE_PROMPT = """Develop this NYC legislative idea into a one-page concept memo for
a Council member's staff. Stay grounded — no invented numbers, sponsors, or
statutes; name sources to check instead.

IDEA (JSON): {idea}
{context}

Output Markdown with these sections:
## {title}
**The problem** — 2–3 bullets on what's broken and who it hurts (name the NYC
Open Data / agency source that would document the scale; don't invent figures).
**The proposal** — 3–4 bullets: the mechanism, the lead agency, who's covered, triggers/timeline.
**Why now / precedent** — 2 bullets (mark comparisons illustrative).
**Coalition** — likely supporters and opponents, and the committee this goes to.
**Fiscal & legal check** — cost/revenue drivers to verify and any preemption/authority risk.
**Talking points** — 3 nonpartisan, quotable lines.
**Draft intro summary** — a 2–3 sentence "statement of intent" in the style of an
NYC Introduction's summary.
**Next steps** — 3 concrete staff actions.
"""


def ideate(llm, goal, context="", n=5):
    """Return a list of idea dicts (or [] with a scaffold flag on no key)."""
    if not (llm and llm.ready):
        return []
    ctx = f"CONTEXT: {context}" if context else ""
    prompt = IDEATE_PROMPT.format(n=int(n), goal=goal[:1200], context=ctx)
    try:
        data = llm.complete_json(prompt, max_tokens=2600)
    except Exception:
        return []
    if isinstance(data, dict):
        # tolerate {"ideas": [...]} shapes
        for v in data.values():
            if isinstance(v, list):
                data = v
                break
        else:
            data = []
    return [_clean_idea(d) for d in data if isinstance(d, dict)][:int(n)]


def refine(llm, idea, context=""):
    if not (llm and llm.ready):
        return _scaffold_memo(idea)
    ctx = f"CONTEXT: {context}" if context else ""
    prompt = REFINE_PROMPT.format(idea=json.dumps(idea, ensure_ascii=False)[:2500],
                                  title=idea.get("title", "Concept"), context=ctx)
    try:
        return llm.complete(prompt, max_tokens=1700, system=LAB_STYLE)
    except Exception as e:
        return _scaffold_memo(idea) + f"\n\n> _(AI step failed: {e})_"


_IDEA_KEYS = ["title", "one_liner", "instrument", "mechanism", "lead_agency",
              "who_benefits", "who_pushes_back", "fiscal_flag", "legal_flag",
              "precedent", "pr_angle", "first_step", "boldness"]


def _clean_idea(d):
    out = {k: d.get(k, "") for k in _IDEA_KEYS}
    try:
        out["boldness"] = max(1, min(5, int(out["boldness"] or 3)))
    except Exception:
        out["boldness"] = 3
    return out


def _scaffold_memo(idea):
    t = idea.get("title", "New policy concept")
    return (f"## {t}\n\n"
            f"**One-liner:** {idea.get('one_liner','—')}\n\n"
            f"- **Instrument:** {idea.get('instrument','—')}\n"
            f"- **Mechanism:** {idea.get('mechanism','—')}\n"
            f"- **Lead agency:** {idea.get('lead_agency','—')}\n\n"
            "_Add your Anthropic key to expand this into a full concept memo._")


def ideas_to_rows(ideas):
    """Flatten ideas for an Excel export."""
    return [{
        "Title": i.get("title", ""), "One-liner": i.get("one_liner", ""),
        "Instrument": i.get("instrument", ""), "Mechanism": i.get("mechanism", ""),
        "Lead agency": i.get("lead_agency", ""), "Benefits": i.get("who_benefits", ""),
        "Pushback": i.get("who_pushes_back", ""), "Fiscal flag": i.get("fiscal_flag", ""),
        "Legal flag": i.get("legal_flag", ""), "Precedent": i.get("precedent", ""),
        "PR angle": i.get("pr_angle", ""), "First step": i.get("first_step", ""),
        "Boldness": i.get("boldness", 3),
    } for i in ideas]
