"""Daily curation: kind repeats per airframe and the mismatch story's registry gate.

Ported from the new tests in the live project's tests/test_curation.py, with its finding and
group helpers; the rest of that file exercises publisher modules and stays there.
"""

import unittest
from datetime import datetime, timezone

from wtf_adsb_engine.analysis import curation, selection, typecheck

START = datetime(2026, 10, 1, 12, tzinfo=timezone.utc).timestamp()


def finding(identifier, kind="circling", *, day=0, value=3, address=None, flags=(), confirmed=True):
    metrics = {
        "confirmed": confirmed,
        "shape_verified": confirmed,
        "quality_review_required": bool(flags),
        "quality_flags": list(flags),
        "duration_seconds": 720,
        "sample_count": 46,
        "closed_loops": 3,
    }
    evidence = {"aircraft": {"t": "C172", "type": "adsb_icao"}}
    if kind in typecheck.TYPE_DEPENDENT_KINDS:
        # A completed, agreeing second-source type check: publication waits for one
        # (curation.type_hold); the states themselves are covered in test_typecheck.
        evidence["type_check"] = {"state": "agrees", "source": "adsbdb", "checked_at": START}
    if kind in {"rapid_descent", "rapid_climb"}:
        metrics.update(
            peak_rate_fpm=value,
            started_at=START + day * 86400,
            ended_at=START + day * 86400 + 720,
            threshold_fpm=4500,
            qualifying_duration_seconds=30,
            qualifying_sample_count=4,
        )
        if abs(value) >= 7000:
            metrics.update(
                sustained_rate_fpm=value,
                sustained_duration_seconds=30,
                sustained_sample_count=4,
                sustained_altitude_change_ft=value / 2,
                sustained_expected_altitude_change_ft=value / 2,
                sustained_started_at=START + day * 86400,
                sustained_ended_at=START + day * 86400 + 30,
            )
    if kind in {"circling", "repeated_passes", "loitering", "confined_activity"}:
        evidence["pattern"] = metrics
    elif kind not in curation.IDENTITY_KINDS:
        evidence["maneuver"] = metrics
    event = {
        "id": identifier,
        "hex": address or f"{identifier:06x}",
        "kind": kind,
        "first_seen": START + day * 86400,
        "last_seen": START + day * 86400 + 720,
        "value": value,
        "hits": 46,
        "evidence": evidence,
    }
    event["selection"] = selection.assess(event, {})
    return event


def group(*events, key=None):
    return {
        "id": key or str(events[0]["id"]),
        "events": list(events),
        "first_seen": min(event["first_seen"] for event in events),
        "last_seen": max(event["last_seen"] for event in events),
    }


class CurationTests(unittest.TestCase):
    def test_a_kind_repeats_for_one_airframe_only_after_thirty_days(self):
        def spiral(identifier, day, address="abcdef"):
            return finding(identifier, "descending_spiral", value=6, address=address, day=day)

        groups = [group(spiral(i + 1, day)) for i, day in enumerate((0, 1, 29, 30))]
        result = curation.choose(groups)
        self.assertEqual([row["finding_id"] for row in result["selected"]], [1, 4])
        self.assertEqual(
            next(row for row in result["decisions"] if row["finding_id"] == 2)["disposition"], "kind_repeat"
        )
        # Another kind, another airframe, and an exceptional repeat all run.
        other_kind = group(finding(5, "rapid_descent", value=-6000, address="abcdef", day=2))
        other_aircraft = group(spiral(6, 2, address="fedcba"))
        exceptional = group(finding(7, "special_squawk_7700", value=7700, address="abcdef", day=3))
        exceptional_again = group(finding(8, "special_squawk_7700", value=7700, address="abcdef", day=4))
        chosen = curation.choose([groups[0], other_kind, other_aircraft, exceptional, exceptional_again])
        self.assertEqual(sorted(row["finding_id"] for row in chosen["selected"]), [1, 5, 6, 7, 8])
        # Squawk kinds share a family: a special and an archive code repeat each other.
        self.assertEqual(
            curation.kind_family("archive_squawk_7600"), curation.kind_family("special_squawk_7600")
        )
        archive = {
            "addresses": ["abcdef"],
            "kind": "archive_squawk_7600",
            "day": "2026-10-06",
            "start": START,
        }
        special = {**archive, "kind": "special_squawk_7600", "day": "2026-10-07", "start": START + 86400}
        self.assertEqual(curation.StoryLog([archive]).repeat(special), "kind_repeat")

    def test_a_series_repeat_on_one_airframe_takes_the_airframe_rules_reason(self):
        # The airframe rules run before the series rule (docs/daily-curation-2026-10-05.md).
        # A watchlist match is an identity kind, never a series.
        self.assertNotIn("watchlist_match", curation.SERIES_KINDS)
        for kind, reason in (
            ("identity_conflict", "identity_repeat"),
            ("seasonal_speed_record", "kind_repeat"),
            ("above_rated_altitude", "kind_repeat"),
        ):
            earlier = {
                "addresses": ["abcdef"],
                "kind": kind,
                "day": "2026-10-06",
                "start": START,
                "series": kind,
            }
            later = {**earlier, "day": "2026-10-08", "start": START + 2 * 86400}
            with self.subTest(kind=kind):
                self.assertEqual(curation.StoryLog([earlier]).repeat(later), reason)
                other = {**later, "addresses": ["fedcba"]}
                self.assertEqual(curation.StoryLog([earlier]).repeat(other), "series_repeat")

    def test_released_history_holds_a_kind_repeat_both_ways_unless_withdrawn(self):
        prior = curation.choose([group(finding(1, "descending_spiral", value=6, address="abcdef"))])
        before = curation.story_entry(prior["selected"][0])
        after = curation.story_entry(
            curation.choose([group(finding(1, "descending_spiral", value=6, address="abcdef", day=20))])[
                "selected"
            ][0]
        )
        candidate = group(finding(2, "descending_spiral", value=6, address="abcdef", day=10))
        for history, disposition in (
            ([before], "kind_repeat"),
            ([after], "kind_repeat"),
            ([{**before, "withdrawn": True}], "selected"),
        ):
            with self.subTest(history=history[0]["day"], withdrawn=history[0].get("withdrawn", False)):
                row = curation.choose([candidate], history=history)["decisions"][0]
                self.assertEqual(row["disposition"], disposition)
                if disposition == "kind_repeat":
                    self.assertIn(curation.REPEAT_REASONS["kind_repeat"], row["reasons"])

    def test_a_held_kind_repeat_gives_its_slot_to_the_next_candidate(self):
        prior = curation.choose([group(finding(1, "descending_spiral", value=6, address="abcdef"))])
        history = [curation.story_entry(prior["selected"][0])]
        # The repeat would outrank every other candidate on this full day.
        repeat = group(finding(2, "descending_spiral", value=6, address="abcdef", day=10))
        kinds = (
            ("rapid_descent", -6000),
            ("tcas_ra", 3),
            ("departure_return", 3),
            ("formation_flight", 3),
            ("possible_go_around", 3),
            ("holding_stack", 3),
            ("fast", 600),
            ("hovering", 3),
            ("vintage", 100),
        )
        others = [group(finding(20 + i, kind, value=value, day=10)) for i, (kind, value) in enumerate(kinds)]
        result = curation.choose([repeat, *others], history=history)
        self.assertEqual(sorted(row["finding_id"] for row in result["selected"]), list(range(20, 29)))
        last = max(result["selected"], key=lambda row: row["rank"])
        self.assertEqual((last["rank"], last["disposition"]), (9, "selected"))
        self.assertEqual(
            next(row for row in result["decisions"] if row["finding_id"] == 2)["disposition"], "kind_repeat"
        )

    def test_a_disagreeing_registry_no_longer_blocks_a_mismatch_story_but_still_blocks_a_type_story(self):
        def with_check(kind, state, code="B752"):
            event = finding(1, kind, value=394, address="abcdef")
            event["evidence"]["aircraft"]["t"] = "C172"
            event["evidence"]["type_check"] = {
                "state": state,
                "source": "adsbdb",
                "type_code": code,
                "listed_type": "C172",
                "checked_at": 1,
            }
            return event

        for kind in sorted(typecheck.REGISTRY_TYPE_KINDS):
            with self.subTest(kind=kind):
                self.assertEqual([rule for rule, _ in curation.gates(with_check(kind, "disagrees"))], [])
                self.assertEqual(curation.type_hold(with_check(kind, "pending"))[0], "type_check_pending")
                # No registry listing the address still leaves the type uncorroborated.
                self.assertIn(
                    "type_uncorroborated",
                    [rule for rule, _ in curation.gates(with_check(kind, "unavailable", code=None))],
                )
        self.assertEqual(
            [rule for rule, _ in curation.gates(with_check("first_type_here", "disagrees"))],
            ["type_disagrees"],
        )


if __name__ == "__main__":
    unittest.main()
