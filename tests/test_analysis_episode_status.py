"""Transponder status and special-use code episodes."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wtf_adsb_engine.analysis import app, maneuvers

NOW = 1791150000
AIRCRAFT = {"hex": "abc123", "r": "N12345", "t": "C172", "type": "adsb_icao"}


def report(t, squawk=None, source="adsb_icao", **extra):
    result = {"ts": NOW + t, "source": source, "alt_baro": 9000, **extra}
    if squawk is not None:
        result["squawk"] = squawk
    return result


def statuses(points):
    return [f for f in maneuvers.signals(points) if f.kind.startswith("special_squawk_")]


def info(finding):
    return finding.evidence["maneuver"]


class StatusTagTests(unittest.TestCase):
    def test_two_poll_hijack_code_that_reverts_is_transient_and_disagrees(self):
        points = [report(0, "7500", emergency="none"), report(10, "7500", emergency="none")]
        points.append(report(20, "1200", emergency="none"))
        finding = statuses(points)[0]
        self.assertEqual(finding.kind, "special_squawk_7500")
        self.assertTrue(info(finding)["confirmed"])
        self.assertEqual(
            set(info(finding)["context_tags"]), {"transient_code_selection", "status_field_disagreement"}
        )
        self.assertEqual((info(finding)["state"], info(finding)["end_reason"]), ("ended", "reverted"))
        self.assertEqual(info(finding)["ending_code"], "1200")
        self.assertEqual(info(finding)["evaluated_at"], NOW + 20)

    def test_ground_only_status_is_tagged_with_its_share(self):
        points = [report(t, "7700", ground=True, alt_baro="ground") for t in range(0, 31, 10)]
        finding = statuses(points)[0]
        self.assertIn("ground_only_status", info(finding)["context_tags"])
        self.assertEqual(info(finding)["ground_report_share"], 1.0)

    def test_sustained_agreeing_airborne_emergency_has_no_caveat_tags(self):
        points = [report(t, "7700", emergency="general") for t in range(0, 91, 10)]
        finding = statuses(points)[0]
        self.assertEqual(info(finding)["context_tags"], [])
        self.assertEqual(info(finding)["distinct_report_count"], 10)
        self.assertEqual(info(finding)["state"], "active")
        self.assertEqual(info(finding)["ground_report_share"], 0)

    def test_one_lagging_emergency_field_is_not_a_disagreement(self):
        points = [report(0, "7700", emergency="none")]
        points += [report(t, "7700", emergency="general") for t in range(10, 91, 10)]
        self.assertNotIn("status_field_disagreement", info(statuses(points)[0])["context_tags"])
        points = [report(t, "7500", emergency="none") for t in range(0, 91, 10)]
        self.assertIn("status_field_disagreement", info(statuses(points)[0])["context_tags"])


class ProvenanceTests(unittest.TestCase):
    def test_mode_s_and_mlat_codes_are_transponder_replies_not_uncertain_sources(self):
        for source in ("mode_s", "mlat"):
            with self.subTest(source=source):
                finding = statuses([report(t, "7700", source=source) for t in range(0, 91, 10)])[0]
                self.assertNotIn("uncertain_source", info(finding)["quality_flags"])
                self.assertIn("status_from_mode_s_reply", info(finding)["context_tags"])
                self.assertFalse(info(finding)["quality_review_required"])

    def test_rebroadcast_codes_stay_uncertain(self):
        finding = statuses([report(t, "7700", source="tisb_icao") for t in range(0, 91, 10)])[0]
        self.assertIn("uncertain_source", info(finding)["quality_flags"])

    def test_missing_squawk_is_never_saved_as_0000(self):
        finding = statuses([report(t, emergency="minfuel") for t in (0, 10, 20)])[0]
        self.assertEqual(finding.kind, "special_squawk_minfuel")
        self.assertNotIn("reported_squawk", info(finding))
        self.assertEqual(info(finding)["reported_status"], "minfuel")
        self.assertEqual(statuses([report(t, "0000") for t in (0, 10, 20)]), [])


class SequenceTests(unittest.TestCase):
    def test_7700_then_7600_is_one_episode_with_a_code_sequence(self):
        points = [report(t, "7700") for t in range(0, 31, 10)] + [
            report(t, "7600") for t in range(40, 71, 10)
        ]
        points.append(report(80, "1200"))
        found = statuses(points)
        self.assertEqual([f.kind for f in found], ["special_squawk_7700"])
        self.assertEqual(
            info(found[0])["code_sequence"],
            [{"code": "7700", "seconds": 40}, {"code": "7600", "seconds": 30}],
        )
        self.assertEqual(info(found[0])["state"], "ended")

    def test_a_carried_emergency_field_continues_the_same_episode(self):
        points = [report(t, "7700", emergency="general") for t in range(0, 31, 10)]
        points += [report(t, "1200", emergency="general") for t in range(40, 61, 10)]
        found = statuses(points)
        self.assertEqual([f.kind for f in found], ["special_squawk_7700"])
        self.assertEqual(info(found[0])["state"], "active")

    def test_a_one_poll_code_while_dialling_does_not_name_a_lasting_status(self):
        points = [report(0, "7700")] + [report(t, "7600") for t in range(10, 61, 10)]
        found = {f.kind: info(f) for f in statuses(points)}
        self.assertEqual(set(found), {"special_squawk_7600", "special_squawk_7700"})
        self.assertTrue(found["special_squawk_7600"]["confirmed"])
        # The row saved under the first report's name is closed, not left open.
        early = found["special_squawk_7700"]
        self.assertFalse(early["confirmed"])
        self.assertEqual((early["state"], early["end_reason"]), ("ended", "superseded"))
        self.assertEqual(early["superseded_by"], "special_squawk_7600")
        self.assertEqual(early["evaluated_at"], NOW + 20)

    def test_every_evaluation_keeps_one_open_row_per_sequence(self):
        points = [report(0, "7700")] + [report(t, "7600") for t in range(10, 61, 10)] + [report(70, "1200")]
        rows = {}
        for end in range(1, len(points) + 1):
            for finding in statuses(points[:end]):
                rows[(finding.kind, info(finding)["episode_start"])] = info(finding)
        self.assertEqual(rows[("special_squawk_7700", NOW)]["state"], "ended")
        self.assertEqual(rows[("special_squawk_7600", NOW)]["state"], "ended")
        self.assertEqual(rows[("special_squawk_7600", NOW)]["end_reason"], "reverted")

    def test_a_priority_status_does_not_hide_a_later_emergency(self):
        points = [report(t, "3421", emergency="minfuel") for t in range(0, 121, 10)]
        points += [report(t, "7700", emergency="general") for t in range(130, 431, 10)]
        found = {}
        for end in range(1, len(points) + 1):
            found.update({f.kind: info(f) for f in statuses(points[:end])})
        self.assertEqual(set(found), {"special_squawk_minfuel", "special_squawk_7700"})
        self.assertEqual(found["special_squawk_minfuel"]["end_reason"], "reverted")
        self.assertEqual(found["special_squawk_minfuel"]["ending_code"], "7700")
        self.assertEqual(found["special_squawk_7700"]["state"], "active")

    def test_an_emergency_field_ahead_of_the_code_keeps_one_name(self):
        points = [report(t, "1200", emergency="general") for t in (0, 10)]
        points += [report(t, "7700", emergency="general") for t in range(20, 121, 10)]
        kinds = {f.kind for end in range(1, len(points) + 1) for f in statuses(points[:end])}
        self.assertEqual(kinds, {"special_squawk_general"})

    def test_a_brief_mode_s_source_change_does_not_split_a_status(self):
        points = [report(t, "7700") for t in range(0, 91, 10)]
        points[4]["source"] = "mode_s"
        found = [f for f in maneuvers.analyze(points, AIRCRAFT) if f.kind.startswith("special_squawk_")]
        self.assertEqual(len(found), 1)
        self.assertEqual(info(found[0])["duration_seconds"], 90)

    def test_lost_link_code_is_a_status(self):
        finding = statuses([report(t, "7400") for t in range(0, 31, 10)])[0]
        self.assertEqual(finding.kind, "special_squawk_7400")
        self.assertIn("7400", maneuvers.STATUS_CODES)


class EndingTests(unittest.TestCase):
    def test_a_status_without_a_reversion_ends_once_a_minute_after_its_last_report(self):
        points = [report(t, "7700") for t in range(0, 31, 10)] + [report(t) for t in range(40, 151, 10)]
        finding = statuses(points)[0]
        self.assertEqual((info(finding)["state"], info(finding)["end_reason"]), ("ended", "stale"))
        self.assertEqual(info(finding)["evaluated_at"], NOW + 90)
        later = points + [report(t) for t in range(160, 300, 10)]
        self.assertEqual(statuses(later), [])

    def test_coverage_loss_closes_the_status_at_the_next_report(self):
        points = [report(t, "7700") for t in range(0, 31, 10)] + [report(200, "1200")]
        found = [f for f in maneuvers.analyze(points, AIRCRAFT) if f.kind.startswith("special_squawk_")]
        self.assertEqual(len(found), 1)
        self.assertEqual((info(found[0])["state"], info(found[0])["end_reason"]), ("ended", "segment_break"))

    def test_silent_aircraft_status_becomes_stale(self):
        points = [report(t, "7700") for t in range(0, 91, 10)]
        found = maneuvers.analyze(points, AIRCRAFT, now=NOW + 200)
        self.assertEqual([(f.kind, info(f)["end_reason"]) for f in found], [("special_squawk_7700", "stale")])


class SpecialUseCodeTests(unittest.TestCase):
    def test_special_use_codes_carry_their_code(self):
        for code in ("1202", "1255", "1276", "1277", "4000", "7777"):
            with self.subTest(code=code):
                found = [
                    f
                    for f in maneuvers.signals([report(t, code) for t in (0, 10, 20)])
                    if f.kind == "special_use_code"
                ]
                self.assertEqual(len(found), 1)
                self.assertEqual(info(found[0])["code"], code)
                self.assertTrue(info(found[0])["confirmed"])
                self.assertEqual(found[0].category, "signals")

    def test_law_enforcement_block_is_held_for_review(self):
        finding = maneuvers.signals([report(t, "4455") for t in (0, 10, 20)])[0]
        self.assertIn("sensitive_code_block", info(finding)["quality_flags"])
        self.assertTrue(info(finding)["quality_review_required"])

    def test_discrete_codes_are_not_special(self):
        self.assertEqual(
            maneuvers.signals([report(t, code) for t, code in ((0, "5123"), (10, "2316"), (20, "1200"))]), []
        )

    def test_changing_special_codes_are_separate_episodes(self):
        points = [report(t, "1277") for t in (0, 10, 20)] + [report(t, "1255") for t in (30, 40, 50)]
        found = maneuvers.signals(points)
        self.assertEqual([info(f)["code"] for f in found], ["1277", "1255"])
        self.assertEqual(info(found[0])["ending_code"], "1255")


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

    def test_status_sequence_is_saved_as_one_event(self):
        codes = ["7700"] * 4 + ["7600"] * 4 + ["1200"] * 2
        for index, code in enumerate(codes):
            a = {**AIRCRAFT, "squawk": code, "seen": 0, "lat": 42, "lon": -84, "seen_pos": 0, "gs": 200}
            a.update(alt_baro=9000, messages=100 + index)
            self.detector.process({"now": NOW + index * 10, "aircraft": [a]}, NOW + index * 10)
        with app.connect(self.path) as db:
            rows = db.execute(
                "SELECT kind, evidence FROM events WHERE kind LIKE 'special_squawk_%'"
            ).fetchall()
        self.assertEqual([r["kind"] for r in rows], ["special_squawk_7700"])
        saved = json.loads(rows[0]["evidence"])["maneuver"]
        self.assertEqual([s["code"] for s in saved["code_sequence"]], ["7700", "7600"])
        self.assertEqual(saved["state"], "ended")


if __name__ == "__main__":
    unittest.main()
