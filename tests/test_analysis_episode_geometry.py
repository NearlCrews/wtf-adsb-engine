"""Track geometry: leg-based reversals, orbit precedence, hovers, airports and long shapes."""

import collections
import math
import types
import unittest
from unittest.mock import patch

from wtf_adsb_engine.analysis import airport_context, maneuvers, rules, telemetry
from wtf_adsb_engine.analysis.rules import circling

T0 = 1791150000.0
LAT0, LON0 = 44.0, -80.0
LIGHT = {"hex": "abc123", "t": "C172", "category": "A1", "type": "adsb_icao"}
JET = {"hex": "abc124", "t": "B738", "category": "A3", "type": "adsb_icao"}
HELICOPTER = {"hex": "abc125", "t": "EC35", "category": "A7", "type": "adsb_icao"}


def ll(x, y):
    return LAT0 + y / 60, LON0 + x / (60 * math.cos(math.radians(LAT0)))


class Track:
    """Kinematic path in local NM (heading 0 = north), stepped every 0.5 s."""

    def __init__(self, x=0.0, y=0.0, heading=90.0, altitude=3000.0, speed=100.0):
        self.x, self.y, self.heading, self.altitude, self.speed, self.t = x, y, heading, altitude, speed, 0.0
        self.states = [(0.0, x, y, heading, altitude, speed, 0.0)]

    def _step(self, turn, rate):
        self.heading += turn
        distance = self.speed * 0.5 / 3600
        self.x += distance * math.sin(math.radians(self.heading))
        self.y += distance * math.cos(math.radians(self.heading))
        self.altitude += rate * 0.5 / 60
        self.t += 0.5
        self.states.append((self.t, self.x, self.y, self.heading % 360, self.altitude, self.speed, rate))

    def straight(self, seconds, rate=0.0, speed=None):
        self.speed = speed or self.speed
        for _ in range(round(seconds / 0.5)):
            self._step(0, rate)
        return self

    def turn(self, degrees, per_second=3.0, rate=0.0, speed=None):
        self.speed = speed or self.speed
        steps = max(1, round(abs(degrees) / per_second / 0.5))
        for _ in range(steps):
            self._step(degrees / steps, rate)
        return self


def sample(track, period=10.0, wind=(0.0, 0.0), source="adsb_icao", gaps=(), **extra):
    """Poll like readsb: fresh positions every ``period`` seconds, rounded to 0.1 s."""
    out, t, index = [], 0.0, 0
    states = track.states
    while t <= states[-1][0]:
        while index + 1 < len(states) and states[index + 1][0] <= t:
            index += 1
        _, x, y, heading, altitude, speed, rate = states[index]
        x, y = x + wind[0] * t / 3600, y + wind[1] * t / 3600
        if not any(a <= t < b for a, b in gaps):
            vx = speed * math.sin(math.radians(heading)) + wind[0]
            vy = speed * math.cos(math.radians(heading)) + wind[1]
            lat, lon = ll(x, y)
            stamp = round(T0 + t, 1)
            out.append(
                {
                    "ts": stamp,
                    "position_at": stamp,
                    "lat": lat,
                    "lon": lon,
                    "gs": round(math.hypot(vx, vy), 1),
                    "track": round(math.degrees(math.atan2(vx, vy)) % 360, 1),
                    "alt_baro": round(altitude / 25) * 25,
                    "baro_rate": round(rate / 64) * 64,
                    "source": source,
                    "nic": 8,
                    "nac_p": 10,
                    **extra,
                }
            )
        t += period
    return out


def runway_at(x, y, heading=90, length=1.0):
    dx, dy = length / 2 * math.sin(math.radians(heading)), length / 2 * math.cos(math.radians(heading))
    a, b = ll(x - dx, y - dy), ll(x + dx, y + dy)
    return {
        "airport_ident": "SYNT",
        "airport_name": "Synthetic",
        "runway": "09/27",
        "ends": [[a[1], a[0]], [b[1], b[0]]],
    }


def runways(*items):
    """Patch runway lookups with synthetic runways (no pinned-data dependence)."""
    return patch.object(
        airport_context,
        "nearby",
        side_effect=lambda p, r=3: [rw for rw in items if airport_context.runway_distance(p, rw) <= r],
    )


def incremental(points, aircraft=LIGHT, **kwargs):
    """Evaluate after every report like the collector; keep the last finding per episode."""
    history, found = collections.deque(maxlen=telemetry.LIMIT), collections.defaultdict(dict)
    for p in points:
        telemetry.bounded_append(history, dict(p))
        for finding in maneuvers.analyze(list(history), aircraft, **kwargs):
            info = finding.evidence["maneuver"]
            found[finding.kind][info.get("episode_start", info["started_at"])] = finding
    return found


def u_turn(per_second, speed, altitude=35000):
    return Track(altitude=altitude, speed=speed).straight(300).turn(180, per_second).straight(300)


class ReversalTests(unittest.TestCase):
    def test_genuine_u_turns_give_exactly_one_completed_reversal(self):
        for per_second in (0.6, 0.75, 1.5, 3.0):
            for speed in (250, 460):
                with self.subTest(per_second=per_second, speed=speed):
                    found = incremental(sample(u_turn(per_second, speed)), JET)["course_reversal"]
                    self.assertEqual(len(found), 1)
                    finding = next(iter(found.values()))
                    self.assertAlmostEqual(finding.value, 180, delta=5)
                    self.assertEqual(finding.evidence["maneuver"]["state"], "ended")
                    self.assertGreaterEqual(finding.evidence["maneuver"]["exit_seconds"], 250)

    def test_wind_does_not_hide_a_slow_reversal(self):
        for wind in ((15, 5), (0, 25)):
            with self.subTest(wind=wind):
                found = incremental(sample(u_turn(0.75, 460), wind=wind), JET)
                self.assertEqual(len(found["course_reversal"]), 1)

    def test_turns_that_resume_or_zigzag_are_not_reversals(self):
        shapes = {
            "360": Track(speed=90).straight(180).turn(360).straight(240),
            "orbits": Track(speed=90).straight(180).turn(4 * 360).straight(240),
            "two 90s": Track(heading=0, altitude=4000, speed=200)
            .straight(300)
            .turn(90)
            .straight(60)
            .turn(90)
            .straight(200),
        }
        s_turns = Track(speed=90).straight(120)
        for index in range(6):
            s_turns.turn(180 if index % 2 == 0 else -180).straight(20)
        shapes["S-turns"] = s_turns
        for name, track in shapes.items():
            with self.subTest(name=name):
                self.assertNotIn("course_reversal", incremental(sample(track)))

    def test_racetrack_and_short_lawnmower_legs_give_one_reversal(self):
        racetrack = Track(altitude=9000, speed=210).straight(120)
        lawnmower = Track(speed=120).straight(120)
        for _ in range(5):
            racetrack.turn(180).straight(60)
            lawnmower.turn(180).straight(150)
        for track in (racetrack, lawnmower):
            self.assertEqual(len(incremental(sample(track))["course_reversal"]), 1)
        back_and_forth = Track(speed=120).straight(120)
        for _ in range(3):
            back_and_forth.turn(180).straight(400)
        self.assertEqual(len(incremental(sample(back_and_forth))["course_reversal"]), 3)

    def test_low_reversal_near_a_runway_has_terminal_context(self):
        track = Track(x=-8, y=1, altitude=2500, speed=110).straight(150).turn(-180).straight(160)
        with runways(runway_at(-6, 0)):
            finding = next(iter(incremental(sample(track))["course_reversal"].values()))
        self.assertIn("terminal_area_context", finding.evidence["maneuver"]["context_tags"])
        with runways():
            finding = next(iter(incremental(sample(track))["course_reversal"].values()))
        self.assertNotIn("terminal_area_context", finding.evidence["maneuver"]["context_tags"])

    def test_chains_in_wind_still_give_one_reversal(self):
        for wind in ((0, 25), (-30, 0)):
            hold = Track(altitude=9000, speed=200).straight(120)
            s_turns = Track(speed=120).straight(240)
            for index in range(5):
                hold.turn(180).straight(60)
                s_turns.turn(180 if index % 2 == 0 else -180).straight(50)
            with self.subTest(wind=wind):
                self.assertEqual(len(incremental(sample(hold, wind=wind))["course_reversal"]), 1)
                self.assertLessEqual(len(incremental(sample(s_turns, wind=wind))["course_reversal"]), 1)


def orbit_track(laps=6, speed=90, altitude=3000, rate=0.0):
    return Track(speed=speed, altitude=altitude).straight(60).turn(360 * laps, rate=rate)


class OrbitPrecedenceTests(unittest.TestCase):
    def test_position_less_reports_do_not_fragment_geometry(self):
        points = sample(orbit_track())
        for p in points[3::9]:
            for key in ("lat", "lon", "position_at"):
                p.pop(key)
        self.assertEqual(len(telemetry.positions(points)), len([p for p in points if "lat" in p]))
        mixed = sample(orbit_track())
        for p in mixed[3::9]:
            p.update(source="mode_s")
            for key in ("lat", "lon", "position_at"):
                p.pop(key)
        kinds = {f.kind for f in maneuvers.analyze(mixed, LIGHT)}
        self.assertIn("prolonged_holding", kinds)

    def test_positioned_source_changes_and_gaps_still_break_geometry(self):
        points = sample(orbit_track(laps=2))
        for p in points[-5:]:
            p["source"] = "mlat"
        self.assertEqual(telemetry.positions(points)[0]["ts"], points[-5]["ts"])
        points = sample(orbit_track(laps=4), gaps=((300, 360),))
        self.assertGreaterEqual(telemetry.positions(points)[0]["ts"], T0 + 360)

    def test_a_supplied_orbit_verdict_decides_holding(self):
        points = sample(orbit_track())
        verdict = circling(points)
        self.assertIsNotNone(verdict)
        # The verdict dict, or its bare pattern summary.
        for orbit in (rules.orbit_verdict([verdict]), verdict.evidence["pattern"]):
            with self.subTest(samples="pattern_samples" in orbit):
                self.assertIn(
                    "prolonged_holding", {f.kind for f in maneuvers.analyze(points, LIGHT, orbit=orbit)}
                )
        self.assertNotIn("prolonged_holding", {f.kind for f in maneuvers.analyze(points, LIGHT, orbit=False)})
        unverified = {**verdict.evidence["pattern"], "shape_verified": False}
        self.assertNotIn(
            "prolonged_holding", {f.kind for f in maneuvers.analyze(points, LIGHT, orbit=unverified)}
        )

    def test_holding_evidence_uses_the_verdict_samples(self):
        points = sample(orbit_track(laps=9), gaps=((700, 760),))
        verdict = circling(sample(orbit_track(laps=9)))
        holding = next(
            f
            for f in maneuvers.analyze(points, LIGHT, orbit=rules.orbit_verdict([verdict]))
            if f.kind == "prolonged_holding"
        )
        info = holding.evidence["maneuver"]
        self.assertAlmostEqual(
            info["duration_seconds"], verdict.evidence["pattern"]["duration_seconds"], delta=1
        )
        self.assertAlmostEqual(holding.value * 60, info["duration_seconds"], delta=1)
        tagged = {**verdict.evidence["pattern"], "context_tags": ["drifting_orbit", "free text"]}
        tagged["pattern_samples"] = verdict.evidence["pattern_samples"]
        holding = next(
            f for f in maneuvers.analyze(points, LIGHT, orbit=tagged) if f.kind == "prolonged_holding"
        )
        self.assertEqual(holding.evidence["maneuver"]["context_tags"], ["drifting_orbit"])

    def test_reversals_inside_an_orbit_verdict_are_suppressed(self):
        points = sample(Track(altitude=9000, speed=210).straight(120).turn(180).straight(70))
        reversal = next(f for f in maneuvers.analyze(points, LIGHT) if f.kind == "course_reversal")
        orbit = {
            "started_at": points[0]["ts"],
            "ended_at": points[-1]["ts"],
            "closed_loops": 1,
            "quality_review_required": False,
        }
        self.assertNotIn("course_reversal", {f.kind for f in maneuvers.analyze(points, LIGHT, orbit=orbit)})
        self.assertEqual(
            reversal.evidence["maneuver"]["episode_start"], reversal.evidence["maneuver"]["turn_started_at"]
        )

    def test_holding_and_descending_spiral_coexist_away_from_airports(self):
        points = sample(Track(altitude=15000, speed=120).turn(360 * 7, rate=-1000))
        with runways():
            kinds = {f.kind for f in maneuvers.analyze(points, LIGHT)}
        self.assertTrue({"prolonged_holding", "descending_spiral"} <= kinds)


class AirportContextTests(unittest.TestCase):
    def test_descending_spiral_over_a_runway_is_airport_descent_context(self):
        points = sample(Track(y=-0.5, altitude=13500, speed=120).turn(360 * 6, rate=-2000))
        with runways(runway_at(0, 0)):
            spiral = next(f for f in maneuvers.analyze(points, LIGHT) if f.kind == "descending_spiral")
        self.assertIn("airport_descent_context", spiral.evidence["maneuver"]["context_tags"])
        with runways(runway_at(30, 30)):
            spiral = next(f for f in maneuvers.analyze(points, LIGHT) if f.kind == "descending_spiral")
        self.assertNotIn("airport_descent_context", spiral.evidence["maneuver"]["context_tags"])
        with patch.object(airport_context, "available", return_value=False):
            spiral = next(f for f in maneuvers.analyze(points, LIGHT) if f.kind == "descending_spiral")
        self.assertIn("airport_context_unavailable", spiral.evidence["maneuver"]["quality_flags"])

    def test_go_around_is_an_airport_hypothesis_tag_not_a_blocking_flag(self):
        track = Track(x=-4.5, altitude=2000, speed=100).straight(150, rate=-700).straight(150, rate=1500)
        with runways(runway_at(0, 0)):
            finding = maneuvers.go_around(sample(track))
        info = finding.evidence["maneuver"]
        self.assertEqual(finding.kind, "possible_go_around")
        self.assertIn("airport_maneuver_hypothesis", info["context_tags"])
        self.assertNotIn("airport_maneuver_hypothesis", info["quality_flags"])

    def test_runway_circuits_fold_holding_and_oscillation_into_the_circling_episode(self):
        track = Track(x=-0.4, altitude=900, speed=80)
        for _ in range(5):
            track.straight(45, rate=700).turn(-90).straight(30).turn(-90).straight(90)
            track.turn(-90).straight(30).turn(-90).straight(45, rate=-700)
        points = sample(track)
        runway = runway_at(0, 0, length=0.8)
        with runways(runway):
            found = incremental(points)
            kinds = set(found)
            attributes = maneuvers.circuit_attributes(points, LIGHT)
            final = {f.kind: f for f in maneuvers.analyze(points, LIGHT)}
        self.assertFalse({"prolonged_holding", "altitude_oscillation", "descending_spiral"} & kinds)
        self.assertLessEqual(len(found["course_reversal"]), 1)
        # Once the circuit is classified, overlapping findings carry its context.
        if "possible_go_around" in final:
            tags = final["possible_go_around"].evidence["maneuver"]["context_tags"]
            self.assertIn("runway_circuit_context", tags)
        self.assertEqual(attributes["context_tags"], ["runway_circuit_context"])
        self.assertTrue(attributes["attributes"]["oscillating"])
        with runways():
            self.assertIsNone(maneuvers.circuit_attributes(points, LIGHT))
            self.assertIn("prolonged_holding", incremental(points))

    def test_a_lone_fix_merges_as_two_identical_copies_do(self):
        base = {"ts": T0, "lat": LAT0, "lon": LON0, "source": "mlat"}
        for extra in (
            {"break_before": False},
            {"break_before": True, "altitude": 3000, "altitude_reference": "barometric", "speed": 120},
            {"break_before": False, "speed": None, "note": "x"},
        ):
            fix = {**base, **extra}
            merged = airport_context.merged_fix([fix])
            self.assertEqual(merged, airport_context.merged_fix([fix, dict(fix)]))
            self.assertIsNot(merged, fix)
        self.assertIsNone(airport_context.merged_fix([None]))


def hover_track(before=True, after=True, hover_seconds=300):
    track = Track(altitude=1200, speed=100)
    if before:
        track.straight(240).straight(10, speed=40)
    track.straight(hover_seconds, speed=1)
    if after:
        track.straight(10, speed=40).straight(240, speed=100)
    return track


class HoverTests(unittest.TestCase):
    def setUp(self):
        # Synthetic provider fixture: these tests exercise class reconciliation,
        # independently of optional redistributable aircraft database bundles.
        types_fixture = {"C172": ["L1P", "L"], "P149": ["L1P", "L"], "EC35": ["H2T", "L"]}
        self.types = patch.object(
            airport_context.reference,
            "_type_sets",
            return_value={
                "types": types_fixture,
                "groups": frozenset({"ZZZZ"}),
                "uav": frozenset(),
                "glider": frozenset(),
                "years": {},
            },
        )
        self.types.start()
        self.addCleanup(self.types.stop)

    def latest(self, points, aircraft=HELICOPTER):
        found = incremental(points, aircraft).get("hovering", {})
        return list(found.values())[-1] if found else None

    def test_parked_airborne_rotorcraft_is_held_for_review(self):
        points = [
            {
                "ts": round(T0 + i * 10, 1),
                "lat": LAT0 + 0.00001 * (i % 2),
                "lon": LON0,
                "gs": 0,
                "alt_baro": 675,
                "source": "adsb_icao",
            }
            for i in range(31)
        ]
        finding = self.latest(points)
        self.assertIn("ground_status_unverified", finding.evidence["maneuver"]["quality_flags"])

    def test_hover_between_transits_is_verified_and_arrival_alone_is_not(self):
        finding = self.latest(sample(hover_track()))
        self.assertEqual(finding.evidence["maneuver"]["quality_flags"], [])
        self.assertEqual(finding.evidence["maneuver"]["state"], "ended")
        finding = self.latest(sample(hover_track(after=False)))
        self.assertIn("ground_status_unverified", finding.evidence["maneuver"]["quality_flags"])

    def test_long_stationary_periods_and_buffer_cut_runs(self):
        finding = self.latest(sample(hover_track(hover_seconds=660)))
        self.assertIn("ground_status_unverified", finding.evidence["maneuver"]["quality_flags"])
        cut = [p for p in sample(hover_track()) if p["ts"] >= T0 + 300]
        finding = self.latest(cut)
        self.assertEqual(finding.evidence["maneuver"]["state"], "active")

    def test_a_hover_whose_aircraft_goes_silent_ends(self):
        points = sample(hover_track(after=False))
        stale = [
            f
            for f in maneuvers.analyze(points, HELICOPTER, now=points[-1]["ts"] + 90)
            if f.kind == "hovering"
        ]
        self.assertEqual(len(stale), 1)
        info = stale[0].evidence["maneuver"]
        self.assertEqual(
            (info["state"], info["end_reason"], info["evaluated_at"]),
            ("ended", "stale", points[-1]["ts"] + 60),
        )

    def test_vertical_climb_out_counts_as_movement(self):
        track = hover_track(after=False).straight(60, rate=600, speed=2)
        self.assertEqual(self.latest(sample(track)).evidence["maneuver"]["quality_flags"], [])

    def test_rotorcraft_types_and_conflicting_categories(self):
        # maneuvers uses the one rotorcraft rule (rules.rotorcraft).
        self.assertIs(maneuvers.rotorcraft, rules.rotorcraft)
        for typecode in ("EC55", "AS65", "S61", "A139", "H160"):
            self.assertTrue(maneuvers.rotorcraft({"t": typecode}))
        self.assertTrue(maneuvers.rotorcraft({"t": "", "category": "A7"}))
        # A rotorcraft category on a known fixed-wing type is a metadata conflict.
        self.assertFalse(maneuvers.rotorcraft({"t": "C172", "category": "A7"}))
        self.assertFalse(maneuvers.rotorcraft({"t": "P149", "category": "A7"}))
        self.assertTrue(airport_context.fixed_wing("P149"))
        # An unknown or unassigned designator leaves the category to decide.
        self.assertTrue(maneuvers.rotorcraft({"t": "XH1", "category": "A7"}))
        self.assertTrue(maneuvers.rotorcraft({"t": "ZZZZ", "category": "A7"}))
        self.assertFalse(maneuvers.rotorcraft({"t": "ZZZZ", "category": "A1"}))
        classes = types.SimpleNamespace(type_class={"P149": "airplane", "XH1": "helicopter"}.get)
        with patch.object(airport_context, "reference", classes):
            self.assertTrue(airport_context.fixed_wing("P149"))
            self.assertEqual(airport_context.type_class("XH1"), "rotorcraft")

    def test_configured_reference_classes(self):
        self.assertTrue(maneuvers.rotorcraft({"t": "EC35"}))
        self.assertFalse(maneuvers.rotorcraft({"t": "C172", "category": "A7"}))
        self.assertTrue(airport_context.fixed_wing("C172"))

    def test_archive_altitude_supports_the_spread_check(self):
        points = [
            {k: v for k, v in p.items() if k not in ("alt_baro", "baro_rate")}
            | {"archive_alt": p["alt_baro"], "archive": "retained archive"}
            for p in sample(hover_track())
        ]
        finding = self.latest(points)
        self.assertEqual(finding.evidence["maneuver"]["altitude_reference"], "archive_alt")
        self.assertIn("archive_field_freshness_unknown", finding.evidence["maneuver"]["quality_flags"])


class NewShapeTests(unittest.TestCase):
    def test_extreme_turn_needs_a_large_aircraft_and_is_always_reviewed(self):
        tight = sample(Track(altitude=10000, speed=250).straight(120).turn(360, 8).straight(120))
        finding = next(iter(incremental(tight, JET)["extreme_turn_rate"].values()))
        self.assertIn("extreme_turn_review", finding.evidence["maneuver"]["quality_flags"])
        self.assertGreater(finding.value, 6)
        self.assertNotIn("extreme_turn_rate", incremental(tight, LIGHT))
        normal = sample(Track(altitude=10000, speed=250).straight(120).turn(180, 3).straight(120))
        self.assertNotIn("extreme_turn_rate", incremental(normal, JET))

    def test_soaring_needs_glider_evidence_and_a_steady_circling_climb(self):
        points = sample(Track(altitude=2500, speed=55).turn(360 * 7, 2.0, rate=450), wind=(3, 0))
        glider = {"hex": "abc126", "t": "", "category": "B1", "type": "adsb_icao"}
        soaring = [f for f in maneuvers.analyze(points, glider) if f.kind == "thermal_soaring"]
        self.assertEqual(len(soaring), 1)
        self.assertGreaterEqual(soaring[0].evidence["maneuver"]["climb_gain_ft"], 500)
        self.assertNotIn("thermal_soaring", {f.kind for f in maneuvers.analyze(points, LIGHT)})

    def test_stratospheric_balloon_is_corroborated_and_tolerates_a_speed_glitch(self):
        points = []
        for i in range(80):
            lat, lon = ll(i * 15 * 10 / 3600, 0)
            points.append(
                {
                    "ts": round(T0 + i * 10, 1),
                    "lat": lat,
                    "lon": lon,
                    "gs": 1005.6 if i == 40 else 15,
                    "alt_baro": 65000 + (i % 3) * 25,
                    "alt_geom": 66500,
                    "baro_rate": 0,
                    "source": "adsb_icao",
                    "flight": "HBAL837",
                }
            )
        finding = next(
            f
            for f in maneuvers.analyze(points, {"hex": "abc127", "t": ""})
            if f.kind == "stratospheric_balloon"
        )
        info = finding.evidence["maneuver"]
        self.assertTrue(info["confirmed"])
        self.assertEqual(finding.category, "airframes")
        self.assertIn("balloon_callsign", info["corroboration"])
        self.assertEqual(info["max_altitude_ft"], 65050)
        bare = [{k: v for k, v in p.items() if k not in ("alt_geom", "flight")} for p in points[:70]]
        finding = next(
            f
            for f in maneuvers.analyze(bare, {"hex": "abc127", "t": ""})
            if f.kind == "stratospheric_balloon"
        )
        self.assertFalse(finding.evidence["maneuver"]["confirmed"])
        fast = [{**p, "gs": 450} for p in points]
        self.assertNotIn(
            "stratospheric_balloon", {f.kind for f in maneuvers.analyze(fast, {"hex": "abc127"})}
        )


def grid_legs(count=8, spacing=0.5, length=4.0, minutes=36, altitude=4000, jitter=0.0):
    legs, start = [], T0
    seconds = minutes * 60 / count
    for index in range(count):
        y = index * spacing * (1 + (jitter if index % 2 else -jitter))
        x0, x1 = (0, length) if index % 2 == 0 else (length, 0)
        a, b = ll(x0, y), ll(x1, y)
        legs.append(
            {
                "started_at": start,
                "ended_at": start + seconds - 30,
                "start": list(a),
                "end": list(b),
                "length_nm": length,
                "alt_min": altitude,
                "alt_max": altitude + 100,
                "source": "adsb_icao",
            }
        )
        start += seconds
    return legs


class SurveyGridTests(unittest.TestCase):
    def test_strict_grid_is_recognised(self):
        grid = maneuvers.survey_grid(grid_legs())
        self.assertEqual(grid["leg_count"], 8)
        self.assertAlmostEqual(grid["leg_spacing_nm"], 0.5, places=2)

    def test_short_irregular_racetrack_or_brief_patterns_are_not_grids(self):
        self.assertIsNone(maneuvers.survey_grid(grid_legs(minutes=20)))
        self.assertIsNone(maneuvers.survey_grid(grid_legs(jitter=0.6)))
        self.assertIsNone(maneuvers.survey_grid(grid_legs(length=2.0)))
        racetrack = grid_legs()
        for index, leg in enumerate(racetrack):
            y = 0 if index % 2 == 0 else 1
            leg["start"][0] = leg["end"][0] = LAT0 + y / 60
        self.assertIsNone(maneuvers.survey_grid(racetrack))

    def test_a_line_split_between_summaries_and_the_buffer_is_rejoined(self):
        track = Track(speed=60, altitude=4000)
        for index in range(8):
            track.straight(240)
            if index < 7:
                track.turn(180 if index % 2 == 0 else -180, 3.82)
        points = sample(track)
        for split in range(len(points) // 2 - 30, len(points) // 2 + 31, 6):
            with self.subTest(split=split):
                legs = [
                    *maneuvers.straight_legs(points[:split]),
                    *maneuvers.straight_legs(points[split - 20 :]),
                ]
                self.assertEqual(maneuvers.survey_grid(legs)["leg_count"], 8)

    def test_uncertain_reports_keep_their_flags_through_legs_history_and_merging(self):
        # Eight grid lines whose geometry passes every survey threshold.
        def grid(**extra):
            track = Track(speed=60, altitude=4000)
            for index in range(8):
                track.straight(240)
                if index < 7:
                    track.turn(180 if index % 2 == 0 else -180, 3.82)
            return sample(track, **extra)

        poor = {
            "mlat_position": ({"mlat": ["lat", "lon"]}, "uncertain_source"),
            "tisb_position": ({"tisb": ["lat", "lon"]}, "uncertain_source"),
            "zero_integrity": ({"nic": 0, "nac_p": 0}, "low_position_quality"),
        }
        for name, (extra, flag) in poor.items():
            points, half = grid(**extra), len(grid()) // 2
            with self.subTest(data=name, legs="current window"):
                legs = maneuvers.straight_legs(points)
                self.assertTrue(legs and all(flag in leg["quality_flags"] for leg in legs))
                self.assertIn(
                    flag,
                    maneuvers._survey_finding(maneuvers.survey_grid(legs)).evidence["maneuver"][
                        "quality_flags"
                    ],
                )
            with self.subTest(data=name, legs="retained history"):
                # Only the reports before the buffer were poor; their legs come back as history.
                history = maneuvers.straight_legs(points[:half])
                found = {f.kind: f for f in maneuvers.analyze(grid()[half:], LIGHT, legs=history)}
                info = found["survey_grid"].evidence["maneuver"]
                self.assertEqual(info["leg_count"], 8)
                self.assertIn(flag, info["quality_flags"])
                self.assertTrue(info["quality_review_required"])
            with self.subTest(data=name, legs="merged"):
                # A line split between a poor summary and a clean buffer stays marked once joined.
                clean = grid()
                split = len(clean) // 2
                legs = [
                    *maneuvers.straight_legs(points[:split]),
                    *maneuvers.straight_legs(clean[split - 20 :]),
                ]
                merged = maneuvers.merge_legs(legs)
                self.assertLess(len(merged), len(legs))
                self.assertTrue(any(flag in leg["quality_flags"] for leg in merged))
                grid_found = maneuvers.survey_grid(legs)
                self.assertEqual(grid_found["leg_count"], 8)
                self.assertIn(
                    flag, maneuvers._survey_finding(grid_found).evidence["maneuver"]["quality_flags"]
                )
        # Clean reports leave the legs and the grid unflagged, as before.
        legs = maneuvers.straight_legs(grid())
        self.assertTrue(legs and all(leg["quality_flags"] == [] for leg in legs))
        info = maneuvers._survey_finding(maneuvers.survey_grid(legs)).evidence["maneuver"]
        self.assertEqual((info["quality_flags"], info["quality_review_required"]), ([], False))
        # Summaries saved without flags, as older ones were, merge and build without them.
        bare = [{key: value for key, value in leg.items() if key != "quality_flags"} for leg in legs]
        self.assertEqual(
            maneuvers._survey_finding(maneuvers.survey_grid(bare)).evidence["maneuver"]["quality_flags"], []
        )

    def test_a_grid_needs_altitude_on_every_leg(self):
        def grid():
            track = Track(speed=60, altitude=4000)
            for index in range(8):
                track.straight(240)
                if index < 7:
                    track.turn(180 if index % 2 == 0 else -180, 3.82)
            return sample(track)

        def flags(points, history=None):
            legs = [*(history or []), *maneuvers.straight_legs(points)]
            found = maneuvers.survey_grid(legs)
            self.assertEqual(found["leg_count"], 8)
            return maneuvers._survey_finding(found).evidence["maneuver"]["quality_flags"]

        full = grid()
        self.assertEqual(flags(full), [])
        # Altitude only in the first minute: one measured leg must not carry the grid.
        first_minute = [
            p if p["ts"] <= T0 + 60 else {k: v for k, v in p.items() if k != "alt_baro"} for p in full
        ]
        self.assertEqual(flags(first_minute), ["altitude_coverage_incomplete"])
        # Missing on later legs only, with the early ones complete.
        later = [p if p["ts"] <= T0 + 1300 else {k: v for k, v in p.items() if k != "alt_baro"} for p in full]
        self.assertEqual(flags(later), ["altitude_coverage_incomplete"])
        # Sparse readings within one leg: a third of its reports is not coverage.
        sparse = [
            p
            if not (T0 + 300 <= p["ts"] <= T0 + 540) or int(p["ts"] - T0) % 30 == 0
            else {k: v for k, v in p.items() if k != "alt_baro"}
            for p in full
        ]
        self.assertEqual(flags(sparse), ["altitude_coverage_incomplete"])
        legs = maneuvers.straight_legs(sparse)
        self.assertTrue(any(leg["altitude_coverage"] < 0.5 for leg in legs))
        self.assertTrue(all(0 <= leg["altitude_coverage"] <= 1 for leg in legs))
        # A retained history leg without altitude flags the grid too, and the flag
        # survives merging with a measured copy of the same line.
        half = len(full) // 2
        bare = [{k: v for k, v in p.items() if k != "alt_baro"} for p in full[:half]]
        self.assertEqual(flags(full[half:], maneuvers.straight_legs(bare)), ["altitude_coverage_incomplete"])
        merged = maneuvers.merge_legs(
            [*maneuvers.straight_legs(bare[: half - 10]), *maneuvers.straight_legs(full[half - 40 : half])]
        )
        self.assertLess(min(leg["altitude_coverage"] for leg in merged), 0.5)
        # The flag blocks publication without discarding the shape.
        info = maneuvers._survey_finding(maneuvers.survey_grid(maneuvers.straight_legs(later))).evidence
        self.assertTrue(info["maneuver"]["quality_review_required"])
        self.assertTrue(info["maneuver"]["confirmed"])

    def test_earlier_legs_upgrade_parallel_passes(self):
        # Eight 4-NM lines 0.5 NM apart at 60 kn take about 36 minutes, beyond the
        # 20-minute buffer; the collector supplies the earlier legs.
        track = Track(speed=60, altitude=4000)
        for index in range(8):
            track.straight(240)
            if index < 7:
                track.turn(180 if index % 2 == 0 else -180, 3.82)
        points = sample(track)
        half = len(points) // 2
        history = maneuvers.straight_legs(points[:half])
        window = points[half:]
        self.assertGreaterEqual(len(history), 3)
        kinds = {f.kind for f in maneuvers.analyze(window, LIGHT)}
        self.assertNotIn("survey_grid", kinds)
        found = {f.kind: f for f in maneuvers.analyze(window, LIGHT, legs=history)}
        self.assertIn("survey_grid", found)
        self.assertNotIn("parallel_passes", found)
        info = found["survey_grid"].evidence["maneuver"]
        self.assertEqual(info["leg_count"], 8)
        self.assertAlmostEqual(info["leg_spacing_nm"], 0.5, delta=0.05)
        self.assertGreaterEqual(info["duration_seconds"], 1800)


class AerobaticPractice(unittest.TestCase):
    def sawtooth(
        self, *, typecode="PTS2", category="A1", turn_dps=8.0, period=90, amplitude=2000, seconds=720
    ):
        points = []
        for i in range(seconds):
            phase = (i % period) / period
            alt = 3000 + amplitude * (phase * 2 if phase < 0.5 else 2 - phase * 2)
            heading = (i * turn_dps) % 360
            lat = 42.50 + 0.01 * math.sin(math.radians(heading))
            lon = -83.40 + 0.01 * math.cos(math.radians(heading))
            points.append(
                {
                    "ts": 1_700_000_000 + i,
                    "lat": lat,
                    "lon": lon,
                    "alt_baro": alt,
                    "gs": 120,
                    "track": heading,
                    "source": "adsb_icao",
                }
            )
        aircraft = {"hex": "a1b2c3", "t": typecode, "category": category, "r": "N12AB"}
        return points, aircraft

    def test_aerobatic_type_with_large_reversals_fires(self):
        points, aircraft = self.sawtooth()
        kinds = {f.kind: f for f in maneuvers.trajectory(points, aircraft)}
        self.assertIn("aerobatic_practice", kinds)
        info = kinds["aerobatic_practice"].evidence["maneuver"]
        self.assertGreaterEqual(info["reversals"], 5)
        self.assertLessEqual(info["span_nm"], 5)
        self.assertEqual(info["basis"], "type")

    def test_light_category_needs_turn_rate(self):
        points, aircraft = self.sawtooth(typecode="C172", turn_dps=2.0)
        self.assertNotIn("aerobatic_practice", {f.kind for f in maneuvers.trajectory(points, aircraft)})
        points, aircraft = self.sawtooth(typecode="C172", turn_dps=8.0)
        found = {f.kind: f for f in maneuvers.trajectory(points, aircraft)}
        self.assertEqual(found["aerobatic_practice"].evidence["maneuver"]["basis"], "turn_rate")

    def test_type_basis_without_a_held_turn_omits_the_turn_rate(self):
        points, aircraft = self.sawtooth(turn_dps=2.0)
        found = {f.kind: f for f in maneuvers.trajectory(points, aircraft)}
        info = found["aerobatic_practice"].evidence["maneuver"]
        self.assertEqual(info["basis"], "type")
        self.assertNotIn("max_turn_rate_dps", info)

    def test_turn_rate_is_the_best_rate_held_for_twenty_seconds(self):
        track = Track(speed=120).straight(60).turn(200, 6.5).turn(300, 12.0).straight(60)
        rate = maneuvers._max_turn_rate(sample(track, period=1.0))
        self.assertGreater(rate, 10.0)

    def test_small_reversals_do_not_count(self):
        points, aircraft = self.sawtooth(amplitude=600)
        self.assertNotIn("aerobatic_practice", {f.kind for f in maneuvers.trajectory(points, aircraft)})

    def test_pinned_type_table_keeps_only_listed_designators(self):
        listed = {"PTS2": {"designator": "PTS2"}}
        with (
            patch.object(maneuvers.reference, "GENERATION", -1),
            patch.object(maneuvers.reference, "dataset", return_value={"types": {}}),
            patch.object(maneuvers.reference, "type_info", side_effect=listed.get),
        ):
            self.assertEqual(maneuvers.aerobatic_types(), frozenset({"PTS2"}))
        self.assertIn("E330", maneuvers.aerobatic_types())


class RacetrackOrbit(unittest.TestCase):
    def legs(
        self, *, length_nm=12.0, spacing_nm=6.0, count=4, leg_seconds=720, turn_seconds=180, drift_nm=0.0
    ):
        lat0, lon0 = 42.0, -84.0
        dlon = 1 / (60 * math.cos(math.radians(lat0)))
        legs, t = [], 1_700_000_000.0
        for i in range(count):
            east = i % 2 == 0
            lat = lat0 + (spacing_nm / 60 if east else 0) + (drift_nm * i / 60)
            start = [lat, lon0] if east else [lat, lon0 + length_nm * dlon]
            end = [lat, lon0 + length_nm * dlon] if east else [lat, lon0]
            legs.append(
                {
                    "started_at": t,
                    "ended_at": t + leg_seconds,
                    "heading": 90.0 if east else 270.0,
                    "length_nm": length_nm,
                    "start": start,
                    "end": end,
                }
            )
            t += leg_seconds + turn_seconds
        return legs

    def pos(self, legs):
        return [
            {
                "ts": leg["started_at"],
                "lat": leg["start"][0],
                "lon": leg["start"][1],
                "source": "adsb_icao",
                "alt_baro": 25000,
            }
            for leg in legs
        ] + [
            {
                "ts": legs[-1]["ended_at"],
                "lat": legs[-1]["end"][0],
                "lon": legs[-1]["end"][1],
                "source": "adsb_icao",
                "alt_baro": 25000,
            }
        ]

    def test_four_reciprocal_long_legs_form_a_racetrack(self):
        legs = self.legs()
        aircraft = {"hex": "ae1234", "t": "K35R", "dbFlags": 1, "r": "58-0001"}
        found = maneuvers.racetrack(legs, self.pos(legs), aircraft)
        self.assertIsNotNone(found)
        info = found.evidence["maneuver"]
        self.assertEqual(info["racetrack_legs"], 4)
        self.assertNotIn("legs", info)
        self.assertNotIn("duration_s", info)
        self.assertAlmostEqual(info["lateral_spacing_nm"], 6.0, delta=0.3)
        self.assertGreaterEqual(info["duration_seconds"], 2400)
        self.assertIn("tanker_type", info["context_tags"])
        self.assertIn("military_flag", info["context_tags"])

    def test_short_legs_three_legs_or_wide_spacing_do_not(self):
        self.assertIsNone(
            maneuvers.racetrack(self.legs(length_nm=6.0), self.pos(self.legs(length_nm=6.0)), {})
        )
        self.assertIsNone(maneuvers.racetrack(self.legs(count=3), self.pos(self.legs(count=3)), {}))
        self.assertIsNone(
            maneuvers.racetrack(self.legs(spacing_nm=20.0), self.pos(self.legs(spacing_nm=20.0)), {})
        )

    def test_stepping_lines_are_not_a_racetrack(self):
        legs = self.legs(drift_nm=1.5)
        self.assertIsNone(maneuvers.racetrack(legs, self.pos(legs), {}))

    @staticmethod
    def line(started_at, center, heading, *, length_nm=12.0, seconds=720):
        """One leg through ``center`` ([lat, lon]) at ``heading``."""
        dlon = 1 / (60 * math.cos(math.radians(center[0])))
        half_east = length_nm / 2 * math.sin(math.radians(heading)) * dlon
        half_north = length_nm / 2 * math.cos(math.radians(heading)) / 60
        return {
            "started_at": started_at,
            "ended_at": started_at + seconds,
            "length_nm": length_nm,
            "start": [center[0] - half_north, center[1] - half_east],
            "end": [center[0] + half_north, center[1] + half_east],
        }

    def test_transit_legs_do_not_bias_the_axis(self):
        legs = self.legs()
        start = legs[0]["started_at"]
        transits = [
            self.line(start - 2400, [41.6, -84.5], 30.0, length_nm=20.0, seconds=900),
            self.line(start - 1400, [41.8, -84.3], 45.0, length_nm=20.0, seconds=1000),
        ]
        found = maneuvers.racetrack([*transits, *legs], self.pos(legs), {})
        self.assertIsNotNone(found)
        info = found.evidence["maneuver"]
        self.assertEqual(info["racetrack_legs"], 4)
        self.assertAlmostEqual(info["axis_heading_deg"], 90.0, delta=1.0)
        self.assertEqual(info["started_at"], start)

    def test_axis_wraps_through_north(self):
        lat0, lon0 = 42.0, -84.0
        east = 6 / (60 * math.cos(math.radians(lat0)))
        legs, t = [], 1_700_000_000.0
        for heading in (358.0, 182.0, 2.0, 178.0):
            northbound = math.cos(math.radians(heading)) > 0
            legs.append(self.line(t, [lat0, lon0 if northbound else lon0 + east], heading))
            t += 900
        for ordered in (legs, legs[::-1]):
            found = maneuvers.racetrack(ordered, self.pos(legs), {})
            self.assertIsNotNone(found)
            info = found.evidence["maneuver"]
            self.assertLessEqual(abs((info["axis_heading_deg"] - 180 + 90) % 180 - 90), 0.5)
            self.assertAlmostEqual(info["lateral_spacing_nm"], 6.0, delta=0.1)

    def test_a_leg_slanting_off_its_line_fails_the_repeat(self):
        legs = self.legs()
        # Same midpoint, but turned 8 degrees: the ends sit about 1.7 NM off the line.
        slanted = self.line(
            legs[2]["started_at"],
            [(legs[2]["start"][0] + legs[2]["end"][0]) / 2, (legs[2]["start"][1] + legs[2]["end"][1]) / 2],
            98.0,
            length_nm=24.0,
        )
        legs[2] = slanted
        self.assertIsNone(maneuvers.racetrack(legs, self.pos(legs), {}))

    def test_runway_distance_is_unknown_near_or_outside_the_table_edge(self):
        self.assertIsNone(maneuvers._nearest_runway_nm([38.3, -84.0]))
        self.assertIsNone(maneuvers._nearest_runway_nm([30.0, -84.0]))
        self.assertIsNotNone(maneuvers._nearest_runway_nm([42.0, -84.0]))

    def test_trajectory_joins_saved_and_buffered_legs_into_a_racetrack(self):
        track = Track(altitude=25000, speed=240)
        for index in range(6):
            track.straight(450)
            if index < 5:
                track.turn(180, 3.0)
        points = sample(track)
        half = len(points) // 2
        history = maneuvers.straight_legs(points[:half])
        aircraft = {**JET, "t": "K35R", "dbFlags": 1}
        found = {f.kind: f for f in maneuvers.analyze(points[half:], aircraft, legs=history)}
        self.assertIn("racetrack_orbit", found)
        self.assertNotIn("survey_grid", found)
        info = found["racetrack_orbit"].evidence["maneuver"]
        self.assertEqual(info["racetrack_legs"], 6)
        self.assertAlmostEqual(info["lateral_spacing_nm"], 2.55, delta=0.2)
        self.assertEqual(info["quality_flags"], [])


if __name__ == "__main__":
    unittest.main()
