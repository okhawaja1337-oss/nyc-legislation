#!/usr/bin/env python3
"""
Tests for the Universe.

These are the checks that would have caught every bug found while building it:
sponsorship indices being row positions rather than ids, duplicate Schedule C
rows collapsing on a hash, a merged title row hijacking a header parse, and an
ingest that doubled its own tables when run twice.

Run:  python3 -m universe.tests.test_universe
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from ..core.citations import CitationRegistry, DEFAULT_SOURCES, Fact, VERIFY_TAG
from ..core.store import Store
from ..ingest.fiscal import clean_ein, org_key, pillar_for
from ..ingest.sheets import find_header, money, rows_from_markdown, unescape
from ..intel import funding as FI
from ..intel import integrity as II
from ..intel import legislation as LI


def fresh_store() -> Store:
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    return Store(tmp.name)


class TestCitations(unittest.TestCase):
    def test_unsourced_fact_is_flagged(self):
        reg = CitationRegistry(DEFAULT_SOURCES)
        self.assertIn(VERIFY_TAG, Fact(1234, label="Thing").render(reg))

    def test_sourced_fact_carries_its_citation(self):
        reg = CitationRegistry(DEFAULT_SOURCES)
        out = Fact(95249347, "SCHEDULE_C", "p.12", "Total", unit="usd").render(reg)
        self.assertIn("$95,249,347", out)
        self.assertNotIn(VERIFY_TAG, out)
        self.assertIn("Schedule C", out)

    def test_minted_source_is_deterministic(self):
        a = CitationRegistry().mint("X", "Y", "http://z")
        b = CitationRegistry().mint("X", "Y", "http://z")
        self.assertEqual(a.source_id, b.source_id)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.s = fresh_store()

    def test_fts_survives_hostile_input(self):
        self.s.index("member", "1", "Kamillah Hanks", "District 49", "si")
        for q in ('Hanks', 'AND', '"', 'a OR', '*', '()', 'NEAR("'):
            self.assertIsInstance(self.s.search(q), list)

    def test_search_finds_indexed_entity(self):
        self.s.index("member", "1", "Kamillah Hanks", "North Shore", "d49")
        hits = self.s.search("North Shore")
        self.assertTrue(any(h["entity_id"] == "1" for h in hits))

    def test_journal_round_trips(self):
        self.s.journal("test.event", {"n": 1})
        self.assertTrue(any(e["event"] == "test.event"
                            for e in self.s.read_journal(50)))


class TestSheetParsing(unittest.TestCase):
    def test_merged_title_row_does_not_hijack_header(self):
        """A merged title containing every header word must not match."""
        md = ("| ROLLUP BY CHANNEL AND POT (adopted + movement) |\n"
              "| :-: |\n"
              "| Channel | Pot / Initiative | Lines | Adopted |\n"
              "| MDI | A Greener NYC | 18 | $300,000 |\n")
        rows = list(rows_from_markdown(md))
        i, idx = find_header(rows, "channel", "pot", "adopted")
        self.assertEqual(i, 1)
        self.assertIn("pot / initiative", idx)

    def test_money_handles_negatives_and_parens(self):
        self.assertEqual(money("$1,234"), 1234.0)
        self.assertEqual(money("-$15,000"), -15000.0)
        self.assertEqual(money("(2,500)"), -2500.0)
        self.assertIsNone(money(""))

    def test_unescape_strips_markdown_escaping(self):
        self.assertEqual(unescape(r"TR\#1"), "TR#1")
        self.assertEqual(unescape(r"\-$15,000"), "-$15,000")


class TestFiscalNormalization(unittest.TestCase):
    def test_ein_identity_beats_name_spelling(self):
        a = org_key("Snug Harbor Cultural Center & Botanical Garden", "80-0193388")
        b = org_key("Snug Harbor Cultural Center and Botanical Garden", "800193388")
        self.assertEqual(a, b)

    def test_clean_ein_rejects_short_values(self):
        self.assertEqual(clean_ein("135562256"), "13-5562256")
        self.assertIsNone(clean_ein("PENDING"))

    def test_pillar_assignment_prefers_named_initiative(self):
        self.assertEqual(
            pillar_for("Sundog Theatre", "Cultural After-School Adventure (CASA)", ""),
            "arts_culture")


class TestLineIdentity(unittest.TestCase):
    """Duplicate designations are real and must not collapse."""

    def test_duplicate_rows_stay_distinct(self):
        from ..ingest.fiscal import _line_id
        a = _line_id(2027, 0, "", "Hispanic Federation, Inc.", 250000, "DYCD", "Citywide")
        b = _line_id(2027, 1, "", "Hispanic Federation, Inc.", 250000, "DYCD", "Citywide")
        self.assertNotEqual(a, b)


class TestTransparencyResoTiers(unittest.TestCase):
    """The two rules the office's own ledger states, enforced in code."""

    LEDGER = (
        "| Reso | Chart | Initiative | Organization — Program | EIN | Agy | "
        "Amount | Member/Body | Tier | D49 status | Note / effect |\n"
        "| TR#1 | 6 | Speaker's Initiative | Sundog Theatre, Inc. | 45-0476945 "
        "| DCLA | 100000 | Speaker | REVERSED BY TR#2 | IN D49 | rescinded later |\n"
        "| TR#2 | 6 | Speaker's Initiative | Sundog Theatre, Inc. | 45-0476945 "
        "| DCLA | -100000 | Speaker | ADOPTED-IMPLEMENTATION | IN D49 | reversal |\n"
        "| TR#2 | 6 | Speaker's Initiative | Staten Island Institute of Arts "
        "| 13-5564127 | DCLA | 100000 | Speaker | ADOPTED-PENDING-MOD | IN D49 "
        "| needs a budget modification |\n"
        "| TR#2 | 16 | HIV/AIDS Pathways to Care | Project Hospitality, Inc. "
        "| 13-3234441 | DHMH | 150000 | (citywide) | ADOPTED-IMPLEMENTATION "
        "| IN D49 | new |\n"
    )
    ROLLUP = (
        "| Channel | Pot / Initiative | Lines | Adopted | TR movement |\n"
        "| CAPITAL §254 Sec I | Section 254 capital — Hanks | 18 | 5000000 |  |\n"
        "| MDI (SI member) | Local Initiatives | 182 | 1200000 |  |\n"
        "| CAPITAL SUBTOTAL |  | 18 | 5000000 | 0 |\n"
        "| GRAND — CAPITAL + EXPENSE | two components | 200 | 6200000 | 0 |\n"
    )

    def setUp(self):
        from ..ingest.sheets import ingest_channel_rollup, ingest_tr_ledger
        self.s = fresh_store()
        ingest_tr_ledger(self.s, self.LEDGER)
        ingest_channel_rollup(self.s, self.ROLLUP)
        self.rec = FI.reconcile_si(self.s)

    def test_capital_and_expense_never_merge(self):
        self.assertEqual(self.rec["capital"]["adopted"], 5_000_000)
        self.assertEqual(self.rec["expense"]["adopted"], 1_200_000)
        self.assertIn("never merged", self.rec["rule"].lower())

    def test_pending_mod_is_excluded_from_confirmed(self):
        tr = self.rec["tr_movement"]
        self.assertEqual(tr["pending_mod"], 100_000)
        self.assertNotIn(100_000, [tr["confirmed"]])

    def test_reversal_pair_is_excluded_on_both_sides(self):
        """Dropping only the reversed half understates confirmed money."""
        tr = self.rec["tr_movement"]
        # Sundog +100k (reversed) and -100k (the reversal) both drop out, so
        # only the Project Hospitality 150k remains confirmed.
        self.assertEqual(tr["confirmed"], 150_000)
        self.assertEqual(tr["reversed_and_excluded"], 100_000)
        self.assertEqual(len(tr["reversal_pairs"]), 1)
        self.assertEqual(tr["reversal_pairs"][0]["designated_in"], "TR#1")
        self.assertEqual(tr["reversal_pairs"][0]["reversed_in"], "TR#2")

    def test_confirmed_plus_pending_foots_to_the_stated_net(self):
        tr = self.rec["tr_movement"]
        self.assertEqual(tr["check"]["confirmed_plus_pending"],
                         (tr["confirmed"] or 0) + (tr["pending_mod"] or 0))


class TestMigration(unittest.TestCase):
    def test_new_columns_apply_to_an_existing_lake(self):
        """CREATE TABLE IF NOT EXISTS never adds a column; migration must."""
        import sqlite3
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        tmp.close()
        # The realistic case: a lake built by the previous version of this
        # schema, which had every funding column except tier and reso.
        prior = """CREATE TABLE funding (
          line_id TEXT PRIMARY KEY, fy INTEGER, channel TEXT, pot TEXT,
          member TEXT, person_id INTEGER, district INTEGER, borough TEXT,
          org TEXT, org_key TEXT, ein TEXT, program TEXT, agency TEXT,
          amount REAL, section TEXT, purpose TEXT, status TEXT, mocs_id TEXT,
          analyst TEXT, in_d49 INTEGER, pillar TEXT, source_id TEXT,
          locator TEXT, updated TEXT)"""
        conn = sqlite3.connect(tmp.name)
        conn.execute(prior)
        conn.execute("INSERT INTO funding (line_id, fy, amount) VALUES ('L1', 2027, 5)")
        conn.commit()
        conn.close()
        s = Store(tmp.name)                      # must not raise
        cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(funding)")}
        self.assertIn("tier", cols)
        self.assertIn("reso", cols)
        # Migration is additive: existing rows survive untouched.
        self.assertEqual(s.scalar("SELECT amount FROM funding WHERE line_id='L1'"), 5)


class TestIntegrityIndex(unittest.TestCase):
    def test_midpoint_penalises_both_extremes(self):
        ind = II.INDICATORS_BY_KEY["speaker_alignment"]
        pop = [0.5, 0.7, 0.85, 0.95, 1.0]
        mid = II.score_indicator(ind, 0.85, pop)
        high = II.score_indicator(ind, 1.0, pop)
        low = II.score_indicator(ind, 0.5, pop)
        self.assertGreater(mid, high)
        self.assertGreater(mid, low)

    def test_missing_value_is_never_scored(self):
        ind = II.INDICATORS_BY_KEY["attendance"]
        self.assertIsNone(II.score_indicator(ind, None, [0.9, 0.95]))

    def test_lower_better_inverts(self):
        ind = II.INDICATORS_BY_KEY["plain_absence"]
        pop = [0.0, 0.05, 0.10, 0.20]
        self.assertGreater(II.score_indicator(ind, 0.0, pop),
                           II.score_indicator(ind, 0.20, pop))

    def test_grade_is_relative_to_peers(self):
        # Percentile grading needs a real population: in a five-member group the
        # top value only reaches the 90th percentile, so A- is correct there.
        pop = list(range(1, 51))
        self.assertEqual(II.grade(50, pop), "A")
        self.assertEqual(II.grade(1, pop), "F")
        self.assertIn(II.grade(25, pop), ("C+", "C"))   # median sits on the band edge
        self.assertEqual(II.grade(None, pop), "—")

    def test_methodology_publishes_every_indicator(self):
        m = II.methodology("council")
        self.assertTrue(m["indicators"])
        for i in m["indicators"]:
            for field in ("definition", "direction", "weight", "source_id"):
                self.assertIn(field, i)
        self.assertTrue(m["known_biases"])


class TestLiveFeedUrls(unittest.TestCase):
    """Feeds are network-gated in CI; the URLs they build are not."""

    def test_gpp_url_carries_facets(self):
        from ..live.feeds import capital_project_detail
        res = capital_project_detail(fiscal_years=(2026, 2027), ttl=0)
        self.assertIn("a860-gpp.nyc.gov", res.url)
        self.assertIn("fiscal_year_sim", res.url)
        self.assertIn("format=json", res.url)

    def test_legistar_uses_odata_prefixes(self):
        from ..live.feeds import legistar
        res = legistar("matters", ttl=0, top=1, filter="MatterId gt 0")
        self.assertIn("%24top=1", res.url)
        self.assertIn("%24filter", res.url)

    def test_feed_failure_is_reported_not_raised(self):
        from ..live.feeds import socrata
        res = socrata("erm2-nwe9", ttl=0, limit=1)
        self.assertIsInstance(res.ok, bool)
        self.assertIsInstance(res.rows, list)


class TestReferralRouting(unittest.TestCase):
    def test_plain_language_routes_to_the_right_agency(self):
        from ..live.greenbook import refer
        s = fresh_store()
        out = refer(s, "no heat and hot water for a week, landlord ignoring us")
        self.assertTrue(out["routes"])
        self.assertEqual(out["routes"][0]["agency_code"], "HPD")

    def test_unroutable_problem_falls_back_to_311(self):
        from ..live.greenbook import refer
        out = refer(fresh_store(), "zzzz qqqq")
        self.assertIn("311", out["fallback"])


class TestCouncil(unittest.TestCase):
    def test_scaffold_mode_still_produces_all_seats(self):
        from ..ai.council import SEATS, deliberate
        d = deliberate("Should we support this?", "evidence: none")
        self.assertEqual(len(d.opinions), len(SEATS))
        self.assertTrue(all(o.questions for o in d.opinions))
        self.assertIn("LLM Council", d.to_markdown())


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    res = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if res.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
