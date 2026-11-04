"""Vertical-rate episodes: repeats, provenance, outliers, endings and profiles."""

import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wtf_adsb_engine.analysis import airport_context, app, curation, maneuvers

NOW = 1791150000
AIRCRAFT = {"hex": "abc123", "r": "N12345", "t": "C208", "type": "adsb_icao"}
RUNWAY = {
    "airport_ident": "TEST",
    "airport_name": "Test Airport",
    "runway": "09/27",
    "ends": [[-100.01, 40], [-99.99, 40]],
}


def point(t, x=0, y=0, **extra):
    return {
        "ts": round(NOW + t, 1),
        "lat": 40 + y / 60,
        "lon": -100 + x / (60 * math.cos(math.radians(40))),
        "source": "adsb_icao",
        **extra,
    }


def series(rates, start=30000, step=10, field="baro_rate", reference="alt_baro", ratio=1.0, **extra):
    """Reports whose altitude follows ``ratio`` times the integrated rate."""
    altitude, result = start, []
    for i, rate in enumerate(rates):
        if i:
            altitude += (rates[i - 1] + rate) / 2 * step / 60 * ratio
        result.append(point(i * step, i * step / 60, **{reference: round(altitude, 1), field: rate}, **extra))
    return result


def rapid(points, **kwargs):
    return [f for f in maneuvers.analyze(points, AIRCRAFT, **kwargs) if f.kind.startswith("rapid_")]


class RepeatAndToleranceTests(unittest.TestCase):
    def test_polls_carrying_two_distinct_reports_cannot_confirm(self):
        points = [point(t, alt_baro=30000, baro_rate=-8000) for t in (0, 10, 20)]
        points += [point(t, alt_baro=28700, baro_rate=-8000) for t in (30, 40)]
        finding = rapid(points)[0]
        self.assertEqual(finding.evidence["maneuver"]["distinct_report_count"], 2)
        self.assertEqual(finding.evidence["maneuver"]["sample_count"], 5)
        self.assertFalse(finding.evidence["maneuver"]["confirmed"])

    def test_a_repeated_rate_with_a_moving_altitude_is_still_a_report(self):
        finding = rapid(series((-6000,) * 5, start=9000))[0]
        self.assertEqual(finding.evidence["maneuver"]["distinct_report_count"], 5)
        self.assertTrue(finding.evidence["maneuver"]["confirmed"])

    def test_altitude_supporting_only_part_of_the_rate_needs_review(self):
        for rates, ratio in (((-7000,) * 4, 0.55), ((-8000,) * 6, 0.5625), ((-4600,) * 6, 0.565)):
            with self.subTest(rate=rates[0]):
                finding = rapid(series(rates, start=9000, ratio=ratio))[0]
                metrics = finding.evidence["maneuver"]
                self.assertIn("rate_altitude_disagreement", metrics["quality_flags"])
                self.assertFalse(curation.exceptional(finding.kind, finding.value, metrics))

    def test_tolerance_is_thirty_percent_with_a_500_ft_floor(self):
        self.assertTrue(maneuvers.altitude_agrees(-7200, -10000, -1))
        self.assertFalse(maneuvers.altitude_agrees(-6900, -10000, -1))
        self.assertTrue(maneuvers.altitude_agrees(-600, -1000, -1))

    def test_coherent_high_rate_descent_records_its_altitude_derived_rate(self):
        finding = rapid(series((-7400,) * 6))[0]
        metrics = finding.evidence["maneuver"]
        self.assertAlmostEqual(metrics["sustained_derived_rate_fpm"], -7400, delta=1)
        self.assertEqual(metrics["start_altitude_ft"], 30000)
        self.assertLess(metrics["end_altitude_ft"], 30000)
        self.assertTrue(curation.exceptional(finding.kind, finding.value, metrics))
        self.assertIn("sustained_derived_rate_fpm", maneuvers.METRICS)

    def test_rates_without_any_supporting_neighbour_need_review(self):
        rates = (-5000, -9000) * 4
        finding = rapid(series(rates))[0]
        self.assertIn("uncorroborated_rate", finding.evidence["maneuver"]["quality_flags"])


class ProvenanceTests(unittest.TestCase):
    def test_direct_geometric_rate_wins_over_an_mlat_pressure_rate(self):
        points = series(
            (-6000,) * 8, field="geom_rate", reference="alt_geom", baro_rate=0, mlat=["baro_rate"]
        )
        finding = rapid(points)[0]
        self.assertEqual(finding.evidence["maneuver"]["rate_field"], "geom_rate")
        self.assertFalse(finding.evidence["maneuver"]["quality_review_required"])

    def test_an_mlat_rate_cannot_create_a_descent_beside_a_direct_level_rate(self):
        points = series((-6000,) * 8, mlat=["baro_rate"], alt_geom=30000, geom_rate=0)
        self.assertEqual(rapid(points), [])

    def test_an_intermittent_mlat_rate_does_not_fragment_the_episode(self):
        points = series((-6000,) * 8, field="geom_rate", reference="alt_geom")
        for p in points[::2]:
            p.update(baro_rate=0, mlat=["baro_rate"])
        found = rapid(points)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].evidence["maneuver"]["confirmed"])

    def test_one_carried_forward_rate_does_not_switch_a_running_episode(self):
        points = series((-6000,) * 10, alt_geom=0, geom_rate=-6000)
        for p in points:
            p["alt_geom"] = p["alt_baro"] + 200
        points[5]["baro_rate"] = points[4]["baro_rate"]
        points[5]["alt_baro"] = points[4]["alt_baro"]
        found = rapid(points)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].evidence["maneuver"]["rate_field"], "baro_rate")

    def test_disagreeing_direct_rates_prefer_the_updating_field_and_need_review(self):
        points = series((-6000,) * 8, field="geom_rate", reference="alt_geom", alt_baro=30000, baro_rate=0)
        finding = rapid(points)[0]
        self.assertEqual(finding.evidence["maneuver"]["rate_field"], "geom_rate")
        self.assertIn("rate_field_conflict", finding.evidence["maneuver"]["quality_flags"])


class OutlierTests(unittest.TestCase):
    def test_isolated_extreme_rate_is_evidence_not_the_peak(self):
        points = series((-8000,) * 3 + (-15000,) + (-8000,) * 3)
        points[3]["alt_baro"] = (points[2]["alt_baro"] + points[4]["alt_baro"]) / 2
        finding = rapid(points)[0]
        metrics = finding.evidence["maneuver"]
        self.assertEqual(finding.value, -8000)
        self.assertNotIn("extreme_rate", metrics["quality_flags"])
        self.assertIn("isolated_rate_outlier", metrics["context_tags"])
        self.assertEqual(metrics["rate_outliers"], [{"ts": points[3]["ts"], "rate_fpm": -15000}])
        self.assertIn(-15000, [p["baro_rate"] for p in finding.evidence["maneuver_samples"]])
        self.assertTrue(curation.exceptional(finding.kind, finding.value, metrics))

    def test_corroborated_extreme_rates_still_need_review(self):
        finding = rapid(series((-8000, -13000, -13000, -8000, -8000)))[0]
        self.assertIn("extreme_rate", finding.evidence["maneuver"]["quality_flags"])
        self.assertEqual(finding.value, -13000)


class EndingTests(unittest.TestCase):
    def test_touchdown_rate_loss_and_breaks_end_the_episode(self):
        points = series((-6000,) * 5, start=9000)
        cases = {
            "ground": point(50, alt_baro="ground", ground=True),
            "rate_lost": point(50),
            "segment_break": point(50, alt_baro=25000, baro_rate=-6000, mlat=["baro_rate"]),
        }
        for reason, last in cases.items():
            with self.subTest(reason=reason):
                finding = rapid([*points, last])[0]
                info = finding.evidence["maneuver"]
                self.assertEqual((info["state"], info["end_reason"]), ("ended", reason))
                self.assertEqual(info["evaluated_at"], last["ts"])

    def test_recovery_keeps_its_state(self):
        finding = rapid(series((-6000,) * 4 + (-1000,) * 3, start=9000))[0]
        self.assertEqual(finding.evidence["maneuver"]["state"], "recovered")

    def test_gap_closes_the_previous_segment_once(self):
        points = series((-6000,) * 5, start=9000)
        later = point(120, alt_baro=20000, baro_rate=0)
        closed = rapid([*points, later])
        self.assertEqual(len(closed), 1)
        info = closed[0].evidence["maneuver"]
        self.assertEqual(
            (info["state"], info["end_reason"], info["evaluated_at"]), ("ended", "segment_break", later["ts"])
        )
        self.assertEqual(rapid([*points, later, point(240, alt_baro=20000, baro_rate=0)]), [])

    def test_silent_aircraft_episode_ends_after_sixty_seconds(self):
        points = series((-6000,) * 5, start=9000)
        self.assertEqual(rapid(points, now=points[-1]["ts"] + 30)[0].evidence["maneuver"]["state"], "active")
        stale = rapid(points, now=points[-1]["ts"] + 90)
        info = stale[0].evidence["maneuver"]
        self.assertEqual((info["state"], info["end_reason"]), ("ended", "stale"))
        self.assertEqual(info["evaluated_at"], points[-1]["ts"] + 60)
        again = rapid(points, now=points[-1]["ts"] + 400)[0].evidence["maneuver"]
        self.assertEqual(again["evaluated_at"], info["evaluated_at"])


class ProfileAndAirportTests(unittest.TestCase):
    def test_high_altitude_descent_with_a_level_off_gets_the_profile_tag(self):
        rates = (-6000,) * 23 + (-3000, -1000) + (0,) * 10
        points = series(rates, start=35000)
        finding = rapid(points)[0]
        info = finding.evidence["maneuver"]
        self.assertIn("emergency_descent_profile", info["context_tags"])
        self.assertAlmostEqual(info["level_off_altitude_ft"], points[-1]["alt_baro"], delta=200)
        self.assertGreater(info["evaluated_at"], info["ended_at"])

    def test_a_steady_slower_descent_is_not_a_level_off(self):
        rates = (-6000,) * 23 + (-1000,) * 14
        finding = rapid(series(rates, start=35000))[0]
        self.assertNotIn("emergency_descent_profile", finding.evidence["maneuver"]["context_tags"])

    def test_profile_needs_a_high_start_and_a_mid_level_hold(self):
        for start, rates in ((20000, (-6000,) * 12 + (0,) * 5), (35000, (-6000,) * 12 + (0,) * 10)):
            with self.subTest(start=start):
                finding = rapid(series(rates, start=start))[0]
                self.assertNotIn("emergency_descent_profile", finding.evidence["maneuver"]["context_tags"])

    def test_descent_centred_on_a_runway_is_airport_context(self):
        points = [
            point(i * 10, 0.2 * math.cos(i), 0.2 * math.sin(i), alt_baro=12000 - i * 1200, baro_rate=-7200)
            for i in range(8)
        ]
        with patch.object(airport_context, "nearby", side_effect=lambda p, r=3: [RUNWAY] if r >= 3 else []):
            finding = rapid(points)[0]
        self.assertIn("airport_descent_context", finding.evidence["maneuver"]["context_tags"])
        with patch.object(airport_context, "nearby", return_value=[]):
            finding = rapid(points)[0]
        self.assertNotIn("airport_descent_context", finding.evidence["maneuver"]["context_tags"])

    def test_every_ending_keeps_the_airport_context(self):
        points = [
            point(i * 10, 0.2 * math.cos(i), 0.2 * math.sin(i), alt_baro=12000 - i * 1200, baro_rate=-7200)
            for i in range(8)
        ]
        endings = {
            "segment_break": [*points, point(120, alt_baro=2000, baro_rate=0)],
            "ground": [*points, point(80, alt_baro="ground", ground=True)],
        }
        with patch.object(airport_context, "nearby", side_effect=lambda p, r=3: [RUNWAY] if r >= 3 else []):
            for reason, track in endings.items():
                with self.subTest(reason=reason):
                    finding = rapid(track)[0]
                    self.assertEqual(finding.evidence["maneuver"]["end_reason"], reason)
                    self.assertIn("airport_descent_context", finding.evidence["maneuver"]["context_tags"])
            stale = rapid(points, now=points[-1]["ts"] + 90)[0]
            self.assertEqual(stale.evidence["maneuver"]["end_reason"], "stale")
            self.assertIn("airport_descent_context", stale.evidence["maneuver"]["context_tags"])
        with patch.object(airport_context, "available", return_value=False):
            closed = rapid(endings["segment_break"])[0]
        self.assertIn("airport_context_unavailable", closed.evidence["maneuver"]["quality_flags"])

    def test_missing_runway_data_blocks_descents_that_need_airport_context(self):
        points = series((-7200,) * 6)
        with patch.object(airport_context, "available", return_value=False):
            finding = rapid(points)[0]
        self.assertIn("airport_context_unavailable", finding.evidence["maneuver"]["quality_flags"])


class DetectorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "journal.db"
        app.initialize(self.path)
        self.detector = app.Detector(self.path)
        lookup = patch.object(app.watchlists, "lookup", return_value=None)
        lookup.start()
        self.addCleanup(lookup.stop)

    def feed(self, points):
        for index, p in enumerate(points):
            a = {**AIRCRAFT, **p, "type": p["source"], "seen": 0, "seen_pos": 0, "messages": index + 100}
            a.pop("ground", None)
            self.detector.process({"now": p["ts"], "aircraft": [a]}, p["ts"])

    def test_touchdown_is_saved_as_ended(self):
        self.feed(
            [*series((-6000,) * 5, start=9000), *[point(50 + i * 10, alt_baro="ground") for i in range(15)]]
        )
        with app.connect(self.path) as db:
            rows = [app.event_dict(r) for r in db.execute("SELECT * FROM events WHERE kind='rapid_descent'")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["evidence"]["maneuver"]["state"], "ended")
        # The collector debounces a ground flag right after reports above 2,000 ft
        # (RULES-11), so the episode ends where the rate reports stop.
        self.assertEqual(rows[0]["evidence"]["maneuver"]["end_reason"], "rate_lost")


def descent_points(start_alt, rate, seconds, step=1):
    """1 Hz direct ADS-B reports at a steady vertical ``rate`` from ``start_alt``."""
    return series((rate,) * (seconds // step + 1), start=start_alt, step=step, gs=300)


class DescentBands(unittest.TestCase):
    """A descent from 10,000 ft or higher needs 6,000 ft/min for 60 s and 6 reports."""

    def kinds(self, points):
        return {f.kind for f in maneuvers.vertical(points)}

    def test_5200_fpm_from_fl200_is_not_a_rapid_descent(self):
        points = descent_points(start_alt=20000, rate=-5200, seconds=90)
        self.assertNotIn("rapid_descent", self.kinds(points))

    def test_6500_fpm_from_fl200_for_60_s_is(self):
        points = descent_points(start_alt=20000, rate=-6500, seconds=90)
        self.assertIn("rapid_descent", self.kinds(points))

    def test_6500_fpm_from_fl200_for_45_s_is_not(self):
        points = descent_points(start_alt=20000, rate=-6500, seconds=45)
        self.assertNotIn("rapid_descent", self.kinds(points))

    def test_a_brief_7000_fpm_peak_is_still_saved_unconfirmed(self):
        found = maneuvers.vertical(descent_points(start_alt=20000, rate=-7400, seconds=20))
        self.assertEqual([f.kind for f in found], ["rapid_descent"])
        self.assertFalse(found[0].evidence["maneuver"]["confirmed"])

    def test_the_exceptional_window_still_confirms_a_high_descent(self):
        # 7,400 ft/min for 40 s from FL300 misses the band's 60 s but holds 7,000 ft/min for 30 s.
        finding = maneuvers.vertical(descent_points(start_alt=30000, rate=-7400, seconds=40))[0]
        info = finding.evidence["maneuver"]
        self.assertTrue(info["confirmed"])
        self.assertEqual((info["threshold_fpm"], info["qualifying_seconds_required"]), (7000, 30))
        self.assertTrue(curation.exceptional(finding.kind, finding.value, info))

    def test_4800_fpm_below_10000_ft_still_is(self):
        points = descent_points(start_alt=8000, rate=-4800, seconds=45)
        self.assertIn("rapid_descent", self.kinds(points))

    def test_climbs_keep_the_flat_rule(self):
        points = descent_points(start_alt=20000, rate=4800, seconds=45)
        self.assertIn("rapid_climb", self.kinds(points))

    def test_rule_lookup(self):
        self.assertEqual(maneuvers.descent_rule(20000), (10000, 6000, 60, 6))
        self.assertEqual(maneuvers.descent_rule(10000), (10000, 6000, 60, 6))
        self.assertEqual(maneuvers.descent_rule(9000), (0, 4500, 30, 4))
        self.assertEqual(maneuvers.descent_rule(None), (0, 4500, 30, 4))

    def test_the_finding_states_its_rule(self):
        cases = (
            (descent_points(20000, -6500, 90), (6000, 60, 6)),
            (descent_points(8000, -4800, 45), (4500, 30, 4)),
            (descent_points(20000, 4800, 45), (4500, 30, 4)),
        )
        keys = ("threshold_fpm", "qualifying_seconds_required", "qualifying_reports_required")
        for points, rule in cases:
            with self.subTest(rule=rule):
                info = maneuvers.vertical(points)[0].evidence["maneuver"]
                self.assertEqual(tuple(info[key] for key in keys), rule)
        for key in keys:
            self.assertIn(key, maneuvers.METRICS)


if __name__ == "__main__":
    unittest.main()
