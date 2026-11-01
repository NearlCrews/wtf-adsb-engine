"""A native journal commit cannot strand the core's earlier input checkpoint."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_analysis_export import report

from wtf_adsb_engine.analysis import app
from wtf_adsb_engine.runtime import Runtime


class NativeOutboxRecoveryTests(unittest.TestCase):
    def test_failed_journal_setup_closes_its_connection(self):
        database = sqlite3.connect(":memory:", factory=app.JournalConnection)
        self.addCleanup(database.close)
        database.set_authorizer(
            lambda action, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_PRAGMA else sqlite3.SQLITE_OK
        )
        with (
            patch.object(app.sqlite3, "connect", return_value=database),
            self.assertRaises(sqlite3.DatabaseError),
        ):
            app.connect("unused.sqlite3")
        with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
            database.execute("SELECT 1")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "config.toml"
        self.config.write_text(
            'version=1\nstate_directory="state"\nsource_instance="recovery-fixture"\n'
            "synthetic=true\npoll_interval_seconds=1\n"
            '[[pipelines]]\nname="replay"\n'
            'input={plugin="wtf_adsb.json_replay",options={path="receiver.ndjson"}}\n'
            'detectors=[{plugin="wtf_adsb.collection"}]\n'
            '[[outputs]]\nname="files"\nplugin="wtf_adsb.files"\n'
            'options={directory="export"}\n'
        )
        (self.root / "receiver.ndjson").write_text(
            "\n".join(json.dumps(report(index)) for index in range(8)) + "\n"
        )

    def coverage(self):
        journals = list((self.root / "state").rglob("journal.sqlite3"))
        self.assertEqual(len(journals), 1)
        with app.connect(journals[0]) as database:
            return tuple(database.execute("SELECT SUM(seconds),SUM(polls) FROM coverage").fetchone())

    def fail_after_native_commit(self, runtime):
        with patch.object(runtime.store, "commit", side_effect=OSError("simulated core commit failure")):
            result = runtime.poll()
        self.assertEqual(result["errors"], {"replay": "OSError"})
        self.assertIsNone(runtime.store.checkpoint("replay"))
        self.assertEqual(runtime.store.state()["head_sequence"], 0)
        self.assertEqual(self.coverage()[1], 8)

    def recover(self, runtime, expected_coverage):
        result = runtime.once()
        self.assertEqual(result["errors"], {})
        self.assertIsNotNone(runtime.store.checkpoint("replay"))
        self.assertGreater(runtime.store.state()["head_sequence"], 0)
        self.assertEqual(self.coverage(), expected_coverage)
        head = runtime.store.state()["head_sequence"]
        self.assertEqual(runtime.once()["errors"], {})
        self.assertEqual(runtime.store.state()["head_sequence"], head)

    def test_same_process_replays_precommitted_native_batch_after_core_failure(self):
        runtime = Runtime(self.config)
        with runtime.locked():
            self.fail_after_native_commit(runtime)
            self.recover(runtime, self.coverage())

    def test_restart_replays_precommitted_native_batch_after_core_failure(self):
        runtime = Runtime(self.config)
        with runtime.locked():
            self.fail_after_native_commit(runtime)
            before = self.coverage()
        restarted = Runtime(self.config)
        with restarted.locked():
            self.recover(restarted, before)


if __name__ == "__main__":
    unittest.main()
