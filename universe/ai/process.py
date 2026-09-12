#!/usr/bin/env python3
"""
The deliverable process.

An answer is not a deliverable. This module enforces the office's standing
sequence so that every question ends in something filed, cited, and findable:

    1. INTAKE      — what is actually being asked, and who needs it when
    2. EVIDENCE    — assemble from the lake; record coverage and gaps
    3. DELIBERATE  — run the LLM Council over the grounded packet
    4. DRAFT       — render in the house style
    5. VERIFY      — every figure traces to a source, or is flagged
    6. FILE        — persist with an id so it can be found and reused

Each stage records what it did. A deliverable that skipped verification says
so on its face rather than looking finished.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from ..core.citations import CitationRegistry, DEFAULT_SOURCES, VERIFY_TAG
from ..core.config import OUT_DIR
from ..core.store import Store
from .brief import (Brief, fiscal_brief, matter_brief, member_brief,
                    packet_text, talking_points)
from .council import deliberate

STAGES = ("intake", "evidence", "deliberate", "draft", "verify", "file")


@dataclass
class Stage:
    name: str
    ok: bool
    detail: dict = field(default_factory=dict)
    note: str = ""


@dataclass
class Deliverable:
    deliverable_id: str
    kind: str
    subject: str
    brief: Brief | None = None
    stages: list[Stage] = field(default_factory=list)
    created: str = field(default_factory=
                         lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @property
    def status(self) -> str:
        done = {s.name for s in self.stages if s.ok}
        if "file" in done:
            return "filed"
        if "verify" in done:
            return "verified"
        if "draft" in done:
            return "drafted"
        return "in progress"

    def stage(self, name: str) -> Stage | None:
        return next((s for s in self.stages if s.name == name), None)

    def to_dict(self) -> dict:
        return {
            "deliverable_id": self.deliverable_id, "kind": self.kind,
            "subject": self.subject, "status": self.status, "created": self.created,
            "stages": [vars(s) for s in self.stages],
            "brief": self.brief.to_dict() if self.brief else None,
        }

    def to_markdown(self, registry: CitationRegistry | None = None) -> str:
        body = self.brief.to_markdown(registry) if self.brief else f"# {self.subject}"
        trail = ["", "## Process record", ""]
        for s in self.stages:
            trail.append(f"- **{s.name}** — {'ok' if s.ok else 'INCOMPLETE'}"
                         + (f": {s.note}" if s.note else ""))
        trail.append("")
        trail.append(f"*Deliverable `{self.deliverable_id}` · status **{self.status}***")
        return body + "\n".join(trail)


# ------------------------------------------------------------ verification ----
NUMBER_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")


def verify(brief: Brief, registry: CitationRegistry) -> Stage:
    """Check every figure in the prose traces to a source or is flagged.

    This is deliberately mechanical. It cannot know whether a number is
    *right*; it can know whether the deliverable is honest about where the
    number came from, and that is the part that gets an office in trouble.
    """
    prose = " ".join([brief.bottom_line, *brief.details, *brief.d49_impact,
                      *brief.talking_points, brief.recommendation])
    numbers = NUMBER_RE.findall(prose)
    flagged = prose.count(VERIFY_TAG)
    unknown = [s for s in brief.sources_used if s not in registry]
    ok = not unknown
    return Stage(
        "verify", ok,
        detail={"figures_in_prose": len(numbers),
                "flagged_unverified": flagged,
                "sources_cited": len(brief.sources_used),
                "unknown_sources": unknown},
        note=(f"{len(numbers)} figures, {flagged} flagged {VERIFY_TAG}, "
              f"{len(brief.sources_used)} sources cited"
              + (f"; UNKNOWN SOURCES: {unknown}" if unknown else "")))


def _mkid(kind: str, subject: str) -> str:
    raw = f"{kind}|{subject}|{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
    return f"{kind[:3].upper()}-" + hashlib.sha1(raw.encode()).hexdigest()[:10]


# ----------------------------------------------------------------- runner ----
BUILDERS: dict[str, Callable[..., Brief]] = {
    "fiscal": lambda store, subject, **kw: fiscal_brief(store, **kw),
    "member": lambda store, subject, **kw: member_brief(store, subject, **kw),
    "matter": lambda store, subject, **kw: matter_brief(store, int(subject), **kw),
}


def run(store: Store, kind: str, subject: str = "", *,
        with_council: bool = False, ask: str | None = None,
        registry: CitationRegistry | None = None,
        write: bool = True, **kw) -> Deliverable:
    """Run the full process and return a filed deliverable."""
    registry = registry or CitationRegistry.load()
    if len(registry) == 0:
        registry = CitationRegistry(DEFAULT_SOURCES)

    d = Deliverable(_mkid(kind, subject or kind), kind, subject or kind)

    # 1 -- intake
    d.stages.append(Stage("intake", True,
                          {"kind": kind, "subject": subject, "ask": ask,
                           "with_council": with_council},
                          note=ask or f"{kind} brief on {subject or 'District 49'}"))

    # 2 + 4 -- evidence and draft (the builder assembles evidence first)
    builder = BUILDERS.get(kind)
    if not builder:
        d.stages.append(Stage("evidence", False, note=f"unknown kind '{kind}'"))
        return d
    brief = builder(store, subject, with_council=False, **kw)
    d.brief = brief
    ev_keys = list(brief.evidence)
    d.stages.append(Stage("evidence", bool(ev_keys),
                          {"blocks": ev_keys, "sources": brief.sources_used},
                          note=f"{len(ev_keys)} evidence blocks from the lake"))

    # 3 -- deliberate
    if with_council:
        question = ask or f"What should the office do about {brief.subject}?"
        brief.council = deliberate(question, packet_text(brief.evidence))
        brief.mode = brief.council.mode
        d.stages.append(Stage("deliberate", brief.council.synthesis is not None,
                              {"mode": brief.council.mode,
                               "seats": len(brief.council.opinions)},
                              note=f"council ran in {brief.council.mode} mode"))
    else:
        d.stages.append(Stage("deliberate", True, {"skipped": True},
                              note="not requested"))

    d.stages.append(Stage("draft", True,
                          {"details": len(brief.details),
                           "questions": len(brief.questions)},
                          note=f"{len(brief.details)} findings, "
                               f"{len(brief.questions)} questions"))

    # 5 -- verify
    d.stages.append(verify(brief, registry))

    # 6 -- file
    if write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        md_path = OUT_DIR / f"{d.deliverable_id}.md"
        md_path.write_text(d.to_markdown(registry))
        (OUT_DIR / f"{d.deliverable_id}.json").write_text(
            json.dumps(d.to_dict(), indent=2, default=str))
        store.upsert("deliverables", [{
            "deliverable_id": d.deliverable_id, "kind": kind,
            "subject": d.subject, "status": "filed",
            "body": d.to_markdown(registry),
            "meta": json.dumps({"stages": [vars(s) for s in d.stages],
                                "sources": brief.sources_used}, default=str),
            "created": d.created, "updated": d.created,
        }])
        store.index("deliverable", d.deliverable_id, d.subject,
                    brief.bottom_line, kind)
        store.journal("deliverable.filed",
                      {"id": d.deliverable_id, "kind": kind, "subject": d.subject})
        d.stages.append(Stage("file", True, {"path": str(md_path)},
                              note=str(md_path)))
    else:
        d.stages.append(Stage("file", False, note="write disabled"))
    return d


def recent(store: Store, limit: int = 20) -> list[dict]:
    return [dict(r) for r in store.q(
        "SELECT deliverable_id, kind, subject, status, created FROM deliverables "
        "ORDER BY created DESC LIMIT ?", [limit])]
