"""Standalone archive entry points and private output boundaries."""

import contextlib
import gzip
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import analysis_archive_fixtures as fx

from wtf_adsb_engine.analysis import (
    app,
    archive_paths,
    backfill_history,
    backfill_metrics,
    history_backfill,
    history_metrics,
    import_replay,
)

START = history_backfill.day_stamp("2026-06-10")


class ArchiveCommandTests(unittest.TestCase):
    def invoke(self, entrypoint, arguments):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                entrypoint(arguments)
            except SystemExit as error:
                return error.code, output.getvalue(), errors.getvalue()
        return 0, output.getvalue(), errors.getvalue()

    def freeze_args(self, corpus):
        return [
            "--output",
            str(corpus.output),
            "freeze",
            "--root",
            str(corpus.archive),
            "--journal",
            str(corpus.journal),
            "--metadata",
            str(corpus.metadata),
            "--no-watchlist",
        ]

    def test_freeze_run_status_and_checkpoint_without_a_project_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            corpus = fx.Corpus(temp)
            fx.write_compact(corpus.archive, fx.labelled_compact_track(START))
            before = corpus.journal.read_bytes()
            code, output, errors = self.invoke(backfill_history.main, self.freeze_args(corpus))
            self.assertEqual(code, 0, errors)
            self.assertEqual(json.loads(output)["unique_input_versions"], 1)
            self.assertEqual(stat.S_IMODE(corpus.output.stat().st_mode), 0o700)
            for path in corpus.output.rglob("*"):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
            code, _, errors = self.invoke(
                backfill_history.main, ["--output", str(corpus.output), "run", "--workers", "1"]
            )
            self.assertEqual(code, 0, errors)
            code, output, errors = self.invoke(
                backfill_history.main, ["--output", str(corpus.output), "status"]
            )
            self.assertEqual(code, 0, errors)
            self.assertEqual(json.loads(output)["remaining_dates"], 0)
            code, output, errors = self.invoke(
                backfill_history.main, ["--output", str(corpus.output), "checkpoint"]
            )
            self.assertEqual(code, 0, errors)
            self.assertTrue(json.loads(output)["wal_checkpointed"])
            self.assertEqual(corpus.journal.read_bytes(), before)

    def test_live_journal_and_its_hard_link_are_refused_before_output_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            corpus = fx.Corpus(temp)
            linked = Path(temp) / "hard-link.sqlite3"
            os.link(corpus.journal, linked)
            for known_live in (corpus.journal, linked):
                code, _, errors = self.invoke(
                    backfill_history.main,
                    [*self.freeze_args(corpus), "--live-journal", str(known_live)],
                )
                self.assertNotEqual(code, 0)
                self.assertIn("backup copy", errors)
                self.assertFalse(corpus.output.exists())

    def test_status_uses_verified_pinned_code_and_refuses_an_edited_pin(self):
        with tempfile.TemporaryDirectory() as temp:
            corpus = fx.Corpus(temp)
            fx.write_compact(corpus.archive, fx.labelled_compact_track(START))
            corpus.freeze()
            history_backfill.pin_source(corpus.output)
            runner = subprocess.run(
                [sys.executable, "-I", str(corpus.output / history_backfill.RUNNER), "--workers", "1"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(runner.returncode, 0, runner.stderr)
            arguments = ["--output", str(corpus.output), "status"]
            with patch.object(
                history_backfill, "summary", side_effect=AssertionError("current summary used")
            ):
                code, output, errors = self.invoke(backfill_history.main, arguments)
            self.assertEqual(code, 0, errors)
            self.assertEqual(json.loads(output)["remaining_dates"], 0)
            pinned_rule = corpus.output / history_backfill.PINNED_SOURCE / "rules.py"
            pinned_rule.write_text(pinned_rule.read_text() + "\n# modified pinned rule\n")
            code, _, errors = self.invoke(backfill_history.main, arguments)
            self.assertNotEqual(code, 0)
            self.assertIn("pinned analysis source changed", errors)

    def test_metrics_capture_and_offline_analysis_have_explicit_endpoint_and_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            corpus = fx.Corpus(temp)
            corpus.freeze()
            output = Path(temp) / "metrics"
            payload = {"status": "success", "data": {"resultType": "matrix", "result": []}}
            arguments = [
                "--output",
                str(output),
                "freeze",
                "--start",
                history_backfill.utc(START),
                "--end",
                history_backfill.utc(START + 3600),
            ]
            code, _, errors = self.invoke(backfill_metrics.main, arguments)
            self.assertNotEqual(code, 0)
            self.assertIn("--endpoint", errors)
            self.assertFalse(output.exists())
            with patch.object(
                history_metrics, "open_local", return_value=io.BytesIO(json.dumps(payload).encode())
            ):
                code, _, errors = self.invoke(
                    backfill_metrics.main, [*arguments, "--endpoint", "http://127.0.0.1:9090/prometheus"]
                )
            self.assertEqual(code, 0, errors)
            with patch.object(
                history_metrics, "open_local", side_effect=AssertionError("unexpected network")
            ):
                code, _, errors = self.invoke(
                    backfill_metrics.main,
                    ["--output", str(output), "analyze", "--corpus", str(corpus.output)],
                )
            self.assertEqual(code, 0, errors)
            self.assertTrue((output / "findings.json").is_file())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)

    def test_replay_import_is_idempotent_and_refuses_active_and_missing_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive, metadata, database = root / "archive", root / "metadata.csv.gz", root / "backup.sqlite3"
            fx.write_compact(archive, fx.labelled_compact_track(START))
            metadata.write_bytes(gzip.compress(b"a00a01;N0TEST;A388;0000;Synthetic airliner;2020\n"))
            arguments = ["--root", str(archive), "--metadata", str(metadata), "--database", str(database)]
            code, _, errors = self.invoke(import_replay.main, arguments)
            self.assertNotEqual(code, 0)
            self.assertIn("existing SQLite backup", errors)
            self.assertFalse(database.exists())
            app.initialize(database)
            before = database.read_bytes()
            hard_link = root / "known-live.sqlite3"
            os.link(database, hard_link)
            code, _, errors = self.invoke(import_replay.main, [*arguments, "--live-journal", str(hard_link)])
            self.assertNotEqual(code, 0)
            self.assertIn("separate SQLite backup", errors)
            self.assertEqual(database.read_bytes(), before)
            for attempt in range(2):
                code, _, errors = self.invoke(import_replay.main, arguments)
                self.assertEqual(code, 0, errors)
                with app.connect(database) as db:
                    actual = (
                        db.execute("SELECT SUM(hits) FROM events").fetchone()[0],
                        db.execute("SELECT SUM(seconds) FROM coverage").fetchone()[0],
                        db.execute("SELECT COUNT(*) FROM replay_imports").fetchone()[0],
                    )
                if attempt == 0:
                    expected = actual
                else:
                    self.assertEqual(actual, expected)
            self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)

    def test_module_help_works_from_an_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            for name in ("backfill_history", "backfill_metrics", "import_replay"):
                result = subprocess.run(
                    [sys.executable, "-I", "-m", "wtf_adsb_engine.analysis." + name, "--help"],
                    cwd=temp,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)
                self.assertNotIn("/opt/", result.stdout)


class ArchivePathTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git"), "Git boundary check requires git")
    def test_git_outputs_must_be_ignored_and_cannot_contain_tracked_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".gitignore").write_text(".private/\n")
            with self.assertRaisesRegex(ValueError, "outside a Git worktree"):
                archive_paths.destination(root / "unignored" / "archive")
            self.assertEqual(
                archive_paths.destination(root / ".private" / "archive"), root / ".private" / "archive"
            )
            private = root / ".private"
            private.mkdir(mode=0o700)
            tracked = private / "accidentally-tracked.txt"
            tracked.write_text("synthetic")
            subprocess.run(["git", "-C", str(root), "add", "-f", str(tracked)], check=True)
            with self.assertRaisesRegex(ValueError, "outside a Git worktree"):
                archive_paths.destination(private)

    def test_import_does_not_change_shared_directory_permissions(self):
        with tempfile.TemporaryDirectory() as temp:
            shared = Path(temp) / "shared"
            shared.mkdir(mode=0o755)
            shared.chmod(0o755)
            backup = shared / "backup.sqlite3"
            backup.write_bytes(b"synthetic")
            with self.assertRaisesRegex(ValueError, "mode 0700"):
                archive_paths.journal_backup(backup)
            self.assertEqual(stat.S_IMODE(shared.stat().st_mode), 0o755)

    def test_symlink_output_is_refused_and_umask_is_restored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            alias = root / "alias"
            alias.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                archive_paths.destination(alias)
            original = os.umask(0o022)
            try:
                with archive_paths.private_umask():
                    private = root / "private"
                    private.mkdir()
                public = root / "public"
                public.mkdir()
                self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(public.stat().st_mode), 0o755)
            finally:
                os.umask(original)
