"""Unchanged identity snapshots stay read-only after structural reconciliation."""

import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_analysis_export import sample_source

from wtf_adsb_engine.analysis.identity import IdentityStore


class IdentityWriteTests(unittest.TestCase):
    def test_noop_snapshot_does_not_rewrite_identity_or_cached_replacement_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identities.sqlite3"
            first = sample_source()
            second = {**copy.deepcopy(first), "id": first["id"] + 1}

            def snapshot(merge):
                with IdentityStore(path, "receiver") as identities:
                    before = identities.db.total_changes
                    findings = [identities.finding(source) for source in (first, second)]
                    groups = (
                        [("merged", findings)]
                        if merge
                        else [("first", [findings[0]]), ("second", [findings[1]])]
                    )
                    assigned = identities.assign_groups(groups)
                    rows = [
                        {"key": group_id, "observation": {"group": {"id": group_id}, "members": members}}
                        for group_id, (_anchor, members) in zip(assigned, groups, strict=True)
                    ]
                    identities.remember_replacements(rows)
                    result = (findings, assigned, identities.retractions())
                    writes = identities.db.total_changes - before
                return result, writes

            snapshot(False)
            merged, writes = snapshot(True)
            self.assertGreater(writes, 0)
            self.assertEqual(len(merged[2]), 2)
            for _ in range(2):
                repeated, writes = snapshot(True)
                self.assertEqual(repeated, merged)
                self.assertEqual(writes, 0)


class IdentityLifecycleTests(unittest.TestCase):
    def assert_closed_and_unlocked(self, db, path):
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")
        other = sqlite3.connect(path, timeout=0.01)
        try:
            other.execute("BEGIN IMMEDIATE")
        finally:
            other.close()

    def test_constructor_failure_closes_the_new_connection(self):
        for stage in ("schema", "chmod"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "identities.sqlite3"
                db = sqlite3.connect(path)
                if stage == "schema":
                    db.set_authorizer(
                        lambda action, *_: (
                            sqlite3.SQLITE_DENY
                            if action == sqlite3.SQLITE_CREATE_TABLE
                            else sqlite3.SQLITE_OK
                        )
                    )
                with (
                    patch("wtf_adsb_engine.analysis.identity.sqlite3.connect", return_value=db),
                    patch.object(Path, "chmod", side_effect=PermissionError("private mode unavailable")),
                    self.assertRaises((sqlite3.DatabaseError, PermissionError)),
                ):
                    IdentityStore(path, "receiver")
                self.assert_closed_and_unlocked(db, path)

    def test_failed_entry_closes_connection_and_releases_writer_lock(self):
        for stage in ("begin", "retired"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "identities.sqlite3"
                identities = IdentityStore(path, "receiver")
                if stage == "begin":
                    identities.db.execute("BEGIN IMMEDIATE")
                else:
                    identities.db.execute(
                        "INSERT INTO supersessions VALUES (?,?,?)", ("receiver", "old", "invalid JSON")
                    )
                    identities.db.commit()
                with self.assertRaises((sqlite3.OperationalError, ValueError)):
                    with identities:
                        self.fail("Entry should fail before the body runs")
                self.assert_closed_and_unlocked(identities.db, path)
