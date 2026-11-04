"""Journal health gates evidence delivery without changing the source journal."""

import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from test_analysis_export import NOW, report

from wtf_adsb_engine import wire
from wtf_adsb_engine.analysis import app, rules
from wtf_adsb_engine.analysis.native import NativeCollector
from wtf_adsb_engine.builtins.io import JournalInput
from wtf_adsb_engine.plugin_api.v1 import Context
from wtf_adsb_engine.runtime import Runtime


class JournalInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "journal.sqlite3"
        collector = NativeCollector(self.path)
        for index in range(8):
            snapshot = report(index)
            collector.process(snapshot, wall_now=snapshot["now"])
        self.stamp = NOW + 70
        self.context = Context("journal", self.root, "synthetic", self.stamp)
        self.config = self.root / "config.toml"
        self.config.write_text(
            'version=1\nstate_directory="state"\nsource_instance="test"\nsynthetic=true\n'
            '[[pipelines]]\nname="journal"\n'
            'input={plugin="wtf_adsb.journal", options={path="journal.sqlite3", mode="replay"}}\n'
            'detectors=[{plugin="wtf_adsb.collection"}]\n'
            '[[outputs]]\nname="files"\nplugin="wtf_adsb.files"\noptions={directory="export"}\n'
        )

    def health(self, **values):
        with app.connect(self.path) as db:
            for key, value in values.items():
                app.put_meta(db, key, value)

    def source(self, mode="live"):
        return JournalInput({"path": str(self.path), "mode": mode})

    def test_healthy_journal_uses_recorded_time_in_both_modes(self):
        for mode in ("live", "replay"):
            with self.subTest(mode=mode):
                batch = self.source(mode).poll(None, self.context)
                self.assertTrue(batch.complete)
                self.assertEqual(batch.watermark, self.stamp)
                self.assertEqual(batch.checkpoint, self.stamp)

    def test_detector_errors_or_missing_health_hold_both_modes(self):
        for errors in ({"patterns": "ValueError"}, [], "failed", None, False):
            self.health(detector_errors=errors)
            for mode in ("live", "replay"):
                with self.subTest(errors=errors, mode=mode), self.assertRaises(ValueError):
                    self.source(mode).poll(None, self.context)

    def test_receiver_error_holds_live_but_not_saved_replay(self):
        self.health(last_error={"type": "OSError"})
        with self.assertRaises(ValueError):
            self.source().poll(None, self.context)
        self.assertTrue(self.source("replay").poll(None, self.context).complete)

    def test_archive_completion_must_be_explicit_before_replay(self):
        for scan in ({"complete": False}, {"complete": 1}, {}, None):
            self.health(archive_scan=scan)
            with self.subTest(scan=scan), self.assertRaises(ValueError):
                self.source("replay").poll(None, self.context)
        self.health(archive_scan={"complete": True})
        self.assertTrue(self.source("replay").poll(None, self.context).complete)

    def test_complete_archive_uses_latest_observation_without_live_metadata(self):
        with app.connect(self.path) as db:
            db.execute("DELETE FROM meta WHERE key IN ('last_receiver_timestamp','detector_errors')")
            app.put_meta(db, "archive_scan", {"complete": True})
        self.assertEqual(self.source("replay").poll(None, self.context).watermark, self.stamp)
        self.health(archive_scan={"complete": False})
        with self.assertRaises(ValueError):
            self.source("replay").poll(None, self.context)

    def test_no_timestamp_is_not_replaced_with_wall_clock(self):
        with app.connect(self.path) as db:
            db.execute("DELETE FROM meta WHERE key='last_receiver_timestamp'")
            db.execute("DELETE FROM aircraft")
            app.put_meta(db, "archive_scan", {"complete": True})
        with self.assertRaisesRegex(ValueError, "timestamp"):
            self.source("replay").poll(None, self.context)

    def test_invalid_timestamp_holds_input(self):
        for stamp in (None, -1, True, "recent", float("inf")):
            self.health(last_receiver_timestamp=stamp)
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                self.source().poll(None, self.context)

    def test_failure_retains_committed_evidence_and_recovers_without_churn(self):
        runtime = Runtime(self.config)
        with runtime.locked():
            self.assertFalse(runtime.once()["errors"])
            head = runtime.store.state()["head_sequence"]
            self.assertGreater(head, 0)
            self.health(detector_errors={"patterns": "ValueError"})
            self.assertEqual(runtime.once()["errors"], {"journal": "ValueError"})
            self.assertEqual(runtime.store.checkpoint("journal"), self.stamp)
            self.assertEqual(runtime.store.state()["head_sequence"], head)
            manifest = wire.loads((self.root / "export/manifest.json").read_bytes())
            self.assertFalse(manifest["health"]["ready"])
            self.assertFalse(manifest["health"]["complete"])
            self.health(detector_errors={})
            self.assertFalse(runtime.once()["errors"])
            self.assertTrue(runtime.store.state()["health"]["ready"])
            self.assertEqual(runtime.store.state()["head_sequence"], head)

    def test_projection_rechecks_health_in_its_own_read_transaction(self):
        runtime = Runtime(self.config)
        source = runtime.pipelines[0][1][1]
        poll = source.poll

        def fail_between_reads(checkpoint, context):
            batch = poll(checkpoint, context)
            self.health(detector_errors={"patterns": "ValueError"})
            return batch

        with runtime.locked():
            with patch.object(source, "poll", side_effect=fail_between_reads):
                self.assertEqual(runtime.once()["errors"], {"journal": "ValueError"})
            self.assertIsNone(runtime.store.checkpoint("journal"))
            self.assertEqual(runtime.store.state()["head_sequence"], 0)
            self.assertFalse(runtime.store.state()["health"]["ready"])


class DailyDesignator(unittest.TestCase):
    def test_column_added_to_an_old_journal_and_written_by_remember(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "journal.sqlite3"
            old = sqlite3.connect(path)
            old.execute("CREATE TABLE daily (day TEXT, hex TEXT, typecode TEXT, PRIMARY KEY(day,hex))")
            old.commit()
            old.close()
            app.initialize(path)
            db = app.connect(path)
            self.addCleanup(db.close)
            columns = [row[1] for row in db.execute("PRAGMA table_info(daily)")]
            self.assertIn("designator", columns)
            report = {
                "hex": "a9067b",
                "type": "adsb_icao",
                "r": "N123DL",
                "t": "A321",
                "flight": "DAL1234 ",
                "lat": 42.5,
                "lon": -83.4,
                "alt_baro": 35000,
                "gs": 440,
            }
            app.remember(db, report, 1_700_000_000.0, 1_700_000_010.0)
            self.assertEqual(
                db.execute("SELECT designator FROM daily WHERE hex='a9067b'").fetchone()[0], "DAL"
            )
            report["flight"] = "N123DL"
            app.remember(db, {**report, "hex": "a9067c"}, 1_700_000_000.0, 1_700_000_010.0)
            self.assertEqual(db.execute("SELECT designator FROM daily WHERE hex='a9067c'").fetchone()[0], "")


class FirstOperatorBaseline(unittest.TestCase):
    TODAY = "2026-10-02"
    # 2026-10-02 00:30 UTC.
    STAMP = 1_790_901_000.0
    AIRCRAFT = {
        "hex": "a05f2d",
        "type": "adsb_icao",
        "r": "N123DL",
        "t": "A321",
        "flight": "DAL1234",
        "lat": 42.5,
        "lon": -83.4,
    }

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "journal.sqlite3"
        app.initialize(path)
        self.path = path
        self.db = app.connect(path)
        self.addCleanup(self.db.close)
        # 31 covered days, 2026-09-01 through 2026-10-01.
        for offset in range(31):
            day = (date(2026, 9, 1) + timedelta(days=offset)).isoformat()
            self.db.execute("INSERT INTO coverage VALUES (?,3600,1)", (day,))
        self.daily("2026-09-10", "a1b2c3", "A321", "DAL")
        # Neither a ~ track identifier nor a row without a designator counts.
        self.daily("2026-09-11", "~a00001", "A320", "UAL")
        self.daily("2026-09-12", "a1b2c4", "C172", "")

    def daily(self, day, address, typecode, designator):
        self.db.execute("INSERT INTO daily VALUES (?,?,?,?)", (day, address, typecode, designator))

    def test_full_and_incremental_reads_count_designator_days(self):
        full = app.Detector._first_history(self.db, self.TODAY)
        self.assertEqual(full["coverage_days"], 31)
        self.assertEqual(full["operators"], {"DAL": 1})
        yesterday = app.Detector._first_history(self.db, "2026-10-01")
        self.assertEqual(yesterday["operators"], {"DAL": 1})
        self.daily("2026-10-01", "a1b2c5", "B738", "DAL")
        self.daily("2026-10-01", "a1b2c6", "B737", "SWA")
        self.daily("2026-10-01", "a1b2c7", "B738", "SWA")
        incremental = app.Detector._first_history(self.db, self.TODAY, yesterday)
        self.assertEqual(incremental["operators"], {"DAL": 2, "SWA": 1})
        self.assertEqual(
            incremental["operators"], app.Detector._first_history(self.db, self.TODAY)["operators"]
        )

    def test_operators_wait_for_coverage_and_a_baseline_without_them_is_read_again(self):
        self.db.execute("DELETE FROM coverage WHERE day>='2026-09-30'")
        self.assertIsNone(app.Detector._first_history(self.db, self.TODAY)["operators"])
        self.db.execute("INSERT INTO coverage VALUES ('2026-09-30',3600,1),('2026-10-01',3600,1)")
        saved = app.Detector._first_history(self.db, "2026-10-01")
        del saved["operators"]
        self.assertEqual(app.Detector._first_history(self.db, self.TODAY, saved)["operators"], {"DAL": 1})

    def test_expiring_days_leave_the_operator_counts(self):
        baseline = app.Detector._first_history(self.db, self.TODAY)
        expiring = app.Detector._first_expiring(self.db, "2026-09-02")
        self.assertEqual(expiring["operators"], {})
        self.db.execute("DELETE FROM coverage WHERE day<'2026-09-02'")
        kept = {**baseline, **app.Detector._without_days(self.db, baseline, expiring)}
        self.assertEqual(kept["operators"], {"DAL": 1})
        expiring = app.Detector._first_expiring(self.db, "2026-09-11")
        self.assertEqual(expiring["operators"], {"DAL": 1})
        self.db.execute("DELETE FROM coverage WHERE day<'2026-09-11'")
        # Fewer than 30 covered days remain, so a full read would not count operators either.
        self.assertIsNone(app.Detector._without_days(self.db, kept, expiring)["operators"])
        self.assertIsNone(
            app.Detector._without_days(
                self.db, {**baseline, "operators": {"DAL": 0}}, {**expiring, "coverage_days": 0}
            )
        )

    def test_first_sightings_pass_operator_days_and_count_earlier_today(self):
        detector = app.Detector(self.path)
        checks = rules.identity_checks(self.AIRCRAFT)
        kinds = [f.kind for f in detector._first_sightings(self.db, self.AIRCRAFT, self.STAMP, checks)]
        self.assertNotIn("first_operator_here", kinds)
        first = {**self.AIRCRAFT, "flight": "SWA22"}
        kinds = [f.kind for f in detector._first_sightings(self.db, first, self.STAMP, checks)]
        self.assertIn("first_operator_here", kinds)
        # Another address already flew the designator today, before this baseline was built.
        self.daily(self.TODAY, "a1b2c8", "B38M", "JBU")
        later = app.Detector(self.path)
        jetblue = {**self.AIRCRAFT, "flight": "JBU100"}
        kinds = [f.kind for f in later._first_sightings(self.db, jetblue, self.STAMP, checks)]
        self.assertNotIn("first_operator_here", kinds)


if __name__ == "__main__":
    unittest.main()
