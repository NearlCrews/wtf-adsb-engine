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


class ForeignMilitary(unittest.TestCase):
    # The packaged tests carry no identity tables, so the allocation lookups stand in for
    # them: 3f4a21 in a German block, 43c123 in a United Kingdom military range, and
    # ae1234 in the US military range.
    ALLOCATIONS = {
        "3f4a21": {"country": "DE", "name": "Germany"},
        "43c123": {"country": "GB", "name": "United Kingdom"},
        "ae1234": {"country": "US", "name": "United States"},
    }
    MILITARY = frozenset({"43c123", "ae1234"})
    generations = iter(range(-100, -200, -1))

    def setUp(self):
        self.addCleanup(reference.clear_cache)
        for patcher in (
            # A fresh generation keeps the memoized identity checks apart from other tests.
            patch.object(reference, "GENERATION", next(self.generations)),
            patch.object(reference, "allocation", side_effect=self.ALLOCATIONS.get),
            patch.object(reference, "military_allocation", side_effect=self.MILITARY.__contains__),
            patch.object(reference, "country_group", side_effect=lambda code: frozenset({code.lower()})),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def kinds_of(self, aircraft):
        return {f.kind: f for f in rules.candidates(aircraft, 1_700_000_000.0)}

    def test_military_flag_with_a_german_address_fires(self):
        aircraft = {
            "hex": "3f4a21",
            "type": "adsb_icao",
            "r": "54+12",
            "t": "A400",
            "dbFlags": 1,
            "lat": 42.5,
            "lon": -83.4,
            "alt_baro": 24000,
            "gs": 300,
        }
        found = self.kinds_of(aircraft)
        self.assertIn("foreign_military", found)
        self.assertIn("military", found)
        block = found["foreign_military"].evidence["foreign_military"]
        self.assertEqual(block["country"], "DE")
        self.assertEqual(block["country_name"], "Germany")
        self.assertEqual(block["basis"], ["dbFlags"])
        self.assertEqual(found["foreign_military"].score, 80)

    def test_us_military_does_not_fire(self):
        aircraft = {
            "hex": "ae1234",
            "type": "adsb_icao",
            "r": "98-0001",
            "t": "C32",
            "dbFlags": 1,
            "lat": 42.5,
            "lon": -83.4,
            "alt_baro": 30000,
            "gs": 450,
        }
        self.assertNotIn("foreign_military", self.kinds_of(aircraft))

    def test_military_allocation_without_flag_fires_with_that_basis(self):
        aircraft = {
            "hex": "43c123",
            "type": "adsb_icao",
            "r": "ZZ123",
            "t": "A332",
            "dbFlags": 0,
            "lat": 42.5,
            "lon": -83.4,
            "alt_baro": 30000,
            "gs": 450,
        }
        found = self.kinds_of(aircraft)
        block = found["foreign_military"].evidence["foreign_military"]
        self.assertEqual(block["basis"], ["military_allocation"])
        self.assertEqual(block["country_name"], "United Kingdom")

    def test_civil_address_without_flag_or_allocation_does_not_fire(self):
        aircraft = {"hex": "3f4a21", "type": "adsb_icao", "r": "D-AIMA", "t": "A388", "dbFlags": 0}
        self.assertNotIn("foreign_military", self.kinds_of(aircraft))


class UnregisteredAddress(unittest.TestCase):
    def kinds_of(self, aircraft):
        return {f.kind: f for f in rules.supplementary_candidates(aircraft, 1_700_000_000.0)}

    def test_direct_adsb_without_registration_fires(self):
        aircraft = {
            "hex": "a1b2c3",
            "type": "adsb_icao",
            "r": "",
            "t": "",
            "flight": "TEST01",
            "category": "A2",
            "lat": 42.5,
            "lon": -83.4,
            "seen_pos": 1.0,
            "alt_baro": 10000,
            "gs": 250,
        }
        found = self.kinds_of(aircraft)
        self.assertIn("unregistered_address", found)
        self.assertEqual(
            found["unregistered_address"].evidence["unregistered"],
            {"callsign": "TEST01", "category": "A2", "source": "adsb_icao"},
        )
        self.assertIn("registry_gap", found["unregistered_address"].evidence["context_tags"])

    def test_placeholder_registration_counts_as_missing(self):
        aircraft = {
            "hex": "a1b2c3",
            "type": "adsb_icao",
            "r": "UNKNOWN",
            "lat": 42.5,
            "lon": -83.4,
            "seen_pos": 1.0,
            "alt_baro": 10000,
        }
        found = self.kinds_of(aircraft)
        self.assertIn("unregistered_address", found)
        self.assertEqual(
            found["unregistered_address"].evidence["unregistered"],
            {"callsign": None, "category": None, "source": "adsb_icao"},
        )

    def test_conflict_tags_merge_with_the_registry_gap(self):
        aircraft = {"hex": "a1b2c3", "type": "adsb_icao", "r": "", "lat": 42.5, "lon": -83.4, "seen_pos": 1.0}
        conflict = dict(rules.UNCOMPUTED_IDENTITY) | {"conflict": True}
        found = {f.kind: f for f in rules.supplementary_candidates(aircraft, 0.0, identity=conflict)}
        self.assertEqual(
            found["unregistered_address"].evidence["context_tags"],
            ["metadata_identity_conflict", "registry_gap"],
        )

    def test_an_uncorroborated_identity_does_not_block_the_finding(self):
        aircraft = {
            "hex": "a1b2c3",
            "type": "adsb_icao",
            "r": "",
            "dbFlags": 1,
            "lat": 42.5,
            "lon": -83.4,
            "seen_pos": 1.0,
        }
        loaded = reference.dataset
        checks = dict(rules.UNCOMPUTED_IDENTITY)
        self.addCleanup(reference.clear_cache)
        # With the identity tables loaded and nothing corroborating the identity, _annotate
        # flags identity-led kinds; an address with no registration is the finding itself.
        with (
            patch.object(
                reference, "dataset", side_effect=lambda name: {} if name == "identity" else loaded(name)
            ),
            patch.object(reference, "military_allocation", return_value=False),
            patch.object(reference, "allocation", return_value=None),
            patch.object(rules, "identity_corroborated", return_value=False),
        ):
            unlisted = {
                f.kind: f for f in rules.supplementary_candidates(aircraft, 1_700_000_000.0, identity=checks)
            }["unregistered_address"]
            military = {f.kind: f for f in rules.candidates(aircraft, 1_700_000_000.0, identity=checks)}[
                "military"
            ]
        self.assertNotIn("unregistered_address", rules.IDENTITY_LED_KINDS)
        self.assertNotIn("identity_unverified", unlisted.evidence.get("quality_flags", []))
        self.assertIn("identity_unverified", military.evidence["quality_flags"])

    def test_mlat_military_or_registered_do_not_fire(self):
        base = {
            "hex": "a1b2c3",
            "type": "adsb_icao",
            "r": "",
            "lat": 42.5,
            "lon": -83.4,
            "seen_pos": 1.0,
            "alt_baro": 10000,
        }
        self.assertIn("unregistered_address", self.kinds_of(base))
        self.assertNotIn("unregistered_address", self.kinds_of({**base, "type": "mlat"}))
        self.assertNotIn("unregistered_address", self.kinds_of({**base, "r": "N123AB"}))
        self.assertNotIn("unregistered_address", self.kinds_of({**base, "hex": "~a1b2c3"}))
        self.assertNotIn("unregistered_address", self.kinds_of({**base, "lat": None, "lon": None}))
        self.assertNotIn("unregistered_address", self.kinds_of({**base, "seen_pos": 60.0}))
        # The packaged tests carry no identity tables; the lookup stands in for the US military range.
        with patch.object(reference, "military_allocation", side_effect={"ae1234"}.__contains__):
            self.assertNotIn("unregistered_address", self.kinds_of({**base, "hex": "ae1234"}))
