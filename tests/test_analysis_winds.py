"""Day-level winds aloft from eastbound and westbound ground speeds."""

import json
import sqlite3
import unittest

from wtf_adsb_engine.analysis import wind, winds


def report(hex_, track, gs, *, alt=35000, rate=0, source="adsb_icao"):
    return {
        "hex": hex_,
        "type": source,
        "track": track,
        "gs": gs,
        "alt_baro": alt,
        "baro_rate": rate,
        "lat": 42.5,
        "lon": -83.4,
    }


class Qualifies(unittest.TestCase):
    def test_direction_and_filters(self):
        self.assertEqual(winds.qualifies(report("a", 90, 500)), "east")
        self.assertEqual(winds.qualifies(report("a", 270, 400)), "west")
        self.assertIsNone(winds.qualifies(report("a", 0, 500)))
        self.assertIsNone(winds.qualifies(report("a", 90, 500, alt=27000)))
        self.assertIsNone(winds.qualifies(report("a", 90, 500, rate=-1500)))
        self.assertIsNone(winds.qualifies(report("a", 90, 250)))
        self.assertIsNone(winds.qualifies(report("a", 90, 500, source="mlat")))
        self.assertIsNone(winds.qualifies({**report("a", 90, 500), "baro_rate": None}))
        # A ground speed the report's MLAT or TIS-B solution supplied is not direct.
        self.assertIsNone(winds.qualifies({**report("a", 90, 500), "mlat": ["gs"]}))
        self.assertIsNone(winds.qualifies({**report("a", 90, 500), "tisb": ["gs", "track"]}))
        self.assertEqual(winds.qualifies({**report("a", 90, 500), "mlat": ["lat", "lon"]}), "east")


class Evaluate(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        winds.initialize(self.db)
        wind.initialize(self.db)
        self.now = 1_762_344_000.0  # 2025-11-05 12:00 UTC

    def fill(self, east=35, west=35, east_gs=560, west_gs=380):
        for i in range(east):
            winds.remember(self.db, report(f"e{i:05x}", 90, east_gs + (i % 5)), self.now - 3600 + i)
        for i in range(west):
            winds.remember(self.db, report(f"f{i:05x}", 270, west_gs - (i % 5)), self.now - 3600 + i)

    def test_strong_asymmetry_makes_a_finding_on_the_fastest_eastbound_aircraft(self):
        self.fill()
        primary, finding, participants = winds.evaluate(self.db, self.now)
        self.assertTrue(primary.startswith("e"))
        self.assertEqual(finding.kind, "winds_aloft_day")
        self.assertEqual(finding.category, "performance")
        self.assertEqual(finding.score, 80)
        block = finding.evidence["winds_aloft"]
        self.assertEqual(block["east_addresses"], 35)
        self.assertEqual(block["day"], "2025-11-05")
        self.assertGreaterEqual(block["asymmetry_kn"], 150)
        self.assertEqual(finding.value, block["asymmetry_kn"])
        self.assertLessEqual(len(participants), 12)
        self.assertNotIn(primary, [item["hex"] for item in participants])
        info = finding.evidence["maneuver"]
        self.assertEqual(info["kind"], "winds_aloft_day")
        self.assertEqual(info["evaluated_at"], self.now)
        # The row closes at the next UTC midnight.
        self.assertEqual(info["closes_at"], self.now + 12 * 3600)
        self.assertNotIn("wind_250_hpa", block)
        self.assertEqual(info["sample_count"], 70)
        self.assertEqual(info["episode_start"], self.now - 3600)
        self.assertFalse(info["quality_review_required"])
        # No positions reach the evidence.
        text = json.dumps(finding.evidence)
        self.assertNotIn("lat", text)
        self.assertNotIn("lon", text)

    def test_too_few_addresses_or_weak_asymmetry_gives_none(self):
        self.fill(east=20)
        self.assertIsNone(winds.evaluate(self.db, self.now))
        self.db.execute("DELETE FROM day_speed_samples")
        self.fill(east_gs=470, west_gs=400)
        self.assertIsNone(winds.evaluate(self.db, self.now))

    def test_the_threshold_compares_the_unrounded_difference(self):
        # Medians 527.5 and 378.0 differ by 149.5 kn, which would round to 150.
        self.fill(east_gs=525.5)
        self.assertIsNone(winds.evaluate(self.db, self.now))

    def test_only_the_current_utc_day_counts(self):
        self.fill()
        self.assertIsNone(winds.evaluate(self.db, self.now + 86400))

    def test_median_per_address_updates_in_place(self):
        for offset, gs in enumerate((500, 520, 600)):
            winds.remember(self.db, report("abc123", 90, gs), self.now + 60 * offset)
        row = self.db.execute("SELECT gs, samples FROM day_speed_samples WHERE hex='abc123'").fetchone()
        self.assertEqual(row[1], 3)
        self.assertAlmostEqual(row[0], 520.0, delta=1)
        columns = {column[1] for column in self.db.execute("PRAGMA table_info(day_speed_samples)")}
        self.assertNotIn("alt_baro", columns)

    def test_the_kept_speeds_are_bounded(self):
        for i in range(winds.MAX_VALUES + 10):
            winds.remember(self.db, report("abc123", 90, 400 + i), self.now + i)
        row = self.db.execute("SELECT samples, values_json FROM day_speed_samples").fetchone()
        self.assertEqual(row[0], winds.MAX_VALUES + 10)
        self.assertEqual(len(json.loads(row[1])), winds.MAX_VALUES)

    def test_one_speed_per_address_and_direction_each_minute(self):
        taken = {}
        for second in range(0, 181, 5):
            winds.remember(self.db, report("abc123", 90, 500 + second), self.now + second, taken)
        winds.remember(self.db, report("abc123", 270, 380), self.now + 5, taken)
        rows = {
            row["direction"]: (row["samples"], json.loads(row["values_json"]))
            for row in self.db.execute("SELECT direction, samples, values_json FROM day_speed_samples")
        }
        self.assertEqual(rows["east"], (4, [500, 560, 620, 680]))
        self.assertEqual(rows["west"], (1, [380]))
        self.assertEqual(taken[("abc123", "east")], self.now + 180)
        # An older report than the newest kept one is skipped too.
        winds.remember(self.db, report("abc123", 90, 999), self.now + 100, taken)
        self.assertEqual(
            self.db.execute("SELECT samples FROM day_speed_samples WHERE direction='east'").fetchone()[0], 4
        )

    def test_the_preferred_carrier_is_kept_and_left_out_of_the_participants(self):
        self.fill()
        primary, _, participants = winds.evaluate(self.db, self.now, prefer="e00001")
        self.assertEqual(primary, "e00001")
        self.assertNotIn(primary, [item["hex"] for item in participants])
        self.assertEqual(len(participants), winds.MAX_PARTICIPANTS)
        self.assertEqual(set(participants[0]), {"hex", "median_gs_kn"})
        # Without the carrier among the day's addresses, the fastest present one; none present, none.
        # e00003 (563 kn) is faster than e00002 (562 kn).
        primary, _, _ = winds.evaluate(self.db, self.now, prefer="ffffff", present={"e00002", "e00003"})
        self.assertEqual(primary, "e00003")
        self.assertIsNone(winds.evaluate(self.db, self.now, present=set()))

    def test_the_cached_wind_for_the_nearest_hour_is_attached(self):
        self.fill()
        payload = {
            "time": [self.now - 3600, self.now, self.now + 3600],
            "levels": {"250": {"speed": [120.0, 135.5, 150.0], "direction": [250.0, 265.0, 270.0]}},
        }
        self.db.execute(
            "INSERT INTO wind_cache VALUES (?,?,?,?,?)",
            (42.5, -83.5, "2025-11-05", self.now - 600, json.dumps(payload)),
        )
        # 12:40 UTC is nearer 13:00 than 12:00.
        _, finding, _ = winds.evaluate(self.db, self.now + 2400)
        found = finding.evidence["winds_aloft"]["wind_250_hpa"]
        self.assertEqual(
            found,
            {"speed_kn": 150.0, "direction_deg": 270.0, "source": "open-meteo", "hour": "2025-11-05T13:00"},
        )

    def test_prune_keeps_three_days(self):
        self.fill()
        winds.prune(self.db, self.now + winds.RETENTION - 3600 + 10)
        self.assertGreater(self.db.execute("SELECT COUNT(*) FROM day_speed_samples").fetchone()[0], 0)
        winds.prune(self.db, self.now + winds.RETENTION)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM day_speed_samples").fetchone()[0], 0)


class CachedWind(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        wind.initialize(self.db)
        self.now = 1_762_344_000.0
        payload = {
            "time": [self.now - 3600, self.now, self.now + 3600],
            "levels": {"250": {"speed": [120.0, 135.5, 150.0], "direction": [250.0, 265.0, 270.0]}},
        }
        self.db.execute(
            "INSERT INTO wind_cache VALUES (?,?,?,?,?)",
            (42.5, -83.5, "2025-11-05", self.now - 600, json.dumps(payload)),
        )

    def test_the_newest_cell_gives_speed_and_direction_only(self):
        found = winds.cached_wind(self.db, self.now + 1200)
        self.assertEqual(
            found,
            {"speed_kn": 135.5, "direction_deg": 265.0, "source": "open-meteo", "hour": "2025-11-05T12:00"},
        )

    def test_a_newer_cell_cached_for_another_day_is_not_used(self):
        self.db.execute(
            "INSERT INTO wind_cache VALUES (?,?,?,?,?)",
            (40.0, -80.0, "2025-11-06", self.now, json.dumps({"time": [self.now], "levels": {}})),
        )
        self.assertEqual(winds.cached_wind(self.db, self.now)["speed_kn"], 135.5)

    def test_a_missing_day_empty_cache_or_missing_table_gives_none(self):
        self.assertIsNone(winds.cached_wind(self.db, self.now + 86400))
        self.db.execute("DELETE FROM wind_cache")
        self.assertIsNone(winds.cached_wind(self.db, self.now))
        self.db.execute("DROP TABLE wind_cache")
        self.assertIsNone(winds.cached_wind(self.db, self.now))


if __name__ == "__main__":
    unittest.main()
