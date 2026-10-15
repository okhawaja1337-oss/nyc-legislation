#!/usr/bin/env python3
"""
Tests for the workspace.

These encode the rules the office relies on and the bugs found building it:
a v1 database must migrate without losing a row, a dependency cycle must be
refused, a loose search must not produce a publishable total, a failed refresh
must not delete good data, and a quote must never come from anywhere but a
recording.

Run:  python3 -m universe.tests.test_workspace
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta

from ..core.store import Store
from ..workspace import assistant, events, indexer, media, model, schema, search


def fresh() -> Store:
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    s = Store(tmp.name)
    schema.apply(s.conn)
    media.init(s)
    return s


class TestSchema(unittest.TestCase):
    def test_applies_twice_without_error(self):
        s = fresh()
        schema.apply(s.conn)          # must be idempotent
        tables = {r[0] for r in s.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("ws_programs", "ws_projects", "ws_sections", "ws_people",
                  "ws_task_deps", "ws_notifications", "ws_events"):
            self.assertIn(t, tables)

    def test_v1_database_migrates_without_losing_rows(self):
        """An existing workspace must open, keep its data and gain the columns."""
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        tmp.close()
        c = sqlite3.connect(tmp.name)
        c.executescript("""
            CREATE TABLE workspace_tasks (id TEXT PRIMARY KEY, title TEXT,
              owner TEXT, backup TEXT, due TEXT, priority TEXT, status TEXT,
              reviewer TEXT, link TEXT, blocker TEXT, followup TEXT,
              outcome TEXT, created TEXT, updated TEXT, project TEXT,
              description TEXT, revision INTEGER DEFAULT 1);
            CREATE TABLE workspace_comments (id INTEGER PRIMARY KEY,
              task_id TEXT, author TEXT, body TEXT, created TEXT);
        """)
        c.execute("INSERT INTO workspace_tasks (id,title,status,revision) "
                  "VALUES ('t1','Existing work','Research',3)")
        c.execute("INSERT INTO workspace_comments (task_id,author,body) "
                  "VALUES ('t1','OK','keep me')")
        c.commit()
        c.close()

        s = Store(tmp.name)
        schema.apply(s.conn)
        row = dict(s.one("SELECT * FROM workspace_tasks WHERE id='t1'"))
        self.assertEqual(row["title"], "Existing work")
        self.assertEqual(row["revision"], 3)
        for col in ("project_id", "section_id", "parent_id", "milestone"):
            self.assertIn(col, row)
        self.assertEqual(
            s.scalar("SELECT body FROM workspace_comments WHERE task_id='t1'"),
            "keep me")


class TestTasks(unittest.TestCase):
    def setUp(self):
        self.s = fresh()
        self.p = model.save_project(self.s, {"name": "Test project"})

    def test_optimistic_concurrency_refuses_a_stale_write(self):
        t = model.save_task(self.s, {"title": "First", "project_id": self.p["id"]})
        model.save_task(self.s, {"id": t["id"], "title": "Second",
                                 "revision": t["revision"]})
        with self.assertRaises(ValueError) as e:      # same old revision again
            model.save_task(self.s, {"id": t["id"], "title": "Third",
                                     "revision": t["revision"]})
        self.assertIn("changed while you were editing", str(e.exception))

    def test_subtask_inherits_its_parents_project(self):
        other = model.save_project(self.s, {"name": "Elsewhere"})
        parent = model.save_task(self.s, {"title": "Parent",
                                          "project_id": self.p["id"]})
        child = model.save_task(self.s, {"title": "Child", "parent_id": parent["id"],
                                         "project_id": other["id"]})
        self.assertEqual(child["project_id"], self.p["id"])

    def test_a_task_cannot_be_its_own_subtask(self):
        t = model.save_task(self.s, {"title": "Loop"})
        with self.assertRaises(ValueError):
            model.save_task(self.s, {"id": t["id"], "title": "Loop",
                                     "parent_id": t["id"],
                                     "revision": t["revision"]})

    def test_invalid_stage_is_refused(self):
        with self.assertRaises(ValueError):
            model.save_task(self.s, {"title": "Bad", "status": "Whenever"})


class TestDependencies(unittest.TestCase):
    def setUp(self):
        self.s = fresh()
        self.a = model.save_task(self.s, {"title": "A"})
        self.b = model.save_task(self.s, {"title": "B"})
        self.c = model.save_task(self.s, {"title": "C"})

    def test_cycles_are_refused(self):
        model.add_dependency(self.s, self.b["id"], self.a["id"])
        model.add_dependency(self.s, self.c["id"], self.b["id"])
        with self.assertRaises(ValueError) as e:      # would close A→B→C→A
            model.add_dependency(self.s, self.a["id"], self.c["id"])
        self.assertIn("circular", str(e.exception))

    def test_self_dependency_is_refused(self):
        with self.assertRaises(ValueError):
            model.add_dependency(self.s, self.a["id"], self.a["id"])

    def test_completing_a_blocker_reports_what_it_unblocks(self):
        model.add_dependency(self.s, self.b["id"], self.a["id"])
        out = model.complete_task(self.s, self.a["id"])
        self.assertEqual([u["id"] for u in out["unblocked"]], [self.b["id"]])

    def test_a_task_still_blocked_elsewhere_is_not_reported_ready(self):
        model.add_dependency(self.s, self.c["id"], self.a["id"])
        model.add_dependency(self.s, self.c["id"], self.b["id"])
        out = model.complete_task(self.s, self.a["id"])
        self.assertEqual(out["unblocked"], [])        # B still blocks C


class TestMentions(unittest.TestCase):
    def setUp(self):
        self.s = fresh()
        model.save_person(self.s, {"name": "Kamillah Hanks", "initials": "KH"})
        model.save_person(self.s, {"name": "Omar Khawaja", "initials": "OK"})
        self.t = model.save_task(self.s, {"title": "Brief", "owner": "Omar Khawaja",
                                          "actor": "Omar Khawaja"})

    def test_mention_reaches_the_right_inbox(self):
        model.add_comment(self.s, {"task_id": self.t["id"], "author": "Omar Khawaja",
                                   "body": "@Kamillah can you look at this"})
        inbox = model.inbox(self.s, "Kamillah Hanks")
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["kind"], "mention")

    def test_an_unknown_handle_notifies_nobody(self):
        """A mention that matches no one must not create a phantom notification."""
        out = model.add_comment(self.s, {"task_id": self.t["id"],
                                         "author": "Omar Khawaja",
                                         "body": "@nobody_at_all please advise"})
        self.assertEqual(out["mentions"], [])
        self.assertEqual(self.s.scalar(
            "SELECT COUNT(*) FROM ws_notifications WHERE kind='mention'"), 0)

    def test_an_import_with_no_actor_notifies_nobody(self):
        """Seeding or importing work must not fill every inbox."""
        before = self.s.scalar("SELECT COUNT(*) FROM ws_notifications")
        model.save_task(self.s, {"title": "Imported", "owner": "Kamillah Hanks"})
        self.assertEqual(
            self.s.scalar("SELECT COUNT(*) FROM ws_notifications"), before)

    def test_author_is_not_notified_of_their_own_mention(self):
        model.add_comment(self.s, {"task_id": self.t["id"], "author": "Omar Khawaja",
                                   "body": "@Omar noting this for myself"})
        self.assertEqual(model.inbox(self.s, "Omar Khawaja"), [])


class TestSearchLanguage(unittest.TestCase):
    def setUp(self):
        self.s = fresh()
        rows = [
            {"key": "funding:1", "kind": "funding", "title": "Snug Harbor — CASA",
             "body": "Snug Harbor Cultural Center arts", "fy": 2027,
             "amount": 75000, "org": "Snug Harbor Cultural Center",
             "sponsor": "Hanks", "status": "adopted", "pillar": "Arts & Cultural"},
            {"key": "funding:2", "kind": "funding", "title": "Project Hospitality",
             "body": "Project Hospitality, Inc. shelter", "fy": 2027,
             "amount": 150000, "org": "Project Hospitality, Inc.",
             "sponsor": "Hanks", "status": "adopted", "pillar": "Health & Hospitals"},
            {"key": "matter:1", "kind": "matter", "title": "Int 0001-2026 — ferry",
             "body": "ferry service reliability", "year": 2026,
             "status": "Committee", "committee": "Transportation"},
        ]
        for r in rows:
            indexer.index_one(self.s, r["kind"], r)
        self.s.conn.commit()

    def test_field_filters_bind_values(self):
        r = search.search(self.s, 'kind:funding fy:2027')
        self.assertEqual(r["total"], 2)

    def test_free_text_fields_match_on_contains(self):
        """org:"Project Hospitality" must find "Project Hospitality, Inc."."""
        r = search.search(self.s, 'org:"Project Hospitality"')
        self.assertEqual(r["total"], 1)

    def test_comparison_and_range(self):
        self.assertEqual(search.search(self.s, "amount:>100000")["total"], 1)
        self.assertEqual(search.search(self.s, "fy:2026..2027")["total"], 2)

    def test_negation_excludes(self):
        r = search.search(self.s, "-kind:funding")
        self.assertTrue(all(x["kind"] != "funding" for x in r["rows"]))

    def test_facets_agree_with_results(self):
        r = search.search(self.s, "kind:funding")
        self.assertEqual(sum(f["n"] for f in r["facets"]["fy"]), r["total"])

    def test_a_quote_in_the_query_cannot_break_the_index(self):
        for hostile in ['"', 'org:"', 'AND', '*', 'fy:not-a-number', '(']:
            self.assertIsInstance(search.search(self.s, hostile), dict)

    def test_natural_language_degrades_then_reports_how(self):
        r = search.search_nl(self.s, "What did we fund for the ferry?")
        self.assertGreater(r["total"], 0)
        self.assertIn(r["strategy"], ("content-words", "any-word"))


class TestAssistantHonesty(unittest.TestCase):
    def setUp(self):
        self.s = fresh()
        indexer.index_one(self.s, "funding", {
            "key": "funding:9", "kind": "funding", "title": "Senior center",
            "body": "senior services North Shore", "fy": 2027, "amount": 50000,
            "org": "A Senior Center", "sponsor": "Hanks", "status": "adopted"})
        self.s.conn.commit()

    def test_a_loose_match_never_produces_a_total(self):
        """Summing an any-word match would look authoritative and be wrong."""
        ev = assistant.gather(self.s, "What did we fund for seniors and ferries?")
        if ev.get("strategy") == "any-word":
            self.assertTrue(ev["funding_totals"].get("suppressed"))
            self.assertNotIn("matched_total", ev["funding_totals"])

    def test_a_precise_match_totals_with_its_caveat(self):
        ev = assistant.gather(self.s, "kind:funding fy:2027 senior")
        self.assertEqual(ev["funding_totals"]["matched_total"], 50000)
        self.assertIn("not a reconciled budget total",
                      ev["funding_totals"]["caveat"])

    def test_a_quote_without_a_transcript_is_refused(self):
        out = assistant.ask(self.s, "ferry reliability", kind="quote")
        self.assertTrue(out["blocked"])
        self.assertEqual(out["quotes"], [])
        self.assertIn("nothing the Member is on record saying", out["why"])

    def test_a_quote_with_a_transcript_is_verbatim(self):
        media.add_clip(self.s, {
            "title": "Transportation Committee, June 2026",
            "url": "https://legistar.council.nyc.gov/event/1",
            "transcript": "The ferry schedule has to be reliable for the "
                          "North Shore before we talk about anything else.",
            "kind": "hearing"})
        out = assistant.ask(self.s, "ferry", kind="quote")
        self.assertFalse(out["blocked"])
        self.assertTrue(out["quotes"])
        self.assertIn("ferry", out["quotes"][0]["text"].lower())
        self.assertTrue(out["quotes"][0]["url"])

    def test_unknown_deliverable_is_refused(self):
        with self.assertRaises(ValueError):
            assistant.ask(self.s, "anything", kind="manifesto")


class TestSyncSafety(unittest.TestCase):
    def test_a_failed_refresh_keeps_the_last_good_data(self):
        """An unreachable source must never empty the table it feeds."""
        from ..workspace import sync
        s = fresh()
        s.upsert("matters", [{"matter_id": 1, "file": "Int 0001-2026",
                              "name": "Existing bill", "status": "Committee"}])
        before = s.scalar("SELECT COUNT(*) FROM matters")
        out = sync.sync_legistar(s, year=2026)     # network is unavailable here
        self.assertFalse(out["ok"])
        self.assertEqual(s.scalar("SELECT COUNT(*) FROM matters"), before)
        health = {h["source"]: h for h in sync.source_health(s)}
        self.assertEqual(health["LEGISTAR"]["status"], "Error")
        self.assertTrue(health["LEGISTAR"]["error"])


class TestEvents(unittest.TestCase):
    def test_every_write_is_recorded_and_streamable(self):
        s = fresh()
        start = events.latest(s)
        model.save_task(s, {"title": "Watch me", "actor": "OK"})
        rows = events.since(s, start)
        self.assertTrue(rows)
        frame = events.format_sse(rows[0])
        self.assertTrue(frame.startswith("id: "))
        self.assertIn("data: ", frame)
        self.assertTrue(frame.endswith("\n\n"))

    def test_a_client_resumes_from_its_cursor(self):
        s = fresh()
        model.save_task(s, {"title": "One"})
        cursor = events.latest(s)
        model.save_task(s, {"title": "Two"})
        missed = events.since(s, cursor)
        self.assertEqual(len(missed), 1)


class TestWorkload(unittest.TestCase):
    def test_load_is_measured_against_stated_capacity(self):
        s = fresh()
        model.save_person(s, {"name": "Aide", "weekly_hours": 35})
        due = (date.today() + timedelta(days=3)).isoformat() + "T17:00:00"
        for i in range(3):
            model.save_task(s, {"title": f"Job {i}", "owner": "Aide",
                                "due": due, "estimate_hours": 10})
        row = [w for w in model.workload(s, days=14) if w["name"] == "Aide"][0]
        self.assertEqual(row["open"], 3)
        self.assertEqual(row["hours"], 30.0)
        self.assertEqual(row["capacity"], 70.0)
        self.assertEqual(row["load_pct"], 43)


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
