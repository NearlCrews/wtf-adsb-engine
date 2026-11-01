"""Type speed norms: a population baseline from performance_samples, never a limit, exported per finding."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from wtf_adsb_engine import wire
from wtf_adsb_engine.analysis import app, curation, export, rules, selection

# The journal's own sample table (selection.initialize), filled in this column order.
SAMPLE = (
    "INSERT INTO performance_samples (typecode,phase,altitude_band,hex,encounter,values_json,updated_at) "
    "VALUES (?,?,?,?,?,?,?)"
)
NOW = 1_791_150_000
DAY = 86400
# Every fixture group is quiet by then (selection.QUIET_SECONDS).
QUIET = NOW + 30000


def journal(addresses=25, days=20):
    db = sqlite3.connect(":memory:")
    selection.initialize(db)
    base = 1_760_000_000
    for n in range(addresses):
        day = n % days
        speed = 380 + n * 2  # medians from 380 to 428
        db.execute(
            SAMPLE,
            ("C560", "level", 3, f"a{n:05x}", base + day * DAY, json.dumps([speed] * 5), base + day * DAY),
        )
    return db


class TypeNorms(unittest.TestCase):
    """Ported from the live project's tests/test_type_norms.py (selection.type_norm)."""

    def test_percentiles_round_to_five_knots_and_name_the_band(self):
        with closing(journal()) as db:
            contexts = selection.norm_contexts(db)
        norm = selection.type_norm(contexts, "C560", ("level", 3), "zzzzzz")
        # 25 equally weighted medians 380..428: the 10th percentile is the 3rd (384), the 90th the 23rd (424).
        self.assertEqual(norm["low"], 385)
        self.assertEqual(norm["high"], 425)
        self.assertEqual(
            (norm["phase"], norm["band_floor_ft"], norm["band_ceiling_ft"], norm["window_days"]),
            ("level", 30000, 40000, 30),
        )
        self.assertEqual(norm["addresses"], 25)
        self.assertEqual(norm["days"], 20)

    def test_the_lowest_band_starts_where_the_samples_do(self):
        with closing(journal()) as db:
            entries = selection.norm_contexts(db)[("C560", "level", 3)]
        norm = selection.type_norm({("C560", "level", 0): entries}, "C560", ("level", 0), "zzzzzz")
        self.assertEqual((norm["band_floor_ft"], norm["band_ceiling_ft"]), (5000, 10000))

    def test_thin_contexts_and_the_own_address_are_left_out(self):
        with closing(journal(addresses=19)) as db:
            self.assertIsNone(
                selection.type_norm(selection.norm_contexts(db), "C560", ("level", 3), "zzzzzz")
            )
        with closing(journal(days=10)) as db:
            self.assertIsNone(
                selection.type_norm(selection.norm_contexts(db), "C560", ("level", 3), "zzzzzz")
            )
        with closing(journal(addresses=20)) as db:
            contexts = selection.norm_contexts(db)
        self.assertIsNotNone(selection.type_norm(contexts, "C560", ("level", 3), "zzzzzz"))
        self.assertIsNone(selection.type_norm(contexts, "C560", ("level", 3), "a00000"))
        self.assertIsNone(selection.type_norm(contexts, "C560", ("climb", 3), "zzzzzz"))
        self.assertIsNone(selection.type_norm(contexts, None, ("level", 3), "zzzzzz"))
        self.assertIsNone(selection.type_norm(contexts, "C560", None, "zzzzzz"))

    def test_an_address_with_many_encounters_weighs_as_one(self):
        with closing(journal()) as db:
            # Forty more fast encounters at one address must not drag the high end up.
            for repeat in range(40):
                db.execute(
                    SAMPLE,
                    (
                        "C560",
                        "level",
                        3,
                        "a00000",
                        1_760_000_000 + repeat + 1,
                        json.dumps([600] * 5),
                        1_760_000_000,
                    ),
                )
            norm = selection.type_norm(selection.norm_contexts(db), "C560", ("level", 3), "zzzzzz")
        self.assertEqual((norm["low"], norm["high"], norm["addresses"]), (385, 425, 25))

    def test_no_sample_table_gives_no_contexts(self):
        with closing(sqlite3.connect(":memory:")) as db:
            self.assertEqual(selection.norm_contexts(db), {})


def record(db, address, kind, typecode, altitude, rate, value=640, at=NOW):
    aircraft = {"hex": address, "r": "N1", "t": typecode, "type": "adsb_icao", "alt_baro": altitude}
    aircraft.update(baro_rate=rate, gs=value)
    finding = rules.Finding(kind, "performance", "", value, "kn", "", "", 80)
    return app.record(db, aircraft, finding, at, at + 60, hits=6, extra={"encounter_start": at})


def samples(db, typecode, phase, band, speeds, *, days, end=NOW, prefix="b"):
    for n, speed in enumerate(speeds):
        stamp = end - (n % days) * DAY
        db.execute(
            SAMPLE,
            (typecode, phase, band, f"{prefix}{n:05x}", stamp, json.dumps([speed] * 5), stamp),
        )


def state(result):
    """Observations by key (restored replacements come last) and the retractions."""
    return {row["key"]: row["observation"] for row in result["observations"]}, result["retractions"]


class ExportedNorms(unittest.TestCase):
    """context.type_norms (contract 1.1.0): the norm autopilot.collect derives, per norm-kind finding."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "journal.sqlite3"
        self.sidecar = Path(self.directory.name) / "identities.sqlite3"
        app.initialize(self.path)
        with app.connect(self.path) as db:
            # Two norm kinds over two types, a type without samples, and a kind without a norm.
            record(db, "abcdef", "seasonal_speed_record", "B738", 35000, 0)
            record(db, "4ca123", "type_speed_mismatch", "A320", 25000, 1500, value=380, at=NOW + 7200)
            record(db, "a0c560", "type_speed_mismatch", "C560", 35000, 0, value=500, at=NOW + 14400)
            vintage = rules.Finding("vintage", "airframes", "", 70, "years", "", "", 80)
            aircraft = {"hex": "a0b52b", "r": "N2", "t": "B52", "type": "adsb_icao"}
            app.record(
                db, aircraft, vintage, NOW + 21600, NOW + 21660, extra={"encounter_start": NOW + 21600}
            )
            # The story's own address is left out of its norm, however fast its samples.
            samples(db, "B738", "level", 3, [420 + n * 2 for n in range(25)], days=20)
            db.execute(SAMPLE, ("B738", "level", 3, "abcdef", NOW, json.dumps([900] * 5), NOW))
            samples(db, "A320", "climb", 2, [300 + n * 3 for n in range(22)], days=15, prefix="c")

    def norms(self, now=QUIET, identity_path=None):
        rows = export.snapshot_state(self.path, now, identity_path=identity_path)["observations"]
        return {
            event["kind"] + ":" + event["evidence"]["aircraft"]["t"]: (
                event["id"],
                row["observation"]["context"].get("type_norms"),
            )
            for row in rows
            for event in row["observation"]["group"]["events"]
        }

    def state(self):
        return state(export.snapshot_state(self.path, QUIET, identity_path=self.sidecar))

    def test_each_norm_kind_finding_gets_its_own_types_norm_and_nothing_else_does(self):
        found = self.norms()
        identifier, norms = found["seasonal_speed_record:B738"]
        self.assertEqual(
            norms,
            {
                identifier: {
                    "low": 425,
                    "high": 465,
                    "phase": "level",
                    "band_floor_ft": 30000,
                    "band_ceiling_ft": 40000,
                    "addresses": 25,
                    "days": 20,
                    "window_days": 30,
                }
            },
        )
        identifier, norms = found["type_speed_mismatch:A320"]
        with closing(sqlite3.connect(self.path)) as db:
            expected = selection.type_norm(selection.norm_contexts(db), "A320", ("climb", 2), "4ca123")
        self.assertEqual(expected["phase"], "climb")
        self.assertEqual(norms, {identifier: expected})
        # Too few addresses: the entry is present and null.
        identifier, norms = found["type_speed_mismatch:C560"]
        self.assertEqual(norms, {identifier: None})
        # A group without a norm-kind finding carries no type_norms, as under 1.0.0.
        self.assertIsNone(found["vintage:B52"][1])
        self.assertEqual(curation.NORM_KINDS, frozenset({"seasonal_speed_record", "type_speed_mismatch"}))

    def test_exported_norms_pass_the_structural_and_semantic_contract(self):
        for row in export.snapshot_state(self.path, QUIET, identity_path=self.sidecar)["observations"]:
            wire.validate_observation(row["observation"])
        observation = row["observation"]
        stray = {**observation, "context": {**observation["context"], "type_norms": {"0" * 8: None}}}
        with self.assertRaises(ValueError):
            wire.validate_observation(stray)
        missing = "00000000-0000-4000-8000-0000000000ff"
        stray = {**observation, "context": {**observation["context"], "type_norms": {missing: None}}}
        with self.assertRaisesRegex(ValueError, "names no finding"):
            wire.validate_observation(stray)

    def move_window(self):
        # Forty days later the old samples leave the 30-day window and faster ones replace them.
        with app.connect(self.path) as db:
            samples(
                db,
                "B738",
                "level",
                3,
                [500 + n * 2 for n in range(25)],
                days=20,
                end=NOW + 40 * DAY,
                prefix="d",
            )

    def test_a_quiet_group_keeps_its_norm_while_the_window_moves(self):
        before = export.snapshot_state(self.path, QUIET, identity_path=self.sidecar)["observations"]
        self.move_window()
        later = NOW + 40 * DAY
        after = export.snapshot_state(self.path, later, identity_path=self.sidecar)["observations"]
        self.assertEqual(before, after)
        # The window did move: without the sidecar the same export computes a new norm.
        unsaved, fresh = self.norms(later)["seasonal_speed_record:B738"]
        saved, kept = self.norms(later, self.sidecar)["seasonal_speed_record:B738"]
        self.assertEqual((fresh[unsaved]["low"], kept[saved]["low"]), (505, 425))

    def test_an_active_group_follows_the_window(self):
        active = NOW + 100
        _, first = self.norms(active, self.sidecar)["seasonal_speed_record:B738"]
        self.move_window()
        identifier, second = self.norms(active, self.sidecar)["seasonal_speed_record:B738"]
        self.assertEqual((first[identifier]["low"], second[identifier]["low"]), (425, 505))
        # Once quiet, it keeps the value it last sent.
        _, quiet = self.norms(NOW + 40 * DAY, self.sidecar)["seasonal_speed_record:B738"]
        self.assertEqual(quiet, second)

    def test_a_changed_type_is_not_given_the_saved_norm(self):
        self.norms(QUIET, self.sidecar)
        with app.connect(self.path) as db:
            db.execute(
                "UPDATE events SET evidence=json_set(evidence,'$.aircraft.t','A320','$.aircraft.alt_baro',25000,"
                "'$.aircraft.baro_rate',1500) WHERE hex='abcdef'"
            )
        identifier, norms = self.norms(QUIET, self.sidecar)["seasonal_speed_record:A320"]
        self.assertEqual(norms[identifier]["phase"], "climb")

    def test_a_restored_replacement_keeps_its_norms(self):
        # Split the B738 record from a second norm finding in its encounter, then lose the source rows.
        with app.connect(self.path) as db:
            record(db, "abcdef", "type_speed_mismatch", "B738", 35000, 0, value=560)
        merged = export.snapshot_state(self.path, QUIET, identity_path=self.sidecar)
        group = next(row for row in merged["observations"] if len(row["observation"]["group"]["events"]) == 2)
        self.assertEqual(len(group["observation"]["context"]["type_norms"]), 2)
        with app.connect(self.path) as db:
            db.execute(
                "UPDATE events SET evidence=json_set(evidence,'$.encounter_start',?) "
                "WHERE hex='abcdef' AND kind='type_speed_mismatch'",
                (NOW + 10,),
            )
        split = export.snapshot_state(self.path, QUIET, identity_path=self.sidecar)
        self.assertEqual([row["key"] for row in split["retractions"]], [group["key"]])
        children = state(split)[0]
        for key in split["retractions"][0]["replacement_keys"]:
            self.assertEqual(len(children[key]["context"]["type_norms"]), 1)
        with app.connect(self.path) as db:
            db.execute("DELETE FROM events WHERE hex='abcdef'")
        self.assertEqual(self.state(), state(split))
        # A payload saved before norms existed takes them from its own evidence.
        with closing(sqlite3.connect(self.sidecar)) as db, db:
            for key, payload in db.execute(
                "SELECT group_id,observation FROM replacement_payloads"
            ).fetchall():
                observation = json.loads(payload)
                observation["context"].pop("type_norms", None)
                db.execute(
                    "UPDATE replacement_payloads SET observation=? WHERE group_id=?",
                    (json.dumps(observation), key),
                )
        self.assertEqual(self.state(), state(split))


if __name__ == "__main__":
    unittest.main()
