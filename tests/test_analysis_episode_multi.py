"""Multi-aircraft helpers, TCAS advisories, integrity loss and optional readsb fields."""

import itertools
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_analysis_episode_geometry import LAT0, LON0, T0, ll

from wtf_adsb_engine.analysis import airport_context, app, maneuvers, selection, telemetry

OWN = {"hex": "a00001", "r": "N1CJ", "t": "C25C", "type": "adsb_icao"}


def flight(offset_x=0.0, offset_y=0.0, altitude=5000, speed=200, seconds=200, period=10, start=0, **extra):
    """A straight eastbound flight, sampled every ``period`` seconds."""
    points = []
    for index in range(int(seconds / period) + 1):
        t = start + index * period
        lat, lon = ll(offset_x + speed * t / 3600, offset_y)
        stamp = round(T0 + t, 1)
        points.append(
            {
                "ts": stamp,
                "position_at": stamp,
                "lat": lat,
                "lon": lon,
                "alt_baro": altitude,
                "gs": speed,
                "track": 90,
                "source": "adsb_icao",
                **extra,
            }
        )
    return points


class FormationTests(unittest.TestCase):
    def findings(self, history, aircraft=None):
        return maneuvers.formation_findings(maneuvers.formation_pairs(history, aircraft), aircraft)

    def test_two_adsb_aircraft_in_close_company_form_one_episode(self):
        history = {"a00001": flight(), "a00002": flight(offset_y=0.2, altitude=5200)}
        found = self.findings(history)
        self.assertEqual(len(found), 1)
        primary, finding = found[0]
        self.assertEqual(primary, "a00001")
        info = finding.evidence["maneuver"]
        self.assertEqual(finding.kind, "formation_flight")
        self.assertEqual(info["participant_count"], 2)
        self.assertAlmostEqual(info["min_separation_nm"], 0.2, places=2)
        self.assertEqual(info["vertical_separation_ft"], 200)
        self.assertEqual(info["quality_flags"], [])
        self.assertEqual([p["hex"] for p in finding.evidence["participants"]], ["a00002"])
        self.assertEqual(finding.evidence["participants"][0]["role"], "member")

    def test_uncertain_distant_brief_or_anonymous_pairs_are_ignored(self):
        cases = {
            "mlat": {"a00001": flight(), "a00002": flight(offset_y=0.2, source="mlat")},
            "anonymous": {"a00001": flight(), "~a00002": flight(offset_y=0.2)},
            "apart": {"a00001": flight(), "a00002": flight(offset_y=1.0)},
            "brief": {"a00001": flight(seconds=90), "a00002": flight(offset_y=0.2, seconds=90)},
            "crossing": {"a00001": flight(), "a00002": [{**p, "track": 140} for p in flight(offset_y=0.2)]},
            "low": {"a00001": flight(altitude=1200), "a00002": flight(offset_y=0.2, altitude=1200)},
        }
        for name, history in cases.items():
            with self.subTest(name=name):
                self.assertEqual(self.findings(history), [])

    def test_low_pairs_need_distance_from_runways(self):
        history = {"a00001": flight(altitude=2000), "a00002": flight(offset_y=0.2, altitude=2000)}
        with patch.object(airport_context, "nearby", return_value=[]):
            self.assertEqual(len(self.findings(history)), 1)
            # Without runway data the distance cannot be shown, so low pairs are not accepted.
            with patch.object(airport_context, "available", return_value=False):
                self.assertEqual(self.findings(history), [])
        with patch.object(airport_context, "nearby", return_value=[{"ends": [[LON0, LAT0], [LON0, LAT0]]}]):
            self.assertEqual(self.findings(history), [])

    def test_rebroadcast_participant_and_echo_targets_are_held_for_review(self):
        history = {"a00001": flight(), "a00002": flight(offset_y=0.2, source="adsr_icao")}
        flags = self.findings(history)[0][1].evidence["maneuver"]["quality_flags"]
        self.assertIn("participant_uncertain_source", flags)
        history = {"a00001": flight(flight="ABC123"), "a00002": flight(offset_y=0.005, flight="ABC123")}
        flags = self.findings(history)[0][1].evidence["maneuver"]["quality_flags"]
        self.assertIn("possible_duplicate_target", flags)

    def test_poor_data_on_either_participant_reaches_the_finding_and_its_eligibility(self):
        # Each participant's matched reports count for every field the geometry reads:
        # positions for the horizontal gap, the altitude for the vertical one and the
        # ground speed for the motion comparison. The coarse source stays direct ADS-B.
        poor = {
            "mlat_position": ({"mlat": ["lat", "lon"]}, "uncertain_source"),
            "tisb_position": ({"tisb": ["lat", "lon"]}, "uncertain_source"),
            "zero_integrity": ({"nic": 0, "nac_p": 0}, "low_position_quality"),
            "mlat_altitude": ({"mlat": ["altitude"]}, "uncertain_source"),
            "tisb_speed": ({"tisb": ["gs"]}, "uncertain_source"),
        }
        for carrier in ("a00001", "a00002"):
            for name, (extra, flag) in poor.items():
                with self.subTest(carrier=carrier, data=name):
                    history = {"a00001": flight(), "a00002": flight(offset_y=0.2, altitude=5200)}
                    history[carrier] = [{**p, **extra} for p in history[carrier]]
                    primary, finding = self.findings(history)[0]
                    info = finding.evidence["maneuver"]
                    self.assertEqual(primary, "a00001")
                    self.assertIn(flag, info["quality_flags"])
                    self.assertTrue(info["quality_review_required"])
                    self.assertTrue(info["confirmed"])
                    source = {
                        "kind": "formation_flight",
                        "verdict": None,
                        "hits": 8,
                        "evidence": finding.evidence,
                    }
                    self.assertFalse(selection.publishable(source))
        clean = self.findings({"a00001": flight(), "a00002": flight(offset_y=0.2, altitude=5200)})[0][1]
        self.assertEqual(clean.evidence["maneuver"]["quality_flags"], [])
        self.assertTrue(
            selection.publishable(
                {"kind": "formation_flight", "verdict": None, "hits": 8, "evidence": clean.evidence}
            )
        )
        # In a group, a poor third member's pair marks the finding saved on the primary.
        history = {f"a0000{i}": flight(offset_y=0.3 * i, altitude=6000 + 100 * i) for i in range(1, 4)}
        history["a00003"] = [{**p, "nic": 0, "nac_p": 0} for p in history["a00003"]]
        primary, finding = self.findings(history)[0]
        self.assertEqual(primary, "a00002")
        self.assertIn("low_position_quality", finding.evidence["maneuver"]["quality_flags"])

    def test_tanker_and_aerotow_tags(self):
        history = {
            "a00001": flight(altitude=24000, speed=300),
            "a00002": flight(offset_y=0.1, altitude=24300, speed=300),
        }
        meta = {"a00001": {"t": "K35R", "r": "60-0329"}, "a00002": {"t": "C17"}}
        finding = self.findings(history, meta)[0][1]
        self.assertIn("refuelling_like", finding.evidence["maneuver"]["context_tags"])
        self.assertEqual(finding.evidence["participants"][0].get("type"), "C17")
        tow = {
            "a00001": [{**p, "alt_baro": 3000 + i * 80} for i, p in enumerate(flight(speed=65))],
            "a00002": [
                {**p, "alt_baro": 3100 + i * 80} for i, p in enumerate(flight(offset_y=0.1, speed=65))
            ],
        }
        meta = {"a00001": {"t": "PA25"}, "a00002": {"t": "", "category": "B1"}}
        finding = self.findings(tow, meta)[0][1]
        self.assertIn("aerotow_like", finding.evidence["maneuver"]["context_tags"])

    def test_three_aircraft_are_one_group(self):
        history = {f"a0000{i}": flight(offset_y=0.3 * i, altitude=6000 + 100 * i) for i in range(1, 4)}
        found = self.findings(history)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0], "a00002")
        self.assertEqual(found[0][1].evidence["maneuver"]["participant_count"], 3)
        self.assertEqual(len(found[0][1].evidence["participants"]), 2)


def integrity(nic, accuracy, t, **extra):
    return {
        "ts": T0 + t,
        "source": "adsb_icao",
        "nic": nic,
        "nac_p": accuracy,
        "alt_baro": 33000,
        "lat": LAT0,
        "lon": LON0,
        **extra,
    }


def loss(onset=0, **extra):
    good = [integrity(8, 10, onset + t, **extra) for t in range(0, 71, 10)]
    lost = [integrity(0, 0, onset + t, **extra) for t in range(80, 111, 10)]
    return good + lost


class IntegrityTests(unittest.TestCase):
    def test_integrity_drop_after_a_healthy_baseline(self):
        finding = maneuvers.gnss_integrity_loss(loss())
        info = finding.evidence["maneuver"]
        self.assertEqual(finding.category, "oddities")
        self.assertEqual(info["onset_at"], T0 + 80)
        self.assertEqual((info["last_good_lat"], info["last_good_lon"]), (LAT0, LON0))
        recovered = loss() + [integrity(8, 10, 120), integrity(8, 10, 130)]
        self.assertEqual(maneuvers.gnss_integrity_loss(recovered).evidence["maneuver"]["state"], "recovered")

    def test_brief_intermediate_reports_and_endings(self):
        points = loss()
        points.insert(8, integrity(4, 5, 75))
        self.assertIsNotNone(maneuvers.gnss_integrity_loss(points))
        lingering = loss() + [integrity(4, 5, 120)]
        info = maneuvers.gnss_integrity_loss(lingering).evidence["maneuver"]
        self.assertEqual((info["state"], info["end_reason"]), ("ended", "integrity_changed"))
        self.assertEqual(info["evaluated_at"], T0 + 120)

    def test_marker_counts_and_ineligible_reports_do_not(self):
        marked = loss()
        for p in marked[-4:]:
            p.update(nic=8, nac_p=10, gpsOkBefore=T0 + 75, gpsOkLat=LAT0 + 0.1, gpsOkLon=LON0)
        finding = maneuvers.gnss_integrity_loss(marked)
        self.assertEqual(finding.evidence["maneuver"]["last_good_lat"], LAT0 + 0.1)
        for extra in ({"source": "adsr_icao"}, {"mlat": ["nic", "nac_p"]}, {"version": 1}):
            with self.subTest(extra=extra):
                self.assertIsNone(maneuvers.gnss_integrity_loss(loss(**extra)))
        self.assertIsNone(maneuvers.gnss_integrity_loss(loss()[:9]))
        brief_baseline = loss()[5:]
        self.assertIsNone(maneuvers.gnss_integrity_loss(brief_baseline))

    def test_cluster_needs_three_nearby_addresses_within_ten_minutes(self):
        members = []
        for index, (dx, onset) in enumerate(((0, 0), (20, 120), (40, 500), (200, 60))):
            finding = maneuvers.gnss_integrity_loss(loss(onset))
            finding.evidence["maneuver"]["last_good_lon"] = LON0 + dx / (60 * math.cos(math.radians(LAT0)))
            members.append(maneuvers.gnss_member(f"a0000{index}", finding, {"r": f"N{index}"}))
        clusters = maneuvers.gnss_clusters(members)
        self.assertEqual(len(clusters), 1)
        primary, finding = clusters[0]
        self.assertEqual(primary, "a00000")
        self.assertEqual(finding.kind, "gnss_integrity_cluster")
        self.assertEqual([p["hex"] for p in finding.evidence["participants"]], ["a00001", "a00002"])
        self.assertEqual(finding.evidence["maneuver"]["participant_count"], 3)
        self.assertEqual(maneuvers.gnss_clusters(members[:2]), [])

    def test_holding_stack_needs_three_aircraft_at_different_levels(self):
        def hold(index, altitude, dx=0.0):
            lat, lon = ll(dx, 0)
            return {
                "hex": f"a0001{index}",
                "started_at": T0 + index * 60,
                "ended_at": T0 + 1200,
                "lat": lat,
                "lon": lon,
                "altitude_ft": altitude,
                "source": "adsb_icao",
            }

        stack = [hold(1, 6000), hold(2, 7000, 3), hold(3, 8000, 6)]
        found = maneuvers.holding_stacks(stack)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][1].evidence["maneuver"]["participant_count"], 3)
        self.assertEqual(found[0][1].evidence["maneuver"]["quality_flags"], [])
        self.assertEqual(maneuvers.holding_stacks([hold(1, 6000), hold(2, 6000), hold(3, 8000)]), [])
        self.assertEqual(maneuvers.holding_stacks([hold(1, 3000), hold(2, 4000), hold(3, 8000)]), [])
        self.assertEqual(maneuvers.holding_stacks([hold(1, 6000), hold(2, 7000, 3), hold(3, 8000, 35)]), [])
        sequential = [
            {**hold(i, 6000 + 1000 * i), "started_at": T0 + i * 700, "ended_at": T0 + i * 700 + 600}
            for i in (1, 2, 3)
        ]
        self.assertEqual(maneuvers.holding_stacks(sequential), [])
        text = [{**h, "altitude_ft": str(h["altitude_ft"])} for h in stack]
        self.assertEqual(len(maneuvers.holding_stacks(text)), 1)


class StackContinuationTests(unittest.TestCase):
    def test_a_flagged_stack_stays_flagged_after_its_flagged_member_leaves(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "journal.db"
        app.initialize(path)
        detector = app.Detector(path)
        for address in ("a00011", "a00012", "a00013", "a00014"):
            detector.previous[address] = {
                "now": T0,
                "a": {"hex": address, "type": "adsb_icao"},
                "session": T0,
                "checks": {},
            }

        def hold(address, altitude, dx, end, reference="barometric"):
            lat, lon = ll(dx, 0)
            return {
                "hex": address,
                # The first aircraft entered the hold first, so it is the stack's primary.
                "started_at": T0 - 60 if address == "a00011" else T0,
                "ended_at": end,
                "lat": lat,
                "lon": lon,
                "altitude_ft": altitude,
                "altitude_reference": reference,
                "source": "adsb_icao",
            }

        def evaluate(members):
            inputs = {member["hex"]: member for member in members}
            ((primary, finding),) = maneuvers.holding_stacks(inputs.values())
            primary, finding = detector._continued_stack(
                primary, detector._flag_stack(primary, finding, inputs), inputs
            )
            detector.multi[("holding_stack", primary)] = {
                "finding": finding,
                "a": {},
                "extra": {},
                "seen": T0,
            }
            with app.connect(path) as db:
                app.record(
                    db,
                    {"hex": primary, "type": "adsb_icao"},
                    finding,
                    finding.evidence["maneuver"]["started_at"],
                    finding.evidence["maneuver"]["ended_at"],
                    extra={"encounter_start": T0},
                )
            return primary, finding

        # a00013 reports only a geometric altitude: its level is on another reference.
        first, flagged = evaluate(
            [
                hold("a00011", 6000, 0, T0 + 1200),
                hold("a00012", 7000, 3, T0 + 1200),
                hold("a00013", 8000, 6, T0 + 1200, "geometric"),
            ]
        )
        self.assertIn("altitude_reference_mixed", flagged.evidence["maneuver"]["quality_flags"])
        # It leaves the hold; a fourth aircraft at another barometric level keeps three circling.
        held, later = evaluate(
            [
                hold("a00011", 6000, 0, T0 + 1500),
                hold("a00012", 7000, 3, T0 + 1500),
                hold("a00014", 9000, 4, T0 + 1500),
            ]
        )
        info = later.evidence["maneuver"]
        self.assertEqual(held, first)
        self.assertEqual(
            [item["hex"] for item in later.evidence["participants"]], ["a00012", "a00013", "a00014"]
        )
        self.assertIn("altitude_reference_mixed", info["quality_flags"])
        self.assertEqual(info["altitude_references"], ["barometric", "geometric"])
        self.assertTrue(info["quality_review_required"])
        # The one saved row took the later evaluation and is still held for review.
        with app.connect(path) as db:
            rows = [
                app.event_dict(row) for row in db.execute("SELECT * FROM events WHERE kind='holding_stack'")
            ]
        self.assertEqual(len(rows), 1)
        saved = rows[0]["evidence"]["maneuver"]
        self.assertEqual(
            (saved["ended_at"], saved["quality_flags"]), (T0 + 1500, ["altitude_reference_mixed"])
        )
        self.assertTrue(selection.assess(rows[0], {})["review_required"])


def advisory(t, text="Level Off", rat=0, df=17, threat="a00002", **extra):
    return {
        "unix_timestamp": T0 + t,
        "advisory": text,
        "RAT": rat,
        "MTE": 0,
        "TTI": "01",
        "threat_id_hex": threat,
        "df_type": df,
        "ARA": "0100000000000",
        "bytes": "E2000000000000",
        **extra,
    }


def climb(rates):
    """The advising aircraft: one report every 10 s from t=-30, eastbound at 240 kn."""
    points = []
    altitude = 30000
    for index, rate in enumerate(rates):
        t = -30 + index * 10
        if index:
            altitude += (rates[index - 1] + rate) / 2 * 10 / 60
        lat, lon = ll(240 * (t + 30) / 3600, 0)
        points.append(
            {
                "ts": T0 + t,
                "lat": lat,
                "lon": lon,
                "alt_baro": round(altitude),
                "baro_rate": rate,
                "source": "adsb_icao",
            }
        )
    return points


THREAT = [
    {
        "ts": T0 + t,
        "lat": ll(240 * (t + 30) / 3600, 3.5 - t * 0.005)[0],
        "lon": ll(240 * (t + 30) / 3600, 0)[1],
        "alt_baro": 31000,
        "source": "adsb_icao",
    }
    for t in range(-30, 71, 10)
]


class TcasTests(unittest.TestCase):
    def episode(self, reports, own=None, threat=THREAT):
        return maneuvers.tcas_episode(reports, own or climb([2800] * 4 + [600] * 7), threat, own=OWN)

    def test_level_off_advisory_with_response_and_threat_track(self):
        finding = self.episode([advisory(0), advisory(5), advisory(31, "Clear of Conflict", rat=1)])
        info = finding.evidence["maneuver"]
        self.assertEqual(finding.kind, "tcas_ra")
        self.assertEqual(finding.category, "signals")
        self.assertTrue(info["confirmed"])
        self.assertEqual(info["quality_flags"], [])
        self.assertEqual(info["advisory"], "Level Off")
        self.assertEqual(info["state"], "ended")
        self.assertEqual(info["response_rate_change_fpm"], 600 - 2800)
        self.assertLess(info["min_separation_nm"], 3.6)
        self.assertEqual(info["participant_count"], 2)
        self.assertEqual(finding.evidence["participants"][0]["role"], "threat")
        self.assertEqual(finding.evidence["participants"][0]["hex"], "a00002")
        self.assertNotIn("bytes", str(finding.evidence))

    def test_single_message_or_ground_does_not_confirm(self):
        self.assertFalse(self.episode([advisory(0)]).evidence["maneuver"]["confirmed"])
        ground = [{**p, "ground": True} for p in climb([2800] * 4 + [600] * 7)]
        self.assertFalse(
            self.episode([advisory(0), advisory(5)], own=ground).evidence["maneuver"]["confirmed"]
        )

    def test_review_flags(self):
        cases = {
            "acas_decoder_experimental": ([advisory(0, df=16), advisory(5, df=16)], None, THREAT),
            "threat_track_missing": ([advisory(0), advisory(5)], None, []),
            "no_advised_response": ([advisory(0), advisory(5)], climb([2800] * 11), THREAT),
            "participant_uncertain_source": (
                [advisory(0), advisory(5)],
                None,
                [{**p, "source": "mlat"} for p in THREAT],
            ),
        }
        for flag, (reports, own, threat) in cases.items():
            with self.subTest(flag=flag):
                info = self.episode(reports, own, threat).evidence["maneuver"]
                self.assertIn(flag, info["quality_flags"])

    def test_poor_fixes_on_either_track_mark_the_separation_but_keep_the_advisory(self):
        reports = [advisory(0), advisory(5), advisory(31, "Clear of Conflict", rat=1)]
        poor = {
            "mlat_position": ({"mlat": ["lat", "lon"]}, "uncertain_source"),
            "tisb_position": ({"tisb": ["lat", "lon"]}, "uncertain_source"),
            "zero_integrity": ({"nic": 0, "nac_p": 0}, "low_position_quality"),
            "mlat_altitude": ({"mlat": ["altitude"]}, "uncertain_source"),
        }
        # The threat's fixes are read as given, and interpolated when they fall between
        # the aircraft's own report times; both ways keep their provenance.
        threats = {"aligned": THREAT, "interpolated": [{**p, "ts": p["ts"] + 5} for p in THREAT]}
        for track, (name, (extra, flag)) in itertools.product(("own", "threat"), poor.items()):
            for alignment, threat in threats.items():
                with self.subTest(track=track, data=name, threat=alignment):
                    own = climb([2800] * 4 + [600] * 7)
                    if track == "own":
                        own = [{**p, **extra} for p in own]
                    else:
                        threat = [{**p, **extra} for p in threat]
                        flag = "participant_uncertain_source" if flag == "uncertain_source" else flag
                    info = self.episode(reports, own, threat).evidence["maneuver"]
                    self.assertTrue(info["confirmed"])
                    self.assertEqual(info["advisory"], "Level Off")
                    self.assertIn(flag, info["quality_flags"])
                    self.assertTrue(info["quality_review_required"])
                    self.assertIn(flag, info["separation_quality_flags"])
                    self.assertLess(info["min_separation_nm"], 3.6)
                    self.assertIn("vertical_separation_ft", info)
                    self.assertNotIn("threat_track_missing", info["quality_flags"])
        clean = self.episode(reports, threat=threats["interpolated"]).evidence["maneuver"]
        self.assertEqual(clean["quality_flags"], [])
        self.assertNotIn("separation_quality_flags", clean)
        # An interpolated position carries what both fixes say about their measurement.
        others = [{**p, "nic": 8, "nac_p": 9} for p in threats["interpolated"]]
        others[3].update(mlat=["lat", "lon"], nic=0, archive="traces")
        times = [q["ts"] for q in others]
        between = maneuvers._threat_at(others, times, others[3]["ts"] + 5)
        self.assertEqual((between["mlat"], between["nic"], between["nac_p"]), (["lat", "lon"], 0, 9))
        self.assertEqual((between["archive"], between["ts"]), ("traces", others[3]["ts"] + 5))
        self.assertEqual(telemetry.source_for(between, "lat"), "mlat")
        self.assertNotIn("mlat", maneuvers._threat_at(others, times, others[0]["ts"] + 5))

    def test_an_indirect_response_rate_is_flagged_apart_from_altitude_and_position(self):
        reports = [advisory(0), advisory(5), advisory(31, "Clear of Conflict", rat=1)]
        for listed in ({"mlat": ["baro_rate"]}, {"tisb": ["baro_rate"]}):
            with self.subTest(listed=listed):
                # Only the rate is indirect: the altitude and position stay direct ADS-B.
                own = [{**p, **listed} for p in climb([2800] * 4 + [600] * 7)]
                info = self.episode(reports, own).evidence["maneuver"]
                self.assertTrue(info["confirmed"])
                self.assertEqual(info["quality_flags"], ["uncertain_source"])
                self.assertNotIn("separation_quality_flags", info)
                self.assertEqual(info["response_rate_change_fpm"], 600 - 2800)
                source = {
                    "kind": "tcas_ra",
                    "verdict": None,
                    "hits": 3,
                    "evidence": self.episode(reports, own).evidence,
                }
                self.assertFalse(selection.publishable(source))
        # The rate reports before (t=0) and after (t=20) the advisory each count on their own.
        own = climb([2800] * 4 + [600] * 7)
        own[3]["mlat"] = ["baro_rate"]
        self.assertIn("uncertain_source", self.episode(reports, own).evidence["maneuver"]["quality_flags"])
        own = climb([2800] * 4 + [600] * 7)
        own[5]["tisb"] = ["baro_rate"]
        self.assertIn("uncertain_source", self.episode(reports, own).evidence["maneuver"]["quality_flags"])
        clean = {"kind": "tcas_ra", "verdict": None, "hits": 3, "evidence": self.episode(reports).evidence}
        self.assertTrue(selection.publishable(clean))

    def test_a_level_off_needs_level_flight_or_a_rate_cut_towards_it(self):
        reports = [advisory(0), advisory(5)]
        cases = {
            "level then climbing": ([0] * 4 + [3200] * 7, False),
            "level then descending": ([0] * 4 + [-3200] * 7, False),
            "slow then faster climb": ([300] * 4 + [1400] * 7, False),
            "climb halved": ([2800] * 4 + [1400] * 7, True),
            "descent halved": ([-2800] * 4 + [-1300] * 7, True),
            "climb to level": ([2800] * 4 + [100] * 7, True),
            "level kept": ([200] * 4 + [-300] * 7, True),
            "climb kept": ([2800] * 11, False),
            "climb reversed into a descent": ([2800] * 4 + [-2000] * 7, False),
        }
        for name, (rates, responded) in cases.items():
            with self.subTest(case=name):
                info = self.episode(reports, climb(rates)).evidence["maneuver"]
                self.assertEqual("no_advised_response" not in info["quality_flags"], responded)
                self.assertEqual(info["response_rate_change_fpm"], rates[-1] - rates[0])
        self.assertTrue(maneuvers._response("level", None, None) is False)
        self.assertTrue(maneuvers._response("maintain", 0, 3200))

    def test_an_advisory_without_a_captured_termination_still_ends(self):
        info = self.episode([advisory(0), advisory(5)]).evidence["maneuver"]
        self.assertEqual((info["state"], info["end_reason"]), ("ended", "no_messages"))
        self.assertEqual(info["evaluated_at"], T0 + 65)
        own = [p for p in climb([2800] * 4 + [600] * 7) if p["ts"] <= T0 + 40]
        self.assertEqual(
            self.episode([advisory(0), advisory(5)], own=own).evidence["maneuver"]["state"], "active"
        )

    def test_separation_interpolates_the_other_track(self):
        offset = [{**p, "ts": p["ts"] + 5} for p in THREAT]
        near = self.episode([advisory(0), advisory(5)]).evidence["maneuver"]["min_separation_nm"]
        shifted = self.episode([advisory(0), advisory(5)], threat=offset).evidence["maneuver"][
            "min_separation_nm"
        ]
        self.assertAlmostEqual(near, shifted, delta=0.05)

    def test_unknown_advisory_text_is_never_kept(self):
        finding = self.episode([advisory(0, "Pull up now!"), advisory(5, "Pull up now!")])
        self.assertNotIn("advisory", finding.evidence["maneuver"])
        self.assertIn("no_advised_response", finding.evidence["maneuver"]["quality_flags"])

    def test_analyze_builds_the_episode_from_retained_reports(self):
        own = climb([2800] * 4 + [600] * 7)
        for p in own[3:6]:
            p["acas_ra"] = telemetry.acas_report(advisory(p["ts"] - T0))
        threats = {"a00002": {"samples": THREAT, "aircraft": None}}
        found = [f for f in maneuvers.analyze(own, OWN, threats=threats) if f.kind == "tcas_ra"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].evidence["maneuver"]["quality_flags"], [])
        found = [f for f in maneuvers.analyze(own, OWN) if f.kind == "tcas_ra"]
        self.assertIn("threat_track_missing", found[0].evidence["maneuver"]["quality_flags"])

    def test_the_threat_is_the_one_the_episode_names_not_the_last_message(self):
        # The episode names its threat in its first active message with TTI=1; a
        # terminating message without a threat identity must not lose that track.
        own = climb([2800] * 4 + [600] * 7)
        reports = [advisory(0), advisory(5), advisory(31, "Clear of Conflict", rat=1, TTI="00", threat=None)]
        for p, report in zip(own[3:6], reports, strict=True):
            p["acas_ra"] = telemetry.acas_report(report)
        threats = {"a00002": {"samples": THREAT, "aircraft": None}}
        found = [f for f in maneuvers.analyze(own, OWN, threats=threats) if f.kind == "tcas_ra"]
        self.assertNotIn("threat_track_missing", found[0].evidence["maneuver"]["quality_flags"])
        self.assertEqual(found[0].evidence["participants"][0]["hex"], "a00002")
        runs = list(maneuvers.advisory_runs([advisory(0), advisory(5), advisory(200)]))
        self.assertEqual([len(run) for run in runs], [2, 1])
        self.assertEqual(maneuvers.advisory_threat(runs[0], "a00002"), None)
        self.assertEqual(maneuvers.advisory_threat(runs[0]), "a00002")


class OvershootTests(unittest.TestCase):
    def climb_to(self, selected, altitudes, qnh=None):
        points = []
        for index, altitude in enumerate(altitudes):
            rate = (altitudes[min(index + 1, len(altitudes) - 1)] - altitudes[max(index - 1, 0)]) * 3
            point = {
                "ts": T0 + index * 10,
                "alt_baro": altitude,
                "baro_rate": rate,
                "nav_altitude_mcp": selected,
                "source": "adsb_icao",
            }
            if qnh is not None:
                point["nav_qnh"] = qnh
            points.append(point)
        return points

    def test_overshoot_of_an_unchanged_selection_is_always_reviewed(self):
        altitudes = [20000 + 400 * i for i in range(10)] + [24350, 24450, 24480, 24420, 24300, 24150, 24050]
        finding = maneuvers.selected_altitude_overshoot(self.climb_to(24000, altitudes))
        info = finding.evidence["maneuver"]
        self.assertEqual(finding.kind, "selected_altitude_overshoot")
        self.assertEqual(finding.category, "performance")
        self.assertIn("selected_altitude_not_clearance", info["quality_flags"])
        self.assertEqual(info["altitude_excursion_ft"], 480)

    def test_low_altitudes_need_the_altimeter_setting(self):
        altitudes = [4000 + 400 * i for i in range(10)] + [8350, 8450, 8480, 8420, 8300, 8150, 8050]
        self.assertIsNone(maneuvers.selected_altitude_overshoot(self.climb_to(8000, altitudes)))
        found = maneuvers.selected_altitude_overshoot(self.climb_to(8000, altitudes, qnh=1013.2))
        self.assertIsNotNone(found)
        # At 990 hPa a level-off at 8,000 ft indicated reads about 8,630 ft of pressure altitude.
        level = [4400 + 400 * i for i in range(10)] + [8628] * 8
        self.assertIsNone(maneuvers.selected_altitude_overshoot(self.climb_to(8000, level, qnh=990)))

    def test_a_changed_selection_or_a_brief_excursion_is_not_an_overshoot(self):
        altitudes = [20000 + 400 * i for i in range(10)] + [24350, 24450, 24480, 24420, 24300, 24150, 24050]
        points = self.climb_to(24000, altitudes)
        for p in points[11:]:
            p["nav_altitude_mcp"] = 25000
        self.assertIsNone(maneuvers.selected_altitude_overshoot(points))
        brief = [20000 + 400 * i for i in range(10)] + [24350, 24100, 24000, 24000]
        self.assertIsNone(maneuvers.selected_altitude_overshoot(self.climb_to(24000, brief)))


class AnomalyTagTests(unittest.TestCase):
    def test_only_same_source_direct_airborne_pairs_compare(self):
        good = {"ts": T0, "source": "adsb_icao", "nic": 8, "nac_p": 10}
        drop = {"ts": T0 + 10, "source": "adsb_icao", "nic": 0, "nac_p": 0}
        self.assertEqual(maneuvers.anomaly_tags(good, drop), {"position_quality_drop"})
        self.assertEqual(maneuvers.anomaly_tags(good, {**drop, "source": "adsr_icao"}), set())
        self.assertEqual(maneuvers.anomaly_tags(good, {**drop, "mlat": ["nic", "nac_p"]}), set())
        self.assertEqual(maneuvers.anomaly_tags(good, {**drop, "ground": True}), set())
        mlat = {**good, "source": "mlat"}
        self.assertEqual(maneuvers.anomaly_tags(mlat, {**drop, "source": "mlat"}), set())

    def test_detector_does_not_cluster_a_shared_source_switch(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "journal.db"
        app.initialize(path)
        detector = app.Detector(path)
        with patch.object(app.watchlists, "lookup", return_value=None):
            for step, (source, quality) in enumerate((("adsb_icao", 8), ("adsb_icao", 8), ("adsr_icao", 0))):
                aircraft = [
                    {
                        "hex": f"a0010{i}",
                        "type": source,
                        "seen": 0,
                        "seen_pos": 0,
                        "lat": LAT0 + i * 0.1,
                        "lon": LON0,
                        "alt_baro": 9000,
                        "nic": quality,
                        "nac_p": quality,
                        "messages": 100 + step,
                    }
                    for i in range(3)
                ]
                detector.process({"now": T0 + step * 10, "aircraft": aircraft}, T0 + step * 10)
        with app.connect(path) as db:
            count = db.execute("SELECT COUNT(*) FROM events WHERE kind='correlated_data_anomaly'").fetchone()[
                0
            ]
        self.assertEqual(count, 0)


NOON = 1_793_880_000.0  # 2026-11-05 12:00 UTC


class WindsAloftCollectorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "journal.db"
        app.initialize(self.path)
        lookup = patch.object(app.watchlists, "lookup", return_value=None)
        lookup.start()
        self.addCleanup(lookup.stop)

    @staticmethod
    def poll(detector, step, now, aircraft=True, without=()):
        """40 level eastbound and 40 level westbound jets at FL350, or an empty poll."""
        reports = [
            {
                "hex": f"{'b' if east else 'c'}{i:05x}",
                "type": "adsb_icao",
                "seen": 0,
                "seen_pos": 0,
                "lat": LAT0 + i * 0.05 + (0.02 if east else 0),
                "lon": LON0,
                "alt_baro": 35000,
                "baro_rate": 0,
                "track": 90 if east else 270,
                "gs": (560 + i % 5) if east else (380 - i % 5),
                "messages": 100 + step,
            }
            for east in (True, False)
            for i in range(40)
        ]
        reports = [report for report in reports if report["hex"] not in without] if aircraft else []
        detector.process({"now": now, "aircraft": reports}, now)

    def rows(self):
        with app.connect(self.path) as db:
            return [
                app.event_dict(row)
                for row in db.execute("SELECT * FROM events WHERE kind='winds_aloft_day' ORDER BY id")
            ]

    def test_level_jets_by_direction_make_one_row_per_day_that_ends_after_midnight(self):
        detector = app.Detector(self.path)
        self.poll(detector, 0, NOON)
        self.poll(detector, 1, NOON + 600)
        saved = self.rows()
        self.assertEqual(len(saved), 1)
        self.assertGreaterEqual(saved[0]["value"], 150)
        evidence = saved[0]["evidence"]
        self.assertEqual(evidence["winds_aloft"]["day"], "2026-11-05")
        self.assertEqual(evidence["winds_aloft"]["east_addresses"], 40)
        self.assertEqual(evidence["maneuver"]["state"], "active")
        self.assertEqual(evidence["maneuver"]["evaluated_at"], NOON + 600)
        self.assertEqual(evidence["maneuver"]["sample_count"], 160)
        self.assertNotIn("lat", evidence["aircraft"])
        self.assertTrue(evidence["participants"])
        self.assertTrue(all("median_gs_kn" in item["measurements"] for item in evidence["participants"]))
        # The UTC day closes: the row ends once, and no new row starts without samples.
        self.poll(detector, 2, NOON + 43200 + 60, aircraft=False)
        saved = self.rows()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["evidence"]["maneuver"]["state"], "ended")
        self.assertEqual(saved[0]["evidence"]["maneuver"]["end_reason"], "day_closed")

    def test_a_lull_keeps_the_row_open_until_midnight(self):
        detector = app.Detector(self.path)
        self.poll(detector, 0, NOON)
        self.poll(detector, 1, NOON + 600)
        # No reports for well over the buffer and stale windows: the track sweep runs.
        for offset in (1500, 2100, 2700, 3300, 6000):
            self.poll(detector, 2, NOON + 600 + offset, aircraft=False)
            info = self.rows()[0]["evidence"]["maneuver"]
            self.assertEqual(info["state"], "active", offset)
        self.poll(detector, 3, NOON + 43200 + 30, aircraft=False)
        (saved,) = self.rows()
        self.assertEqual(saved["evidence"]["maneuver"]["state"], "ended")
        self.assertEqual(saved["evidence"]["maneuver"]["end_reason"], "day_closed")
        self.assertEqual(saved["evidence"]["maneuver"]["evaluated_at"], NOON + 43200 + 30)

    def test_a_restarted_collector_continues_the_days_row_on_its_aircraft(self):
        first = app.Detector(self.path)
        self.poll(first, 0, NOON)
        self.poll(first, 1, NOON + 600)
        (before,) = self.rows()
        carrier = before["hex"]
        # A new collector over the same journal; the carrier no longer reports.
        second = app.Detector(self.path)
        self.poll(second, 2, NOON + 1200, without={carrier})
        (after,) = self.rows()
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["hex"], carrier)
        info = after["evidence"]["maneuver"]
        self.assertEqual((info["state"], info["evaluated_at"]), ("active", NOON + 1200))
        self.assertEqual(info["episode_start"], before["evidence"]["maneuver"]["episode_start"])

    def test_an_open_row_from_an_earlier_day_ends_on_the_first_pass(self):
        first = app.Detector(self.path)
        self.poll(first, 0, NOON)
        self.poll(first, 1, NOON + 600)
        second = app.Detector(self.path)
        self.poll(second, 2, NOON + 86400, aircraft=False)
        (saved,) = self.rows()
        info = saved["evidence"]["maneuver"]
        self.assertEqual((info["state"], info["end_reason"]), ("ended", "day_closed"))
        self.assertEqual(info["evaluated_at"], NOON + 86400)


class TelemetryFieldTests(unittest.TestCase):
    def test_optional_readsb_fields_are_kept_only_when_valid(self):
        aircraft = {
            "seen": 0,
            "type": "adsb_icao",
            "nav_altitude_mcp": 24000,
            "nav_qnh": 1013.2,
            "nav_modes": ["autopilot", "vnav", "<script>"],
            "nic_baro": 1,
            "sil": 3,
            "gva": 2,
            "sda": 2,
            "version": 2,
            "gpsOkBefore": T0 - 30,
            "gpsOkLat": 95,
            "acas_ra": advisory(0),
            "rr_lat": 1,
            "r_dst": 12.3,
        }
        sample = telemetry.sample(aircraft, T0)
        self.assertEqual(sample["nav_modes"], ["autopilot", "vnav"])
        self.assertEqual(sample["version"], 2)
        self.assertNotIn("gpsOkLat", sample)
        for private in ("rr_lat", "r_dst"):
            self.assertNotIn(private, sample)
        self.assertEqual(sample["acas_ra"]["TTI"], 1)
        self.assertNotIn("bytes", sample["acas_ra"])
        self.assertEqual(telemetry.sample({"seen": 0, "nav_qnh": 50}, T0).get("nav_qnh"), None)

    def test_acas_sanitizer_maps_text_through_the_allowlist(self):
        report = telemetry.acas_report(
            {
                **advisory(0, "Don't Descend", threat="NOT-HEX"),
                "advisory_complement": "Do not pass below",
                "full_bytes": "x",
            }
        )
        self.assertEqual(report["advisory"], "Don't Descend")
        self.assertEqual(report["advisory_complement"], "Do not pass below")
        self.assertNotIn("threat_id_hex", report)
        self.assertNotIn("full_bytes", report)
        self.assertIsNone(telemetry.acas_report({"advisory": "Climb"}))
        self.assertNotIn("advisory", telemetry.acas_report(advisory(0, "Free text <b>")))
        self.assertEqual(telemetry.advisory_sense("Level Off"), "level")

    def test_positions_can_keep_mixed_sources_and_new_legs(self):
        points = [
            {"ts": T0 + t, "lat": LAT0 + t / 6000, "lon": LON0, "source": "adsb_icao"}
            for t in range(0, 101, 10)
        ]
        points[5]["source"] = "mlat"
        self.assertEqual(len(telemetry.positions(points)), 5)
        self.assertEqual(len(telemetry.positions(points, split_sources=False)), 11)
        points[8]["new_leg"] = True
        self.assertEqual(telemetry.positions(points, split_sources=False)[0]["ts"], T0 + 80)
        text = [{**p, "lat": str(p["lat"])} for p in points]
        self.assertIsInstance(telemetry.positions(text)[0]["lat"], float)

    def test_mlat_altitude_alias_is_pressure_altitude(self):
        point = {"source": "adsb_icao", "mlat": ["altitude"]}
        self.assertEqual(telemetry.source_for(point, "alt_baro"), "mlat")
        self.assertEqual(telemetry.source_for(point, "alt_geom"), "adsb_icao")
        # Malformed attribution lists are ignored rather than raising in the collector.
        for listed in (5, "baro_rate", {"baro_rate": 1}):
            self.assertEqual(
                telemetry.source_for({"source": "adsb_icao", "mlat": listed}, "baro_rate"), "adsb_icao"
            )


if __name__ == "__main__":
    unittest.main()
