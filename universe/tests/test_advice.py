#!/usr/bin/env python3
"""
Tests for stage, the code index, and the sign-on recommender.

These pin a class of bug the system produced three times in one sitting: a
derived value drifting away from the source it was derived from, and nobody
noticing because the output stayed plausible. A bill on the Mayor's desk
offered for sign-on, a section with a 0% track record raising no warning, and
eighty live bills silently withheld are all the same failure wearing different
clothes.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from universe.core import stage
from universe.core.store import Store
from universe.ingest import codes
from universe.intel import law, signon


class TestStage(unittest.TestCase):
    def test_the_legistar_vocabulary_is_classified(self):
        for status, want in [
            ("Committee", "live"),
            ("Laid Over in Committee", "live"),
            ("Introduced", "live"),
            ("Reported from Committee", "live"),
            ("Companion Pending Approval by Council", "live"),
            ("Enacted", "law"),
            ("Adopted", "adopted"),
            ("Filed", "dead"),
            ("Filed (End of Session)", "dead"),
            ("Withdrawn", "dead"),
            ("Disapproved", "dead"),
            ("Vetoed", "dead"),
        ]:
            self.assertEqual(stage.of(status), want, status)

    def test_a_bill_on_the_mayors_desk_is_passed_not_enacted_and_not_live(self):
        """
        "Enacted (Mayor's Desk for Signature)" contains both "enacted" and the
        parenthetical, and the parenthetical is the operative part: the
        Council is done and the Mayor is not. Twenty-four matters were flagged
        enacted=0, and seven of those pending=1 — so the recommender offered
        the Councilmember bills there was nothing left to sign.
        """
        st, enacted, pending = stage.flags("Enacted (Mayor's Desk for Signature)")
        self.assertEqual(st, "passed")
        self.assertEqual((enacted, pending), (0, 0))
        self.assertFalse(stage.signable("Enacted (Mayor's Desk for Signature)"))

    def test_only_a_live_matter_is_signable(self):
        self.assertTrue(stage.signable("Committee"))
        for other in ("Enacted", "Adopted", "Filed", "Vetoed",
                      "Enacted (Mayor's Desk for Signature)"):
            self.assertFalse(stage.signable(other), other)

    def test_an_unrecognised_status_is_unknown_not_guessed(self):
        self.assertEqual(stage.of("Referred to the Sublime Porte"), "unknown")
        self.assertFalse(stage.signable("Referred to the Sublime Porte"))

    def test_the_booleans_follow_the_stage_and_never_lead_it(self):
        for status in ("Committee", "Enacted", "Filed", "Adopted",
                       "Enacted (Mayor's Desk for Signature)"):
            st, enacted, pending = stage.flags(status)
            self.assertEqual(enacted, 1 if st == "law" else 0, status)
            self.assertEqual(pending, 1 if st == "live" else 0, status)


class TestCodeExtraction(unittest.TestCase):
    AMEND = ("Section 1. Subchapter 6 of chapter 2 of title 24 of the "
             "administrative code of the city of New York is amended by "
             "adding a new section 24-244.1 to read as follows:")

    def test_a_section_and_its_action_are_read_out(self):
        got = {r["section"]: r["action"] for r in codes.extract(self.AMEND)}
        self.assertEqual(got.get("24-244.1"), "added")

    def test_a_section_numbered_like_a_year_is_still_a_section(self):
        """
        A guard that rejected year-shaped suffixes silently discarded
        § 27-2005, the housing maintenance code — and with it every section in
        titles 26 to 28, which number their sections in the 2000s and are the
        most-legislated part of the code. The title check alone excludes bill
        numbers.
        """
        text = ("section 27-2005 of the administrative code of the city of "
                "New York is amended to read as follows")
        self.assertIn("27-2005", {r["section"] for r in codes.extract(text)})

    def test_a_bill_number_is_not_read_as_a_code_section(self):
        for text in ("Int 0365-2026 was introduced",
                     "covering the period 2020-2021",
                     "Res 1142-2023 is hereby adopted"):
            for r in codes.extract(text):
                self.assertNotEqual(r["body_of_law"], "admin_code", text)

    def test_a_title_above_thirty_four_is_not_the_administrative_code(self):
        self.assertFalse([r for r in codes.extract("section 87-1200 applies")
                          if r["body_of_law"] == "admin_code"])

    def test_charter_and_state_law_are_kept_apart_from_the_code(self):
        text = ("section 1043 of the New York city charter and section 552 of "
                "the general municipal law")
        bodies = {r["body_of_law"] for r in codes.extract(text)}
        self.assertIn("charter", bodies)
        self.assertIn("state", bodies)

    def test_the_operative_verb_beats_a_passing_mention(self):
        text = ("section 17-179 is amended to read as follows. "
                + "As used in section 17-179, the term applies. " * 5)
        got = {r["section"]: r["action"] for r in codes.extract(text)}
        self.assertEqual(got["17-179"], "amended")


class TestPrecedentAndSignon(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "lake.sqlite")
        self.store.conn.executescript(codes.SCHEMA)
        with self.store.tx() as c:
            c.executemany(
                "INSERT INTO matters (matter_id, file, name, type, status, "
                "stage, committee, year, session, enacted, pending, "
                "local_law, n_sponsors, pillars, source_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
                    (1, "Int 0001-2026", "A North Shore housing bill", "I",
                     "Committee", "live", "Housing and Buildings", 2026, "10",
                     0, 1, None, 4, '["neighborhood_development"]',
                     "LEGISTAR_MIRROR"),
                    (2, "Int 0002-2024", "An old bill still marked committee",
                     "I", "Committee", "live", "Housing and Buildings", 2024,
                     "9", 0, 1, None, 6, '["neighborhood_development"]',
                     "LEGISTAR_MIRROR"),
                    (3, "Int 0003-2026", "On the Mayor's desk", "I",
                     "Enacted (Mayor's Desk for Signature)", "passed",
                     "Health", 2026, "10", 0, 0, None, 30,
                     '["health_hospitals"]', "LEGISTAR_MIRROR"),
                ])
            # Ten failed attempts on one section, none enacted.
            c.executemany(
                "INSERT INTO code_refs (ref_id, matter_id, file, body_of_law, "
                "title, section, action, year, stage, source_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(f"x{i}", 100 + i, f"Int 0{i}-2020", "admin_code", "17",
                  "17-194.2", "amended", 2020, "dead", "LEGISTAR_MIRROR")
                 for i in range(10)]
                + [("y", 1, "Int 0001-2026", "admin_code", "17", "17-194.2",
                    "amended", 2026, "live", "LEGISTAR_MIRROR")])
            c.executemany(
                "INSERT INTO matters (matter_id, file, name, type, status, "
                "stage, year, session, enacted, pending, n_sponsors, source_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(100 + i, f"Int 0{i}-2020", "old attempt", "I", "Filed",
                  "dead", 2020, "7", 0, 0, 3, "LEGISTAR_MIRROR")
                 for i in range(10)])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_a_section_nobody_has_ever_passed_reports_zero_not_nothing(self):
        p = law.precedent(self.store, "17-194.2")
        self.assertEqual(p["attempts"], 11)
        self.assertEqual(p["enacted"], 0)
        self.assertEqual(p["rate"], 0.0)
        self.assertIn("hard", p["says"])

    def test_a_zero_percent_section_still_raises_the_warning(self):
        """
        `rate or 1` reads 0.0 as 1.0 because zero is falsy, so the sections
        nobody has ever succeeded on were the only ones that could never trip
        the warning. Exactly inverted.
        """
        got = signon.assess(self.store, 1)
        self.assertTrue(
            any("historically hard" in r for r in got["against"]),
            got["against"])
        self.assertEqual(got["verdict"], "WATCH")

    def test_a_bill_from_a_closed_session_is_never_offered_for_signing(self):
        got = signon.assess(self.store, 2)
        self.assertEqual(got["verdict"], "DECLINE")
        self.assertTrue(got["stale_session"])
        self.assertIn("closed", " ".join(got["against"]).lower())

    def test_a_bill_past_the_council_gets_a_letter_not_a_signature(self):
        got = signon.assess(self.store, 3)
        self.assertEqual(got["verdict"], "LETTER")

    def test_every_verdict_carries_its_reasoning(self):
        for mid in (1, 2, 3):
            got = signon.assess(self.store, mid)
            self.assertTrue(got["headline"])
            self.assertTrue(got["for"] or got["against"], mid)

    def test_the_queue_never_returns_an_unsignable_bill_as_sign(self):
        q = signon.queue(self.store, limit=20)
        for item in q["items"]:
            if item["verdict"] == "SIGN":
                self.assertEqual(item["stage"], "live", item["file"])
                self.assertFalse(item["stale_session"], item["file"])


if __name__ == "__main__":
    unittest.main()
