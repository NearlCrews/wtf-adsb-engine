"""Journal health gates evidence delivery without changing the source journal."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_analysis_export import NOW, report

from wtf_adsb_engine import wire
from wtf_adsb_engine.analysis import app
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


if __name__ == "__main__":
    unittest.main()
