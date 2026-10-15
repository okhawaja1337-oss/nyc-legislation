#!/usr/bin/env python3
"""
The deliverable contract: what each stage takes in, puts out, and must prove.

The office already had a six-stage process. What it did not have was teeth.
Every stage recorded what it did, and then the deliverable was filed whether or
not those records said it was sound -- a brief that cited a source nobody could
find still came out the far end looking finished. That is the failure mode this
module exists to close.

Three ideas, and they are all boring on purpose:

  **Inputs and outputs are declared.** Each stage states what it consumes and
  what it is obliged to produce. A stage that produced nothing cannot be marked
  done because the runner said so; the contract says what "done" means.

  **Every stage carries gates.** A gate is a named check with a reason, run
  against the actual artefact rather than against the intention. Gates are
  `blocking` or `advisory`: a blocking gate that fails stops the deliverable
  from being called shippable, full stop. An advisory gate that fails is
  printed and the work continues.

  **The verdict is a receipt, not a feeling.** What was consumed, what was
  produced, which gates ran, which failed and why. A staffer can hand the
  receipt to the Councilmember and defend every line of it, or see immediately
  that they cannot.

The design rule throughout: **a gate must be able to fail.** A check that
cannot return false is decoration, and decoration is how an office convinces
itself a process is working.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

# ------------------------------------------------------------------ model ----
BLOCKING, ADVISORY = "blocking", "advisory"


@dataclass(frozen=True)
class Gate:
    """One named check. `test` returns (passed, what it found)."""
    key: str
    asks: str                       # the question, in plain words
    severity: str                   # blocking | advisory
    because: str                    # why this gate exists at all
    test: Callable[[dict], tuple[bool, str]]


@dataclass(frozen=True)
class Stage:
    key: str
    name: str
    does: str
    consumes: tuple[str, ...]       # inputs, named
    produces: tuple[str, ...]       # outputs, named
    gates: tuple[Gate, ...]


@dataclass
class GateResult:
    key: str
    asks: str
    severity: str
    passed: bool
    found: str
    because: str

    def to_dict(self) -> dict:
        return vars(self)


@dataclass
class StageResult:
    key: str
    name: str
    consumed: dict
    produced: dict
    gates: list[GateResult] = field(default_factory=list)

    @property
    def blocked_by(self) -> list[GateResult]:
        return [g for g in self.gates if g.severity == BLOCKING and not g.passed]

    @property
    def warnings(self) -> list[GateResult]:
        return [g for g in self.gates if g.severity == ADVISORY and not g.passed]

    @property
    def passed(self) -> bool:
        return not self.blocked_by

    def to_dict(self) -> dict:
        return {"key": self.key, "name": self.name,
                "consumed": self.consumed, "produced": self.produced,
                "passed": self.passed,
                "gates": [g.to_dict() for g in self.gates],
                "blocked_by": [g.key for g in self.blocked_by],
                "warnings": [g.key for g in self.warnings]}


@dataclass
class Receipt:
    """What happened, what it proves, and whether it can leave the building."""
    subject: str
    kind: str
    stages: list[StageResult] = field(default_factory=list)
    # How many stages this receipt was asked to judge. A pre-file assessment
    # covers everything up to `verify`; the file gates cannot pass before the
    # thing is filed, and requiring them early meant nothing could ever ship.
    required: int = 0
    created: str = field(default_factory=
                         lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @property
    def shippable(self) -> bool:
        expected = self.required or len(STAGES)
        return all(s.passed for s in self.stages) and len(self.stages) >= expected

    @property
    def complete(self) -> bool:
        """Was the whole contract judged, filing included?"""
        return len(self.stages) == len(STAGES)

    @property
    def blockers(self) -> list[GateResult]:
        return [g for s in self.stages for g in s.blocked_by]

    @property
    def warnings(self) -> list[GateResult]:
        return [g for s in self.stages for g in s.warnings]

    def verdict(self) -> str:
        if self.shippable and not self.warnings:
            return "shippable"
        if self.shippable:
            return "shippable with notes"
        return "blocked"

    def why(self) -> str:
        """One sentence a staffer can read and act on."""
        if self.shippable and not self.warnings:
            return ("Every stage produced what it owed and every blocking gate "
                    "passed. This can go to the Councilmember.")
        if self.shippable:
            return (f"Clear to send, with {len(self.warnings)} note"
                    f"{'s' if len(self.warnings) != 1 else ''} to read first: "
                    + "; ".join(g.asks for g in self.warnings))
        first = self.blockers[0]
        return (f"Not deliverable. {len(self.blockers)} blocking gate"
                f"{'s' if len(self.blockers) != 1 else ''} failed, starting with "
                f"'{first.asks}' — {first.found}")

    def to_dict(self) -> dict:
        return {"subject": self.subject, "kind": self.kind,
                "created": self.created, "verdict": self.verdict(),
                "complete": self.complete, "required": self.required,
                "shippable": self.shippable, "why": self.why(),
                "stages": [s.to_dict() for s in self.stages],
                "blockers": [g.to_dict() for g in self.blockers],
                "warnings": [g.to_dict() for g in self.warnings]}


# ------------------------------------------------------------------ gates ----
# A figure in prose is a claim. These are the shapes that are not claims: a
# year, a bill number, a small count ("three questions"), an ordinal.
YEAR = re.compile(r"^(19|20)\d{2}$")
FIGURE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")
SMALL = 12                          # counts this size are prose, not findings


def _figures(text: str) -> list[str]:
    """Every numeric claim in prose, with the non-claims removed."""
    out = []
    for raw in FIGURE.findall(text or ""):
        bare = raw.strip("$%").replace(",", "")
        if YEAR.match(bare):
            continue
        try:
            if "." not in bare and abs(int(bare)) <= SMALL:
                continue            # "three questions", "5 lines"
        except ValueError:
            continue
        out.append(raw)
    return out


def _value(raw: str) -> float | None:
    """A figure as a number, or None if it is not one."""
    try:
        return float(raw.strip("$%").replace(",", "").strip())
    except ValueError:
        return None


def _places(raw: str) -> int:
    """How precise the prose is claiming to be."""
    bare = raw.strip("$%").replace(",", "")
    return len(bare.split(".")[1]) if "." in bare else 0


def _norm(raw: str) -> str:
    """A figure reduced to a canonical numeric string, for exact comparison."""
    value = _value(raw)
    if value is None:
        return raw.strip("$%").replace(",", "").strip()
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def _traces(figure: str, evidence_values: list[float]) -> bool:
    """
    Does this figure in the prose come from the evidence?

    Two accommodations, both narrow, both necessary, and both for the same
    reason: a gate that cries wolf gets waved through, and then it misses the
    real fabrication.

      *Rounding.* Prose prints a derived figure to one decimal -- "86.8%" --
      while the evidence carries 0.8681. Exact string matching calls that
      invented. So the comparison asks the honest question instead: does any
      evidence value round to what the prose claims, at the precision the prose
      chose?

      *Percent scale.* A rate is stored as 0.868 and printed as "86.8%". Only a
      figure actually written as a percentage is also tried at 1/100 scale, so
      a plain 86.8 in prose is not quietly excused by an unrelated 0.868.
    """
    claimed = _value(figure)
    if claimed is None:
        return False
    places = _places(figure)

    def matches(candidate: float) -> bool:
        return round(candidate, places) == round(claimed, places)

    if any(matches(v) for v in evidence_values):
        return True
    if figure.endswith("%"):
        scaled = claimed / 100.0
        return any(round(v, places + 2) == round(scaled, places + 2)
                   for v in evidence_values)
    return False


def _g(key, asks, severity, because, test) -> Gate:
    return Gate(key, asks, severity, because, test)


def _has(ctx: dict, *keys: str) -> bool:
    return all(ctx.get(k) for k in keys)


# -- intake ------------------------------------------------------------------
def _subject_named(ctx: dict) -> tuple[bool, str]:
    subject = (ctx.get("subject") or "").strip()
    return (bool(subject),
            f"subject is '{subject}'" if subject else
            "no subject was given; a brief on nothing cannot be checked")


def _requester_known(ctx: dict) -> tuple[bool, str]:
    who = ctx.get("requester") or ctx.get("ask")
    return (bool(who),
            f"requested by/for: {str(who)[:80]}" if who else
            "no requester or question recorded, so nobody owns the answer")


# -- evidence ----------------------------------------------------------------
def _evidence_present(ctx: dict) -> tuple[bool, str]:
    blocks = ctx.get("evidence_blocks") or []
    return (bool(blocks),
            f"{len(blocks)} evidence block(s) assembled from the lake"
            if blocks else "the lake returned nothing for this subject")


def _sources_attached(ctx: dict) -> tuple[bool, str]:
    sources = ctx.get("sources") or []
    return (bool(sources),
            f"{len(sources)} source(s) cited: {', '.join(sources[:6])}"
            if sources else "evidence was assembled with no source attributed")


def _coverage_declared(ctx: dict) -> tuple[bool, str]:
    """
    Does the packet say what it does NOT know?

    Silence about a gap reads as an absence of a gap. The evidence assembler is
    obliged to publish its own holes, because the reader cannot see them.
    """
    gaps = ctx.get("gaps")
    if gaps is None:
        return False, "the packet never states what it could not find"
    return True, (f"{len(gaps)} gap(s) declared" if gaps
                  else "declared complete for this subject")


# -- deliberate --------------------------------------------------------------
def _grounded(ctx: dict) -> tuple[bool, str]:
    if not ctx.get("council_ran"):
        return True, "council not requested for this deliverable"
    size = ctx.get("council_evidence_chars") or 0
    return (size > 200,
            f"council argued over {size:,} characters of evidence" if size > 200
            else f"council was given only {size} characters -- that is a bare "
                 f"question, not a grounded deliberation")


def _dissent_recorded(ctx: dict) -> tuple[bool, str]:
    if not ctx.get("council_ran"):
        return True, "council not requested"
    seats = ctx.get("council_seats") or 0
    dissent = ctx.get("council_dissent")
    if seats and dissent is None:
        return False, ("the synthesis does not say where the seats disagreed; "
                       "unanimity that is never stated looks like agreement "
                       "nobody tested")
    return True, f"{seats} seats, disagreement recorded"


# -- draft -------------------------------------------------------------------
def _house_style(ctx: dict) -> tuple[bool, str]:
    """
    The Councilmember's format, checked rather than hoped for.

    A paragraph, then bullets, then questions. A draft that arrives as an essay
    gets rewritten by a staffer at the worst possible moment.
    """
    missing = []
    if not (ctx.get("bottom_line") or "").strip():
        missing.append("no bottom line")
    if not (ctx.get("details") or []):
        missing.append("no bullets")
    questions = ctx.get("questions") or []
    if not questions:
        missing.append("no questions")
    return (not missing,
            "; ".join(missing) if missing else
            f"bottom line, {len(ctx.get('details') or [])} bullets, "
            f"{len(questions)} questions")


def _three_questions(ctx: dict) -> tuple[bool, str]:
    n = len(ctx.get("questions") or [])
    if n == 3:
        return True, "three questions, as the house style asks"
    return (False, f"{n} questions; the standing format is three "
                   f"(an oversight packet may run longer -- say so on its face)")


def _district_impact(ctx: dict) -> tuple[bool, str]:
    impact = ctx.get("d49_impact") or []
    return (bool(impact),
            f"{len(impact)} District 49 point(s)" if impact else
            "nothing states what this means for the North Shore, which is the "
            "half of the brief nobody else in the room has")


def _no_invented_figures(ctx: dict) -> tuple[bool, str]:
    """
    Every dollar figure in the prose appears in the evidence, or is tagged.

    This is the gate that matters. It cannot tell whether a number is *right*;
    it can tell whether the number came from somewhere, and a figure that
    appeared between the evidence and the page is the one that ends up on a
    slide and then in a correction.
    """
    prose = ctx.get("prose") or ""
    evidence = ctx.get("evidence_text") or ""
    if not prose.strip():
        return False, "there is no prose to check"
    known = [v for v in (_value(f) for f in FIGURE.findall(evidence))
             if v is not None]
    loose = [f for f in _figures(prose) if not _traces(f, known)]
    if not loose:
        return True, "every figure in the prose appears in the evidence"
    tag = ctx.get("verify_tag") or "[verify: source]"
    if tag in prose and len(loose) <= prose.count(tag):
        return True, (f"{len(loose)} figure(s) not in the evidence, and the "
                      f"draft flags at least that many as unverified")
    return False, (f"{len(loose)} figure(s) in the prose do not appear in the "
                   f"evidence and are not flagged: {', '.join(loose[:5])}")


# -- verify ------------------------------------------------------------------
def _sources_known(ctx: dict) -> tuple[bool, str]:
    unknown = ctx.get("unknown_sources") or []
    return (not unknown,
            f"unknown source id(s): {', '.join(unknown)}" if unknown else
            "every cited source resolves in the registry")


def _totals_are_tight(ctx: dict) -> tuple[bool, str]:
    """A total built from a loose keyword match is not a total."""
    if ctx.get("totals_suppressed"):
        return True, "a loose match was detected and the total was suppressed"
    strategy = ctx.get("search_strategy")
    if strategy == "any-word" and ctx.get("has_total"):
        return False, ("a dollar total was produced from an any-word match; "
                       "that sums unrelated lines and reads as authoritative")
    return True, f"totals derive from a {strategy or 'direct'} match"


def _pending_kept_apart(ctx: dict) -> tuple[bool, str]:
    """Money awaiting a budget modification must never sit inside a headline."""
    if not ctx.get("mentions_money"):
        return True, "no money figures in this deliverable"
    if ctx.get("pending_amount") and not ctx.get("pending_labelled"):
        return False, (f"${ctx['pending_amount']:,.0f} is pending a budget "
                       f"modification but is not labelled as pending")
    return True, "confirmed and pending money are reported separately"


# -- file --------------------------------------------------------------------
def _id_assigned(ctx: dict) -> tuple[bool, str]:
    did = ctx.get("deliverable_id")
    return bool(did), (f"filed as {did}" if did else "no id, so it cannot be cited")


def _findable(ctx: dict) -> tuple[bool, str]:
    return (bool(ctx.get("indexed")),
            "indexed and searchable" if ctx.get("indexed") else
            "written to disk but not indexed, so nobody will find it again")


def _reproducible(ctx: dict) -> tuple[bool, str]:
    return (bool(ctx.get("evidence_stored")),
            "the evidence packet is stored with the deliverable"
            if ctx.get("evidence_stored") else
            "the prose was stored without its evidence, so the figures cannot "
            "be re-derived in six months")


# ----------------------------------------------------------------- stages ----
STAGES: tuple[Stage, ...] = (
    Stage(
        "intake", "Intake", "Pin down what is being asked and who needs it.",
        consumes=("a question or subject", "a requester", "a deadline"),
        produces=("a scoped request", "an owner"),
        gates=(
            _g("subject_named", "Is there a subject?", BLOCKING,
               "A brief on nothing cannot be checked by anything downstream.",
               _subject_named),
            _g("requester_known", "Does someone own the answer?", ADVISORY,
               "Unowned work is work that misses its deadline quietly.",
               _requester_known),
        )),
    Stage(
        "evidence", "Evidence", "Assemble from the lake, and publish the gaps.",
        consumes=("the data lake", "the scoped request"),
        produces=("an evidence packet", "source ids", "a declared coverage gap list"),
        gates=(
            _g("evidence_present", "Did the lake return anything?", BLOCKING,
               "A deliverable with no evidence is an opinion in a house format.",
               _evidence_present),
            _g("sources_attached", "Is every block attributed?", BLOCKING,
               "An unattributed figure cannot be defended in a hearing.",
               _sources_attached),
            _g("coverage_declared", "Does it say what it does not know?", BLOCKING,
               "Silence about a gap reads as the absence of a gap.",
               _coverage_declared),
        )),
    Stage(
        "deliberate", "Deliberate", "Five seats argue the evidence, then review each other.",
        consumes=("the evidence packet",),
        produces=("five opinions", "five peer reviews", "a synthesis", "the disagreements"),
        gates=(
            _g("grounded", "Did the council get evidence, not a bare question?",
               BLOCKING,
               "An ungrounded council produces confident prose about nothing.",
               _grounded),
            _g("dissent_recorded", "Is the disagreement written down?", ADVISORY,
               "Unanimity nobody states looks like agreement nobody tested.",
               _dissent_recorded),
        )),
    Stage(
        "draft", "Draft", "Render in the Councilmember's format.",
        consumes=("the evidence packet", "the synthesis"),
        produces=("a bottom line", "bullets", "District 49 impact", "questions"),
        gates=(
            _g("house_style", "Paragraph, bullets, questions?", BLOCKING,
               "A draft that arrives as an essay gets rewritten at the worst "
               "possible moment.", _house_style),
            _g("no_invented_figures",
               "Does every figure trace to the evidence?", BLOCKING,
               "A number that appeared between the evidence and the page is "
               "the one that ends up in a correction.", _no_invented_figures),
            _g("district_impact", "Does it say what this means for D49?",
               BLOCKING,
               "The district read is the half of the brief nobody else in the "
               "room has.", _district_impact),
            _g("three_questions", "Exactly three questions?", ADVISORY,
               "The standing format is three; an oversight packet may run "
               "longer, but should say so.", _three_questions),
        )),
    Stage(
        "verify", "Verify", "Prove the deliverable is honest about its own numbers.",
        consumes=("the draft", "the citation registry"),
        produces=("a verification record", "a list of flagged figures"),
        gates=(
            _g("sources_known", "Does every source id resolve?", BLOCKING,
               "A citation nobody can follow is worse than no citation.",
               _sources_known),
            _g("totals_are_tight", "Is any total built from a loose match?",
               BLOCKING,
               "Summing loosely-matched lines yields a figure that looks "
               "authoritative and is not.", _totals_are_tight),
            _g("pending_kept_apart",
               "Is pending money labelled as pending?", BLOCKING,
               "Announcing money a budget modification has not passed is the "
               "mistake this office cannot afford twice.", _pending_kept_apart),
        )),
    Stage(
        "file", "File", "Persist it so it can be found, cited and re-derived.",
        consumes=("the verified draft",),
        produces=("a deliverable id", "markdown and JSON on disk",
                  "a search index entry"),
        gates=(
            _g("id_assigned", "Does it have an id?", BLOCKING,
               "Work that cannot be cited cannot be reused.", _id_assigned),
            _g("findable", "Is it indexed?", BLOCKING,
               "A file nobody can search for is a file nobody will find again.",
               _findable),
            _g("reproducible", "Is the evidence stored with it?", ADVISORY,
               "Prose without its evidence cannot be re-derived in six months.",
               _reproducible),
        )),
)

BY_KEY = {s.key: s for s in STAGES}


# ------------------------------------------------------------------- runs ----
def assess_stage(stage: Stage, ctx: dict) -> StageResult:
    """Run one stage's gates against the real artefact."""
    result = StageResult(
        stage.key, stage.name,
        consumed={k: _describe(ctx, k) for k in stage.consumes},
        produced={k: _describe(ctx, k) for k in stage.produces})
    for gate in stage.gates:
        try:
            passed, found = gate.test(ctx)
        except Exception as exc:          # a broken gate fails closed
            passed, found = False, f"the gate itself errored: {type(exc).__name__}"
        result.gates.append(GateResult(gate.key, gate.asks, gate.severity,
                                       bool(passed), found, gate.because))
    return result


# Input and output names map onto context keys so the receipt can show what was
# actually there, not just what the contract asked for.
EVIDENCE_OF = {
    "a question or subject": "subject",
    "a requester": "requester",
    "a deadline": "deadline",
    "the data lake": "lake_counts",
    "the scoped request": "subject",
    "an evidence packet": "evidence_blocks",
    "source ids": "sources",
    "a declared coverage gap list": "gaps",
    "five opinions": "council_seats",
    "five peer reviews": "council_reviews",
    "a synthesis": "council_synthesis",
    "the disagreements": "council_dissent",
    "a bottom line": "bottom_line",
    "bullets": "details",
    "District 49 impact": "d49_impact",
    "questions": "questions",
    "the draft": "prose",
    "the citation registry": "registry_size",
    "a verification record": "unknown_sources",
    "a list of flagged figures": "flagged",
    "the verified draft": "prose",
    "a deliverable id": "deliverable_id",
    "markdown and JSON on disk": "path",
    "a search index entry": "indexed",
    "an owner": "requester",
    "the evidence packet": "evidence_blocks",
    "the synthesis": "council_synthesis",
}


def _describe(ctx: dict, label: str) -> Any:
    """What was actually present for this named input or output."""
    key = EVIDENCE_OF.get(label)
    if key is None or key not in ctx:
        return None
    value = ctx[key]
    if isinstance(value, (list, tuple, dict)):
        return len(value)
    if isinstance(value, str):
        return len(value) if len(value) > 60 else value
    return value


def assess(ctx: dict, subject: str = "", kind: str = "",
           stages: Iterable[Stage] = STAGES) -> Receipt:
    """
    Run the whole contract against one deliverable's context.

    `ctx` is a flat dictionary of everything known about the artefact. Nothing
    here reaches into the lake -- the caller assembles the facts, the contract
    judges them, and the two stay separable so a gate can be tested on its own.
    """
    stages = list(stages)
    receipt = Receipt(subject or ctx.get("subject", ""),
                      kind or ctx.get("kind", ""), required=len(stages))
    for stage in stages:
        receipt.stages.append(assess_stage(stage, ctx))
    return receipt


# Everything a deliverable must clear *before* it is written. The file stage is
# judged afterwards, against the filing that actually happened.
PRE_FILE = tuple(s for s in STAGES if s.key != "file")


def contract() -> list[dict]:
    """The declared pipeline, for a UI or a reader. No data required."""
    return [{
        "key": s.key, "name": s.name, "does": s.does,
        "consumes": list(s.consumes), "produces": list(s.produces),
        "gates": [{"key": g.key, "asks": g.asks, "severity": g.severity,
                   "because": g.because} for g in s.gates],
    } for s in STAGES]


def readiness(store) -> dict:
    """
    Can the pipeline run at all right now?

    Checks the inputs the first stage depends on -- the lake itself. An empty
    table is not an error, it is a reason a deliverable will come out thin, and
    the office should know which one before it asks for a brief.
    """
    tables = ("matters", "funding", "members", "calendar", "contacts",
              "sponsorships", "orgs")
    counts = {}
    for table in tables:
        try:
            counts[table] = store.scalar(f"SELECT COUNT(*) FROM {table}") or 0
        except Exception:
            counts[table] = None
    empty = [t for t, n in counts.items() if not n]
    return {
        "counts": counts,
        "empty": empty,
        "ready": not empty,
        "note": ("Every input table has rows; a brief can be built on any "
                 "subject the lake covers."
                 if not empty else
                 f"{len(empty)} input table(s) are empty: {', '.join(empty)}. "
                 f"A deliverable touching those will fail its evidence gate "
                 f"rather than quietly produce a thin brief."),
    }


# ----------------------------------------------------------------- render ----
MARK = {True: "PASS", False: "FAIL"}


def to_text(receipt: Receipt) -> str:
    """The receipt as a staffer reads it."""
    out = [f"{receipt.kind or 'deliverable'}: {receipt.subject}",
           f"VERDICT: {receipt.verdict().upper()}",
           receipt.why(), ""]
    for stage in receipt.stages:
        flag = "OK " if stage.passed else "!! "
        out.append(f"{flag}{stage.name}")
        consumed = [f"{k}={v}" for k, v in stage.consumed.items() if v not in (None, "")]
        produced = [f"{k}={v}" for k, v in stage.produced.items() if v not in (None, "")]
        out.append(f"     in : {', '.join(consumed) or '—'}")
        out.append(f"     out: {', '.join(produced) or '—'}")
        for gate in stage.gates:
            out.append(f"     [{MARK[gate.passed]}] {gate.asks}")
            out.append(f"            {gate.found}")
        out.append("")
    return "\n".join(out)
