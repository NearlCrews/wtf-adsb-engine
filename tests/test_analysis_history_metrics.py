"""Historical metrics tests use saved synthetic Prometheus responses only."""

import gzip
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from wtf_adsb_engine.analysis import history_backfill as history
from wtf_adsb_engine.analysis import history_metrics as metrics

START = history.day_stamp("2026-10-01")


def matrix(series):
    return {"status": "success", "data": {"resultType": "matrix", "result": series}}


def series(field, readings, **labels):
    return {
        "metric": {
            "__name__": "readsb_aircraft_" + field,
            "hex": "a12345",
            "type": "adsb_icao",
            "flight": "TEST123",
            "host": "PRIVATE",
            "ownOp": "PRIVATE",
            **labels,
        },
        "values": readings,
    }


def capture_fixture(root, payload):
    corpus = root / "corpus"
    history.initialize(corpus)
    (corpus / "inputs").mkdir()
    raw = gzip.compress(b"")
    (corpus / "inputs" / "aircraft-metadata.csv.gz").write_bytes(raw)
    manifest = {"captured_at": history.utc(START + 7200), "metadata_sha256": hashlib.sha256(raw).hexdigest()}
    history.write_private(corpus / "manifest.json", manifest)
    # freeze() records the manifest that loaded the metadata table.
    with history.connect(corpus / "corpus.sqlite3") as database:
        database.execute("INSERT INTO meta VALUES ('manifest',?)", (history.encoded(manifest),))
    with patch.object(metrics, "open_local", return_value=io.BytesIO(json.dumps(payload).encode())):
        metrics.freeze(root / "metrics", start=history.utc(START), end=history.utc(START + 3600))
    return corpus


class HistoryMetricsTests(unittest.TestCase):
    def analyze_fixture(self, payload):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            corpus = capture_fixture(root, payload)
            metrics.analyze(root / "metrics", corpus)
            return json.loads((root / "metrics" / "findings.json").read_text())

    def test_a_metadata_table_from_another_capture_is_refused(self):
        payload = matrix([series("dbFlags", [[START, "1"]], r="NTEST", t="B52", year="1960")])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            corpus = capture_fixture(root, payload)
            raw = gzip.compress(b"a12345;NTEST;A388;0000;Replacement;2020\n")
            (corpus / "inputs" / "aircraft-metadata.csv.gz").write_bytes(raw)
            manifest = json.loads((corpus / "manifest.json").read_text())
            manifest["metadata_sha256"] = hashlib.sha256(raw).hexdigest()
            history.write_private(corpus / "manifest.json", manifest)
            with self.assertRaisesRegex(ValueError, "metadata table does not match"):
                metrics.analyze(root / "metrics", corpus)

    def test_saved_results_are_reproduced_but_never_replaced_by_other_code_or_inputs(self):
        payload = matrix([series("dbFlags", [[START, "1"]], r="NTEST", t="B52", year="1960")])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            corpus = capture_fixture(root, payload)
            metrics.analyze(root / "metrics", corpus)
            saved = json.loads((root / "metrics" / "findings.json").read_text())
            self.assertEqual(saved["inputs"]["metric_chunks"][0][0], 0)
            metrics.analyze(root / "metrics", corpus)
            reproduced = (root / "metrics" / "findings.json").read_bytes()
            with patch.object(history, "code_hashes", return_value={}):
                with self.assertRaisesRegex(ValueError, "other code or inputs"):
                    metrics.analyze(root / "metrics", corpus)
            manifest = json.loads((corpus / "manifest.json").read_text())
            history.write_private(
                corpus / "manifest.json", {**manifest, "captured_at": history.utc(START + 9000)}
            )
            with self.assertRaisesRegex(ValueError, "other code or inputs"):
                metrics.analyze(root / "metrics", corpus)
            self.assertEqual((root / "metrics" / "findings.json").read_bytes(), reproduced)

    def test_fields_join_only_on_exact_scrape_and_matching_aircraft_labels(self):
        payload = matrix(
            [
                series("lat", [[START, "35"]], host="PRIVATE"),
                series("lon", [[START, "-110"]], ownOp="PRIVATE"),
                series("gs", [[START + 1, "600"]]),
                series("alt_baro", [[START, "50000"]], flight="OTHER"),
                series("seen_pos", [[START, "2"]]),
            ]
        )
        tracks, counts, _ = metrics.joined_points([(payload, "fixture@hash")], start=START, end=START + 3600)
        points = tracks["readsb"]["a12345"]
        self.assertEqual(len(points), 3)
        position = next(point for point in points if "lat" in point)
        self.assertIsNone(position["gs"])
        self.assertNotIn("alt_baro", position)
        self.assertFalse(position["stale"])
        self.assertEqual(position["flight"], "TEST123")
        self.assertNotIn("PRIVATE", history.encoded(tracks))
        self.assertEqual(counts["aircraft_metric_samples"], 5)

    def test_duplicate_chunks_do_not_duplicate_snapshots_and_conflicts_stay_stale(self):
        payload = matrix(
            [
                series("lat", [[START, "35"]]),
                series("lon", [[START, "-110"]]),
                series("seen_pos", [[START, "0"]]),
            ]
        )
        conflict = matrix([series("lat", [[START, "36"]])])
        tracks, counts, _ = metrics.joined_points(
            [(payload, "one"), (payload, "two"), (conflict, "three")], start=START, end=START + 3600
        )
        point = tracks["readsb"]["a12345"][0]
        self.assertEqual(counts["joined_aircraft_snapshots"], 1)
        self.assertTrue(point["stale"])
        self.assertEqual(point["archives"], ["one", "two", "three"])

    def test_uat_non_icao_ground_and_freshness_are_not_guessed(self):
        labels = {
            "address": "a12345",
            "address_qualifier": "adsb_other",
            "airground_state": "ground",
            "callsign": "TEST123",
        }
        payload = matrix(
            [
                {"metric": {"__name__": "aircraft_" + field, **labels}, "values": [[START, value]]}
                for field, value in (
                    ("position_lat", "35"),
                    ("position_lon", "-110"),
                    ("pressure_altitude", "1000"),
                )
            ]
        )
        tracks, _, _ = metrics.joined_points([(payload, "test")], start=START, end=START + 3600)
        point = tracks["uat"]["~a12345"][0]
        self.assertEqual(point["alt_baro"], "ground")
        self.assertFalse(point["position_age_gauge_present"])

    def test_freeze_uses_raw_hourly_vectors_and_resumes_without_new_requests(self):
        with tempfile.TemporaryDirectory() as temp:
            responses = []

            def respond(url, **_):
                responses.append(parse_qs(urlsplit(url).query))
                return io.BytesIO(json.dumps(matrix([])).encode())

            with patch.object(metrics, "open_local", side_effect=respond):
                first = metrics.freeze(temp, start=history.utc(START), end=history.utc(START + 9000))
                second = metrics.freeze(temp, start=history.utc(START), end=history.utc(START + 9000))
            self.assertEqual(first, second)
            self.assertEqual(len(responses), 3)
            self.assertTrue(responses[-1]["query"][0].endswith("[1800000ms]"))
            self.assertEqual(responses[0]["limit"], ["0"])
            self.assertTrue(responses[0]["query"][0].endswith("[3600001ms]"))

    def test_omitted_completed_chunk_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(
                metrics,
                "open_local",
                side_effect=lambda *_args, **_kw: io.BytesIO(json.dumps(matrix([])).encode()),
            ):
                metrics.freeze(temp, start=history.utc(START), end=history.utc(START + 7200))
            path = Path(temp) / "manifest.json"
            manifest = json.loads(path.read_text())
            manifest["chunks"].pop()
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "incomplete"):
                metrics.analyze(temp, Path(temp) / "unused-corpus")

    def test_cross_exporter_fields_never_form_a_complete_position(self):
        payload = matrix(
            [
                series("lat", [[START, "35"]], instance="one"),
                series("seen_pos", [[START, "0"]], instance="one"),
                series("lon", [[START, "-110"]], instance="two"),
                series("gs", [[START, "650"]], instance="two"),
            ]
        )
        tracks, _, _ = metrics.joined_points([(payload, "test")], start=START, end=START + 3600)
        self.assertEqual(len(tracks["readsb"]["a12345"]), 2)
        self.assertTrue(all(point["stale"] for point in tracks["readsb"]["a12345"]))
        result = self.analyze_fixture(payload)
        self.assertNotIn("fast", result["findings_by_kind"])

    def test_retained_historical_metadata_survives_empty_current_database(self):
        payload = matrix(
            [
                series(
                    "dbFlags",
                    [[START, "1"]],
                    r="NTEST",
                    t="B52",
                    year="1960",
                    desc="Synthetic historic aircraft",
                )
            ]
        )
        result = self.analyze_fixture(payload)
        self.assertTrue({"military", "watch_type", "vintage"} <= set(result["findings_by_kind"]))
        event = result["findings"][0]
        self.assertEqual(event["evidence"]["aircraft"]["t"], "B52")
        self.assertEqual(
            event["evidence"]["metadata_provenance"]["fallback_metadata_as_of"], history.utc(START + 7200)
        )

    def test_uat_scalar_leads_do_not_become_position_jumps(self):
        labels = {
            "address": "a12345",
            "address_qualifier": "tisb_icao",
            "airground_state": "airborne",
            "emergency": "medical",
            "flightplan_id": "7700",
        }
        payload = matrix(
            [
                {"metric": {"__name__": "aircraft_" + field, **labels}, "values": readings}
                for field, readings in (
                    ("position_lat", [[START, "35"], [START + 10, "37"]]),
                    ("position_lon", [[START, "-110"], [START + 10, "-110"]]),
                    ("ground_speed", [[START, "650"], [START + 10, "650"]]),
                    ("pressure_altitude", [[START, "50000"], [START + 10, "50000"]]),
                )
            ]
        )
        result = self.analyze_fixture(payload)
        self.assertNotIn("position_jump", result["findings_by_kind"])
        self.assertIn("fast", result["findings_by_kind"])
        self.assertIn("special_squawk_7700", result["findings_by_kind"])
        self.assertEqual(result["findings"][0]["hex"], "a12345")

    def test_uat_mixed_ground_fields_do_not_join(self):
        common = {"address": "a12345", "address_qualifier": "adsb_icao"}
        payload = matrix(
            [
                {
                    "metric": {"__name__": "aircraft_" + field, **common, "airground_state": state},
                    "values": [[START, value]],
                }
                for field, value, state in (
                    ("position_lat", "35", "ground"),
                    ("position_lon", "-110", "airborne"),
                    ("pressure_altitude", "50000", "airborne"),
                )
            ]
        )
        result = self.analyze_fixture(payload)
        self.assertNotIn("high", result["findings_by_kind"])

    def test_metadata_changes_do_not_hide_intervening_ground_boundaries(self):
        rows = []
        for offset, latitude, type_code, ground in (
            (0, 35, "C172", "false"),
            (10, 36, "", "true"),
            (20, 37, "C172", "false"),
        ):
            for field, value in (
                ("lat", latitude),
                ("lon", -110),
                ("seen_pos", 0),
                ("gs", 200),
                ("alt_baro", 5000),
            ):
                rows.append(series(field, [[START + offset, str(value)]], t=type_code, ground=ground))
        result = self.analyze_fixture(matrix(rows))
        self.assertNotIn("position_jump", result["findings_by_kind"])

    def test_cached_readsb_positions_never_produce_motion_or_publicly_eligible_stories(self):
        rows = [
            series(field, readings)
            for field, readings in (
                ("lat", [[START, "35"], [START + 10, "37"]]),
                ("lon", [[START, "-110"], [START + 10, "-110"]]),
                ("gs", [[START, "650"], [START + 10, "650"]]),
                ("alt_baro", [[START, "50000"], [START + 10, "50000"]]),
                ("seen_pos", [[START, "0"], [START + 10, "0"]]),
            )
        ]
        result = self.analyze_fixture(matrix(rows))
        self.assertFalse(result["movement_evaluated"])
        self.assertNotIn("position_jump", result["findings_by_kind"])
        self.assertTrue(result["findings"])
        self.assertTrue(
            all(
                event["public_export_eligible"] is False and event["story_eligible"] is False
                for event in result["findings"]
            )
        )

    def test_other_cached_tag_variants_do_not_merge(self):
        payload = matrix(
            [series("lat", [[START, "35"]], alert="0"), series("lon", [[START, "-110"]], alert="1")]
        )
        tracks, _, _ = metrics.joined_points([(payload, "test")], start=START, end=START + 3600)
        self.assertEqual(len(tracks["readsb"]["a12345"]), 2)
        self.assertTrue(all(point["stale"] for point in tracks["readsb"]["a12345"]))

    def test_snapshot_bound_and_redirect_rejection(self):
        payload = matrix([series("lat", [[START, "35"], [START + 1, "35"]])])
        with patch.object(metrics, "MAX_JOINED_SNAPSHOTS", 1):
            with self.assertRaisesRegex(ValueError, "bound"):
                metrics.joined_points([(payload, "test")], start=START, end=START + 3600)
        with self.assertRaisesRegex(ValueError, "redirects"):
            metrics.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com")

    def test_capture_rejects_remote_endpoints_and_partial_responses(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "local Prometheus"):
                metrics.freeze(
                    temp,
                    start=history.utc(START),
                    end=history.utc(START + 3600),
                    endpoint="https://example.com",
                )
            payload = {**matrix([]), "warnings": ["partial result"]}
            with patch.object(metrics, "open_local", return_value=io.BytesIO(json.dumps(payload).encode())):
                with self.assertRaisesRegex(ValueError, "incomplete"):
                    metrics.freeze(temp, start=history.utc(START), end=history.utc(START + 3600))

    def test_saved_metric_hashes_are_checked_before_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(
                metrics, "open_local", return_value=io.BytesIO(json.dumps(matrix([])).encode())
            ):
                metrics.freeze(temp, start=history.utc(START), end=history.utc(START + 3600))
            (Path(temp) / "chunks" / "00000.json.gz").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "hash changed"):
                metrics.freeze(temp, start=history.utc(START), end=history.utc(START + 3600))


if __name__ == "__main__":
    unittest.main()
