#!/usr/bin/env python3
"""
Tests for identifier resolution, citation and briefing anything.

The first class exists because of one reported failure. Typing "365-2026" into
the search box returned "No record contains all of 365, 2026; showing records
matching any of them, best first" -- and the two bills that actually carry that
number were buried under every bill introduced in 2026. A bill number is the
single most common thing a legislative office types. It has to resolve.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from universe.core import identify, reference
from universe.core.store import Store
from universe.workspace import search as WS


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "lake.sqlite")
        with self.store.tx() as c:
            c.executemany(
                "INSERT INTO matters (matter_id, file, name, type, status, "
                "committee, year, enacted, local_law, n_sponsors, source_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
                    (77661, "Int 0365-2026", "Requiring HPD to schedule "
                     "subsequent inspections", "I", "Committee",
                     "Housing and Buildings", 2026, 0, None, 13,
                     "LEGISTAR_MIRROR"),
                    (78339, "Res 0365-2026", "Remove the minimum wage "
                     "requirements", "R", "Adopted", "Education", 2026, 0,
                     None, 8, "LEGISTAR_MIRROR"),
                    (70001, "Int 0977-2024", "Probation reporting", "I",
                     "Enacted", "Criminal Justice", 2024, 1, "2025/042", 20,
                     "LEGISTAR_MIRROR"),
                    (70002, "Int 0100-2022", "Something else", "I", "Enacted",
                     "Finance", 2022, 1, "2022/042", 11, "LEGISTAR_MIRROR"),
                ])
            c.executemany(
                "INSERT INTO funding (line_id, fy, member, district, org, "
                "org_key, ein, amount, source_id) VALUES (?,?,?,?,?,?,?,?,?)", [
                    ("SC2027-aaa", 2027, "Hanks", "49", "Greenbelt "
                     "Conservancy, Inc.", "EIN133481845", "13-3481845", 40000,
                     "SCHEDULE_C"),
                    ("SI-bbb", 2027, "Carr", "50", "Greenbelt Conservancy",
                     "EIN133481845", "13-3481845", 7000, "SI_ROLLUP"),
                ])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class TestParse(unittest.TestCase):
    def test_every_way_the_office_writes_a_bill_number(self):
        for text, want in [
            ("365-2026", "bill 365-2026"),
            ("Int 365-2026", "Int 0365-2026"),
            ("Int. 0365-2026", "Int 0365-2026"),
            ("int0365-2026", "Int 0365-2026"),
            ("Res 365/2026", "Res 0365-2026"),
            ("  res.365-2026 ", "Res 0365-2026"),
        ]:
            got = identify.parse(text)
            self.assertIsNotNone(got, text)
            self.assertEqual(got["display"], want, text)

    def test_zero_padding_is_applied_because_nobody_types_it(self):
        self.assertEqual(identify.parse("Int 5-2026")["number"], "0005")

    def test_a_five_word_local_law_citation_still_parses(self):
        """
        An earlier word-count guard rejected anything over four words, so
        "Local Law 1 of 2025" -- five words, and exactly how a lawyer writes
        it -- never reached the parser and fell through to a text search that
        returned 916 results.
        """
        got = identify.parse("Local Law 1 of 2025")
        self.assertEqual(got["kind"], "local_law")
        self.assertEqual(got["year"], "2025")

    def test_prose_is_not_an_identifier(self):
        for text in ["housing vouchers", "what did we fund for seniors",
                     "Snug Harbor", "the 2026 budget", "", "   "]:
            self.assertIsNone(identify.parse(text), text)

    def test_a_word_that_is_not_a_prefix_is_not_a_bill(self):
        self.assertIsNone(identify.parse("budget 365-2026"))

    def test_four_digits_alone_are_a_year_not_a_matter_id(self):
        self.assertIsNone(identify.parse("2026"))
        self.assertEqual(identify.parse("77661")["kind"], "matter_id")


class TestResolve(Base):
    def test_the_reported_failure_resolves_to_exactly_two_bills(self):
        got = identify.resolve(self.store, "365-2026")
        self.assertEqual(len(got["records"]), 2)
        self.assertEqual({r["file"] for r in got["records"]},
                         {"Int 0365-2026", "Res 0365-2026"})

    def test_a_prefix_narrows_to_one(self):
        got = identify.resolve(self.store, "Int 365-2026")
        self.assertEqual([r["file"] for r in got["records"]], ["Int 0365-2026"])

    def test_an_ambiguous_number_says_how_to_narrow_it(self):
        got = identify.resolve(self.store, "365-2026")
        self.assertIn("Int 0365-2026", got["note"])

    def test_a_bill_that_does_not_exist_says_so_rather_than_guessing(self):
        got = identify.resolve(self.store, "Int 9999-2026")
        self.assertEqual(got["records"], [])
        self.assertIn("has not synced", got["note"])

    def test_local_law_uses_the_enactment_year_not_the_bill_year(self):
        """
        Local Law 42 of 2025 is Int 0977-2024. The year in a local law number
        is the year it was enacted, so filtering on the matter's year returns
        a plausible wrong bill rather than nothing -- the worst kind of miss.
        """
        got = identify.resolve(self.store, "LL 42 of 2025")
        self.assertEqual([r["file"] for r in got["records"]], ["Int 0977-2024"])

    def test_a_local_law_number_without_a_year_returns_every_one(self):
        got = identify.resolve(self.store, "LL 42")
        self.assertEqual(len(got["records"]), 2)

    def test_an_ein_resolves_to_every_line_for_that_organisation(self):
        got = identify.resolve(self.store, "13-3481845")
        self.assertEqual(len(got["records"]), 2)

    def test_prose_returns_none_so_ordinary_search_runs(self):
        self.assertIsNone(identify.resolve(self.store, "housing vouchers"))


class TestSearchLadder(Base):
    def test_an_identifier_is_never_degraded_to_any_word_matching(self):
        got = WS.search_nl(self.store, "365-2026", limit=20)
        self.assertEqual(got["strategy"], "identifier")
        self.assertEqual(got["total"], 2)
        self.assertNotIn("matching any of them", got.get("note", ""))

    def test_a_record_the_index_has_not_caught_up_with_still_returns(self):
        # workspace_records is empty here, so every hit arrives through the
        # direct-resolution path. A bill introduced since the last reindex
        # must not read as missing.
        got = WS.search_nl(self.store, "Int 365-2026", limit=5)
        self.assertEqual(got["total"], 1)
        self.assertTrue(got["rows"][0].get("not_indexed"))

    def test_the_result_has_the_same_shape_as_any_other_search(self):
        got = WS.search_nl(self.store, "365-2026", limit=5)
        for key in ("rows", "total", "facets", "query", "limit", "parsed"):
            self.assertIn(key, got)


class TestCitations(Base):
    def _row(self, key: str) -> dict:
        return {"key": key, "kind": key.split(":")[0],
                "entity_id": key.split(":")[1]}

    def test_a_bill_cites_with_its_file_number_and_a_working_link(self):
        c = reference.for_record(self.store, self._row("matter:77661"))
        self.assertEqual(c["label"], "Int 0365-2026")
        self.assertIn("ID=77661", c["url"])
        self.assertIn("accessed", c["inline"])

    def test_an_enacted_bill_cites_its_local_law_number(self):
        c = reference.for_record(self.store, self._row("matter:70001"))
        self.assertIn("Local Law 42 of 2025", c["locator"])

    def test_a_funding_line_is_labelled_by_the_book_it_came_from(self):
        """
        A line from the office's own rollup, cited as "Schedule C", puts a
        working ledger into the record as though it were the adopted budget.
        """
        rows = [dict(r) for r in self.store.q(
            "SELECT *, 'funding' AS kind FROM funding ORDER BY source_id")]
        cites = {r["source_id"]: reference.for_record(self.store, r)
                 for r in rows}
        self.assertIn("Adopted Schedule C", cites["SCHEDULE_C"]["label"])
        self.assertIn("SI funding rollup", cites["SI_ROLLUP"]["label"])

    def test_the_internal_key_prefix_never_reaches_a_citation(self):
        c = reference.for_record(self.store, {
            "kind": "funding", "key": "funding:SC2027-aaa", "org": "Greenbelt",
            "fy": 2027, "amount": 40000, "source_id": "SCHEDULE_C"})
        self.assertIn("line SC2027-aaa", c["locator"])
        self.assertNotIn("funding:", c["locator"])

    def test_a_citation_that_fails_does_not_take_the_search_with_it(self):
        rows = [{"kind": "matter", "entity_id": None, "title": "x"},
                {"kind": "funding", "amount": "not a number"}]
        out = reference.for_results(self.store, rows)
        self.assertEqual(len(out), 2)
        for r in out:
            self.assertIn("citation", r)

    def test_the_bibliography_deduplicates_and_numbers(self):
        rows = [self._row("matter:77661"), self._row("matter:77661"),
                self._row("matter:78339")]
        reference.for_results(self.store, rows)
        bib = reference.bibliography(self.store, rows)
        self.assertEqual(len(bib), 2)
        self.assertTrue(bib[0].startswith("[1] "))


class TestBriefAnything(Base):
    def test_every_briefable_kind_is_registered_as_a_builder(self):
        from universe.ai import anything
        self.assertEqual(set(anything.PACKETS), set(anything.WRITERS))

    def test_a_bill_brief_carries_the_house_shape(self):
        from universe.ai import anything
        b = anything.brief(self.store, "matter:77661")
        self.assertTrue(b.bottom_line)
        self.assertTrue(b.details)
        self.assertEqual(len(b.questions), 3)
        self.assertTrue(b.d49_impact)

    def test_a_bill_with_no_staten_island_sponsor_says_so(self):
        from universe.ai import anything
        b = anything.brief(self.store, "matter:77661")
        self.assertTrue(any("No Staten Island member" in d
                            for d in b.d49_impact))

    def test_an_unbriefable_key_fails_honestly_rather_than_emptily(self):
        from universe.ai import anything
        b = anything.brief(self.store, "contact:99")
        self.assertIn("Nothing knows how to brief", b.bottom_line)
        self.assertFalse(anything.can_brief("contact:99"))

    def test_a_missing_record_is_reported_not_invented(self):
        from universe.ai import anything
        b = anything.brief(self.store, "matter:404404")
        self.assertIn("No matter", b.bottom_line)

    def test_the_record_kind_runs_through_the_same_gates(self):
        from universe.ai import process
        self.assertIn("record", process.BUILDERS)

    def test_every_source_a_brief_cites_is_in_the_registry(self):
        """
        The gate that caught LEGISTAR_MIRROR: a new source was introduced and
        never registered, so every brief citing it failed verification. Any
        source id the packets claim must resolve to a real citation.
        """
        from universe.ai import anything
        from universe.core.citations import DEFAULT_SOURCES
        known = {s.source_id for s in DEFAULT_SOURCES}
        for key in ("matter:77661", "funding:SC2027-aaa", "org:EIN133481845"):
            b = anything.brief(self.store, key)
            for sid in b.sources_used:
                self.assertIn(sid, known, f"{sid} cited by {key}")


class TestIndexJoins(Base):
    """
    The join that the search rebuild depends on must use an index.

    matter_text.matter_id was declared TEXT while matters.matter_id is
    INTEGER, so the join needed a CAST -- and a CAST on a join column makes
    the index unusable. SQLite quietly scanned 83 MB of bill text once per
    matter and the rebuild went from ten seconds to five and three-quarter
    minutes. Nothing failed; it was simply slow, which is the kind of
    regression that survives review and gets blamed on the data growing.
    """

    def test_the_two_matter_id_columns_are_the_same_type(self):
        kinds = {}
        for table in ("matters", "matter_text"):
            for row in self.store.q(f"PRAGMA table_info({table})"):
                if row["name"] == "matter_id":
                    kinds[table] = row["type"].upper()
        self.assertEqual(kinds["matters"], kinds["matter_text"],
                         "a type mismatch here forces a CAST, and a CAST on a "
                         "join column costs the index")

    def test_the_indexer_join_uses_the_primary_key_rather_than_scanning(self):
        plan = [r["detail"] for r in self.store.q("""
            EXPLAIN QUERY PLAN
            SELECT m.*, t.summary FROM matters m
            LEFT JOIN matter_text t ON t.matter_id = m.matter_id""")]
        joined = " ".join(plan)
        self.assertIn("SEARCH t", joined, plan)
        self.assertNotIn("SCAN t", joined, plan)

    def test_no_query_in_the_package_casts_a_matter_id(self):
        import re
        from pathlib import Path as _P
        root = _P(__file__).resolve().parents[1]
        offenders = []
        for path in root.rglob("*.py"):
            if "test_" in path.name:
                continue
            if re.search(r"CAST\s*\(\s*t?\.?matter_id", path.read_text(),
                         re.I):
                offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
