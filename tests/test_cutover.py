"""Transfers preserve observations/accounting; health never starts providers."""

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wtf_adsb_engine.analysis import app, flightaware
from wtf_adsb_engine.analysis.native import NativeCollector
from wtf_adsb_engine.maintenance import backup_journal, health, migrate_journal, native_journal, write_json
from wtf_adsb_engine.runtime import Runtime, configuration, state_lock


class CutoverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "old.sqlite3"
        app.initialize(self.source)
        with app.connect(self.source) as db:
            app.put_meta(db, "last_receiver_timestamp", 1791000000)
            db.execute(
                "INSERT INTO flightaware_requests(id,requested_at,cache_key,status,cost_microusd) VALUES (7,1791000000,'test','reserved',5000)"
            )
        self.config = self.root / "engine.toml"
        self.config.write_text("""version=1
state_directory="state"
source_instance="test"
[[pipelines]]
name="native"
input={plugin="wtf_adsb.receiver_service",name="receiver",options={url="http://localhost/aircraft.json"}}
detectors=[{plugin="wtf_adsb.collection"}]
[[outputs]]
name="files"
plugin="wtf_adsb.files"
options={directory="feed"}
""")

    def test_transfer_preserves_wal_and_reservations_without_starting_plugins(self):
        writer = sqlite3.connect(self.source)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE flightaware_requests SET status='uncertain' WHERE id=7")
        writer.commit()
        self.assertGreater(Path(str(self.source) + "-wal").stat().st_size, 0)
        with patch("wtf_adsb_engine.registry.load", side_effect=AssertionError("loaded plugin")):
            receipt = migrate_journal(self.source, self.config)
        destination = native_journal(configuration(self.config))
        self.assertEqual(receipt["last_receiver_timestamp"], 1791000000)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        with app.connect(destination) as db:
            self.assertEqual(
                tuple(db.execute("SELECT id,status,cost_microusd FROM flightaware_requests").fetchone()),
                (7, "uncertain", 5000),
            )
        with self.assertRaises(ValueError):
            migrate_journal(self.source, self.config)

    def test_transfer_refuses_busy_state_and_aliases(self):
        with state_lock(self.root / "state"), self.assertRaises(ValueError):
            migrate_journal(self.source, self.config)
        alias = self.root / "hardlink.sqlite3"
        os.link(self.source, alias)
        with self.assertRaises(FileExistsError):
            backup_journal(self.source, alias)
        alias.unlink()
        alias.symlink_to(self.source)
        with self.assertRaises(ValueError):
            backup_journal(self.source, alias)

    def test_partial_transfer_cannot_be_silently_reused(self):
        with patch("wtf_adsb_engine.maintenance.journal_info", side_effect=OSError("failure")):
            with self.assertRaises(OSError):
                migrate_journal(self.source, self.config)
        self.assertTrue(native_journal(configuration(self.config)).exists())
        with patch(
            "wtf_adsb_engine.analysis.native.NativeCollector", side_effect=AssertionError("collector started")
        ):
            with self.assertRaisesRegex(ValueError, "migration is incomplete"):
                Runtime(self.config).start()
        with self.assertRaises(ValueError):
            migrate_journal(self.source, self.config)

    def test_health_requires_live_lock_and_recent_progress(self):
        state = self.root / "state"
        with state_lock(state):
            write_json(state / "heartbeat.json", {"updated_at": 1000, "inputs_alive": True})
            self.assertTrue(health(self.config, now=1100)["alive"])
            self.assertFalse(health(self.config, now=1400)["alive"])
            write_json(state / "heartbeat.json", {"updated_at": 1000, "inputs_alive": False})
            self.assertFalse(health(self.config, now=1100)["alive"])
        self.assertFalse(health(self.config, now=1100)["alive"])

    def test_flightaware_passes_remain_300_seconds_apart(self):
        collector = NativeCollector(
            self.root / "native.sqlite3", flightaware_settings=flightaware.Settings(enabled=True)
        )
        with (
            patch("wtf_adsb_engine.analysis.typecheck.run_once"),
            patch("wtf_adsb_engine.analysis.wind.run_once"),
            patch("wtf_adsb_engine.analysis.flightaware.run_once") as request,
        ):
            for stamp in (1000, 1005, 1060, 1299, 1300, 1305):
                collector.enrich(stamp)
        self.assertEqual([call.kwargs["now"] for call in request.call_args_list], [1000, 1300])

    def test_flightaware_activation_hold_prevents_requests_and_reservations(self):
        hold = self.root / "flightaware.hold"
        hold.touch()
        settings = flightaware.Settings(enabled=True, pause_file=hold)
        with patch.object(flightaware, "Client", side_effect=AssertionError("credential accessed")):
            self.assertEqual(flightaware.run_once(self.source, settings, now=1791000000), {"state": "paused"})
            self.assertEqual(
                flightaware.enrich_owner(self.source, "N123EX", settings, now=1791000000), {"state": "paused"}
            )
        with app.connect(self.source) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM flightaware_requests").fetchone()[0], 1)
        hold.unlink()
        self.assertFalse(settings.paused)
        hold.symlink_to(self.root / "missing")
        self.assertTrue(settings.paused)
        with patch.object(Path, "lstat", side_effect=PermissionError):
            self.assertTrue(settings.paused)

    def test_readiness_checks_analysis_freshness_and_output_backlog(self):
        state = {
            "head_sequence": 3,
            "health": {
                "mode": "live",
                "ready": True,
                "complete": True,
                "last_receiver_poll": 1000,
                "analysis_watermark": 1000,
            },
            "outputs": [{"cursor": 3, "status": "ready"}],
        }
        with patch("wtf_adsb_engine.maintenance.Store.inspect", return_value=state):
            self.assertTrue(health(self.config, readiness=True, now=1100)["ready"])
            state["health"]["analysis_watermark"] = 700
            self.assertFalse(health(self.config, readiness=True, now=1100)["ready"])
            state["health"]["analysis_watermark"] = 1000
            state["outputs"][0]["cursor"] = 2
            self.assertFalse(health(self.config, readiness=True, now=1100)["ready"])
            state["outputs"][0].update(cursor=3, status="failed")
            self.assertFalse(health(self.config, readiness=True, now=1100)["ready"])
            state["outputs"][0]["status"] = "ready"
            state["health"]["mode"] = "replay"
            self.assertFalse(health(self.config, readiness=True, now=1100)["ready"])

    def test_heartbeat_continues_during_slow_projection(self):
        runtime = Runtime(self.config)
        runtime.pipelines = [(None, (None, SimpleNamespace(health=lambda: {"alive": True})), None, None)]
        runtime.outputs = []
        stop = threading.Event()
        beats = []
        second_beat = threading.Event()

        def heartbeat(path, value):
            beats.append(value)
            if len(beats) >= 2:
                second_beat.set()

        def slow_poll():
            second_beat.wait(8)
            stop.set()
            return {}

        with (
            patch.object(runtime, "start"),
            patch.object(runtime, "poll", side_effect=slow_poll),
            patch("wtf_adsb_engine.maintenance.write_json", side_effect=heartbeat),
        ):
            runtime.run(stop)
        self.assertGreaterEqual(len(beats), 2)
        self.assertTrue(all(beat["inputs_alive"] for beat in beats))

    def test_private_fleet_reference_does_not_guess_a_historical_operator(self):
        from wtf_adsb_engine.analysis.fleet import fleet_context

        fleet = self.root / "fleet.json"
        fleet.write_text(
            json.dumps(
                {
                    "checked_on": "2026-10-01",
                    "aircraft": {
                        "N123EX": {
                            "aircraft_type": "C172",
                            "organization": "Example club",
                            "association": "club",
                            "source_url": "https://example.com/fleet",
                        },
                    },
                }
            )
        )
        current = fleet_context({"r": "N123EX", "t": "C172"}, 1791000000, fleet)
        self.assertTrue(current["fresh"])
        self.assertIn("does not establish", current["caveat"])
        self.assertFalse(fleet_context({"r": "N123EX", "t": "C172"}, 1900000000, fleet)["fresh"])
        self.assertIsNone(fleet_context({"r": "N123EX", "t": "B738"}, 1791000000, fleet))
