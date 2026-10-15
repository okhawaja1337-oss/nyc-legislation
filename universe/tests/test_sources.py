#!/usr/bin/env python3
"""
Tests for the source repositories and the office's working papers.

Two things are pinned here above all others. First, that a total is never
parsed as a line -- the District 49 headline "$39,627,090 · 245 items · 17
categories" matched the line-item pattern perfectly and filed the grand total
as a 246th item, reporting exactly twice the truth. Second, that the two
Schedule C questions stay apart: what the Councilmember designated
($3,008,000) and what is coded to her district ($1,042,000) differ three-fold
and are constantly mistaken for each other.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from universe.core.store import Store
from universe.ingest import office
from universe.intel import ledger as L
from universe.live import repos as R


# --------------------------------------------------------- fixtures ----
def _xlsx(path: Path, sheets: dict[str, list[list[str]]]) -> Path:
    """A minimal but real .xlsx, written by hand so tests need no library."""
    strings: list[str] = []
    index: dict[str, int] = {}

    def sid(v: str) -> int:
        if v not in index:
            index[v] = len(strings)
            strings.append(v)
        return index[v]

    def cell(col: int, row: int, value: str) -> str:
        ref = ""
        n = col + 1
        while n:
            n, r = divmod(n - 1, 26)
            ref = chr(65 + r) + ref
        try:
            float(str(value).replace(",", "").replace("$", ""))
            return f'<c r="{ref}{row}"><v>{value}</v></c>'
        except ValueError:
            return f'<c r="{ref}{row}" t="s"><v>{sid(str(value))}</v></c>'

    parts = {}
    names = list(sheets)
    for i, name in enumerate(names, start=1):
        rows = []
        for r, row in enumerate(sheets[name], start=1):
            cells = "".join(cell(c, r, v) for c, v in enumerate(row) if v != "")
            rows.append(f'<row r="{r}">{cells}</row>')
        parts[f"xl/worksheets/sheet{i}.xml"] = (
            '<?xml version="1.0"?><worksheet xmlns="http://schemas.'
            'openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            + "".join(rows) + "</sheetData></worksheet>")

    sheet_tags = "".join(
        f'<sheet name="{n}" sheetId="{i}" r:id="rId{i}"/>'
        for i, n in enumerate(names, start=1))
    rel_tags = "".join(
        f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/'
        f'officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(names) + 1))

    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0"?><workbook xmlns="http://schemas.'
                   'openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/'
                   f'2006/relationships"><sheets>{sheet_tags}</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
                   f'openxmlformats.org/package/2006/relationships">{rel_tags}'
                   '</Relationships>')
        z.writestr("xl/sharedStrings.xml",
                   '<?xml version="1.0"?><sst xmlns="http://schemas.'
                   'openxmlformats.org/spreadsheetml/2006/main">'
                   + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
        for name, body in parts.items():
            z.writestr(name, body)
    return path


def _docx(path: Path, paragraphs: list[str],
          tables: list[list[list[str]]] = ()) -> Path:
    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(
        f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    for table in tables:
        rows = "".join(
            "<w:tr>" + "".join(
                f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>"
                for c in row) + "</w:tr>" for row in table)
        body += f"<w:tbl>{rows}</w:tbl>"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml",
                   f'<?xml version="1.0"?><w:document {W}><w:body>{body}'
                   '</w:body></w:document>')
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "lake.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


# ------------------------------------------------------------ xlsx ----
class TestXlsx(Base):
    def test_reads_sheets_and_detects_the_header(self):
        path = _xlsx(self.dir / "b.xlsx", {"Totals": [
            ["FY 2027 STATEN ISLAND — COMBINED GRAND TOTAL"],
            ["Every figure below is a live formula"],
            ["Component", "Corrected", "As printed", "Basis"],
            ["Hanks — 22 lines", "17750000", "17750000", "PDF Capital Detail"],
            ["CAPITAL TOTAL", "77350000", "77350000", "PDF states"],
        ]})
        sheets = office.read_xlsx(path)
        self.assertEqual(len(sheets), 1)
        # Not row 0: the banner is not a set of column names.
        self.assertEqual(sheets[0]["header_row"], 2)
        self.assertEqual(sheets[0]["headers"][0], "Component")
        self.assertEqual(len(sheets[0]["rows"]), 2)

    def test_a_wholly_numeric_row_is_never_the_header(self):
        path = _xlsx(self.dir / "c.xlsx", {"S": [
            ["1000", "2000", "3000"],
            ["Name", "Amount", "Note"],
            ["A thing", "5000", "x"],
        ]})
        self.assertEqual(office.read_xlsx(path)[0]["header_row"], 1)

    def test_amount_prefers_the_column_the_header_names(self):
        headers = ["Page", "Organization", "Amount"]
        row = ["103", "Greenbelt Conservancy", "40000"]
        col = office._amount_column(headers)
        amount, which = office._amount(row, headers, col)
        self.assertEqual(amount, 40000.0)
        self.assertEqual(which, "Amount")

    def test_a_bare_year_is_not_money(self):
        amount, _ = office._amount(["2027", "note"], ["FY", "Note"], None)
        self.assertIsNone(amount)


class TestWorkbookIngest(Base):
    def setUp(self):
        super().setUp()
        self.path = _xlsx(self.dir / "FY27_Reconciliation.xlsx", {
            "Grand Total": [
                ["SI COMBINED"],
                ["Component", "Corrected", "Basis"],
                ["Hanks capital", "17750000", "PDF"],
                ["CAPITAL TOTAL", "77350000", "PDF states"],
                ["TIE CHECK — as printed vs PDF", "TIES ✓", ""],
            ],
            "Capital Detail": [
                ["SUB ID", "PROJECT TITLE", "FY 2027", "SPON"],
                ["CC0791", "I.S.2 TECH UPGRADES", "75000", "CARR"],
                ["CC0802", "Pier One Fishing Pier", "2500000", "HANKS"],
            ]})
        self.summary = office.ingest_workbook(self.store, self.path)

    def test_every_row_keeps_its_sheet_and_row_number(self):
        rows = self.store.q("SELECT * FROM ledger ORDER BY sheet_no, row_no")
        self.assertTrue(rows)
        for r in rows:
            self.assertTrue(r["sheet"])
            self.assertGreater(r["row_no"], 0)
            self.assertTrue(r["locator"])

    def test_totals_and_tie_checks_are_classified_apart_from_lines(self):
        kinds = {r["label"]: r["kind"] for r in
                 self.store.q("SELECT label, kind FROM ledger")}
        self.assertEqual(kinds["CAPITAL TOTAL"], "total")
        self.assertEqual(kinds["TIE CHECK — as printed vs PDF"], "tie_check")
        self.assertEqual(kinds["CC0791"], "line")

    def test_the_raw_cells_survive_so_a_figure_can_be_walked_back(self):
        row = self.store.q(
            "SELECT cells FROM ledger WHERE label = 'CAPITAL TOTAL'")[0]
        self.assertIn("77350000", json.loads(row["cells"]))

    def test_a_sponsor_is_read_off_the_row(self):
        row = self.store.q(
            "SELECT member FROM ledger WHERE label = 'CC0802'")[0]
        self.assertEqual(row["member"], "Kamillah Hanks")

    def test_reingesting_replaces_rather_than_doubles(self):
        before = self.store.q("SELECT COUNT(*) n FROM ledger")[0]["n"]
        office.ingest_workbook(self.store, self.path)
        after = self.store.q("SELECT COUNT(*) n FROM ledger")[0]["n"]
        self.assertEqual(before, after)

    def test_the_rows_are_searchable(self):
        hits = self.store.search("Pier One Fishing", entity_type="ledger")
        self.assertTrue(hits)

    def test_tie_checks_report_whether_they_pass(self):
        checks = L.tie_checks(self.store)
        self.assertEqual(len(checks), 1)
        self.assertTrue(checks[0]["passes"])


# ------------------------------------------------------------ docx ----
class TestBreakdown(Base):
    HEADLINE = "$39,627,090  ·  245 items  ·  17 categories"

    def _doc(self, extra: list[str] = ()) -> Path:
        return _docx(self.dir / "D49_FY27_Full_Breakdown.docx", [
            "COUNCIL MEMBER KAMILLAH M. HANKS",
            "FY2027 Full Breakdown",
            self.HEADLINE,
            "CULTURALS   $14,115,000  ·  21 items",
            "$5,850,000  St. George Theatre — restoration",
            "$4,000,000  Snug Harbor Cultural Center",
            "WATERFRONT   $9,500,000  ·  2 items",
            "$7,000,000  North Shore Action Plan — Tompkinsville",
            "$2,500,000  Pier One Fishing Pier",
            *extra,
        ])

    def test_the_headline_total_is_not_parsed_as_a_line_item(self):
        """
        The bug this pins: the grand total begins with a dollar figure and so
        matched the line-item pattern, filing $39,627,090 as a 246th item.
        The parse then reported exactly twice the district's real total --
        wrong by 100%, and not obviously absurd on the page.
        """
        out = office.ingest_breakdown(self.store, self._doc())
        self.assertEqual(out["items"], 4)
        self.assertEqual(out["items_sum"], 19350000.0)
        self.assertNotIn(39627090.0,
                         [r["amount"] for r in self.store.q(
                             "SELECT amount FROM ledger WHERE sheet='items'")])

    def test_the_document_states_its_own_totals_and_they_are_recorded(self):
        out = office.ingest_breakdown(self.store, self._doc())
        self.assertEqual(out["stated"]["total"], 39627090.0)
        self.assertEqual(out["stated"]["items"], 245)
        self.assertEqual(out["stated"]["categories"], 17)

    def test_the_parse_reports_that_it_does_not_tie_when_it_does_not(self):
        # The fixture holds 4 of the document's 245 items, so a parse that
        # claimed to tie would be lying.
        out = office.ingest_breakdown(self.store, self._doc())
        self.assertFalse(out["ties"])

    def test_items_carry_their_category(self):
        office.ingest_breakdown(self.store, self._doc())
        rows = {r["label"]: r["channel"] for r in self.store.q(
            "SELECT label, channel FROM ledger WHERE sheet='items'")}
        self.assertEqual(rows["St. George Theatre — restoration"], "Culturals")
        self.assertEqual(rows["Pier One Fishing Pier"], "Waterfront")

    def test_categories_are_totals_not_lines(self):
        office.ingest_breakdown(self.store, self._doc())
        kinds = {r["label"]: r["kind"] for r in self.store.q(
            "SELECT label, kind FROM ledger WHERE sheet='categories'")}
        self.assertEqual(set(kinds.values()), {"total"})


# -------------------------------------------------------- position ----
class TestPosition(Base):
    def setUp(self):
        super().setUp()
        with self.store.tx() as c:
            c.executemany(
                "INSERT INTO funding (line_id, fy, member, district, amount, "
                "source_id) VALUES (?,?,?,?,?,?)", [
                    ("a", 2027, "Kamillah Hanks", "49", 2000000, "SCHEDULE_C"),
                    # Designated by Hanks, coded citywide: counts toward what
                    # she designated, not toward what landed in the district.
                    ("b", 2027, "Kamillah Hanks", None, 1008000, "SCHEDULE_C"),
                    # Another member's money, landing in District 49.
                    ("c", 2027, "David M. Carr", "49", 42000, "SCHEDULE_C"),
                ])

    def test_designated_and_coded_to_the_district_are_different_questions(self):
        bases = {b["key"]: b for b in L.position(self.store, 2027)["bases"]}
        self.assertEqual(bases["designations"]["amount"], 3008000)
        self.assertEqual(bases["landed_in_d49"]["amount"], 2042000)
        self.assertNotEqual(bases["designations"]["amount"],
                            bases["landed_in_d49"]["amount"])

    def test_every_basis_carries_the_question_it_answers(self):
        for b in L.position(self.store, 2027)["bases"]:
            self.assertTrue(b["question"].endswith("?"))
            self.assertTrue(b["caution"])

    def test_an_unloaded_basis_is_reported_missing_not_guessed(self):
        pos = L.position(self.store, 2027)
        missing = [b for b in pos["bases"] if b["amount"] is None]
        self.assertTrue(missing)
        self.assertTrue(pos["notes"])

    def test_channels_are_not_summed_across_the_workbook(self):
        """
        The workbook restates the same $77,350,000 of capital on eight sheets,
        because restating money from several angles is what a reconciliation
        does. Grouping every line row by channel and summing reported $2.13
        billion of capital for Staten Island -- eight times over -- and looked
        entirely plausible on screen.
        """
        book = "FY27_SI_EIN_Census_and_Reconciliation_v3"
        rows = [
            # The decomposition sheet: non-overlapping components.
            (book, L.GRAND_SHEET, "Hanks — 22 lines", 17750000.0, "line", 5),
            (book, L.GRAND_SHEET, "Speaker (pure)", 26046000.0, "line", 6),
            (book, L.GRAND_SHEET, "CAPITAL TOTAL", 43796000.0, "total", 7),
            (book, L.GRAND_SHEET, "Citywide initiatives", 8341467.0, "line", 8),
            (book, L.GRAND_SHEET, "EXPENSE TOTAL", 8341467.0, "total", 9),
            # The same capital money, restated on three other sheets.
            (book, "Capital — Allocator", "SI-SUBSTANTIVE", 43796000.0, "line", 2),
            (book, "Capital — Category", "Cultural", 43796000.0, "line", 2),
            (book, "§254 SI Capital", "CC0791", 43796000.0, "line", 2),
        ]
        with self.store.tx() as c:
            c.executemany(
                "INSERT INTO ledger (row_id, book, sheet, label, amount, kind, "
                "row_no, channel) VALUES (?,?,?,?,?,?,?,'capital')",
                [(f"r{i}", *r) for i, r in enumerate(rows)])
        chans = L.channels(self.store, book)
        total = sum(c["total"] for c in chans if c["section"] == "capital")
        self.assertEqual(total, 43796000.0)
        self.assertNotEqual(total, 43796000.0 * 4)
        rec = L.channels_reconcile(self.store, book)
        self.assertTrue(rec["capital"]["ties"])
        self.assertTrue(rec["expense"]["ties"])

    def test_every_channel_component_names_the_row_it_came_from(self):
        book = "FY27_SI_EIN_Census_and_Reconciliation_v3"
        with self.store.tx() as c:
            c.execute(
                "INSERT INTO ledger (row_id, book, sheet, label, amount, kind, "
                "row_no) VALUES ('x',?,?,'Hanks — 22 lines',17750000,'line',5)",
                (book, L.GRAND_SHEET))
        got = L.channels(self.store, book)
        self.assertTrue(got[0]["locator"].endswith("row 5"))

    def test_evidence_values_are_real_numbers_for_the_gate(self):
        values = L.evidence(self.store)["values"]
        self.assertIn(3008000, values)
        self.assertTrue(all(isinstance(v, (int, float)) for v in values))


# ----------------------------------------------------------- repos ----
class TestRepos(Base):
    def _repo(self, name="budget") -> R.Repo:
        base = self.dir / "clone"
        (base / name / "data").mkdir(parents=True)
        return R.Repo(name, "https://example.invalid/x", "test", ("data",))

    def test_a_file_with_no_handler_is_marked_unhandled_not_loaded(self):
        repo = self._repo()
        (repo.path(self.dir / "clone") / "data" / "notes.txt").write_text("hi")
        out = R.manifest(self.store, repo, self.dir / "clone")
        self.assertEqual(out["counts"]["unhandled"], 1)

    def test_a_never_seen_handled_file_is_new(self):
        repo = self._repo()
        (repo.path(self.dir / "clone") / "data" / "fy27_schedule_c.json"
         ).write_text("[]")
        out = R.manifest(self.store, repo, self.dir / "clone")
        self.assertEqual(out["counts"]["new"], 1)

    def test_content_not_mtime_decides_whether_a_file_moved(self):
        """
        A clone rewrites every timestamp. If sync trusted mtime it would
        report the whole budget repository as changed on every run, and the
        office would learn to ignore the report inside a week.
        """
        root = self.dir / "clone"
        repo = self._repo()
        path = repo.path(root) / "data" / "fy27_schedule_c.json"
        path.write_text("[]")
        R.manifest(self.store, repo, root)
        with self.store.tx() as c:
            c.execute("UPDATE repo_files SET ingested_sha = sha, "
                      "ingested = '2026-01-01', status='fresh'")
        import os, time
        os.utime(path, (time.time() + 9999, time.time() + 9999))
        self.assertEqual(
            R.manifest(self.store, repo, root)["counts"]["fresh"], 1)

        path.write_text('[{"x": 1}]')
        self.assertEqual(
            R.manifest(self.store, repo, root)["counts"]["stale"], 1)

    def test_a_file_that_vanished_upstream_stops_claiming_to_be_fresh(self):
        root = self.dir / "clone"
        repo = self._repo()
        path = repo.path(root) / "data" / "fy27_schedule_c.json"
        path.write_text("[]")
        R.manifest(self.store, repo, root)
        path.unlink()
        out = R.manifest(self.store, repo, root)
        self.assertEqual(out["files"], 0)
        self.assertEqual(
            self.store.q("SELECT COUNT(*) n FROM repo_files")[0]["n"], 0)

    def test_handlers_are_matched_by_name_wherever_the_file_sits(self):
        self.assertEqual(R.handler_for("data/fy27_schedule_c.json"), "fiscal")
        self.assertEqual(R.handler_for("fy22_schedule_c.json"), "fiscal")
        self.assertEqual(
            R.handler_for("FY27_SI_EIN_Census_and_Reconciliation_v3.xlsx"),
            "workbook")
        self.assertEqual(R.handler_for("D49_FY27_Full_Breakdown.docx"),
                         "breakdown")
        self.assertEqual(R.handler_for("random.txt"), "")

    def test_an_absent_clone_says_so_rather_than_reporting_success(self):
        out = R.sync(self.store, ["budget"], root=self.dir / "nothing",
                     pull=False)
        rec = out["repos"]["budget"]
        self.assertEqual(rec["state"], "absent")
        self.assertIn("Nothing from this source is loaded", rec["says"])

    def test_working_from_disk_is_named_not_dressed_up_as_a_refresh(self):
        root = self.dir / "clone"
        repo = self._repo()
        R.REPOS["_t"] = repo
        try:
            out = R.sync(self.store, ["_t"], root=root, pull=False)
            rec = out["repos"]["_t"]
            self.assertEqual(rec["state"], "on-disk")
            self.assertIn("Could not reach GitHub", rec["says"])
        finally:
            R.REPOS.pop("_t", None)

    def test_a_code_repository_is_tracked_but_never_ingested(self):
        """
        This system's own repository contains the lake it builds. Walking it
        would hash a 144 MB output as though it were an input -- slow, and a
        lie about where the data came from.
        """
        root = self.dir / "clone"
        repo = R.Repo("_c", "https://example.invalid/c", "code", role="code")
        (repo.path(root) / "data").mkdir(parents=True)
        (repo.path(root) / "data" / "fy27_schedule_c.json").write_text("[]")
        R.REPOS["_c"] = repo
        try:
            out = R.sync(self.store, ["_c"], root=root, pull=False)
            rec = out["repos"]["_c"]
            self.assertNotIn("manifest", rec)
            self.assertIn("Code, not data", rec["says"])
            self.assertEqual(
                self.store.q("SELECT COUNT(*) n FROM repo_files")[0]["n"], 0)
        finally:
            R.REPOS.pop("_c", None)

    def test_one_failing_book_does_not_abort_the_others(self):
        root = self.dir / "clone"
        repo = self._repo()
        good = repo.path(root) / "data" / "D49_FY27_Full_Breakdown.docx"
        _docx(good, ["$1,000  A thing — note"])
        bad = repo.path(root) / "data" / "X_Reconciliation.xlsx"
        bad.write_bytes(b"not a zip at all")
        R.manifest(self.store, repo, root)
        out = R.ingest_changed(self.store, repo, root)
        self.assertIn("breakdown", out["ingested"])
        self.assertIn("workbook", out["failed"])


if __name__ == "__main__":
    unittest.main()
