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

from ..core import pipeline as P
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
    receipt: Any = None          # core.pipeline.Receipt, once assessed
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


def context(brief: Brief, *, kind: str, subject: str, ask: str | None,
            registry: CitationRegistry, with_council: bool,
            deliverable_id: str = "", path: str = "",
            indexed: bool = False) -> dict:
    """
    Flatten a deliverable into the facts the contract can judge.

    Assembly and judgement stay apart on purpose: this function knows what a
    Brief looks like, and `core.pipeline` knows what "deliverable" means. A
    gate can then be tested on a dictionary, with no Brief in sight.
    """
    prose = " ".join([brief.bottom_line, *brief.details, *brief.d49_impact,
                      *brief.talking_points, brief.recommendation])
    evidence_text = json.dumps(brief.evidence, default=str)
    council = brief.council
    return {
        "kind": kind, "subject": subject, "ask": ask,
        "requester": ask,
        "evidence_blocks": list(brief.evidence),
        "evidence_text": evidence_text,
        "sources": list(brief.sources_used),
        "gaps": brief.evidence.get("gaps", []) if isinstance(brief.evidence, dict) else [],
        "council_ran": bool(with_council and council),
        "council_seats": len(council.opinions) if council else 0,
        "council_reviews": len(council.reviews) if council else 0,
        "council_synthesis": (council.synthesis or "") if council else "",
        "council_evidence_chars": len(council.evidence) if council else 0,
        "council_dissent": _dissent(council),
        "bottom_line": brief.bottom_line,
        "details": list(brief.details),
        "d49_impact": list(brief.d49_impact),
        "questions": list(brief.questions),
        "prose": prose,
        "verify_tag": VERIFY_TAG,
        "flagged": prose.count(VERIFY_TAG),
        "registry_size": len(registry),
        "unknown_sources": [s for s in brief.sources_used if s not in registry],
        "mentions_money": "$" in prose,
        "pending_amount": brief.evidence.get("pending_total")
                          if isinstance(brief.evidence, dict) else None,
        "pending_labelled": "pending" in prose.lower(),
        "search_strategy": brief.evidence.get("strategy")
                           if isinstance(brief.evidence, dict) else None,
        "has_total": "$" in prose,
        "totals_suppressed": bool(brief.evidence.get("totals", {}).get("suppressed"))
                             if isinstance(brief.evidence, dict) else False,
        "deliverable_id": deliverable_id,
        "path": path,
        "indexed": indexed,
        "evidence_stored": bool(evidence_text and evidence_text != "{}"),
    }


def _dissent(council) -> str | None:
    """Where the seats disagreed, if the synthesis says so at all."""
    if council is None:
        return None
    text = (council.synthesis or "").lower()
    markers = ("disagree", "dissent", "split", "objection", "against this",
               "the room", "most wrong")
    return next((m for m in markers if m in text), None)


def _mkid(kind: str, subject: str) -> str:
    raw = f"{kind}|{subject}|{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
    return f"{kind[:3].upper()}-" + hashlib.sha1(raw.encode()).hexdigest()[:10]


# ----------------------------------------------------------------- runner ----
def _record(store: Store, subject: str, **kw) -> Brief:
    """
    Brief whatever the office just found in search.

    Routed through the same builder registry as every other kind, on purpose:
    a brief written off a search result has to clear the same gates as one
    written from the fiscal position. A second path that skipped them would be
    the path everyone used.
    """
    from .anything import brief as record_brief
    return record_brief(store, subject, **kw)


BUILDERS: dict[str, Callable[..., Brief]] = {
    "fiscal": lambda store, subject, **kw: fiscal_brief(store, **kw),
    "member": lambda store, subject, **kw: member_brief(store, subject, **kw),
    "matter": lambda store, subject, **kw: matter_brief(store, int(subject), **kw),
    "record": _record,
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
    # The brief knows what it is about better than the caller does. A record
    # brief is asked for by key -- "matter:79313" -- and filing it under that
    # key puts an internal identifier in front of the Councilmember where the
    # bill number belongs.
    if brief.subject and brief.subject != subject:
        d.subject = brief.subject
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

    # The contract judges the artefact before anything is filed. A blocking
    # gate that fails is the difference between a draft and a deliverable, and
    # the office needs that difference stated rather than discovered.
    ctx = context(brief, kind=kind, subject=d.subject, ask=ask,
                  registry=registry, with_council=with_council)
    receipt = P.assess(ctx, subject=d.subject, kind=kind, stages=P.PRE_FILE)
    d.receipt = receipt

    # 6 -- file
    if write and not receipt.shippable:
        # Still written to disk -- a blocked draft is work, and throwing it away
        # helps nobody -- but never marked filed and never indexed as finished.
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        md_path = OUT_DIR / f"{d.deliverable_id}.blocked.md"
        md_path.write_text(d.to_markdown(registry) + "\n\n---\n\n## Not deliverable\n\n"
                           + receipt.why() + "\n\n```\n" + P.to_text(receipt) + "\n```\n")
        store.upsert("deliverables", [{
            "deliverable_id": d.deliverable_id, "kind": kind,
            "subject": d.subject, "status": "blocked",
            "body": d.to_markdown(registry),
            "meta": json.dumps({"stages": [vars(s) for s in d.stages],
                                "receipt": receipt.to_dict()}, default=str),
            "created": d.created, "updated": d.created,
        }])
        store.journal("deliverable.blocked",
                      {"id": d.deliverable_id, "why": receipt.why(),
                       "blockers": [g.key for g in receipt.blockers]})
        d.stages.append(Stage("file", False,
                              {"path": str(md_path),
                               "blockers": [g.key for g in receipt.blockers]},
                              note=receipt.why()))
        return d

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
        # Re-run the contract now that the filing facts exist, so the stored
        # receipt reflects the deliverable as filed rather than as drafted.
        d.receipt = P.assess({**ctx, "deliverable_id": d.deliverable_id,
                              "path": str(md_path), "indexed": True},
                             subject=d.subject, kind=kind)
        store.conn.execute(
            "UPDATE deliverables SET meta=? WHERE deliverable_id=?",
            (json.dumps({"stages": [vars(s) for s in d.stages],
                         "sources": brief.sources_used,
                         "receipt": d.receipt.to_dict()}, default=str),
             d.deliverable_id))
        store.conn.commit()
    else:
        d.stages.append(Stage("file", False, note="write disabled"))
    return d


def recent(store: Store, limit: int = 20) -> list[dict]:
    return [dict(r) for r in store.q(
        "SELECT deliverable_id, kind, subject, status, created FROM deliverables "
        "ORDER BY created DESC LIMIT ?", [limit])]
