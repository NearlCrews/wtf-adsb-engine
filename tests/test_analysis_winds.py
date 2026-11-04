"""Day-level winds aloft from eastbound and westbound ground speeds."""

import json
import sqlite3
import unittest

from wtf_adsb_engine.analysis import winds


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


class Evaluate(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(winds.DDL)
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

    def test_only_the_current_utc_day_counts(self):
        self.fill()
        self.assertIsNone(winds.evaluate(self.db, self.now + 86400))

    def test_median_per_address_updates_in_place(self):
        for gs in (500, 520, 600):
            winds.remember(self.db, report("abc123", 90, gs), self.now)
        row = self.db.execute("SELECT gs, samples FROM day_speed_samples WHERE hex='abc123'").fetchone()
        self.assertEqual(row[1], 3)
        self.assertAlmostEqual(row[0], 520.0, delta=1)

    def test_the_kept_speeds_are_bounded(self):
        for i in range(winds.MAX_VALUES + 10):
            winds.remember(self.db, report("abc123", 90, 400 + i), self.now + i)
        row = self.db.execute("SELECT samples, values_json FROM day_speed_samples").fetchone()
        self.assertEqual(row[0], winds.MAX_VALUES)
        self.assertEqual(len(json.loads(row[1])), winds.MAX_VALUES)

    def test_a_chooser_picks_the_carrier_and_leaves_it_out_of_the_participants(self):
        self.fill()
        primary, _, participants = winds.evaluate(
            self.db, self.now, choose=lambda addresses: "e00001" if "e00001" in addresses else None
        )
        self.assertEqual(primary, "e00001")
        self.assertNotIn(primary, [item["hex"] for item in participants])
        self.assertEqual(len(participants), winds.MAX_PARTICIPANTS)
        self.assertIsNone(winds.evaluate(self.db, self.now, choose=lambda addresses: None))

    def test_wind_lookup_is_attached_when_given(self):
        self.fill()
        _, finding, _ = winds.evaluate(
            self.db,
            self.now,
            wind_lookup=lambda day, hour: {
                "speed_kn": 140.0,
                "direction_deg": 260.0,
                "source": "open-meteo",
                "hour": hour,
            },
        )
        wind = finding.evidence["winds_aloft"]["wind_250_hpa"]
        self.assertEqual(wind["speed_kn"], 140.0)
        self.assertEqual(wind["hour"], "2025-11-05T12:00")

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
        self.db.execute(
            "CREATE TABLE wind_cache (lat REAL NOT NULL, lon REAL NOT NULL, day TEXT NOT NULL, "
            "fetched_at REAL NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(lat,lon,day))"
        )
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
        wind = winds.cached_wind(self.db, "2025-11-05", "2025-11-05T12:00", self.now)
        self.assertEqual(
            wind,
            {"speed_kn": 135.5, "direction_deg": 265.0, "source": "open-meteo", "hour": "2025-11-05T12:00"},
        )

    def test_a_missing_day_or_empty_cache_gives_none(self):
        self.assertIsNone(winds.cached_wind(self.db, "2025-11-06", "2025-11-06T12:00", self.now))
        self.db.execute("DELETE FROM wind_cache")
        self.assertIsNone(winds.cached_wind(self.db, "2025-11-05", "2025-11-05T12:00", self.now))


if __name__ == "__main__":
    unittest.main()
