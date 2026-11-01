"""Freezing through a complete day, prior captures, pinned analysis code and cloned inputs."""

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import analysis_archive_fixtures as fx
from analysis_archive_fixtures import Corpus

from wtf_adsb_engine.analysis import history_backfill as backfill
from wtf_adsb_engine.analysis import reference

DAY = "2026-06-10"
START = backfill.day_stamp(DAY)
HOUR = 3600


def settle(path, age=2 * HOUR):
    stamp = time.time() - age
    for item in Path(path).rglob("*"):
        os.utime(item, (stamp, stamp))


def tree(path):
    """Every file under `path` with its content hash."""
    return {
        str(item.relative_to(path)): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in sorted(Path(path).rglob("*"))
        if item.is_file()
    }


class ThroughTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.prepare(self.temp.name)

    def prepare(self, root):
        self.corpus = Corpus(root)
        archive = self.corpus.archive
        fx.full_day(archive, START)
        acas = archive / "2026/06/10/acas"
        acas.mkdir(parents=True)
        (acas / "acas.json.gz").write_bytes(gzip.compress(b"", mtime=0))
        (acas / "acas.csv.gz").write_bytes(gzip.compress(b"", mtime=0))
        # A trace rewritten after midnight holds fixes past the last frozen day.
        fx.write_trace(archive, fx.OWN, START + 86400 - 120, fx.straight_rows(0, 300, 1.0, 5000))
        fx.write_compact(archive, [(START + 86400 + 60, [])], name="00.bin.ttf")
        settle(archive)

    def tearDown(self):
        self.temp.cleanup()

    def freeze(self, **kwargs):
        return backfill.freeze(
            self.corpus.archive, self.corpus.journal, self.corpus.metadata, self.corpus.output, **kwargs
        )

    def test_later_days_are_left_out_and_analysis_stops_at_the_next_midnight(self):
        manifest = self.freeze(through=DAY)
        self.assertEqual(manifest["analysis_cutoff"], "2026-06-11T00:00:00+00:00")
        self.assertEqual({row["day"] for row in manifest["files"]}, {DAY})
        self.assertEqual(manifest["excluded_after_through"], 1)
        self.assertTrue(manifest["through"]["checks"]["complete"])
        self.assertIsNone(manifest["through"]["override_reason"])
        result = backfill.analyze_day(DAY, self.corpus.output)
        self.assertEqual(result["dependencies"]["analysis_cutoff"], manifest["analysis_cutoff"])
        saved = json.loads((self.corpus.output / "days" / f"{DAY}.json").read_text())
        traced = saved["aircraft_days"][fx.OWN]["traces"]
        self.assertLess(traced["last"], START + 86400)
        self.assertTrue(all(event["last_seen"] < START + 86400 for event in saved["findings"]))
        summary = backfill.summary(self.corpus.output, persist=False)
        self.assertEqual(
            (summary["last_day"], summary["analysis_cutoff"]), (DAY, manifest["analysis_cutoff"])
        )

    def test_an_incomplete_last_day_is_refused_before_anything_is_written(self):
        cases = {
            "all_slices_present": lambda: (self.corpus.archive / "2026/06/10/heatmap/47.bin.ttf").unlink(),
            "acas_compressed": lambda: (self.corpus.archive / "2026/06/10/acas/acas.json").write_bytes(b""),
            "traces_settled": lambda: os.utime(
                next((self.corpus.archive / "2026/06/10/traces").rglob("*.json"))
            ),
        }
        for check, damage in cases.items():
            with self.subTest(check=check):
                self.prepare(Path(self.temp.name) / check)
                damage()
                with self.assertRaisesRegex(ValueError, check):
                    self.freeze(through=DAY)
                self.assertFalse((self.corpus.output / "manifest.json").exists())
                self.assertFalse((self.corpus.output / "inputs" / "objects").exists())
        today = backfill.utc()[:10]
        with self.assertRaisesRegex(ValueError, "capture_after_cutoff"):
            self.freeze(through=today)

    def test_an_explicit_override_is_recorded_with_the_failed_checks(self):
        (self.corpus.archive / "2026/06/10/heatmap/47.bin.ttf").unlink()
        manifest = self.freeze(through=DAY, allow_incomplete="Receiver offline after 23:30 UTC")
        self.assertEqual(manifest["through"]["override_reason"], "Receiver offline after 23:30 UTC")
        self.assertFalse(manifest["through"]["checks"]["all_slices_present"])
        self.assertEqual(manifest["through"]["checks"]["heatmap_slice_files"], 47)

    def test_a_day_that_has_not_ended_cannot_be_overridden_and_an_override_needs_a_day(self):
        today = backfill.utc()[:10]
        with self.assertRaisesRegex(ValueError, "capture_after_cutoff"):
            self.freeze(through=today, allow_incomplete="Testing")
        with self.assertRaisesRegex(ValueError, "needs a last frozen day"):
            self.freeze(allow_incomplete="Testing")
        self.assertFalse(self.corpus.output.exists())

    def test_snapshot_versions_after_the_last_day_are_left_out(self):
        snapshots = Path(self.temp.name) / "snapshots"
        later = snapshots / "48" / "snapshot" / "ultrafeeder" / "globe_history"
        fx.write_compact(later, fx.labelled_compact_track(START + 86400), name="01.bin.ttf")
        fx.write_compact(later, fx.labelled_compact_track(START), name="02.bin.ttf")
        manifest = self.freeze(through=DAY, snapshots=snapshots)
        self.assertEqual({row["day"] for row in manifest["files"]}, {DAY})
        self.assertEqual(manifest["excluded_after_through"], 2)
        self.assertIn("snapshot-48", {source for row in manifest["files"] for source in row["sources"]})

    def test_a_rerun_after_a_partial_freeze_reloads_the_tables(self):
        backfill.initialize(self.corpus.output)
        with backfill.connect(self.corpus.output / "corpus.sqlite3") as database:
            database.execute("INSERT INTO metadata VALUES ('b00b01',?)", (backfill.encoded({"t": "STALE"}),))
            database.execute("INSERT INTO meta VALUES ('manifest','{}')")
        self.freeze(through=DAY)
        with closing(sqlite3.connect(self.corpus.output / "corpus.sqlite3")) as database:
            self.assertEqual([row[0] for row in database.execute("SELECT hex FROM metadata")], [fx.OWN])
        orphan = Corpus(Path(self.temp.name) / "orphan")
        (orphan.output / "days").mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "analysis results without an input manifest"):
            backfill.freeze(orphan.archive, orphan.journal, orphan.metadata, orphan.output)


class CaptureTimeTests(unittest.TestCase):
    def test_an_archive_still_being_written_is_analyzed_to_its_last_recorded_slice(self):
        with tempfile.TemporaryDirectory() as temp:
            corpus = Corpus(temp)
            now = time.time()
            lat, lon = fx.place()
            slices = [
                (stamp, [("position", fx.OWN, lat, lon, 5000, 150, "adsb_icao")])
                for stamp in range(int(now) - 2400, int(now) - 1200, 15)
            ]
            fx.write_compact(corpus.archive, slices)
            manifest = corpus.freeze()
            self.assertEqual(manifest["cutoff_basis"], "last_recorded_slice")
            self.assertEqual(manifest["analysis_cutoff"], backfill.utc(slices[-1][0] + 15))
            self.assertLess(manifest["analysis_cutoff"], manifest["captured_at"])

    def test_an_archive_no_longer_written_is_analyzed_to_the_capture(self):
        with tempfile.TemporaryDirectory() as temp:
            corpus = Corpus(temp)
            fx.write_compact(corpus.archive, fx.labelled_compact_track(START))
            manifest = corpus.freeze()
        self.assertEqual(
            (manifest["cutoff_basis"], manifest["analysis_cutoff"]), ("capture_time", manifest["captured_at"])
        )

    def test_the_slice_check_is_against_the_capture_start(self):
        with tempfile.TemporaryDirectory() as temp:
            path = fx.write_compact(temp, [(START + index * 15, []) for index in range(4)])
            objects, records = path.parent, [{"format": "heatmap", "day": DAY, "object": path.name}]
            end = START + 60
            # A long capture that began 30 min after the last slice still stops at that slice.
            self.assertEqual(backfill.last_slice_end(records, objects, end + 1800, end + 5400), end)
            self.assertIsNone(backfill.last_slice_end(records, objects, end + 3700, end + 3800))

    def test_a_journal_that_changes_while_it_is_copied_is_refused(self):
        real = backfill.read_only_uri

        def written_meanwhile(path, **kwargs):
            # The collector writes while the backup reads.
            os.utime(path, ns=(1, 2))
            return real(path, **kwargs)

        with tempfile.TemporaryDirectory() as temp:
            corpus = Corpus(temp)
            fx.write_compact(corpus.archive, fx.labelled_compact_track(START))
            with patch.object(backfill, "read_only_uri", side_effect=written_meanwhile):
                with self.assertRaisesRegex(ValueError, "changed while it was copied"):
                    corpus.freeze()


class PriorCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_versions_kept_only_by_a_prior_capture_are_copied_and_labelled(self):
        prior = Corpus(self.root / "prior")
        prior.output = self.root / "2026-10-04"
        fx.write_compact(prior.archive, fx.labelled_compact_track(START))
        snapshots = self.root / "snapshots"
        pruned_later = snapshots / "36" / "snapshot" / "ultrafeeder" / "globe_history"
        fx.write_compact(pruned_later, fx.labelled_compact_track(START, squawk=4512))
        prior_manifest = backfill.freeze(
            prior.archive, prior.journal, prior.metadata, prior.output, snapshots=snapshots
        )
        before = tree(prior.output)
        # Snapper later removed that snapshot's subvolume; only its record remains.
        shutil.rmtree(snapshots / "36" / "snapshot")
        (snapshots / "36" / "info.xml").write_text("<snapshot/>")
        current = Corpus(self.root / "current")
        fx.write_compact(current.archive, fx.labelled_compact_track(START))
        fx.write_compact(current.archive, fx.labelled_compact_track(START + 86400))
        manifest = backfill.freeze(
            current.archive,
            current.journal,
            current.metadata,
            current.output,
            snapshots=snapshots,
            include_prior=[prior.output],
        )
        self.assertEqual(tree(prior.output), before)
        self.assertEqual(manifest["skipped_snapshots"], ["36"])
        old = {(row["relative"], row["sha256"]) for row in prior_manifest["files"]}
        new = {(row["relative"], row["sha256"]) for row in manifest["files"]}
        self.assertLessEqual(old, new)
        self.assertEqual(manifest["sources"], ["active", "prior-capture-2026-10-04"])
        (record,) = manifest["prior_captures"]
        self.assertEqual(
            record["manifest_sha256"],
            hashlib.sha256((prior.output / "manifest.json").read_bytes()).hexdigest(),
        )
        self.assertEqual((record["versions_included"], record["prior_only_versions_added"]), (2, 1))
        shared = next(
            row for row in manifest["files"] if row["sources"] == ["active", "prior-capture-2026-10-04"]
        )
        self.assertEqual(shared["day"], DAY)
        for row in manifest["files"]:
            data = (current.output / "inputs" / "objects" / row["object"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), row["sha256"])
        summary = backfill.run(current.output, workers=1)
        self.assertEqual(summary["remaining_dates"], 0)

    def test_a_damaged_prior_object_is_a_capture_failure_and_later_days_follow_through(self):
        prior = Corpus(self.root / "inputs")
        prior.output = self.root / "prior"
        fx.write_compact(prior.archive, fx.labelled_compact_track(START))
        fx.write_compact(prior.archive, fx.labelled_compact_track(START + 86400))
        prior_manifest = prior.freeze()
        damaged = next(row for row in prior_manifest["files"] if row["day"] == DAY)
        (prior.output / "inputs" / "objects" / damaged["object"]).write_bytes(b"damaged")
        current = Corpus(self.root / "current")
        fx.full_day(current.archive, START)
        settle(current.archive)
        manifest = backfill.freeze(
            current.archive,
            current.journal,
            current.metadata,
            current.output,
            include_prior=[prior.output],
            through=DAY,
        )
        self.assertEqual(
            [(item["source"], item["error_type"]) for item in manifest["capture_failures"]],
            [("prior-capture-prior", "ValueError")],
        )
        self.assertEqual({row["day"] for row in manifest["files"]}, {DAY})
        self.assertEqual(manifest["prior_captures"][0]["versions_included"], 0)


class PinnedSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.corpus = Corpus(self.temp.name)
        for day in (START, START + 86400):
            fx.write_compact(self.corpus.archive, fx.labelled_compact_track(day))
        self.corpus.freeze()

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, *arguments, flags=("-B",)):
        return subprocess.run(
            [sys.executable, *flags, str(self.corpus.output / backfill.RUNNER), *arguments],
            capture_output=True,
            text=True,
        )

    def test_the_pinned_runner_resumes_and_completes_while_the_working_tree_refuses(self):
        pin = backfill.pin_source(self.corpus.output)
        source = self.corpus.output / backfill.PINNED_SOURCE
        self.assertEqual(backfill.code_hashes(source), pin["code_sha256"])
        self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((source / "rules.py").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((source / "data").stat().st_mode), 0o700)
        self.assertIn("data/reference-limits-v1.json", pin["code_sha256"])
        self.assertFalse(any(key.startswith("prompts/") for key in pin["code_sha256"]))
        partial = self.runner("--workers", "1", "--max-days", "1")
        self.assertEqual(partial.returncode, 0, partial.stderr)
        self.assertEqual(partial.stdout.count('"completed_day"'), 1)
        self.assertFalse((self.corpus.output / "completion.json").exists())
        process = json.loads((self.corpus.output / "process.json").read_text())
        self.assertEqual((process["workers"], process["max_days"]), (1, 1))
        with self.assertRaisesRegex(ValueError, "pins its analysis code"):
            backfill.run(self.corpus.output, workers=1)
        # Even without -B, neither the runner nor its workers leave bytecode beside the pinned code.
        complete = self.runner("--workers", "2", flags=())
        self.assertEqual(complete.returncode, 0, complete.stderr)
        self.assertEqual(complete.stdout.count('"completed_day"'), 1)
        completion = json.loads((self.corpus.output / "completion.json").read_text())
        self.assertEqual(completion["summary"]["remaining_dates"], 0)
        self.assertEqual(
            json.loads((self.corpus.output / "analysis.json").read_text())["code_sha256"], pin["code_sha256"]
        )
        self.assertFalse(list(source.rglob("__pycache__")))

    def test_the_pinned_summary_comes_from_unedited_pinned_code(self):
        output = self.corpus.output
        backfill.pin_source(output)
        summary, code = backfill.pinned_summary(output)
        self.assertEqual((code, summary["available_dates"]), ("pinned", 2))
        with (output / backfill.PINNED_SOURCE / "rules.py").open("a") as handle:
            handle.write("\n# edited\n")
        with self.assertRaisesRegex(ValueError, "pinned analysis source changed"):
            backfill.pinned_summary(output)

    def test_private_reference_inputs_are_hashed_pinned_and_isolated_from_later_environment(self):
        external = Path(self.temp.name) / "private-references"
        external.mkdir(mode=0o700)
        filename = reference.FILES["limits"][0]
        raw = (Path(reference.__file__).parent / "data" / filename).read_bytes()
        (external / filename).write_bytes(raw)
        (external / "unrelated-private-file.txt").write_text("not an analysis input")
        with patch.object(reference, "DATA", external):
            pin = backfill.pin_source(self.corpus.output)
            self.assertEqual(backfill.code_hashes(), pin["code_sha256"])
        saved = self.corpus.output / backfill.PINNED_SOURCE / "data"
        self.assertEqual((saved / filename).read_bytes(), raw)
        self.assertFalse((saved / "unrelated-private-file.txt").exists())
        self.assertNotIn("data/" + reference.FILES["beacon-codes"][0], pin["code_sha256"])
        (external / filename).write_text("changed after pinning")
        with patch.dict(os.environ, {"WTF_ADSB_REFERENCE_DIR": str(external)}):
            result = self.runner("--workers", "1")
            self.assertEqual(result.returncode, 0, result.stderr)
            summary, origin = backfill.pinned_summary(self.corpus.output)
        self.assertEqual((origin, summary["remaining_dates"]), ("pinned", 0))
        analysis = json.loads((self.corpus.output / "analysis.json").read_text())
        self.assertEqual(analysis["code_sha256"], pin["code_sha256"])

    def test_an_interrupted_pin_can_be_repeated_and_an_edited_runner_is_refused(self):
        # A pin that stopped before its source appeared left only the record behind.
        backfill.write_private(self.corpus.output / backfill.SOURCE_PIN, {"code_sha256": {}})
        backfill.pin_source(self.corpus.output)
        backfill.verify_pinned_source(self.corpus.output)
        runner = self.corpus.output / backfill.RUNNER
        runner.write_text(runner.read_text().replace("verify_pinned_source(ROOT)", "utc()"))
        with self.assertRaisesRegex(ValueError, "never edit"):
            backfill.verify_pinned_source(self.corpus.output)

    def test_edited_pinned_code_and_repeated_or_late_pins_are_refused(self):
        backfill.pin_source(self.corpus.output)
        with self.assertRaisesRegex(ValueError, "already pins"):
            backfill.pin_source(self.corpus.output)
        pinned = self.corpus.output / backfill.PINNED_SOURCE / "rules.py"
        pinned.write_text(pinned.read_text() + "\n# edited\n")
        result = self.runner("--workers", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("never edit analysis-source", result.stderr)
        self.assertFalse((self.corpus.output / "analysis.json").exists())
        other = Corpus(Path(self.temp.name) / "unpinned")
        fx.write_compact(other.archive, fx.labelled_compact_track(START))
        other.freeze()
        backfill.run(other.output, workers=1)
        with self.assertRaisesRegex(ValueError, "Analysis already started"):
            backfill.pin_source(other.output)


class CloneTests(unittest.TestCase):
    def test_a_clone_shares_the_frozen_inputs_and_cutoff_but_not_the_analysis(self):
        with tempfile.TemporaryDirectory() as temp:
            corpus = Corpus(temp)
            fx.write_compact(corpus.archive, fx.labelled_compact_track(START))
            manifest = corpus.freeze()
            first = backfill.run(corpus.output, workers=1)
            before = tree(corpus.output)
            clone = Path(temp) / "clone"
            record = backfill.clone_inputs(corpus.output, clone)
            self.assertEqual(tree(corpus.output), before)
            self.assertEqual(
                (clone / "manifest.json").read_bytes(), (corpus.output / "manifest.json").read_bytes()
            )
            self.assertEqual(record["objects_linked"] + record["objects_copied"], len(manifest["files"]))
            for row in manifest["files"]:
                source = corpus.output / "inputs" / "objects" / row["object"]
                target = clone / "inputs" / "objects" / row["object"]
                if record["objects_linked"]:
                    self.assertEqual(source.stat().st_ino, target.stat().st_ino)
            self.assertFalse((clone / "analysis.json").exists())
            with closing(sqlite3.connect(clone / "corpus.sqlite3")) as database:
                frozen = json.loads(
                    database.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()[0]
                )
                self.assertEqual(database.execute("SELECT COUNT(*) FROM metadata").fetchone()[0], 1)
            self.assertEqual(frozen["analysis_cutoff"], manifest["analysis_cutoff"])
            second = backfill.run(clone, workers=1)
            self.assertEqual(second["findings_by_kind"], first["findings_by_kind"])
            with self.assertRaisesRegex(ValueError, "already exists"):
                backfill.clone_inputs(corpus.output, clone)
            with self.assertRaisesRegex(ValueError, "separate directory"):
                backfill.clone_inputs(corpus.output, corpus.output / "nested")


class ReadOnlyTests(unittest.TestCase):
    def test_root_never_reads_a_journal_with_wal_content(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp) / "journal.sqlite3"
            journal.write_bytes(b"")
            Path(str(journal) + "-wal").write_bytes(b"frames")
            self.assertTrue(backfill.read_only_uri(journal).endswith("?mode=ro"))
            with patch.object(os, "geteuid", return_value=0):
                with self.assertRaisesRegex(ValueError, "backup copy"):
                    backfill.read_only_uri(journal)
            Path(str(journal) + "-wal").write_bytes(b"")
            self.assertTrue(backfill.read_only_uri(journal).endswith("immutable=1"))


class WalTests(unittest.TestCase):
    def test_a_wal_left_by_a_reader_is_checkpointed_for_settled_readers(self):
        with tempfile.TemporaryDirectory() as temp:
            corpus = Corpus(temp)
            fx.write_compact(corpus.archive, fx.labelled_compact_track(START))
            corpus.freeze()
            path = corpus.output / "corpus.sqlite3"
            with closing(sqlite3.connect(backfill.shared_uri(path), uri=True)) as reader:
                reader.execute("SELECT COUNT(*) FROM meta").fetchone()
                with backfill.connect(path) as writer:
                    writer.execute("INSERT OR REPLACE INTO meta VALUES ('probe','1')")
                # The writer closed while a reader stayed connected: the WAL keeps its frames.
                with self.assertRaisesRegex(ValueError, "checkpoint"):
                    backfill.read_only_uri(path, allow_wal=False)
                self.assertTrue(backfill.checkpoint(corpus.output))
            self.assertTrue(backfill.read_only_uri(path, allow_wal=False).endswith("immutable=1"))
