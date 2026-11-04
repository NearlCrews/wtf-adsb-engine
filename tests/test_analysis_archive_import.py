"""Journal imports from archives: label provenance, versions and the live-overlap boundary."""

import contextlib
import gzip
import io
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import analysis_archive_fixtures as fx

from wtf_adsb_engine.analysis import app, episode_replay, import_replay
from wtf_adsb_engine.analysis import history_backfill as backfill
from wtf_adsb_engine.analysis.replay_patterns import analyze_tracks, apply_episodes
from wtf_adsb_engine.analysis.rules import Finding
from wtf_adsb_engine.runtime import Runtime

START = backfill.day_stamp("2026-10-01")


def position_slices(start, count, altitude=8000, speed=180, step=15):
    slices = []
    for index in range(count):
        lat, lon = fx.place(0.05 * index)
        slices.append((start + index * step, [("position", fx.OWN, lat, lon, altitude, speed, "adsb_icao")]))
    return slices


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.archive = self.root / "archive"
        self.database = self.root / "journal.sqlite3"
        self.metadata = self.root / "metadata.csv.gz"
        self.metadata.write_bytes(gzip.compress(b"a00a01;N0TEST;A388;0000;Synthetic airliner;2020\n"))
        app.initialize(self.database)
        self.install(START + 86400)

    def tearDown(self):
        self.temp.cleanup()

    def install(self, stamp):
        with app.connect(self.database) as db:
            app.put_meta(db, "installed_at", stamp)

    def run_import(self, *extra):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            import_replay.main(
                [
                    "--root",
                    str(self.archive),
                    "--database",
                    str(self.database),
                    "--metadata",
                    str(self.metadata),
                    *extra,
                ]
            )
        return json.loads(output.getvalue().splitlines()[-1])

    def events(self, kind=None):
        with app.connect(self.database) as db:
            rows = [app.event_dict(row) for row in db.execute("SELECT * FROM events ORDER BY id")]
        return [row for row in rows if kind is None or row["kind"] == kind]

    def test_status_hits_count_label_records_and_placeholders_never_become_codes(self):
        fx.write_compact(self.archive, fx.labelled_compact_track(START))
        self.run_import()
        status = self.events("archive_squawk_7700")
        self.assertEqual(len(status), 1)
        event = status[0]
        # Four 7700 label records, inherited by twenty positions.
        self.assertEqual(event["hits"], 4)
        labels = event["evidence"]["archive_labels"]
        self.assertEqual((labels["label_records"], labels["inheriting_positions"]), (4, 20))
        aircraft = event["evidence"]["aircraft"]
        self.assertEqual(aircraft["squawk"], "7700")
        self.assertLess(aircraft["label_at"], event["evidence"]["observed_at"])
        self.assertIn(episode_replay.CARRIED_LABEL, event["evidence"]["context_tags"])
        for row in self.events():
            self.assertNotEqual(row["evidence"]["aircraft"].get("squawk"), "0000")
            self.assertEqual(
                row["evidence"]["replay_import"]["detector_version"], import_replay.IMPORT_VERSION
            )
        watch = self.events("watch_type")[0]["evidence"]
        # The import re-anchors identity findings on later positions; the label keeps its own time.
        self.assertEqual(watch["aircraft"]["squawk"], "7700")
        self.assertLess(watch["aircraft"]["label_at"], watch["observed_at"])
        self.assertIn(episode_replay.CARRIED_LABEL, watch["context_tags"])

    def test_completed_archive_import_can_feed_the_engine_without_receiver_metadata(self):
        fx.write_compact(self.archive, fx.labelled_compact_track(START))
        self.run_import()
        with app.connect(self.database) as db:
            self.assertIsNone(app.meta(db, "last_receiver_timestamp"))
            self.assertTrue(app.meta(db, "archive_scan")["complete"])
            stamp = db.execute("SELECT MAX(last_seen) FROM aircraft").fetchone()[0]
        config = self.root / "engine.toml"
        config.write_text(
            'version=1\nstate_directory="engine"\nsource_instance="archive"\nsynthetic=true\n'
            '[[pipelines]]\nname="archive"\n'
            'input={plugin="wtf_adsb.journal", options={path="journal.sqlite3", mode="replay"}}\n'
            'detectors=[{plugin="wtf_adsb.collection"}]\n'
            '[[outputs]]\nname="files"\nplugin="wtf_adsb.files"\noptions={directory="export"}\n'
        )
        runtime = Runtime(config)
        with runtime.locked():
            result = runtime.once()
            self.assertFalse(result["errors"])
            self.assertEqual(result["outputs"], {"files": "ready"})
            self.assertGreater(runtime.store.state()["head_sequence"], 0)
            self.assertEqual(runtime.store.checkpoint("archive"), stamp)
            health = runtime.store.state()["health"]
            self.assertTrue(health["ready"])
            self.assertTrue(health["complete"])
            self.assertEqual(health["analysis_watermark"], stamp)
            self.assertIsNone(health["last_receiver_poll"])

    def test_one_label_record_inherited_by_many_positions_is_a_single_unconfirmed_report(self):
        slices = position_slices(START, 6)
        slices[0][1].insert(0, ("label", fx.OWN, 7700, "TEST12"))
        fx.write_compact(self.archive, slices)
        self.run_import()
        event = self.events("archive_squawk_7700")[0]
        self.assertEqual(event["hits"], 1)
        self.assertEqual(event["evidence"]["archive_labels"]["inheriting_positions"], 6)
        self.assertEqual(event["confidence"], "Single archived sample · unconfirmed")

    def test_archive_altitude_outliers_keep_their_unknown_reference(self):
        fx.write_compact(self.archive, position_slices(START, 3, altitude=70000))
        self.run_import()
        self.assertTrue(self.events("outlier_archive_alt"))
        self.assertFalse(self.events("outlier_alt_baro"))
        self.assertIn("Archived altitude", self.events("outlier_archive_alt")[0]["why"])

    def test_versions_are_recorded_and_legacy_import_rows_are_migrated(self):
        with app.connect(self.database) as db:
            db.execute("CREATE TABLE replay_imports(path TEXT PRIMARY KEY, imported_at REAL, points INTEGER)")
            db.execute("INSERT INTO replay_imports VALUES ('legacy.bin.ttf',1,20)")
        fx.write_compact(self.archive, position_slices(START, 3))
        self.run_import()
        with app.connect(self.database) as db:
            rows = {row["path"]: dict(row) for row in db.execute("SELECT * FROM replay_imports")}
            known = app.meta(db, "replay_import_code")
        extra = ("detector_version", "code_sha256", "live_boundary", "refused_live_positions")
        self.assertEqual([rows["legacy.bin.ttf"][key] for key in extra], [None] * 4)
        current = rows["2026/10/01/heatmap/20.bin.ttf"]
        self.assertEqual(current["detector_version"], import_replay.IMPORT_VERSION)
        code = known[current["code_sha256"]]
        self.assertEqual(
            (current["live_boundary"], current["refused_live_positions"]), (START + 86400 - 45, 0)
        )
        # The pinned code covers the rules, the decoders and this tool, not only rules.py.
        self.assertTrue({"rules.py", "history.py", "app.py", "import_replay.py"} <= set(code))
        event = self.events()[0]
        self.assertEqual(event["evidence"]["replay_import"]["code_sha256"], current["code_sha256"])

    def test_a_findings_own_context_tags_survive_the_carried_label_tag(self):
        fx.write_compact(self.archive, fx.labelled_compact_track(START))
        original = import_replay.candidates

        def tagged(*args, **kwargs):
            findings = original(*args, **kwargs)
            for finding in findings:
                finding.evidence = {
                    **(finding.evidence or {}),
                    "context_tags": ["metadata_identity_conflict"],
                }
            return findings

        with patch.object(import_replay, "candidates", side_effect=tagged):
            self.run_import()
        event = self.events("archive_squawk_7700")[0]
        self.assertEqual(
            event["evidence"]["context_tags"],
            sorted({episode_replay.CARRIED_LABEL, "metadata_identity_conflict"}),
        )

    def test_positions_at_or_after_live_installation_are_refused_unless_allowed(self):
        live = Finding("watch_type", "airframes", "Airbus A380 spotted", None, "", "Live", "Live", 82)
        with app.connect(self.database) as db:
            live_id = app.record(
                db,
                {"hex": fx.OWN, "t": "A388"},
                live,
                START + 300,
                START + 330,
                extra={"encounter_start": START + 290},
            )
            before = tuple(db.execute("SELECT * FROM events WHERE id=?", (live_id,)).fetchone())
        self.install(START + 285)
        fx.write_compact(self.archive, position_slices(START, 40, altitude=70000))
        result = self.run_import()
        # Twenty-four slices start at or after installed_at - 45 s = START + 240, and
        # the earlier watch-type group would merge into the live row.
        self.assertEqual(result["refused_live_overlap_positions"], 24)
        self.assertEqual(result["refused_live_merge_findings"], 1)
        with app.connect(self.database) as db:
            self.assertEqual(
                tuple(db.execute("SELECT * FROM events WHERE id=?", (live_id,)).fetchone()), before
            )
        imported = self.events("outlier_archive_alt")
        self.assertEqual(len(imported), 1)
        self.assertEqual((imported[0]["first_seen"], imported[0]["last_seen"]), (START, START + 225))
        with app.connect(self.database) as db:
            db.execute("DELETE FROM replay_imports")
            db.execute("DELETE FROM events WHERE id<>?", (live_id,))
        allowed = self.run_import("--allow-live-overlap")
        self.assertEqual(allowed["refused_live_overlap_positions"], 0)
        # Overlap may be allowed, but a merge into the live row never is.
        self.assertEqual(allowed["refused_live_merge_findings"], 1)
        self.assertEqual(self.events("outlier_archive_alt")[0]["last_seen"], START + 585)
        with app.connect(self.database) as db:
            self.assertEqual(
                tuple(db.execute("SELECT * FROM events WHERE id=?", (live_id,)).fetchone()), before
            )

    def test_a_damaged_file_is_reported_and_retried_without_stopping_the_import(self):
        fx.write_compact(self.archive, position_slices(START, 3))
        broken = fx.write_compact(self.archive, position_slices(START + 3600, 3), name="22.bin.ttf")
        broken.write_bytes(fx.corrupt_deflate(gzip.decompress(broken.read_bytes())))
        result = self.run_import()
        self.assertEqual(
            result["unreadable_files"],
            [{"archive": "2026/10/01/heatmap/22.bin.ttf", "error_type": "zlib.error"}],
        )
        with app.connect(self.database) as db:
            scan = app.meta(db, "archive_scan")
            paths = {row[0] for row in db.execute("SELECT path FROM replay_imports")}
        self.assertEqual(paths, {"2026/10/01/heatmap/20.bin.ttf"})
        self.assertFalse(scan["complete"])
        self.assertEqual(scan["unreadable_files"], 1)
        self.assertEqual(len(self.run_import()["unreadable_files"]), 1)

    def test_missing_markers_cannot_be_hidden_by_unrelated_receipts_and_remain_retryable(self):
        with app.connect(self.database) as db:
            db.execute("CREATE TABLE replay_imports(path TEXT PRIMARY KEY,imported_at REAL,points INTEGER)")
            db.execute("INSERT INTO replay_imports VALUES ('2020/01/01/heatmap/00.bin.ttf',0,0)")
            db.execute("INSERT INTO replay_imports VALUES ('2020/01/01/heatmap/01.bin.ttf',0,0)")
        valid = fx.write_compact(self.archive, position_slices(START, 3))
        broken = fx.write_compact(self.archive, position_slices(START + 3600, 3), name="22.bin.ttf")
        broken.write_bytes(gzip.compress(bytes(16)))
        for _ in range(2):
            result = self.run_import()
            self.assertEqual(
                result["unreadable_files"],
                [{"archive": "2026/10/01/heatmap/22.bin.ttf", "error_type": "ValueError"}],
            )
            with app.connect(self.database) as db:
                scan = app.meta(db, "archive_scan")
                paths = {row[0] for row in db.execute("SELECT path FROM replay_imports")}
            self.assertEqual((scan["files"], scan["total_files"], scan["unreadable_files"]), (1, 2, 1))
            self.assertFalse(scan["complete"])
            self.assertIn(str(valid.relative_to(self.archive)), paths)
            self.assertNotIn(str(broken.relative_to(self.archive)), paths)
        fx.write_compact(self.archive, position_slices(START + 3600, 3), name="22.bin.ttf")
        result = self.run_import()
        self.assertEqual(result["unreadable_files"], [])
        with app.connect(self.database) as db:
            scan = app.meta(db, "archive_scan")
        self.assertEqual((scan["files"], scan["total_files"]), (2, 2))
        self.assertTrue(scan["complete"])


class PatternApplyOverlapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "journal.sqlite3"
        app.initialize(self.database)
        self.points = []
        for t in range(0, 601, 15):
            lat, lon = fx.place(0.25 * math.sin(t / 180 * math.tau), 0.25 * math.cos(t / 180 * math.tau))
            self.points.append(
                {
                    "ts": START + t,
                    "lat": lat,
                    "lon": lon,
                    "gs": 31.4,
                    "archive_alt": 1250,
                    "source": "adsb_icao",
                    "archive": "2026/10/01/heatmap/20.bin.ttf",
                }
            )

    def tearDown(self):
        self.temp.cleanup()

    def test_episodes_reaching_live_collection_are_refused_unless_allowed(self):
        episodes = analyze_tracks({fx.OWN: self.points})
        self.assertTrue(episodes)
        with app.connect(self.database) as db:
            db.execute("DELETE FROM meta WHERE key='installed_at'")
        with self.assertRaisesRegex(ValueError, "installation time is unavailable"):
            apply_episodes(self.database, episodes)
        with app.connect(self.database) as db:
            app.put_meta(db, "installed_at", START + 200)
        result = apply_episodes(self.database, episodes)
        self.assertEqual(result["refused_live_overlap_episodes"], len(episodes))
        self.assertEqual(result["imported_endpoints"], 0)
        allowed = apply_episodes(self.database, episodes, allow_live_overlap=True)
        self.assertEqual(allowed["refused_live_overlap_episodes"], 0)
        self.assertGreater(allowed["imported_endpoints"], 0)

    def test_episodes_never_merge_into_a_live_row(self):
        episodes = analyze_tracks({fx.OWN: self.points})
        live = Finding(
            "circling",
            "patterns",
            "Live circling",
            2,
            "turns",
            "Live",
            "Live",
            80,
            evidence={"pattern": {"started_at": START + 100, "ended_at": START + 400}},
        )
        with app.connect(self.database) as db:
            live_id = app.record(
                db, {"hex": fx.OWN}, live, START + 100, START + 400, 5, extra={"encounter_start": START}
            )
            before = tuple(db.execute("SELECT * FROM events WHERE id=?", (live_id,)).fetchone())
        result = apply_episodes(self.database, episodes, allow_live_overlap=True)
        self.assertEqual(result["refused_live_merge_episodes"], len(episodes))
        with app.connect(self.database) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
            self.assertEqual(
                tuple(db.execute("SELECT * FROM events WHERE id=?", (live_id,)).fetchone()), before
            )


class CrossDayOperatorTests(unittest.TestCase):
    """The cross-day phase counts operator designators as the collector's daily table does."""

    COVERAGE = [f"2026-09-{day:02d}" for day in range(1, 31)] + ["2026-10-01"]

    @staticmethod
    def row(address, day, flight, at, designator=None):
        fix = {"ts": at, "lat": 42.5, "lon": -83.4, "source": "adsb_icao", "flight": flight, "label_at": at}
        record = {"positions": 3, "first": at, "last": at + 10, "first_fix": fix}
        record["sighted"] = {"fix": fix, "at": at + 10, "positions": 3}
        if designator:
            record["designator"] = designator
        return address, day, json.dumps({"traces": record})

    def kinds(self, phase):
        return [event["kind"] for event in phase.events]

    def test_a_days_designator_is_its_first_airline_callsign(self):
        points = [
            {"ts": 1},
            {"ts": 2, "flight": "N123DL"},
            {"ts": 3, "flight": "DAL1234"},
            {"ts": 4, "flight": "UAL9"},
        ]
        self.assertEqual(backfill._designator(points), "DAL")
        self.assertEqual(backfill._designator([{"ts": 1, "flight": "ZZQ12"}]), "")

    def test_first_operator_day_then_never_again(self):
        phase = backfill._CrossDay(self.COVERAGE, {}, "2026-10-03T00:00:00Z")
        phase.day("2026-10-02", [self.row("a05f2d", "2026-10-02", "DAL1234", 1_790_901_000, "DAL")])
        self.assertIn("first_operator_here", self.kinds(phase))
        phase.events.clear()
        phase.day("2026-10-03", [self.row("a05f2e", "2026-10-03", "DAL88", 1_790_987_400, "DAL")])
        self.assertNotIn("first_operator_here", self.kinds(phase))

    def test_only_the_days_earliest_aircraft_is_first_and_a_day_without_coverage_is_not(self):
        phase = backfill._CrossDay(self.COVERAGE, {}, "2026-10-03T00:00:00Z")
        phase.day(
            "2026-10-02",
            [
                self.row("a05f2d", "2026-10-02", "SWA10", 1_790_901_600, "SWA"),
                self.row("a05f2e", "2026-10-02", "SWA20", 1_790_901_000, "SWA"),
            ],
        )
        self.assertEqual(
            [event["hex"] for event in phase.events if event["kind"] == "first_operator_here"], ["a05f2e"]
        )
        early = backfill._CrossDay(self.COVERAGE[:5], {}, "2026-10-03T00:00:00Z")
        early.day("2026-10-02", [self.row("a05f2d", "2026-10-02", "JBU1", 1_790_901_000, "JBU")])
        self.assertNotIn("first_operator_here", self.kinds(early))


if __name__ == "__main__":
    unittest.main()
