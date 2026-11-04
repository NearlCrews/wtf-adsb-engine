"""ISA atmosphere, airspeed conversions, rated-limit verdicts and the record plausibility gate."""

import unittest
from unittest.mock import patch

from wtf_adsb_engine.analysis import curation, limits, reference, rules

NOW = 1_791_080_000
B738 = {"max_operating_altitude_ft": 41000, "vmo_kias": 340, "mmo": 0.82, "vne_kias": None}
LIGHT = {"max_operating_altitude_ft": None, "vmo_kias": None, "mmo": None, "vne_kias": 200}


def report(**fields):
    return {
        "hex": "a061d9",
        "r": "N12345",
        "seen": 0,
        "seen_pos": 0,
        "lat": 42.0,
        "lon": -84.0,
        "type": "adsb_icao",
        **fields,
    }


class AtmosphereTests(unittest.TestCase):
    def test_isa_anchors(self):
        self.assertAlmostEqual(limits.isa_temperature_k(0), 288.15, places=2)
        self.assertAlmostEqual(limits.isa_pressure_hpa(0), 1013.25, places=2)
        self.assertAlmostEqual(limits.density_ratio(0), 1.0, places=6)
        self.assertAlmostEqual(limits.speed_of_sound_kt(0), 661.5, places=1)
        # The tropopause: 11,000 m = 36,089 ft.
        self.assertAlmostEqual(limits.isa_temperature_k(36089), 216.65, places=2)
        self.assertAlmostEqual(limits.isa_pressure_hpa(36089), 226.32, places=1)
        # Isothermal above it, then the lapse reverses from 20 km (65,617 ft).
        self.assertAlmostEqual(limits.isa_temperature_k(50000), 216.65, places=2)
        self.assertAlmostEqual(limits.isa_pressure_hpa(65617), 54.75, delta=0.1)
        self.assertGreater(limits.isa_temperature_k(70000), 216.65)
        self.assertAlmostEqual(limits.isa_pressure_hpa(10000), 696.8, delta=0.3)
        self.assertAlmostEqual(limits.isa_pressure_hpa(18000), 506.0, delta=0.3)
        self.assertAlmostEqual(limits.density_ratio(30000), 0.3741, places=3)
        with self.assertRaises(ValueError):
            limits.isa_pressure_hpa(None)

    def test_airspeed_conversions_against_known_values(self):
        # 250 KEAS at 10,000 ft is about 291 KTAS; 340 KEAS at 37,000 ft about 638 KTAS.
        self.assertAlmostEqual(limits.true_airspeed_kt(250, 10000), 290.9, delta=0.5)
        self.assertAlmostEqual(limits.equivalent_airspeed_kt(290.9, 10000), 250, delta=0.5)
        self.assertAlmostEqual(limits.true_airspeed_kt(340, 37000), 637.6, delta=0.5)
        # Mach 0.82 at 37,000 ft is about 470 KTAS; Mach 1 at sea level 661.5 kt.
        self.assertAlmostEqual(limits.mach(470.3, 37000), 0.82, places=3)
        self.assertAlmostEqual(limits.mach(661.5, 0), 1.0, places=3)
        self.assertAlmostEqual(limits.speed_of_sound_kt(37000), 573.6, delta=0.2)

    def test_true_airspeed_from_ground_speed_and_wind(self):
        # Wind direction is where it comes from: a 50-kn wind from 270° carries an
        # eastbound aircraft (tailwind), one from 090° opposes it (headwind).
        self.assertEqual(limits.tas_from_ground_speed(500, 90, 50, 270), (450.0, 90.0))
        self.assertEqual(limits.tas_from_ground_speed(400, 90, 50, 90), (450.0, 90.0))
        # A pure crosswind: the aircraft crabs into it, so TAS exceeds ground speed slightly.
        tas, heading = limits.tas_from_ground_speed(300, 0, 40, 270)
        self.assertAlmostEqual(tas, 302.65, places=1)
        self.assertAlmostEqual(heading, 352.4, places=1)
        # Calm air leaves the ground vector unchanged.
        self.assertEqual(limits.tas_from_ground_speed(300, 45, 0, 0), (300.0, 45.0))
        for missing in ((None, 90, 50, 270), (500, None, 50, 270), (500, 90, None, 270), (500, 90, 50, None)):
            self.assertIsNone(limits.tas_from_ground_speed(*missing))
        self.assertIsNone(limits.tas_from_ground_speed(-5, 90, 50, 270))


class VerdictTests(unittest.TestCase):
    def test_altitude_verdicts(self):
        self.assertEqual(limits.altitude_verdict(B738, 41999), "within")
        self.assertEqual(limits.altitude_verdict(B738, 42000), "exceeded")
        self.assertEqual(limits.altitude_verdict(B738, 61499), "exceeded")
        self.assertEqual(limits.altitude_verdict(B738, 61500), "implausible")
        self.assertIsNone(limits.altitude_verdict(LIGHT, 20000))
        self.assertIsNone(limits.altitude_verdict(B738, None))
        self.assertIsNone(limits.altitude_verdict(None, 42000))

    def test_speed_verdicts_use_eas_against_vmo_and_mach_against_mmo(self):
        # At 37,000 ft Mmo 0.82 binds (470 KTAS); 10% over is 517 KTAS, 50% over 705.
        self.assertEqual(limits.speed_verdict(B738, 500, 37000), "within")
        self.assertEqual(limits.speed_verdict(B738, 520, 37000), "exceeded")
        self.assertEqual(limits.speed_verdict(B738, 720, 37000), "implausible")
        # Low down Vmo binds: 340 KEAS at 5,000 ft is about 366 KTAS; 10% over is 403.
        self.assertEqual(limits.speed_verdict(B738, 400, 5000), "within")
        self.assertEqual(limits.speed_verdict(B738, 410, 5000), "exceeded")
        # A Vne-only light type compares the same way.
        self.assertEqual(limits.speed_verdict(LIGHT, 215, 3000), "within")
        self.assertEqual(limits.speed_verdict(LIGHT, 235, 3000), "exceeded")
        self.assertIsNone(limits.speed_verdict({"mmo": None, "vmo_kias": None}, 500, 37000))
        self.assertIsNone(limits.speed_verdict(B738, 500, None))
        self.assertIsNone(limits.speed_verdict(B738, None, 37000))
        # The verdict is the reading over the limit that binds at its altitude.
        self.assertEqual(limits.binding_limit(B738, 37000)[1], "mmo")
        self.assertEqual(limits.binding_limit(B738, 5000)[1], "vmo_kias")
        self.assertEqual(limits.ratio_verdict(1.0999), "within")
        self.assertEqual(limits.ratio_verdict(1.10), "exceeded")
        self.assertEqual(limits.ratio_verdict(1.5), "implausible")

    def test_the_speed_comparison_counts_one_consecutive_run(self):
        # Two reports over the limit, one within it, then five over it: the five are the run.
        def corrected(index, verdict):
            return {
                "ts": NOW + 15 * index,
                "gs": 560.0 + index,
                "alt_baro": 37000,
                "airspeed_kt": 540.0 if verdict == "exceeded" else 500.0,
                "airspeed_basis": "reported",
                "wind": None,
                "verdict": verdict,
            }

        verdicts = ["exceeded", "exceeded", "within", *["exceeded"] * 5]
        samples = [corrected(index, verdict) for index, verdict in enumerate(verdicts)]
        # Out of order on purpose: the run follows report time.
        run = limits.exceeded_run(list(reversed(samples)))
        self.assertEqual([sample["ts"] for sample in run], [NOW + 15 * index for index in range(3, 8)])
        summary = limits.speed_comparison(B738, samples)
        self.assertEqual((summary["reports"], summary["seconds"], summary["first"]), (5, 60, NOW + 45))
        self.assertEqual(summary["ground_speed_kt"], 563.0)
        # Only a sustained run counts: the two reports before the gap never do, even with a
        # longer span than the run; without one there is no comparison to state.
        early = [corrected(0, "exceeded"), {**corrected(1, "exceeded"), "ts": NOW + 90}]
        sparse = [*early, {**corrected(2, "within"), "ts": NOW + 100}]
        sparse += [{**corrected(index, "exceeded"), "ts": NOW + 105 + 15 * index} for index in range(5)]
        self.assertEqual(limits.exceeded_run(sparse)[0]["ts"], NOW + 105)
        self.assertEqual(limits.exceeded_run(sparse, 1, 0)[0]["ts"], NOW)
        self.assertIsNone(limits.speed_comparison(B738, early))
        # Below the thresholds the longest span wins, and a tie goes to the earlier run.
        tied = [corrected(index, verdict) for index, verdict in enumerate(["exceeded", "within", "exceeded"])]
        self.assertEqual([sample["ts"] for sample in limits.exceeded_run(tied, 1, 0)], [NOW])
        self.assertEqual(limits.exceeded_run([corrected(0, "within")], 1, 0), [])
        self.assertIsNone(limits.speed_comparison(B738, [corrected(0, "within")]))

    def test_a_vmo_schedule_is_carried_as_its_highest_value(self):
        # 286 KIAS at 18,000 ft is about 379 KTAS; 360 KTAS is within, and so is 245-KIAS flying lower.
        dash = reference.limits("DH8D")
        self.assertEqual(limits.speed_verdict(dash, 360, 18000), "within")
        self.assertEqual(limits.speed_verdict(dash, 300, 10000), "within")
        self.assertEqual(limits.speed_verdict(dash, 420, 18000), "exceeded")
        self.assertAlmostEqual(limits.limiting_tas_kt(dash, 18000), 378.8, delta=0.5)

    def test_limiting_true_airspeed_and_plausible_ground_speed(self):
        self.assertAlmostEqual(limits.limiting_tas_kt(B738, 37000), 470.3, delta=0.2)
        self.assertAlmostEqual(limits.limiting_tas_kt(B738, 5000), 366.4, delta=0.3)
        self.assertIsNone(limits.limiting_tas_kt({"vmo_kias": None, "mmo": None}, 5000))
        self.assertAlmostEqual(limits.max_plausible_ground_speed_kt(B738, 37000, 170), 640.3, delta=0.2)
        # Without an altitude the largest limiting airspeed below the ceiling applies
        # (the Vmo/Mmo crossover near 26,000 ft, about 496 KTAS).
        self.assertAlmostEqual(limits.max_plausible_ground_speed_kt(B738, None, 170), 665.6, delta=0.5)
        self.assertIsNone(limits.max_plausible_ground_speed_kt({"vmo_kias": None, "mmo": None}, 5000, 60))
        self.assertEqual(limits.speed_limit(LIGHT), ("vne_kias", 200))
        self.assertIsNone(limits.speed_limit({}))

    def test_one_descriptor_names_each_certified_limit(self):
        # Every limit field the dataset carries, in the order the airspeed limits are preferred.
        self.assertEqual(set(limits.LIMITS), set(reference.LIMIT_FIELDS) - {"name", "source"})
        self.assertEqual(limits.SPEED_FIELDS, ("vmo_kias", "vne_kias"))
        self.assertEqual(
            {key: limit.field for key, limit in limits.LIMITS.items()},
            {
                "max_operating_altitude_ft": "max_operating_altitude_ft",
                "vmo_kias": "vmo",
                "vne_kias": "vne",
                "mmo": "mmo",
            },
        )
        self.assertEqual(limits.LIMITS["vne_kias"].name, "never-exceed speed")
        self.assertEqual(limits.LIMITS["mmo"].card_key, "rated_mmo")


class RecordPlausibilityTests(unittest.TestCase):
    """Season records refuse readings beyond the envelope or a rated limit (rules.speed_plausible,
    which the collector and the archive check before the record rule)."""

    def record(self, typecode, gs, altitude, previous):
        season = {
            "type": typecode,
            "season_start": "2026-09-01",
            "baseline_start": "2026-09-01",
            "baseline_days": 30,
            "max_gs": previous,
            "addresses": 40,
        }
        sustained = {"gs": gs, "duration_seconds": 75, "samples": 8, "source": "adsb_icao"}
        aircraft = report(t=typecode, gs=gs)
        if altitude is not None:
            aircraft["alt_baro"] = altitude
        if not rules.speed_plausible(aircraft, gs):
            return None
        return rules.seasonal_speed_record(aircraft, season, sustained=sustained)

    def test_a_baron_at_432_knots_low_down_is_bad_data_not_a_record(self):
        # BE58: 300-kn bound plus the 60-kn allowance below 10,000 ft.
        self.assertIsNone(self.record("BE58", 432, 5000, 230))
        self.assertFalse(rules.speed_plausible(report(t="BE58", alt_baro=5000), 432))
        # The same speed in the jet stream band would pass the envelope (300 + 170).
        self.assertTrue(rules.speed_plausible(report(t="BE58", alt_baro=35000), 432))
        self.assertIsNone(self.record("BE20", 452, 8000, 300))

    def test_archived_and_geometric_altitudes_choose_the_wind_band(self):
        # Heatmap fixes keep only archive_alt and geometric trace fixes only alt_geom: each
        # still picks its band, instead of the 170-kn allowance of an unknown altitude.
        for fields in ({"archive_alt": 5000}, {"alt_geom": 5000}, {"alt_baro": "ground", "alt_geom": 5000}):
            with self.subTest(fields=fields):
                self.assertFalse(rules.speed_plausible({"t": "BE58", **fields}, 432))
                self.assertEqual(rules.type_envelope({"t": "BE58", **fields})["wind_band_ft"], [None, 10000])
        # Pressure altitude wins over the others, and a non-numeric archive altitude is no band.
        self.assertTrue(rules.speed_plausible({"t": "BE58", "alt_baro": 35000, "archive_alt": 5000}, 432))
        self.assertTrue(rules.speed_plausible({"t": "BE58", "archive_alt": "ground"}, 432))
        # The rated gate reads the same altitude: a 737-800 at 440 kn near the surface.
        self.assertFalse(rules.speed_plausible({"t": "B738", "archive_alt": 3000}, 440))
        self.assertTrue(rules.speed_plausible({"t": "B738", "alt_geom": 37000}, 630))

    def test_a_baron_at_240_knots_is_a_record_given_a_baseline(self):
        finding = self.record("BE58", 240, 5000, 225)
        self.assertEqual(finding.kind, "seasonal_speed_record")
        self.assertEqual(finding.value, 240)

    def test_a_regional_jet_at_563_knots_in_the_jet_stream_is_still_a_record(self):
        # CRJ7 has no speed bound and no limits entry.
        self.assertEqual(self.record("CRJ7", 563, 37000, 540).value, 563)
        # A type with certified limits: 737-800 Mmo 0.82 at 37,000 ft is 470 KTAS, plus 170 kn of wind.
        self.assertEqual(self.record("B738", 563, 37000, 540).value, 563)
        self.assertIsNone(self.record("B738", 650, 37000, 540))
        # Near the surface only 60 kn of wind are allowed over the 355-KTAS Vmo equivalent.
        self.assertIsNone(self.record("B738", 440, 3000, 400))
        self.assertEqual(self.record("B738", 410, 3000, 395).value, 410)
        # An unknown altitude uses the largest limiting airspeed and allowance.
        self.assertEqual(self.record("B738", 650, None, 540).value, 650)
        self.assertIsNone(self.record("B738", 680, None, 540))

    def test_record_needs_a_margin_and_a_30_day_baseline(self):
        # report()'s address and registration agree, so no identity conflict refuses it.
        aircraft = report(t="SR22")
        season = {
            "type": "SR22",
            "season_start": "2026-09-01",
            "baseline_start": "2026-09-01",
            "max_gs": 200.0,
            "addresses": 25,
            "baseline_days": 35,
        }
        sustained = {"gs": 205.0, "duration_seconds": 90, "samples": 6, "source": "adsb_icao"}
        self.assertIsNone(rules.seasonal_speed_record(aircraft, season, sustained=sustained))
        sustained["gs"] = 211.0
        found = rules.seasonal_speed_record(aircraft, season, sustained=sustained)
        self.assertEqual(found.evidence["season_record"]["margin_kn"], 11.0)
        self.assertIn("by 11 kn", found.why)
        season["baseline_days"] = 20
        self.assertIsNone(rules.seasonal_speed_record(aircraft, season, sustained=sustained))

    def test_a_fast_type_needs_a_three_percent_margin(self):
        # 3% of 540 kn is 16.2 kn, more than the 10-kn floor.
        self.assertIsNone(self.record("CRJ7", 555, 37000, 540))
        self.assertEqual(self.record("CRJ7", 557, 37000, 540).value, 557)

    def test_without_the_envelope_file_the_rated_gate_still_applies(self):
        with patch.object(reference, "envelope", return_value=None):
            self.assertFalse(rules.speed_plausible(report(t="B738", alt_baro=3000), 440))
            self.assertTrue(rules.speed_plausible(report(t="B738", alt_baro=37000), 630))
            self.assertTrue(rules.speed_plausible(report(t="C5M", alt_baro=37000), 700))
            self.assertFalse(rules.speed_plausible(report(t="C5M", alt_baro=37000), 901))


class RatedFindingTests(unittest.TestCase):
    def kinds(self, **fields):
        return {f.kind: f for f in rules.candidates(report(**fields), NOW)}

    def test_altitude_findings_follow_the_certified_limit(self):
        self.assertNotIn("above_rated_altitude", self.kinds(t="B738", gs=450, alt_baro=41900))
        found = self.kinds(t="B738", gs=450, alt_baro=42500)["above_rated_altitude"]
        block = found.evidence["rated_limit"]
        self.assertEqual(found.category, "performance")
        self.assertEqual(
            (block["field"], block["limit"], block["unit"], block["reading"]),
            ("alt_baro", 41000, "ft", 42500),
        )
        self.assertEqual(
            (block["verdict"], block["margin_percent"], block["basis"]),
            ("exceeded", 3.7, "type certificate data sheet"),
        )
        self.assertIn("Type Certificate Data Sheet", block["source"]["title"])
        self.assertIn("does not establish a violation", found.explanation)
        self.assertIn("1,500 ft above the 41,000-ft maximum", found.why)
        # Bad data (1.5 times the limit) goes to the envelope guard instead.
        self.assertEqual(set(self.kinds(t="B738", gs=450, alt_baro=62000)), {"type_altitude_mismatch"})
        # A Dash 8 above 28,000 ft.
        self.assertIn("above_rated_altitude", self.kinds(t="DH8D", gs=300, alt_baro=28000))
        self.assertNotIn("above_rated_altitude", self.kinds(t="DH8D", gs=300, alt_baro=27900))
        # Types without an entry get no rated finding.
        self.assertNotIn("above_rated_altitude", self.kinds(t="C5M", gs=450, alt_baro=42500))

    def test_speed_candidates_always_wait_for_the_window_check(self):
        pending = self.kinds(t="B738", gs=600, alt_baro=37000)["above_rated_speed"]
        block = pending.evidence["rated_limit"]
        self.assertEqual(
            (block["field"], block["limit_name"], block["limit"], block["mmo"]),
            ("airspeed", "vmo_kias", 340, 0.82),
        )
        self.assertEqual(block["verdict"], "pending")
        self.assertNotIn("samples", block)
        self.assertIn("verdict pending", pending.title)
        # Mmo 0.82 binds at FL370: about 470 KTAS.
        self.assertIn("exceeds 470 kn, the true airspeed", pending.why)
        self.assertIn("Vmo 340 KIAS / Mmo 0.82 limit", pending.why)
        self.assertIn("no wind assumed", pending.why)
        # Below 10% over the limiting airspeed there is no candidate; 50% over is bad data.
        self.assertNotIn("above_rated_speed", self.kinds(t="B738", gs=510, alt_baro=37000))
        self.assertNotIn("above_rated_speed", self.kinds(t="B738", gs=720, alt_baro=37000))
        # A reported true airspeed decides whether a candidate fires, and is kept for the
        # window check; the verdict still waits for every report (wind.py).
        reported = self.kinds(t="B738", gs=600, tas=530, alt_baro=37000)["above_rated_speed"]
        self.assertEqual(reported.evidence["rated_limit"]["verdict"], "pending")
        # The private text states the margin over the limiting true airspeed, not a verdict.
        self.assertIn(
            "Reported true airspeed 530 kn at 37,000 ft is 13% above 470 kn, the true airspeed at which "
            "Boeing 737-800 (B738) reaches its Vmo 340 KIAS / Mmo 0.82 limit there; ground speed was 600.0 kn.",
            reported.why,
        )
        self.assertNotIn("is exceeded", reported.why)
        self.assertNotIn("above_rated_speed", self.kinds(t="B738", gs=600, tas=430, alt_baro=37000))
        self.assertEqual(found_tags(pending), [])
        # The block records the reading that made the candidate: a true airspeed over the
        # limit with an ordinary ground speed keeps the airspeed and its margin.
        slow = self.kinds(t="B738", gs=450, tas=530, alt_baro=37000)["above_rated_speed"]
        block = slow.evidence["rated_limit"]
        self.assertEqual(
            (block["reading"], block["reading_field"], block["margin_percent"]), (530, "tas", 12.7)
        )
        self.assertEqual((pending.evidence["rated_limit"]["reading_field"], slow.value), ("gs", 450))

    def test_a_speed_past_the_envelope_cap_is_never_an_ordinary_fast_finding(self):
        # A King Air 200 at 450 kn and 8,000 ft is past its 420-kn envelope cap and a rated
        # candidate; with a learned 300-kn threshold the reading still never becomes "fast".
        profiles = {"BE20": {"gs": 300, "alt": 30000}}
        found = {
            f.kind for f in rules.candidates(report(t="BE20", gs=450, alt_baro=8000), NOW, profiles=profiles)
        }
        self.assertEqual(found, {"above_rated_speed"})
        # Under the cap the threshold still applies.
        found = {
            f.kind for f in rules.candidates(report(t="BE20", gs=350, alt_baro=8000), NOW, profiles=profiles)
        }
        self.assertIn("fast", found)

    def test_each_limit_cites_the_sheet_it_is_read_from(self):
        # The 737's EASA sheet defers its airspeeds to FAA A16WE; the altitude is EASA's.
        boeing = {
            kind: finding.evidence["rated_limit"]["source"]["title"]
            for kind, finding in self.kinds(t="B738", gs=600, alt_baro=42500).items()
            if kind in rules.RATED_KINDS
        }
        self.assertEqual(
            boeing,
            {
                "above_rated_altitude": "EASA Type Certificate Data Sheet IM.A.120 (Boeing 737), Issue 29",
                "above_rated_speed": "FAA Type Certificate Data Sheet A16WE (Boeing 737)",
            },
        )
        # The Dash 8-400: altitude from FAA A13NM, Vmo from EASA.IM.A.191 (its own link).
        dash = self.kinds(t="DH8D", gs=520, alt_baro=28000)
        self.assertIn("A13NM", dash["above_rated_altitude"].evidence["rated_limit"]["source"]["title"])
        speed = dash["above_rated_speed"].evidence["rated_limit"]["source"]
        self.assertEqual(
            speed,
            {
                "title": "EASA Type Certificate Data Sheet EASA.IM.A.191 (De Havilland DHC-8), Issue 16",
                "url": "https://www.easa.europa.eu/en/downloads/7257/en",
            },
        )
        # A 1.5-times mismatch bound cites the limit it is built on, too.
        mismatch = self.kinds(t="B738", gs=800, alt_baro=35000)["type_speed_mismatch"]
        self.assertIn("A16WE", mismatch.evidence["envelope"]["source"]["title"])
        # The Citation Longitude's altitude is FAA T00013WI's; its speeds are the EASA sheet's.
        longitude = reference.limits("C700")
        self.assertIn("T00013WI", limits.source_for(longitude, "max_operating_altitude_ft")["title"])
        self.assertIn("EASA.IM.A.620", limits.source_for(longitude, "vmo_kias")["title"])
        # An entry without per-limit sources cites its one sheet; the copy is the caller's.
        plain = {"source": {"title": "Sheet", "url": "https://example.com/"}}
        cited = limits.source_for(plain, "mmo")
        cited["title"] = "changed"
        self.assertEqual(plain["source"]["title"], "Sheet")

    def test_implausible_airspeeds_never_become_performance_findings(self):
        # B738 has no heuristic speed cap and 800 kn is below the outlier bound: the
        # certified limit's 1.5-times bound routes the reading to the data-quality path.
        for extra in ({}, {"tas": 800}):
            with self.subTest(extra=extra):
                found = self.kinds(t="B738", gs=800, alt_baro=35000, **extra)
                self.assertEqual(set(found), {"type_speed_mismatch"})
                envelope = found["type_speed_mismatch"].evidence["envelope"]
                # Mmo binds at 35,000 ft, so the bound names it.
                self.assertEqual((envelope["basis"], envelope["limit_name"]), ("rated-limit", "mmo"))
                self.assertIn("1.5 times the true airspeed", found["type_speed_mismatch"].why)
        # A reported true airspeed alone can be implausible while the ground speed is ordinary.
        found = self.kinds(t="B738", gs=450, tas=800, alt_baro=35000)
        self.assertEqual(set(found), {"type_speed_mismatch"})
        self.assertEqual(found["type_speed_mismatch"].value, 800)
        self.assertIn("Reported true airspeed 800.0 kn exceeds", found["type_speed_mismatch"].why)
        # An implausible altitude on a limited type reports the envelope bound as before.
        self.assertEqual(set(self.kinds(t="DH8D", gs=300, alt_baro=41000)), {"type_altitude_mismatch"})
        self.assertEqual(curation.topic("type_speed_mismatch"), "data-quality")

    def test_rated_findings_are_identity_led_and_skip_conflicting_identities(self):
        conflicting = self.kinds(r="N514CJ", t="B738", gs=450, alt_baro=42500)
        self.assertNotIn("above_rated_altitude", conflicting)
        self.assertIn("above_rated_altitude", rules.IDENTITY_LED_KINDS)
        self.assertIn("above_rated_speed", rules.IDENTITY_LED_KINDS)
        solved = {
            f.kind: f for f in rules.candidates(report(t="B738", gs=600, alt_baro=37000, type="mlat"), NOW)
        }
        self.assertIn("mlat_solver_estimate", solved["above_rated_speed"].evidence["context_tags"])


def found_tags(finding):
    return list(finding.evidence.get("context_tags", []))


if __name__ == "__main__":
    unittest.main()
