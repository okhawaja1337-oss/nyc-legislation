#!/usr/bin/env python3
"""
Tests for the deliverable contract.

The contract's whole value is that a gate can fail. These tests are written the
way the gates were debugged: each one is a deliverable that *should* be stopped,
or one that should sail through, and the bugs found doing it for real.

The false positives matter as much as the true ones. A gate that wrongly blocks
sound work gets waved through by staff within a week, and a waved-through gate
catches nothing. Three of these tests exist because the first implementation
cried wolf:

  * `$3,008,000` normalised to "3008" -- trailing zeros stripped off an integer
    -- so a figure straight out of the evidence read as invented.
  * `86.8%` never matched a stored 0.8681, because prose rounds for display and
    the evidence keeps full precision.
  * The file gates were judged *before* filing, so nothing could ever ship.

And one true positive that changed the product: the member brief printed "the
discretionary pot grew 127.2%" while its evidence packet carried nothing to
trace that to. The renderer was reaching past its own evidence.

Run:  python3 -m universe.tests.test_pipeline
"""
from __future__ import annotations

import unittest

from ..core import pipeline as P


def deliverable(**over) -> dict:
    """A sound deliverable. Each test breaks exactly one thing."""
    ctx = {
        "subject": "North Shore ferry funding", "kind": "fiscal",
        "requester": "CM Hanks",
        "evidence_blocks": ["funding", "matters"],
        "evidence_text": '{"awards": 9488000, "lines": 295, '
                         '"confirmed": 8583000, "rate": 0.86814}',
        "sources": ["SCHEDULE_C", "TRANSPARENCY_RESO"],
        "gaps": ["no ferry ridership data in the lake"],
        "bottom_line": "FY2027 tracks $9,488,000, of which $8,583,000 is "
                       "confirmed and the rest is pending a modification.",
        "details": ["295 lines tracked", "$8,583,000 confirmed"],
        "d49_impact": ["North Shore commute times tie to economic security"],
        "questions": ["a", "b", "c"],
        "verify_tag": "[verify: source]",
        "registry_size": 27, "unknown_sources": [],
        "mentions_money": True, "pending_labelled": True, "has_total": True,
        "search_strategy": "exact",
        "deliverable_id": "FIS-abc", "indexed": True, "evidence_stored": True,
    }
    ctx["prose"] = ctx["bottom_line"] + " " + " ".join(ctx["details"])
    ctx.update(over)
    return ctx


class ContractTests(unittest.TestCase):
    def test_a_sound_deliverable_ships(self):
        self.assertEqual(P.assess(deliverable()).verdict(), "shippable")

    def test_every_gate_states_why_it_exists(self):
        """A gate nobody can argue with is a gate nobody will trust."""
        for stage in P.contract():
            self.assertTrue(stage["consumes"], f"{stage['key']} declares no inputs")
            self.assertTrue(stage["produces"], f"{stage['key']} declares no outputs")
            for gate in stage["gates"]:
                self.assertTrue(gate["asks"].endswith("?"))
                self.assertTrue(gate["because"])
                self.assertIn(gate["severity"], (P.BLOCKING, P.ADVISORY))

    def test_a_broken_gate_fails_closed(self):
        """If the check itself errors, the deliverable does not sail through."""
        boom = P.Gate("boom", "Does it explode?", P.BLOCKING, "because",
                      lambda ctx: 1 / 0)
        stage = P.Stage("t", "T", "test", ("in",), ("out",), (boom,))
        result = P.assess_stage(stage, {})
        self.assertFalse(result.passed)
        self.assertIn("errored", result.gates[0].found)


class FigureTracingTests(unittest.TestCase):
    """The gate that actually protects the office."""

    def test_an_invented_dollar_figure_is_blocked(self):
        bad = deliverable()
        bad["prose"] = bad["bottom_line"] + " Ferry capital need is $42,750,000."
        receipt = P.assess(bad)
        self.assertEqual(receipt.verdict(), "blocked")
        self.assertIn("no_invented_figures", [g.key for g in receipt.blockers])
        self.assertIn("42,750,000", receipt.blockers[0].found)

    def test_an_untraceable_figure_may_be_flagged_instead(self):
        """Saying "I could not verify this" is an honest deliverable."""
        ok = deliverable()
        ok["prose"] = (ok["bottom_line"]
                       + " Ferry capital need is $42,750,000 [verify: source].")
        self.assertTrue(P.assess(ok).shippable)

    def test_a_figure_from_the_evidence_is_not_called_invented(self):
        """Trailing zeros were being stripped: $3,008,000 became "3008"."""
        ctx = deliverable(evidence_text='{"award": 3008000.0}')
        ctx["prose"] = "The award is $3,008,000."
        passed, found = P._no_invented_figures(ctx)
        self.assertTrue(passed, found)

    def test_a_rounded_percentage_matches_a_stored_rate(self):
        """Prose rounds to one decimal; the evidence keeps 0.86814."""
        ctx = deliverable()
        ctx["prose"] = "Alignment with the Speaker is 86.8%."
        passed, found = P._no_invented_figures(ctx)
        self.assertTrue(passed, found)

    def test_the_percent_accommodation_does_not_excuse_a_bare_number(self):
        """A plain 86.8 must not be waved through by an unrelated 0.868."""
        ctx = deliverable(evidence_text='{"rate": 0.868}')
        ctx["prose"] = "The office funded 86.8 organizations."
        passed, _ = P._no_invented_figures(ctx)
        self.assertFalse(passed)

    def test_years_and_small_counts_are_not_treated_as_claims(self):
        ctx = deliverable(evidence_text="{}")
        ctx["prose"] = "In 2027 the office asked 3 questions across 5 hearings."
        passed, found = P._no_invented_figures(ctx)
        self.assertTrue(passed, found)


class BlockingTests(unittest.TestCase):
    def test_pending_money_must_be_labelled(self):
        ctx = deliverable(pending_amount=950000.0, pending_labelled=False)
        ctx["prose"] = "FY2027 tracks $9,488,000 in awards."
        self.assertIn("pending_kept_apart",
                      [g.key for g in P.assess(ctx).blockers])

    def test_a_total_from_a_loose_match_is_blocked(self):
        self.assertIn("totals_are_tight",
                      [g.key for g in P.assess(
                          deliverable(search_strategy="any-word")).blockers])

    def test_a_suppressed_total_clears_that_gate(self):
        ctx = deliverable(search_strategy="any-word", totals_suppressed=True)
        self.assertNotIn("totals_are_tight",
                         [g.key for g in P.assess(ctx).blockers])

    def test_evidence_must_declare_its_own_gaps(self):
        """Silence about a gap reads as the absence of a gap."""
        self.assertIn("coverage_declared",
                      [g.key for g in P.assess(deliverable(gaps=None)).blockers])

    def test_an_unresolvable_source_is_blocked(self):
        ctx = deliverable(unknown_sources=["MADE_UP_BOOK"])
        self.assertIn("sources_known", [g.key for g in P.assess(ctx).blockers])

    def test_a_brief_with_no_district_read_is_blocked(self):
        self.assertIn("district_impact",
                      [g.key for g in P.assess(deliverable(d49_impact=[])).blockers])

    def test_an_unindexed_file_is_blocked(self):
        self.assertIn("findable",
                      [g.key for g in P.assess(deliverable(indexed=False)).blockers])

    def test_an_ungrounded_council_is_blocked(self):
        ctx = deliverable(council_ran=True, council_evidence_chars=40,
                          council_seats=5, council_dissent="disagree")
        self.assertIn("grounded", [g.key for g in P.assess(ctx).blockers])


class StagingTests(unittest.TestCase):
    def test_the_file_stage_is_not_judged_before_filing(self):
        """
        Judging the file gates early meant nothing could ever ship: an id and
        an index entry cannot exist before the thing is written.
        """
        fresh = deliverable(deliverable_id="", indexed=False)
        self.assertTrue(P.assess(fresh, stages=P.PRE_FILE).shippable)
        self.assertFalse(P.assess(fresh).shippable)

    def test_a_pre_file_receipt_knows_it_is_incomplete(self):
        receipt = P.assess(deliverable(), stages=P.PRE_FILE)
        self.assertFalse(receipt.complete)
        self.assertTrue(P.assess(deliverable()).complete)

    def test_the_verdict_reads_like_a_sentence_a_staffer_can_act_on(self):
        why = P.assess(deliverable(d49_impact=[])).why()
        self.assertIn("Not deliverable", why)
        self.assertIn("North Shore", why)


if __name__ == "__main__":
    unittest.main(verbosity=2)
