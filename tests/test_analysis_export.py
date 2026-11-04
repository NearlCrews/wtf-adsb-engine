"""Synthetic end-to-end detector, privacy projection and archive packaging checks."""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from wtf_adsb_engine.analysis import app, export, history_backfill, rules, stories, typecheck, wind
from wtf_adsb_engine.analysis.identity import IdentityReconciliationRequired, IdentityStore
from wtf_adsb_engine.analysis.native import NativeCollector

NOW = 1791150000


def report(index):
    return {
        "now": NOW + index * 10,
        "aircraft": [
            {
                "hex": "a00001",
                "r": "N1",
                "t": "C172",
                "type": "adsb_icao",
                "squawk": "7700",
                "lat": 40 + index * 0.001,
                "lon": -100,
                "alt_baro": 5000,
                "gs": 80,
                "seen": 0,
                "seen_pos": 0,
                "messages": 100 + index,
            }
        ],
    }


def sample_source():
    return {
        "id": 983,
        "hex": "a00001",
        "kind": "rapid_descent",
        "category": "patterns",
        "value": -6400,
        "unit": "ft/min",
        "first_seen": NOW,
        "last_seen": NOW + 60,
        "hits": 7,
        "origin": "Live receiver",
        "title": "PRIVATE TITLE",
        "why": "PRIVATE JOURNAL",
        "explanation": "PRIVATE NOTE",
        "evidence": {
            "aircraft": {
                "hex": "a00001",
                "r": "N1",
                "t": "C172",
                "type": "adsb_icao",
                "baro_rate": -6400,
                "rssi": -13,
                "r_dst": 19,
            },
            "maneuver": {
                "kind": "rapid_descent",
                "started_at": NOW,
                "ended_at": NOW + 60,
                "duration_seconds": 60,
                "sample_count": 7,
                "confirmed": True,
                "quality_review_required": False,
                "quality_flags": [],
                "state": "ended",
                "end_reason": "segment_break",
                "private_notes": "SECRET",
            },
            "encounter_start": NOW,
            "archive_path": "/private/receiver/track.json",
            "receiver": {"lat": 40, "lon": -100},
            "track": [
                {
                    "ts": NOW + i * 10,
                    "lat": 40 + i * 0.001,
                    "lon": -100,
                    "source": "adsb_icao",
                    "archive_alt": 5000,
                    "private": "SECRET",
                }
                for i in range(7)
            ],
        },
    }


class ProjectionTests(unittest.TestCase):
    def test_projection_rebuilds_nested_objects_and_preserves_signed_measurements(self):
        source = sample_source()
        saved = copy.deepcopy(source)
        projected = export.project_source(source)
        serialized = json.dumps(projected)
        for private in ("PRIVATE", "SECRET", "rssi", "r_dst", "archive_path", '"receiver"', "983"):
            self.assertNotIn(private, serialized)
        self.assertEqual(projected["value"], -6400)
        self.assertEqual(projected["evidence"]["aircraft"]["baro_rate"], -6400)
        self.assertEqual(projected["evidence"]["maneuver"]["end_reason"], "segment_break")
        self.assertEqual(projected["hex"], "a00001")
        self.assertEqual(source, saved)
        UUID(projected["id"])

    def test_finding_identity_survives_row_id_reuse_and_evidence_changes(self):
        source = sample_source()
        before = export.project_source(source)["id"]
        source.update(id=14, value=-7200)
        self.assertEqual(before, export.project_source(source)["id"])
        self.assertNotEqual(before, export.project_source(source, source_instance="other")["id"])

    def test_distinct_detector_episode_starts_disambiguate_legacy_source_tuple(self):
        source = sample_source()
        source["evidence"]["maneuver"]["episode_start"] = NOW - 30
        earlier = export.project_source(source)["id"]
        source["evidence"]["maneuver"]["episode_start"] = NOW
        self.assertNotEqual(earlier, export.project_source(source)["id"])
        # SQLite returns REAL timestamps; integer fixture spelling is equivalent.
        current = export.project_source(source)["id"]
        source["first_seen"] = float(source["first_seen"])
        self.assertEqual(current, export.project_source(source)["id"])

    def test_unknown_quality_flags_cannot_disappear_or_carry_private_prose(self):
        source = sample_source()
        source["evidence"]["quality_flags"] = ["secret receiver hostname"]
        projected = export.project_source(source)
        self.assertEqual(projected["evidence"]["quality_flags"], ["unknown_quality_flag"])
        self.assertFalse(projected["selection"]["story_eligible"])

    def test_receiver_relative_finding_is_not_exportable(self):
        source = sample_source()
        source["kind"] = "distant"
        self.assertIsNone(export.project_source(source))

    def test_non_icao_tracking_address_is_not_misrepresented_as_icao_identity(self):
        source = sample_source()
        source["hex"] = "~a00001"
        source["evidence"]["aircraft"]["hex"] = "~a00001"
        projected = export.project_source(source)
        self.assertEqual(projected["hex"], "~a00001")
        packet = stories.evidence_packet(projected)
        self.assertIsNone(packet["aircraft"]["icao_address"])

    def test_operator_first_sighting_projects_unchanged_and_unknown_scopes_drop(self):
        block = {
            "scope": "operator",
            "designator": "DAL",
            "operator": "Delta Air Lines",
            "coverage_days": 45,
            "since": "2026-09-01",
        }
        self.assertEqual(export.EVIDENCE({"first_sighting": block})["first_sighting"], block)
        projected = export.EVIDENCE({"first_sighting": {**block, "scope": "fleet"}})["first_sighting"]
        self.assertNotIn("scope", projected)
        self.assertIsNone(stories.first_sighting({**block, "scope": "fleet"}))
        self.assertEqual(stories.first_sighting(block)["scope"], "operator")

    def test_engine_retains_a_kind_without_a_public_story_policy(self):
        source = sample_source()
        source["kind"] = "vfr_code_above_fl180"
        projected = export.project_source(source)
        self.assertEqual(projected["kind"], "vfr_code_above_fl180")
        self.assertFalse(projected["selection"]["story_eligible"])

    def test_packet_never_fills_a_missing_historical_watchlist(self):
        with patch.object(stories.watchlists, "lookup", side_effect=AssertionError("unexpected lookup")):
            packet = stories.evidence_packet(export.project_source(sample_source()))
        self.assertNotIn("watchlist", packet)

    def test_track_media_retains_altitude_reference_and_gaps(self):
        source = export.project_source(sample_source())
        source["evidence"]["track"][3]["source"] = "mlat"
        parts = export.media_parts(source)
        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[0][0]["altitude_reference"], "unspecified")
        self.assertEqual(sum(map(len, parts)), 7)


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "journal.sqlite3"
        self.collector = NativeCollector(self.path)

    def feed(self):
        for index in range(8):
            snapshot = report(index)
            self.collector.process(snapshot, wall_now=snapshot["now"])

    def test_native_collect_export_and_noop_export_are_deterministic(self):
        self.feed()
        before = self.collector.snapshot(NOW + 100)
        after = self.collector.snapshot(NOW + 120)
        self.assertEqual(before, after)
        self.assertEqual(len(before), 1)
        event = before[0]["observation"]["group"]["events"][0]
        self.assertEqual(event["kind"], "special_squawk_7700")
        self.assertTrue(event["evidence"]["maneuver"]["confirmed"])
        self.assertTrue(before[0]["observation"]["media"]["by_finding"][event["id"]]["parts"])

    def test_restart_does_not_count_a_committed_snapshot_twice(self):
        self.feed()
        with app.connect(self.path) as database:
            before = tuple(database.execute("SELECT seconds,polls FROM coverage").fetchone())
        NativeCollector(self.path).process(report(7), NOW + 70)
        with app.connect(self.path) as database:
            after = tuple(database.execute("SELECT seconds,polls FROM coverage").fetchone())
        self.assertEqual(before, after)

    def test_failed_native_phase_rolls_back_and_same_snapshot_retries_after_restart(self):
        with (
            patch.object(self.collector.detector, "_correlations", side_effect=RuntimeError("phase failed")),
            patch.object(app.LOG, "warning"),
            self.assertRaises(app.CollectorFault),
        ):
            self.collector.process(report(0), NOW)
        self.assertIsNone(self.collector.health()["last_receiver_timestamp"])
        with app.connect(self.path) as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM aircraft").fetchone()[0], 0)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM coverage").fetchone()[0], 0)
        restarted = NativeCollector(self.path)
        restarted.process(report(0), NOW)
        self.assertEqual(restarted.health()["last_receiver_timestamp"], NOW)
        with app.connect(self.path) as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM aircraft").fetchone()[0], 1)
            self.assertEqual(database.execute("SELECT SUM(polls) FROM coverage").fetchone()[0], 1)

    def test_corrected_interval_preserves_export_identity_after_restart(self):
        self.feed()
        before = self.collector.snapshot(NOW + 100)[0]
        with app.connect(self.path) as database:
            database.execute(
                "UPDATE events SET first_seen=first_seen-10, "
                "evidence=json_set(evidence,'$.encounter_start',?,'$.maneuver.episode_start',?)",
                (NOW - 10, NOW - 10),
            )
        after = NativeCollector(self.path).snapshot(NOW + 100)[0]
        self.assertNotEqual(before["observation"], after["observation"])
        self.assertEqual(before["key"], after["key"])
        self.assertEqual(before["observation"]["group"]["id"], after["observation"]["group"]["id"])
        self.assertEqual(
            before["observation"]["group"]["events"][0]["id"],
            after["observation"]["group"]["events"][0]["id"],
        )
        self.assertEqual(after, NativeCollector(self.path).snapshot(NOW + 100)[0])

    def test_readonly_export_keeps_identity_in_a_separate_private_database(self):
        self.feed()
        self.path.chmod(0o400)
        before = self.path.read_bytes()
        sidecar = Path(self.directory.name) / "export-identities.sqlite3"
        first = export.snapshot(self.path, NOW + 100, identity_path=sidecar)
        second = export.snapshot(self.path, NOW + 100, identity_path=sidecar)
        self.assertEqual(first, second)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)

    def test_split_merge_and_retention_preserve_explicit_replacement_evidence(self):
        source = sample_source()
        with app.connect(self.path) as database:
            for kind in ("rapid_descent", "rapid_climb"):
                finding = rules.Finding(
                    kind,
                    "patterns",
                    "",
                    -6400 if kind == "rapid_descent" else 6400,
                    "ft/min",
                    "",
                    "",
                    80,
                    evidence=source["evidence"],
                )
                app.record(database, source["evidence"]["aircraft"], finding, NOW, NOW + 60)
        original = self.collector.snapshot_state(NOW + 100)
        self.assertEqual(len(original["observations"]), 1)
        self.assertEqual(original["retractions"], [])
        parent = original["observations"][0]["key"]
        with app.connect(self.path) as database:
            database.execute(
                "UPDATE events SET evidence=json_set(evidence,'$.encounter_start',?) WHERE kind='rapid_climb'",
                (NOW + 10,),
            )
        split = NativeCollector(self.path).snapshot_state(NOW + 100)
        children = {row["key"] for row in split["observations"]}
        self.assertEqual(len(children), 2)
        self.assertNotIn(parent, children)
        self.assertEqual(
            split["retractions"],
            [{"key": parent, "reason_code": "superseded", "replacement_keys": sorted(children)}],
        )
        self.assertEqual(split, NativeCollector(self.path).snapshot_state(NOW + 100))
        with app.connect(self.path) as database:
            database.execute("UPDATE events SET evidence=json_set(evidence,'$.encounter_start',?)", (NOW,))
        merged = NativeCollector(self.path).snapshot_state(NOW + 100)
        self.assertEqual(len(merged["observations"]), 1)
        replacement = merged["observations"][0]["key"]
        self.assertEqual({row["key"] for row in merged["retractions"]}, children | {parent})
        self.assertTrue(all(row["replacement_keys"] == [replacement] for row in merged["retractions"]))
        initial_findings = {
            row["id"] for row in original["observations"][0]["observation"]["group"]["events"]
        }
        current_findings = {row["id"] for row in merged["observations"][0]["observation"]["group"]["events"]}
        self.assertEqual(initial_findings, current_findings)
        # A failed core commit followed by source retention cannot leave an
        # issued parent pointing to an unavailable, never-issued replacement.
        with app.connect(self.path) as database:
            database.execute("DELETE FROM events")
        self.assertEqual(merged, NativeCollector(self.path).snapshot_state(NOW + 100))

    def test_returning_daily_identity_stays_in_its_first_encounter(self):
        aircraft = {"hex": "a00001", "r": "N1", "t": "B52", "type": "adsb_icao"}
        vintage = rules.Finding("vintage", "airframes", "", 100, "years", "", "", 80)
        fast = rules.Finding("fast", "performance", "", 650, "kn", "", "", 80)
        with app.connect(self.path) as database:
            for finding in (vintage, fast):
                app.record(database, aircraft, finding, NOW, NOW + 60, extra={"encounter_start": NOW})
        before = self.collector.snapshot_state(NOW + 1800)
        with app.connect(self.path) as database:
            app.record(
                database,
                aircraft,
                vintage,
                NOW + 3600,
                NOW + 3660,
                extra={"encounter_start": NOW + 3600},
            )
        after = self.collector.snapshot_state(NOW + 5400)
        self.assertEqual(after["retractions"], [])
        self.assertEqual(len(after["observations"]), 1)
        self.assertEqual(before["observations"][0]["key"], after["observations"][0]["key"])
        self.assertEqual(after["observations"][0]["observation"]["group"]["last_seen"], NOW + 60)

    def test_novelty_uses_only_covered_dates_before_observation_day(self):
        self.feed()
        with app.connect(self.path) as database:
            for day in ("2026-10-03", "2026-10-04", "2026-10-05"):
                database.execute("INSERT OR REPLACE INTO coverage VALUES (?,3600,1)", (day,))
                database.execute(
                    "INSERT OR REPLACE INTO daily(day,hex,typecode) VALUES (?,?,?)", (day, "a00001", "C172")
                )
        counts = self.collector.snapshot(NOW + 80)[0]["observation"]["context"]["novelty"]
        self.assertEqual(counts, {"coverage_days": 1, "type_days": {"C172": 1}})

    def test_absorbed_finding_becomes_explicitly_ineligible(self):
        self.feed()
        with app.connect(self.path) as database:
            database.execute(
                "UPDATE events SET evidence=json_set(evidence,'$.absorbed_by',json(?))",
                ('{"id":999,"kind":"circling"}',),
            )
        event = self.collector.snapshot(NOW + 80)[0]["observation"]["group"]["events"][0]
        self.assertTrue(event["evidence"]["absorbed_by"])
        self.assertFalse(event["selection"]["story_eligible"])
        self.assertEqual(event["evidence"]["absorbed_by"], {"superseded": True})

    def test_native_journal_has_no_ai_budget_or_private_writer_tables(self):
        with app.connect(self.path) as database:
            tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse(tables & {"ai_runs", "notes", "story_drafts"})

    def test_native_default_enrichment_cannot_make_network_requests(self):
        self.feed()
        with (
            patch.object(typecheck, "lookup", side_effect=AssertionError("network lookup")),
            patch.object(wind, "fetch", side_effect=AssertionError("network lookup")),
        ):
            result = self.collector.enrich(NOW + 80)
        self.assertEqual(result["type_registry"]["state"], "disabled")
        self.assertEqual(result["wind"]["state"], "disabled")

    def test_opt_in_registry_enrichment_updates_saved_evidence_with_attribution(self):
        aircraft = {"hex": "a00001", "r": "N1", "t": "B52", "type": "adsb_icao"}
        finding = rules.Finding("watch_type", "airframes", "B52", None, "", "", "", 80)
        with app.connect(self.path) as database:
            app.record(database, aircraft, finding, NOW, NOW + 60, hits=7)
        collector = NativeCollector(self.path, type_registry=True)
        answer = {"source": "adsbdb", "type_code": "B52", "registration": "N1"}
        with patch.object(typecheck, "lookup", return_value=answer):
            result = collector.enrich(NOW + 80)
        self.assertEqual(result["type_registry"]["state"], "checked")
        observation = collector.snapshot(NOW + 80)[0]["observation"]
        check = observation["group"]["events"][0]["evidence"]["type_check"]
        self.assertEqual(check["state"], "agrees")
        self.assertEqual(check["source"], "adsbdb")


class IdentityContinuityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "identities.sqlite3"
        self.first = sample_source()
        self.second = copy.deepcopy(self.first)
        self.second["id"] = 984
        self.second["kind"] = "rapid_climb"

    def store(self):
        return IdentityStore(self.path, "receiver")

    def test_merge_retires_both_parents_and_is_idempotent_after_restart(self):
        with self.store() as store:
            first, second = store.finding(self.first), store.finding(self.second)
            group_a, group_b = store.assign_groups([("a", [first]), ("b", [second])])
        with self.store() as store:
            (replacement,) = store.assign_groups(
                [("corrected", [store.finding(self.first), store.finding(self.second)])]
            )
            self.assertNotIn(replacement, {group_a, group_b})
            retirements = store.retractions()
            self.assertEqual({row["key"] for row in retirements}, {group_a, group_b})
            self.assertTrue(all(row["replacement_keys"] == [replacement] for row in retirements))
        with self.store() as store:
            (current,) = store.assign_groups(
                [("corrected", [store.finding(self.first), store.finding(self.second)])]
            )
            self.assertEqual(current, replacement)
            self.assertEqual(retirements, store.retractions())

    def test_split_is_explicit_and_uncommitted_change_rolls_back(self):
        with self.store() as store:
            (original,) = store.assign_groups(
                [("a", [store.finding(self.first), store.finding(self.second)])]
            )
        with self.assertRaisesRegex(ValueError, "simulated"):
            with self.store() as store:
                children = store.assign_groups(
                    [("a", [store.finding(self.first)]), ("b", [store.finding(self.second)])]
                )
                self.assertEqual(
                    store.retractions(),
                    [{"key": original, "reason_code": "superseded", "replacement_keys": sorted(children)}],
                )
                raise ValueError("simulated projection failure")
        with self.store() as store:
            self.assertEqual(
                store.assign_groups([("a", [store.finding(self.first), store.finding(self.second)])]),
                [original],
            )
            self.assertEqual(store.retractions(), [])

    def test_retention_alone_does_not_retract_an_old_group(self):
        with self.store() as store:
            store.assign_groups([("a", [store.finding(self.first)])])
        with self.store() as store:
            self.assertEqual(store.assign_groups([]), [])
            self.assertEqual(store.retractions(), [])

    def test_active_row_reuse_cannot_silently_change_public_subject(self):
        with self.store() as store:
            original = store.finding(self.first)
        replacement = {**self.first, "hex": "b00001"}
        with self.assertRaisesRegex(IdentityReconciliationRequired, "subject"):
            with self.store() as store:
                store.finding(replacement)
        with self.store() as store:
            self.assertEqual(store.finding(self.first), original)

    def test_retired_row_slot_gets_new_identity_for_an_unrelated_episode(self):
        with self.store() as store:
            original = store.finding(self.first)
        with self.store():
            pass  # A complete snapshot no longer contains the old local row.
        replacement = {**self.first, "first_seen": NOW + 86400, "last_seen": NOW + 86460}
        with self.store() as store:
            self.assertNotEqual(store.finding(replacement), original)

    def test_retired_row_slot_cannot_reuse_identity_for_an_overlapping_source_tuple(self):
        with self.store() as store:
            original = store.finding(self.first)
        with self.store():
            pass
        with self.store() as store:
            self.assertNotEqual(store.finding(self.first), original)


class FrozenPackageTests(unittest.TestCase):
    def test_frozen_runner_imports_relative_package_without_engine_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "corpus"
            destination.mkdir()
            (destination / "manifest.json").write_text("{}")
            history_backfill.pin_source(destination)
            result = subprocess.run(
                [sys.executable, "-I", str(destination / "run_frozen.py"), "--help"],
                capture_output=True,
                text=True,
                cwd=folder,
                timeout=20,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--workers", result.stdout)


if __name__ == "__main__":
    unittest.main()
