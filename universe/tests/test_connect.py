#!/usr/bin/env python3
"""
Tests for the connectors, change detection, breakdowns and meeting packets.

Every case here is a bug that was found by running the thing, not a bug that
was imagined. In order of how much damage each would have done:

  * A model whose reasoning ate the token budget returned no text, and every
    AI-written brief silently degraded to a scaffold while reporting itself as
    written.
  * `session` is stored as text, so MAX() answered '9' for a corpus running
    through session 10 -- every "what is live now" query read the wrong term.
  * A hearing's location was parsed as part of its committee name, which
    matched no committee and produced an empty agenda with no error.
  * Breakdown parameters bound in the wrong order, so every total came back
    zero and the "(not stated)" label leaked into the data.
  * "HANKS" and "Hanks" counted as two members, halving each figure.
  * A deferred hearing generated a packet as though it were happening.

Run:  python3 -m universe.tests.test_connect
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

from ..connect import captions, gcal, gsheets
from ..connect import ics as ics_reader
from ..core import keys
from ..core.store import Store
from ..live import si_directory, watch
from ..workspace import breakdown, indexer, meeting, schema


def fresh() -> Store:
    store = Store(tempfile.mktemp(suffix=".sqlite"))
    schema.apply(store.conn)
    return store


# ------------------------------------------------------------------ keys ----
class KeyTests(unittest.TestCase):
    def test_a_secret_is_never_written_into_the_repository(self):
        """The one rule. A key in the working tree is a key that gets pushed."""
        from pathlib import Path
        repo = Path(keys.__file__).resolve().parents[2]
        with self.assertRaises(ValueError):
            keys.save({"anthropic_api_key": "sk-test"}, repo / "universe" / "leak.json")

    def test_one_variable_may_carry_several_keys(self):
        """Two keys means a revoked one does not stop a briefing."""
        self.assertEqual(keys._as_list("a, b ,c"), ["a", "b", "c"])
        self.assertEqual(keys._as_list(["x", " y "]), ["x", "y"])
        self.assertEqual(keys._as_list(None), [])

    def test_status_never_reveals_a_key(self):
        import os
        os.environ["CAPTIONS_API_KEY"] = "super-secret-value-1234"
        try:
            blob = json.dumps(keys.status())
            self.assertNotIn("super-secret-value", blob)
            self.assertIn("1234", blob)         # last four, to tell keys apart
        finally:
            del os.environ["CAPTIONS_API_KEY"]


# ------------------------------------------------------------------- ics ----
class IcsTests(unittest.TestCase):
    SAMPLE = ("BEGIN:VCALENDAR\r\n"
              "BEGIN:VEVENT\r\nUID:a\r\n"
              "DTSTART;TZID=America/New_York:20260114T100000\r\n"
              "DTEND;TZID=America/New_York:20260114T120000\r\n"
              "SUMMARY:KH/OK: Committee on Public Safety\\, Oversight\r\n"
              "LOCATION:250 Broadway\\, 14th Floor\r\n"
              "RRULE:FREQ=WEEKLY;BYDAY=WE;COUNT=3\r\n"
              "END:VEVENT\r\n"
              "BEGIN:VEVENT\r\nUID:b\r\nDTSTART;VALUE=DATE:20260216\r\n"
              "SUMMARY:Office Closed\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")

    def test_folding_escaping_and_recurrence(self):
        events = ics_reader.parse(self.SAMPLE)
        self.assertEqual(len(events), 4)               # 3 occurrences + 1 all-day
        self.assertIn("Public Safety, Oversight", events[0]["summary"])
        self.assertEqual(events[0]["location"], "250 Broadway, 14th Floor")

    def test_an_all_day_event_is_never_given_a_time(self):
        """Midnight on a holiday sorts against real meetings and reads as 12am."""
        allday = [e for e in ics_reader.parse(self.SAMPLE) if e["id"] == "b"][0]
        self.assertIn("date", allday["start"])
        self.assertNotIn("dateTime", allday["start"])

    def test_a_runaway_rule_cannot_fill_the_lake(self):
        out = ics_reader.expand("2026-01-01T09:00:00", "2026-01-01T10:00:00",
                                "FREQ=DAILY", horizon_days=5000)
        self.assertLessEqual(len(out), ics_reader.MAX_OCCURRENCES)

    def test_an_unsupported_rule_yields_one_date_not_a_wrong_one(self):
        out = ics_reader.expand("2026-01-01T09:00:00", None, "FREQ=YEARLY")
        self.assertEqual(out, [("2026-01-01T09:00:00", None)])


# -------------------------------------------------------------- transport ----
class TransportTests(unittest.TestCase):
    def test_a_failed_fetch_reports_every_door_it_tried(self):
        """An empty screen with no explanation is not an answer."""
        got = gcal.fetch(days_ahead=1, transport="auto")
        self.assertFalse(got["ok"])
        self.assertTrue(got["attempts"])
        self.assertIn("how_to_fix", got)

    def test_a_sign_in_page_is_not_data(self):
        """Google answers 200 with HTML when a sheet is private. Ingesting that
        would load a login page into the budget books."""
        self.assertTrue(gsheets._looks_like_html("<!DOCTYPE html><html>..."))
        self.assertFalse(gsheets._looks_like_html("Org,Amount\nAcme,100"))

    def test_a_pipe_inside_a_cell_cannot_split_a_budget_line(self):
        text = gsheets.to_markdown([["Org", "Note"], ["Acme", "a|b"]])
        self.assertIn(r"a\|b", text)
        self.assertEqual(len(text.splitlines()), 2)


# -------------------------------------------------------------- captions ----
class CaptionTests(unittest.TestCase):
    VTT = ("WEBVTT\n\n1\n00:00:05.000 --> 00:00:09.500\n"
           ">> CHAIR HANKS: Good morning.\n\n"
           "2\n00:00:09.500 --> 00:00:14.000\n"
           "We are here to examine ferry service on the North Shore.\n")

    def test_speaker_and_timestamps_survive_parsing(self):
        cues = captions.parse_vtt(self.VTT)
        self.assertEqual(cues[0]["speaker"], "CHAIR HANKS")
        self.assertEqual(cues[0]["start_s"], 5.0)
        self.assertEqual(cues[1]["end_s"], 14.0)

    def test_rolling_auto_captions_are_collapsed(self):
        """Auto-captions repeat the line as they build it; left alone a hearing
        becomes 300KB of the same phrase and search matches it six times."""
        rolling = [{"start_s": 0, "end_s": 1, "text": "we are here"},
                   {"start_s": 1, "end_s": 2, "text": "we are here to examine"},
                   {"start_s": 2, "end_s": 3, "text": "we are here to examine ferries"}]
        self.assertEqual(len(captions._dedupe(rolling)), 1)

    def test_a_quote_can_only_come_from_a_stored_transcript(self):
        with fresh() as store:
            captions.init(store)
            self.assertEqual(captions.quotes(store, "ferry"), [])
            store.upsert("ws_media", [{"id": "m1", "kind": "hearing",
                                       "title": "Transportation",
                                       "url": "https://www.youtube.com/watch?v=abcdefghijk",
                                       "published": "2026-05-01"}])
            captions.save(store, "m1", captions.parse_vtt(self.VTT), auto=False)
            got = captions.quotes(store, "ferry")
            self.assertTrue(got)
            self.assertIn("ferry", got[0]["quote"].lower())

    def test_a_quote_carries_a_link_to_the_second_it_was_said(self):
        """A quote a reporter can check beats a polished one they cannot."""
        with fresh() as store:
            captions.init(store)
            store.upsert("ws_media", [{"id": "m1", "kind": "hearing", "title": "T",
                                       "url": "https://www.youtube.com/watch?v=abcdefghijk",
                                       "published": "2026-05-01"}])
            captions.save(store, "m1", captions.parse_vtt(self.VTT), auto=False)
            link = captions.quotes(store, "ferry")[0]["link"]
            self.assertIn("abcdefghijk", link)
            self.assertIn("t=", link)

    def test_auto_generated_captions_are_flagged_as_such(self):
        """"Uh" and a mis-heard surname are not a quote; a press secretary has
        to be told which is which."""
        with fresh() as store:
            captions.init(store)
            store.upsert("ws_media", [{"id": "m1", "kind": "video", "title": "T",
                                       "url": "u", "published": "2026-05-01"}])
            captions.save(store, "m1", captions.parse_vtt(self.VTT), auto=True)
            row = store.one("SELECT transcript_source FROM ws_media WHERE id='m1'")
            self.assertIn("auto-generated", (row["transcript_source"] or "").lower())
            quote = captions.quotes(store, "ferry")[0]
            self.assertIn("auto-generated", quote["verify"])


# ----------------------------------------------------------------- watch ----
class WatchTests(unittest.TestCase):
    def seed(self, store):
        store.upsert("funding", [
            {"line_id": "L1", "fy": 2027, "member": "Hanks", "org": "Acme",
             "amount": 50000.0, "tier": "ADOPTED-IMPLEMENTATION",
             "status": "adopted", "source_id": "TRANSPARENCY_RESO"},
            {"line_id": "L2", "fy": 2027, "member": "Hanks", "org": "Beta",
             "amount": 10000.0, "tier": "ADOPTED-IMPLEMENTATION",
             "status": "adopted", "source_id": "SCHEDULE_C"}])

    def test_the_first_scan_is_a_baseline_not_50000_alerts(self):
        with fresh() as store:
            self.seed(store)
            got = watch.scan(store, ["funding"])
            self.assertEqual(got["changes"], 0)
            self.assertTrue(got["counts"]["funding"]["baseline"])

    def test_a_reversal_is_reported_loudly(self):
        """Money already announced being pulled back is the single most
        consequential change in this dataset."""
        with fresh() as store:
            self.seed(store)
            watch.scan(store, ["funding"])
            store.conn.execute("UPDATE funding SET tier='REVERSED' WHERE line_id='L1'")
            store.conn.commit()
            got = watch.scan(store, ["funding"])
            high = [c for c in got["detail"] if c["severity"] == "high"]
            self.assertTrue(high)
            self.assertIn("reversed", high[0]["why"].lower())

    def test_a_small_correction_does_not_page_the_office(self):
        """An alert that fires for everything is an alert that gets muted."""
        with fresh() as store:
            self.seed(store)
            watch.scan(store, ["funding"])
            store.conn.execute("UPDATE funding SET amount=10500 WHERE line_id='L2'")
            store.conn.commit()
            got = watch.scan(store, ["funding"])
            self.assertTrue(got["changes"])
            self.assertEqual(got["high"], 0)

    def test_the_digest_states_the_net_movement_and_its_sources(self):
        with fresh() as store:
            self.seed(store)
            watch.scan(store, ["funding"])
            store.conn.execute("UPDATE funding SET amount=90000 WHERE line_id='L1'")
            store.conn.commit()
            watch.scan(store, ["funding"])
            digest = watch.digest(store)
            self.assertIn("40,000", digest["headline"])
            self.assertIn("TRANSPARENCY_RESO", digest["sources"])

    def test_an_acknowledged_change_stops_being_outstanding(self):
        with fresh() as store:
            self.seed(store)
            watch.scan(store, ["funding"])
            store.conn.execute("UPDATE funding SET tier='REVERSED' WHERE line_id='L1'")
            store.conn.commit()
            change = watch.scan(store, ["funding"])["detail"][0]
            self.assertTrue(watch.acknowledge(store, change["id"], "OK"))
            self.assertEqual(watch.status(store)["unacknowledged_high"], 0)


# ------------------------------------------------------------- breakdown ----
class BreakdownTests(unittest.TestCase):
    def seed(self, store):
        rows = [
            {"key": "f1", "kind": "funding", "sponsor": "HANKS", "sponsor_key": "hanks",
             "initiative": "CASA", "fy": 2027, "amount": 100000.0,
             "status": "adopted", "tier": "ADOPTED-IMPLEMENTATION",
             "source_id": "SCHEDULE_C", "committee": "", "agency": "DCLA"},
            {"key": "f2", "kind": "funding", "sponsor": "Hanks", "sponsor_key": "hanks",
             "initiative": "CASA", "fy": 2027, "amount": 50000.0,
             "status": "adopted", "tier": "ADOPTED-PENDING-MOD",
             "source_id": "TRANSPARENCY_RESO", "committee": "", "agency": "DCLA"},
            {"key": "f3", "kind": "funding", "sponsor": "Carr", "sponsor_key": "carr",
             "initiative": "Local", "fy": 2027, "amount": 25000.0,
             "status": "adopted", "tier": "", "source_id": "SCHEDULE_C",
             "committee": "", "agency": "DYCD"},
        ]
        cols = [c for c in indexer.COLUMNS]
        store.conn.executemany(
            f"INSERT INTO workspace_records ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})",
            [[r.get(c) for c in cols] for r in rows])
        store.conn.commit()

    def test_the_same_member_spelled_two_ways_is_one_bucket(self):
        """"HANKS" and "Hanks" splitting the money is wrong in a way nobody
        notices until it is on a slide."""
        with fresh() as store:
            self.seed(store)
            cut = breakdown.break_by(store, "member", filters={"kind": "funding"})
            hanks = [b for b in cut["buckets"] if b["value"] == "hanks"]
            self.assertEqual(len(hanks), 1)
            self.assertEqual(hanks[0]["records"], 2)
            self.assertEqual(hanks[0]["amount"], 150000.0)

    def test_pending_money_is_never_folded_into_a_confirmed_total(self):
        """Announcing money a budget modification has not passed is the mistake
        this office cannot afford twice."""
        with fresh() as store:
            self.seed(store)
            cut = breakdown.break_by(store, "member", filters={"kind": "funding"})
            hanks = [b for b in cut["buckets"] if b["value"] == "hanks"][0]
            self.assertEqual(hanks["confirmed"], 100000.0)
            self.assertEqual(hanks["pending"], 50000.0)

    def test_totals_are_not_all_zero(self):
        """Parameters bound in the wrong order silently zeroed every figure."""
        with fresh() as store:
            self.seed(store)
            cut = breakdown.break_by(store, "initiative", filters={"kind": "funding"})
            self.assertEqual(cut["totals"]["amount"], 175000.0)

    def test_the_empty_label_does_not_leak_into_the_data(self):
        with fresh() as store:
            self.seed(store)
            cut = breakdown.break_by(store, "tier", filters={"kind": "funding"})
            values = {b["value"] for b in cut["buckets"]}
            self.assertNotIn("funding", values)
            self.assertIn("(not stated)", values)

    def test_every_total_carries_its_sources(self):
        with fresh() as store:
            self.seed(store)
            cut = breakdown.break_by(store, "member", filters={"kind": "funding"})
            self.assertIn("SCHEDULE_C", cut["sources"])
            self.assertIn("TRANSPARENCY_RESO", cut["sources"])

    def test_an_unknown_dimension_says_what_is_allowed(self):
        with fresh() as store:
            with self.assertRaises(ValueError) as caught:
                breakdown.break_by(store, "astrology")
            self.assertIn("committee", str(caught.exception))


# --------------------------------------------------------------- meeting ----
class MeetingTests(unittest.TestCase):
    def test_the_committee_is_read_from_the_title_not_the_address(self):
        """Reading the location too produced "Sanitation ... Management 250
        Broadway", which matched no committee and emptied the agenda."""
        got = meeting.committee_of(
            "OVERSIGHT: Committee on Sanitation and Solid Waste Management")
        self.assertEqual(got, "Sanitation And Solid Waste Management")

    def test_a_committee_it_has_never_heard_of_still_resolves(self):
        self.assertEqual(meeting.committee_of("OVERSIGHT: Committee on Children and Youth"),
                         "Children And Youth")

    def test_a_joint_hearing_names_the_lead_committee(self):
        self.assertEqual(
            meeting.committee_of("Committee on Finance jointly with the Committee on Parks"),
            "Finance")

    def test_a_deferred_hearing_does_not_get_a_packet(self):
        """Preparing for a hearing that was called off wastes a morning and
        leaves the office believing it is happening."""
        self.assertTrue(meeting.is_deferred("Deferred: Committee on Land Use"))
        self.assertTrue(meeting.is_deferred("DEFFERED: Oversight: Economic Development"))
        self.assertEqual(meeting.classify({"summary": "Deferred: OVERSIGHT: Parks"}),
                         "deferred")

    def test_a_day_off_is_not_a_meeting(self):
        """Building a packet for "Out" spends a model call to report that
        nothing is happening."""
        self.assertEqual(meeting.classify({"summary": "Out", "kind": "out"}), "none")
        self.assertEqual(meeting.classify({"summary": "RSVP due for the breakfast",
                                           "kind": "deadline"}), "none")
        self.assertEqual(meeting.classify({"summary": "OVERSIGHT: Committee on Parks",
                                           "kind": "hearing"}), "oversight")

    def test_the_subject_survives_the_scaffolding(self):
        """"Committee on -- :" must never end up in a question read aloud."""
        got = meeting.clean_topic(
            "HEARING: Committee on Public Safety — Oversight: NYPD Response Times")
        self.assertEqual(got, "NYPD Response Times")
        self.assertNotIn(":", got)

    def test_the_same_question_is_never_asked_twice(self):
        """Three copies of one question wastes her turn at the microphone."""
        evidence = {"committee": "Parks", "topic": "tree canopy",
                    "is_oversight": True, "district": {"fiscal_year": 2027},
                    "agenda": [], "member_on_agenda": []}
        asks = [q["ask"] for q in meeting.questions_for(evidence, count=6)]
        self.assertEqual(len(asks), len(set(asks)))

    def test_every_question_carries_its_follow_up(self):
        """A question the agency answers with "we're working on it" has cost
        her the turn."""
        evidence = {"committee": "Parks", "topic": "tree canopy",
                    "is_oversight": True, "district": {"fiscal_year": 2027},
                    "agenda": [], "member_on_agenda": []}
        for q in meeting.questions_for(evidence, count=6):
            self.assertTrue(q["follow_up"].strip())
            self.assertTrue(q["cite"])

    def test_untargeted_awards_are_labelled_as_context_not_evidence(self):
        """Citing a parks grant in a policing hearing is how a member gets
        corrected on the record."""
        with fresh() as store:
            store.upsert("funding", [
                {"line_id": "L1", "fy": 2027, "in_d49": 1, "org": "Acme Parks",
                 "agency": "DPR", "amount": 90000.0, "source_id": "SCHEDULE_C"}])
            stake = meeting.district_stake(store, "Public Safety", "response times")
            self.assertFalse(stake["awards_are_topical"])
            self.assertIn("context, not evidence", stake["awards_note"])

    def test_one_organisation_is_not_listed_as_several(self):
        """The books append the district to the recipient; listed raw, one
        provider looks like two and each figure is halved."""
        with fresh() as store:
            store.upsert("funding", [
                {"line_id": "L1", "fy": 2027, "in_d49": 1,
                 "org": "Acme, Inc. - Council District 49", "agency": "DYCD",
                 "amount": 40000.0, "source_id": "SCHEDULE_C"},
                {"line_id": "L2", "fy": 2027, "in_d49": 1, "org": "Acme, Inc.",
                 "agency": "DYCD", "amount": 40000.0, "source_id": "SCHEDULE_C"}])
            rows = meeting.district_stake(store, "Youth", "youth")["d49_largest_awards"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["amount"], 80000.0)

    def test_an_empty_agenda_says_why_rather_than_looking_empty(self):
        with fresh() as store:
            self.assertIn("confirm the agenda",
                          meeting._agenda_confidence([]))

    def test_an_inferred_agenda_never_claims_to_be_the_published_one(self):
        note = meeting._agenda_confidence([{"on_topic": True}, {"on_topic": False}])
        self.assertIn("Legistar", note)
        self.assertIn("Not the", note)


# ----------------------------------------------------------- the directory ----
class DirectoryTests(unittest.TestCase):
    """
    The contact directory's only real invariant: nothing in it is guessed.

    A wrong phone number is worse than a blank one. A blank prompts a lookup;
    a wrong number sends a constituent to the wrong agency and the office
    never finds out it happened.
    """

    def test_every_record_says_where_it_came_from(self):
        for row in si_directory.normalize(si_directory.ALL):
            self.assertTrue(row["verified_at"], f"{row['office']} has no source")
            self.assertIn(row["confidence"], ("published", "listed", "unverified"))

    def test_a_named_person_with_a_phone_is_never_unverified(self):
        """If we print a name next to a number, we stand behind both."""
        for row in si_directory.normalize(si_directory.ALL):
            if row["person"] and row["phone"]:
                self.assertNotEqual(row["confidence"], "unverified",
                                    f"{row['person']} has a phone but is unverified")

    def test_the_most_called_desks_are_all_routable(self):
        """A routing table missing HPD is not a routing table."""
        with fresh() as store:
            si_directory.load(store)
            for problem in ("no heat and hot water", "older adult meals",
                            "illegal conversion permits", "SNAP benefits",
                            "pothole resurfacing", "illegal dumping"):
                got = si_directory.route(store, problem)
                self.assertTrue(got["matches"], f"nothing routes {problem!r}")

    def test_routing_shows_what_it_matched_on(self):
        """A ranked list a staffer cannot interrogate is one they stop trusting."""
        with fresh() as store:
            si_directory.load(store)
            top = si_directory.route(store, "heat and hot water")["matches"][0]
            self.assertTrue(top["matched_on"])

    def test_an_unconfirmed_contact_carries_a_warning(self):
        with fresh() as store:
            si_directory.load(store)
            got = si_directory.route(store, "sewer capacity stormwater")
            if any(m["confidence"] != "published" for m in got["matches"]):
                self.assertIn("confirmed", got["note"])

    def test_gaps_lists_what_still_needs_looking_up(self):
        with fresh() as store:
            si_directory.load(store)
            got = si_directory.gaps(store)
            self.assertEqual(got["total"], len(si_directory.ALL))
            self.assertIn("Green Book", got["how_to_close"])

    def test_loading_twice_does_not_duplicate(self):
        with fresh() as store:
            si_directory.load(store)
            first = store.scalar("SELECT COUNT(*) FROM contacts")
            si_directory.load(store)
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM contacts"), first)

    def test_loading_twice_does_not_duplicate_the_search_index(self):
        """
        index_many used to insert without clearing, so a second ingest of any
        source doubled its search rows and every match came back twice. The
        live calendar was carrying 719 rows for 345 events before this.
        """
        with fresh() as store:
            si_directory.load(store)
            si_directory.load(store)
            rows = store.scalar(
                "SELECT COUNT(*) FROM search WHERE entity_type='contact'")
            self.assertEqual(rows, store.scalar("SELECT COUNT(*) FROM contacts"))


# ------------------------------------------------------------- the session ----
class SessionTests(unittest.TestCase):
    def test_the_current_session_is_compared_as_a_number(self):
        """`session` is text, so MAX() answers '9' for a corpus running through
        session 10 -- and every "what is live now" query reads the wrong term."""
        from ..intel.legislation import current_session
        with fresh() as store:
            store.upsert("matters", [{"matter_id": 1, "session": "9"},
                                     {"matter_id": 2, "session": "10"}])
            self.assertEqual(current_session(store), "10")


if __name__ == "__main__":
    unittest.main(verbosity=2)
