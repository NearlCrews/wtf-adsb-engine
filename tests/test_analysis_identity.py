"""Unchanged identity snapshots stay read-only after structural reconciliation."""

import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_analysis_export import sample_source

from wtf_adsb_engine.analysis import reference, rules
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


class FirstOperatorHere(unittest.TestCase):
    # a05f2d is the address the US algorithm assigns to N123DL, so the identity checks agree.
    AIRCRAFT = {
        "hex": "a05f2d",
        "type": "adsb_icao",
        "r": "N123DL",
        "t": "A321",
        "flight": "DAL1234",
        "category": "A3",
        "lat": 42.5,
        "lon": -83.4,
    }

    def history(self, **over):
        base = {
            "coverage_days": 45,
            "first_day": "2026-09-01",
            "type_days": 12,
            "other_type_days": 0,
            "country_days": 40,
            "operator_days": 0,
        }
        return {**base, **over}

    def kinds_of(self, aircraft, history):
        return [f.kind for f in rules.first_sighting(aircraft, history)]

    def test_first_operator_fires_with_a_known_designator_and_no_prior_days(self):
        findings = rules.first_sighting(self.AIRCRAFT, self.history())
        found = next(f for f in findings if f.kind == "first_operator_here")
        self.assertEqual(found.evidence["first_sighting"]["scope"], "operator")
        self.assertEqual(found.evidence["first_sighting"]["designator"], "DAL")
        self.assertEqual(found.evidence["first_sighting"]["operator"], "Delta Air Lines")
        self.assertEqual(found.score, 76)

    def test_no_finding_without_coverage_or_with_prior_days(self):
        self.assertNotIn("first_operator_here", self.kinds_of(self.AIRCRAFT, self.history(coverage_days=29)))
        self.assertNotIn("first_operator_here", self.kinds_of(self.AIRCRAFT, self.history(operator_days=1)))
        self.assertNotIn(
            "first_operator_here", self.kinds_of(self.AIRCRAFT, self.history(operator_days=None))
        )

    def test_no_finding_for_unknown_designator_n_number_or_light_type(self):
        self.assertNotIn(
            "first_operator_here", self.kinds_of({**self.AIRCRAFT, "flight": "ZZQ12"}, self.history())
        )
        self.assertNotIn(
            "first_operator_here", self.kinds_of({**self.AIRCRAFT, "flight": "N123DL"}, self.history())
        )
        light = {**self.AIRCRAFT, "t": "C172"}
        self.assertIn("first_operator_here", self.kinds_of(light, self.history()))
        # The packaged tests carry no type or identity tables, so a piston type and a known
        # designator stand in for them; a fresh generation keeps the memoized checks apart.
        with (
            patch.object(reference, "GENERATION", -1),
            patch.object(reference, "type_info", return_value={"engine_type": "P"}),
            patch.object(reference, "operator_designator", return_value=True),
        ):
            self.assertIs(rules.identity_checks(light)["airline_callsign_on_light_type"], True)
            self.assertNotIn("first_operator_here", self.kinds_of(light, self.history()))
