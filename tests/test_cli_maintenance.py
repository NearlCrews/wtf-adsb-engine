"""Administrative outbox writes require the state lock and no plugin lifecycle."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wtf_adsb_engine.cli import main
from wtf_adsb_engine.plugin_api.v1 import Observation
from wtf_adsb_engine.runtime import state_lock
from wtf_adsb_engine.storage import Store

ROOT = Path(__file__).resolve().parents[1]
NOW = 1791150000
HEALTH = {
    "ready": True,
    "complete": True,
    "mode": "live",
    "last_receiver_poll": NOW,
    "analysis_watermark": NOW,
}


class RetractionCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "state"
        self.config = self.root / "engine.toml"
        # The original integration may have been uninstalled or become unavailable.
        self.config.write_text("""version = 1
state_directory = "state"
source_instance = "receiver"
synthetic = true
[[pipelines]]
name = "receiver"
input = { plugin = "org.example.unavailable_input" }
detectors = [{ plugin = "org.example.unavailable_detector" }]
[[outputs]]
name = "files"
plugin = "org.example.unavailable_output"
options = {}
""")
        self.store = Store(self.directory / "engine.sqlite3", synthetic=True)
        self.store.bind_source("receiver")
        observation = json.loads((ROOT / "examples/change-batch.example.json").read_text())["changes"][0][
            "observation"
        ]
        self.store.commit(
            "receiver", {"offset": 10}, [Observation("event", observation)], HEALTH, timestamp=NOW
        )
        self.event = self.store.next_batch(0)["changes"][0]["event_id"]

    def invoke(self):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(
                [
                    "retract",
                    "--config",
                    str(self.config),
                    "--event-id",
                    self.event,
                    "--reason",
                    "invalid_evidence",
                ]
            )
        return code, output.getvalue(), errors.getvalue()

    def test_retract_works_without_loading_plugins_or_touching_input_state(self):
        native = self.directory / "plugins" / "receiver" / "journal.sqlite3"
        native.parent.mkdir(parents=True)
        native.write_bytes(b"synthetic native journal sentinel")
        with self.store.connect() as db:
            inputs = [tuple(row) for row in db.execute("SELECT * FROM inputs")]
        with (
            patch("wtf_adsb_engine.cli.Runtime", side_effect=AssertionError("runtime constructed")),
            patch("wtf_adsb_engine.registry.load", side_effect=AssertionError("plugin loaded")),
            patch("socket.socket.connect", side_effect=AssertionError("network requested")),
        ):
            code, output, errors = self.invoke()
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output), {"retracted": self.event})
        with self.store.connect() as db:
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM inputs")], inputs)
        self.assertEqual(native.read_bytes(), b"synthetic native journal sentinel")
        change = self.store.next_batch(1)["changes"][0]
        self.assertEqual(
            (change["operation"], change["event_id"], change["revision"]), ("retract", self.event, 2)
        )

    def test_retract_refuses_a_running_owner_and_preserves_the_outbox(self):
        with state_lock(self.directory):
            code, _, errors = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(errors), {"error": "ValueError"})
        self.assertEqual(self.store.state()["head_sequence"], 1)

    def test_retract_does_not_create_missing_engine_state(self):
        self.config.write_text(
            self.config.read_text().replace('state_directory = "state"', 'state_directory = "missing"')
        )
        code, _, errors = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(errors), {"error": "ValueError"})
        self.assertFalse((self.root / "missing" / "engine.sqlite3").exists())
