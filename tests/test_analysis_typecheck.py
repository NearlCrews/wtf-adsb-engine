"""A disagreeing registry blocks a type story but lets a mismatch story name the registry's type.

Ported from the changed tests in the live project's tests/test_typecheck.py, with a mocked
registry: no request leaves the process.
"""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from wtf_adsb_engine.analysis import app, curation, enrichment_rules, export, selection, typecheck
from wtf_adsb_engine.analysis.rules import Finding

NOW = 1_791_080_000
HEXDB = {
    "ICAOTypeCode": "E295",
    "Manufacturer": "Embraer",
    "Type": "E195-E2",
    "Registration": "C-GKXM",
    "RegisteredOwners": "Porter Airlines",
}


def not_found(url="https://example.invalid/"):
    return HTTPError(url, 404, "Not Found", {}, io.BytesIO(b'{"response":"unknown aircraft"}'))


def responses(*answers):
    """An opener answering successive requests with JSON bodies or exceptions."""
    queue = list(answers)

    def opener(request, timeout=None):
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(json.dumps(answer).encode())

    return opener


class MismatchStories(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "journal.sqlite3"
        app.initialize(self.path)

    def add(self, kind="type_speed_mismatch"):
        finding = Finding(kind, "oddities", "Test", 500, "kn", "why", "explanation", 74, "Test", {})
        aircraft = {
            "hex": "c06173",
            "r": "C-GKXM",
            "t": "DH8D",
            "type": "adsb_icao",
            "gs": 500,
            "alt_baro": 9000,
        }
        with app.connect(self.path) as db:
            return app.record(db, aircraft, finding, NOW, NOW + 70, hits=6)

    def test_the_mismatch_kinds_are_type_dependent_and_re_exported_by_the_worker(self):
        self.assertTrue(enrichment_rules.REGISTRY_TYPE_KINDS)
        self.assertLessEqual(enrichment_rules.REGISTRY_TYPE_KINDS, enrichment_rules.TYPE_DEPENDENT_KINDS)
        self.assertIs(typecheck.REGISTRY_TYPE_KINDS, enrichment_rules.REGISTRY_TYPE_KINDS)
        self.assertIs(typecheck.type_check_state, enrichment_rules.type_check_state)

    def test_the_selection_projection_carries_the_check_into_encounters(self):
        mismatch_id = self.add()
        event_id = self.add(kind="first_type_here")
        typecheck.run_once(
            self.path, now=NOW + 1000, opener=responses(not_found(), HEXDB), enabled_override=True
        )
        with app.connect(self.path) as db:
            groups = selection.encounters(db, NOW + 4000)
        events = {e["id"]: e for group in groups for e in group["events"]}
        self.assertEqual(events[event_id]["evidence"]["type_check"]["state"], "disagrees")
        self.assertIn("type_disagrees", events[event_id]["selection"]["blocking_rules"])
        # A mismatch story names the registry's type instead.
        self.assertEqual(events[mismatch_id]["evidence"]["type_check"]["state"], "disagrees")
        self.assertNotIn("type_disagrees", events[mismatch_id]["selection"]["blocking_rules"])
        self.assertEqual(curation.VERSION, 11)

    def test_the_export_carries_the_registry_type_for_the_publishers_mismatch_story(self):
        self.add()
        typecheck.run_once(
            self.path, now=NOW + 1000, opener=responses(not_found(), HEXDB), enabled_override=True
        )
        (row,) = export.snapshot(self.path, NOW + 4000)
        (event,) = row["observation"]["group"]["events"]
        check = event["evidence"]["type_check"]
        self.assertEqual(
            (check["state"], check["type_code"], check["source"]), ("disagrees", "E295", "hexdb")
        )
        self.assertNotIn("type_disagrees", event["selection"]["blocking_rules"])


class UnregisteredRecheck(unittest.TestCase):
    """An unregistered_address finding is queued again once its saved check is a week old."""

    DAY = 86400

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "journal.sqlite3"
        app.initialize(self.path)

    def add(self, address, fetched_at, *, first_seen=NOW - 10 * DAY, last_seen=NOW - 3600):
        """Record a finding with a settled check fetched at `fetched_at`."""
        finding = Finding(
            "unregistered_address", "oddities", "Test", None, "", "why", "explanation", 70, "Test", {}
        )
        aircraft = {"hex": address, "r": "", "t": "", "type": "adsb_icao", "alt_baro": 9000}
        with app.connect(self.path) as db:
            app.record(db, aircraft, finding, first_seen, last_seen, hits=6)
            typecheck.remember(db, address, None, fetched_at)
            # The first check settled the event, so only the recheck rule can queue it again.
            for event in typecheck.pending(db, 0, address, now=fetched_at):
                typecheck.attach(db, event, fetched_at)

    def addresses(self):
        with app.connect(self.path) as db:
            return [event["hex"] for event in typecheck.pending(db, since=0, now=NOW)]

    def blocks(self):
        with app.connect(self.path) as db:
            rows = db.execute("SELECT hex, evidence FROM events ORDER BY hex").fetchall()
        return {row["hex"]: json.loads(row["evidence"]).get("type_check") for row in rows}

    def test_a_check_older_than_a_week_is_queued_again(self):
        self.add("a1b2c3", NOW - 8 * self.DAY)
        self.assertEqual(self.addresses(), ["a1b2c3"])

    def test_a_recent_check_is_not_queued(self):
        self.add("a1b2c3", NOW - 1 * self.DAY)
        self.assertEqual(self.addresses(), [])

    def test_the_window_follows_the_first_sighting_not_the_last(self):
        # A one-off sighting checked at first sight is rechecked after it leaves view.
        self.add("a1b2c3", NOW - 9 * self.DAY, first_seen=NOW - 9 * self.DAY, last_seen=NOW - 9 * self.DAY)
        self.add("b2c3d4", NOW - 8 * self.DAY, first_seen=NOW - 31 * self.DAY, last_seen=NOW - 30 * self.DAY)
        self.assertEqual(self.addresses(), ["a1b2c3"])

    def test_new_checks_keep_priority_within_the_pending_limit(self):
        self.add("a1b2c3", NOW - 8 * self.DAY)
        finding = Finding(
            "type_speed_mismatch", "oddities", "Test", 500, "kn", "why", "explanation", 74, "Test", {}
        )
        aircraft = {"hex": "c06173", "r": "C-GKXM", "t": "DH8D", "type": "adsb_icao", "gs": 500}
        with app.connect(self.path) as db:
            app.record(db, aircraft, finding, NOW - 600, NOW - 60, hits=6)
        self.assertEqual(self.addresses(), ["c06173", "a1b2c3"])
        with patch.object(typecheck, "PENDING_LIMIT", 1):
            self.assertEqual(self.addresses(), ["c06173"])

    def test_one_pass_rechecks_one_address_and_leaves_the_other_blocks_alone(self):
        self.add("a1b2c3", NOW - 8 * self.DAY)
        self.add("b2c3d4", NOW - 9 * self.DAY)
        before = self.blocks()
        result = typecheck.run_once(
            self.path, now=NOW, opener=responses(not_found(), HEXDB), enabled_override=True
        )
        self.assertEqual((result["state"], result["hex"], result["attached"]), ("checked", "a1b2c3", 1))
        after = self.blocks()
        # The rechecked address carries the fresh answer; the other keeps its week-old block.
        self.assertEqual((after["a1b2c3"]["source"], after["a1b2c3"]["type_code"]), ("hexdb", "E295"))
        self.assertEqual(after["a1b2c3"]["checked_at"], NOW)
        self.assertEqual(after["b2c3d4"], before["b2c3d4"])
        with app.connect(self.path) as db:
            self.assertEqual(typecheck.saved(db, "a1b2c3", NOW)["fetched_at"], NOW)
        self.assertEqual(self.addresses(), ["b2c3d4"])

    def test_a_failed_recheck_keeps_the_settled_block(self):
        self.add("a1b2c3", NOW - 8 * self.DAY)
        before = self.blocks()
        unavailable = HTTPError("https://example.invalid/", 503, "Unavailable", {}, io.BytesIO(b""))
        result = typecheck.run_once(self.path, now=NOW, opener=responses(unavailable), enabled_override=True)
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(self.blocks(), before)
        self.assertEqual(self.addresses(), ["a1b2c3"])


class CheckedAt(unittest.TestCase):
    def test_a_retained_check_is_dated_when_the_registry_answered(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            app.initialize(path)
            finding = Finding(
                "type_speed_mismatch", "oddities", "Test", 500, "kn", "why", "explanation", 74, "Test", {}
            )
            aircraft = {"hex": "c06173", "r": "C-GKXM", "t": "DH8D", "type": "adsb_icao", "gs": 500}
            with app.connect(path) as db:
                app.record(db, aircraft, finding, NOW - 600, NOW - 60, hits=6)
                typecheck.remember(db, "c06173", {"source": "hexdb", "type_code": "DH8D"}, NOW - 86400)
            result = typecheck.run_once(path, now=NOW, opener=responses(), enabled_override=True)
            self.assertEqual((result["state"], result["attached"]), ("cached", 1))
            with app.connect(path) as db:
                evidence = json.loads(db.execute("SELECT evidence FROM events").fetchone()[0])
        self.assertEqual(evidence["type_check"]["checked_at"], NOW - 86400)
        self.assertEqual(evidence["type_check"]["state"], "agrees")


class PendingOrder(unittest.TestCase):
    """The worker asks about the strongest, newest candidates first."""

    def test_pending_orders_by_score_then_newest_first_sighting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            app.initialize(path)
            with app.connect(path) as db:
                # The latest sighting no longer decides the order.
                for address, score, first, last in (
                    ("a00001", 60, NOW - 600, NOW - 60),
                    ("a00002", 80, NOW - 7200, NOW - 120),
                    ("a00003", 80, NOW - 1200, NOW - 180),
                ):
                    finding = Finding(
                        "type_speed_mismatch", "oddities", "Test", 500, "kn", "w", "e", score, "Test", {}
                    )
                    aircraft = {"hex": address, "r": "N1", "t": "C172", "type": "adsb_icao", "gs": 500}
                    app.record(db, aircraft, finding, first, last, hits=6)
                found = [event["hex"] for event in typecheck.pending(db, since=0, now=NOW)]
        self.assertEqual(found, ["a00003", "a00002", "a00001"])


if __name__ == "__main__":
    unittest.main()
