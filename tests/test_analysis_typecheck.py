"""A disagreeing registry blocks a type story but lets a mismatch story name the registry's type.

Ported from the changed tests in the live project's tests/test_typecheck.py, with a mocked
registry: no request leaves the process.
"""

import io
import json
import tempfile
import unittest
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()


class UnregisteredRecheck(unittest.TestCase):
    """An open unregistered_address finding is queued again once its saved check is a week old."""

    DAY = 86400

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "journal.sqlite3"
        app.initialize(self.path)

    def add(self, fetched_at):
        """Record a finding first seen ten days ago, with a settled check fetched at `fetched_at`."""
        finding = Finding(
            "unregistered_address", "oddities", "Test", None, "", "why", "explanation", 70, "Test", {}
        )
        aircraft = {"hex": "a1b2c3", "r": "", "t": "", "type": "adsb_icao", "alt_baro": 9000}
        with app.connect(self.path) as db:
            app.record(db, aircraft, finding, NOW - 10 * self.DAY, NOW - 3600, hits=6)
            typecheck.remember(db, "a1b2c3", None, fetched_at)
            # The first check settled the event, so only the recheck rule can queue it again.
            for event in typecheck.pending(db, 0, now=fetched_at):
                typecheck.attach(db, event, fetched_at)

    def addresses(self):
        with app.connect(self.path) as db:
            return [event["hex"] for event in typecheck.pending(db, since=0, now=NOW)]

    def test_a_check_older_than_a_week_is_queued_again(self):
        self.add(NOW - 8 * self.DAY)
        self.assertEqual(self.addresses(), ["a1b2c3"])

    def test_a_recent_check_is_not_queued(self):
        self.add(NOW - 1 * self.DAY)
        self.assertEqual(self.addresses(), [])

    def test_the_worker_looks_the_address_up_again_and_replaces_the_saved_check(self):
        self.add(NOW - 8 * self.DAY)
        result = typecheck.run_once(
            self.path, now=NOW, opener=responses(not_found(), not_found()), enabled_override=True
        )
        self.assertEqual(result["state"], "checked")
        self.assertEqual(result["hex"], "a1b2c3")
        with app.connect(self.path) as db:
            self.assertEqual(typecheck.saved(db, "a1b2c3", NOW)["fetched_at"], NOW)
            self.assertEqual(typecheck.pending(db, since=0, now=NOW), [])
