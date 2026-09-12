#!/usr/bin/env python3
"""
The LLM Council.

Five standing perspectives argue a question, then read each other and file a
peer-reviewed synthesis. The point is not consensus -- it is that no single
framing gets to walk out of the room unchallenged.

  contrarian        hunts the failure modes, the politics, and what breaks
  first_principles  strips the assumptions and rebuilds from the statute up
  expansionist      finds the upside and the second-order wins
  outsider          knows nothing about NYC and asks what everyone assumes
  executor          ignores the argument and asks what ships, by when, by whom

Three stages: independent drafts, blind peer review, chairman synthesis.
Every stage is grounded in an evidence packet assembled from the data lake,
and every stage is told the same thing: do not invent a number.

Without an API key the Council still runs -- it produces the structured
scaffold, the evidence, and the questions each seat would ask, so the
deliverable is never empty. What it will not do is fabricate the prose and
pass it off as deliberation.
"""
from __future__ import annotations

import json
import os
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from ..core.config import (ANTHROPIC_API_KEY, DISTRICT_LABEL, HOUSE_STYLE,
                           LAND_USE_DOCTRINE, MEMBER_NAME, PILLARS)

MODEL = os.environ.get("UNIVERSE_MODEL", "claude-opus-5")


@dataclass(frozen=True)
class Seat:
    key: str
    name: str
    mandate: str
    lens: str
    must_answer: tuple[str, ...]


SEATS: tuple[Seat, ...] = (
    Seat(
        "contrarian", "The Contrarian",
        "Find every way this fails.",
        "You are the seat that stops the office from being embarrassed in six "
        "months. Attack the proposal on evidence, implementation, politics, "
        "budget, and legal exposure. Name the constituency that loses. Name "
        "the headline that would be written if this goes wrong. If the "
        "evidence is thin, say the evidence is thin.",
        ("What is the strongest argument against this?",
         "Who is harmed, and do they vote?",
         "What has to be true for this to work, and is it?"),
    ),
    Seat(
        "first_principles", "The First-Principles Thinker",
        "Strip the assumptions; rebuild from the statute and the arithmetic.",
        "Discard how the city currently does this. Start from the legal "
        "authority (Charter, Administrative Code, State law), the money that "
        "actually exists, and the outcome wanted. Rebuild the mechanism from "
        "scratch. If the conventional approach survives, say why. If the "
        "problem is defined wrong, redefine it.",
        ("What are we actually trying to change?",
         "What is the legal instrument that can change it?",
         "If we were designing this today with no precedent, what would it be?"),
    ),
    Seat(
        "expansionist", "The Expansionist",
        "Find the upside everyone else missed.",
        "Look for leverage: adjacent funding streams, precedent value, "
        "coalition partners, state and federal match, second-order effects on "
        "the North Shore economy. Ask what the biggest honest version of this "
        "is, and what it unlocks next cycle.",
        ("What is the largest defensible version of this?",
         "What does this unlock that is not obvious?",
         "Who else would want to be on this, and what do they bring?"),
    ),
    Seat(
        "outsider", "The Outsider",
        "Arrive with zero context and ask the obvious question.",
        "You have never heard of ULURP, Schedule C, a Transparency Resolution, "
        "or Staten Island politics. Ask what the jargon means. Ask why it is "
        "done this way. Ask what a resident would notice. Your value is that "
        "you refuse to accept a premise just because everyone in the room "
        "shares it.",
        ("What does this mean in plain English?",
         "What would a resident actually notice, and when?",
         "Why is it done this way?"),
    ),
    Seat(
        "executor", "The Executor",
        "Deliver the job to the letter. Nothing else.",
        "You do not care whether the idea is good. You care what ships. Give "
        "the sequence, the owner for each step, the deadline tied to the "
        "budget or legislative calendar, the vote count needed, and the "
        "single point of failure. If a step has no owner, say so.",
        ("What is the first step, and who owns it?",
         "What is the deadline the calendar imposes?",
         "What is the one thing that, if it slips, kills this?"),
    ),
)

SEATS_BY_KEY = {s.key: s for s in SEATS}

HOUSE_RULES = "\n".join(f"- {r}" for r in HOUSE_STYLE["rules"])

BASE_CONTEXT = f"""You advise {MEMBER_NAME}, who represents {DISTRICT_LABEL} on
the New York City Council. The office's standing priorities are:
{chr(10).join(f"  - {spec['label']}" for spec in PILLARS.values())}

Land-use doctrine this office applies to every development question:
{chr(10).join(f"  - {v}" for v in LAND_USE_DOCTRINE.values())}

House rules for everything you write:
{HOUSE_RULES}

Ground every claim in the EVIDENCE block. If a number is not in the evidence,
write [verify: source] rather than supplying one. Never invent a quote, a
vote count, a dollar figure, or another official's position."""


# --------------------------------------------------------------- transport ----
def _anthropic_client():
    key = ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic
    except ImportError:
        return None
    return anthropic.Anthropic(api_key=key)


def ask_model(prompt: str, system: str = BASE_CONTEXT,
              max_tokens: int = 2000, client=None) -> str | None:
    """One model call. Returns None when no key or the call fails."""
    client = client or _anthropic_client()
    if client is None:
        return None
    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    except Exception as exc:                      # a dead key must not crash a brief
        return f"[council unavailable: {type(exc).__name__}]"


def council_plus(question: str, evidence: str, url: str | None = None) -> str | None:
    """Route through a running LLM Council Plus server when one is configured."""
    import urllib.error
    import urllib.request
    from ..core.config import LLM_COUNCIL_URL
    base = (url or LLM_COUNCIL_URL or "").rstrip("/")
    if not base:
        return None
    body = json.dumps({"prompt": f"{question}\n\nEVIDENCE:\n{evidence}"}).encode()
    req = urllib.request.Request(f"{base}/api/council", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return data.get("synthesis") or data.get("answer") or json.dumps(data)[:4000]
    except Exception:
        return None


# ------------------------------------------------------------ deliberation ----
@dataclass
class Opinion:
    seat: str
    name: str
    body: str | None
    questions: list[str] = field(default_factory=list)
    grounded: bool = True


@dataclass
class Deliberation:
    question: str
    evidence: str
    opinions: list[Opinion] = field(default_factory=list)
    reviews: list[Opinion] = field(default_factory=list)
    synthesis: str | None = None
    mode: str = "scaffold"
    created: str = field(default_factory=
                         lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        return {
            "question": self.question, "mode": self.mode, "created": self.created,
            "opinions": [vars(o) for o in self.opinions],
            "reviews": [vars(r) for r in self.reviews],
            "synthesis": self.synthesis,
        }

    def to_markdown(self) -> str:
        out = [f"# LLM Council — {self.question}", "",
               f"*Mode: {self.mode} · {self.created}*", ""]
        for o in self.opinions:
            out += [f"## {o.name}", o.body or "_(no model available — questions only)_", ""]
            if o.questions:
                out += ["**Questions this seat presses:**"] + \
                       [f"- {q}" for q in o.questions] + [""]
        if self.reviews:
            out += ["## Peer review", ""]
            for r in self.reviews:
                out += [f"### {r.name} reviewing the room", r.body or "", ""]
        if self.synthesis:
            out += ["## Chairman's synthesis", self.synthesis, ""]
        return "\n".join(out)


def deliberate(question: str, evidence: str, seats: Iterable[Seat] = SEATS,
               peer_review: bool = True, use_council_server: bool = False,
               client=None) -> Deliberation:
    """Run the three-stage Council over a grounded evidence packet."""
    seats = list(seats)
    d = Deliberation(question=question, evidence=evidence)

    if use_council_server:
        synth = council_plus(question, evidence)
        if synth:
            d.mode = "council-plus"
            d.synthesis = synth
            return d

    client = client or _anthropic_client()
    have_model = client is not None
    d.mode = "deliberated" if have_model else "scaffold"

    # ---- stage 1: independent opinions --------------------------------
    for seat in seats:
        prompt = textwrap.dedent(f"""
            You are **{seat.name}**. Mandate: {seat.mandate}

            {seat.lens}

            QUESTION
            {question}

            EVIDENCE
            {evidence}

            Write your independent view in at most 250 words. Open with a
            one-sentence bottom line, then bullets. Close with the single
            question you would force the room to answer.
        """).strip()
        body = ask_model(prompt, client=client, max_tokens=900) if have_model else None
        d.opinions.append(Opinion(seat.key, seat.name, body,
                                  list(seat.must_answer)))

    # ---- stage 2: blind peer review -----------------------------------
    if peer_review and have_model:
        room = "\n\n".join(f"### {o.name}\n{o.body}" for o in d.opinions if o.body)
        for seat in seats:
            prompt = textwrap.dedent(f"""
                You are **{seat.name}**. You have just read the other seats.

                THE ROOM
                {room}

                In at most 150 words: which seat is most wrong and why; which
                point survives your objection; and what the room collectively
                missed. Be blunt. Do not restate your own view.
            """).strip()
            body = ask_model(prompt, client=client, max_tokens=600)
            d.reviews.append(Opinion(seat.key, seat.name, body))

    # ---- stage 3: synthesis -------------------------------------------
    if have_model:
        room = "\n\n".join(f"### {o.name}\n{o.body}" for o in d.opinions if o.body)
        crit = "\n\n".join(f"### {r.name}\n{r.body}" for r in d.reviews if r.body)
        prompt = textwrap.dedent(f"""
            You are the Chairman. Produce the pressure-tested answer.

            QUESTION
            {question}

            EVIDENCE
            {evidence}

            OPINIONS
            {room}

            PEER REVIEW
            {crit}

            Write in the house style: a 2-4 sentence bottom line, then 5-8
            bullets, then a short "Where the council disagreed" note naming the
            live dispute, then exactly three questions for the Councilmember.
            Every figure must trace to the evidence or carry [verify: source].
        """).strip()
        d.synthesis = ask_model(prompt, client=client, max_tokens=2200)

    return d


def available() -> dict:
    """What the AI layer can actually do right now."""
    return {
        "anthropic_key": bool(ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY")),
        "sdk_installed": _sdk_installed(),
        "model": MODEL,
        "seats": [s.key for s in SEATS],
        "mode": ("deliberated" if _anthropic_client() else "scaffold"),
    }


def _sdk_installed() -> bool:
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False
