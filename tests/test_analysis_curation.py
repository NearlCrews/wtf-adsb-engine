"""Daily curation: kind repeats per airframe and the mismatch story's registry gate.

Ported from the new tests in the live project's tests/test_curation.py, with its finding and
group helpers; the rest of that file exercises publisher modules and stays there.
"""

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from wtf_adsb_engine.analysis import curation, reference, selection, typecheck

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
                self.assertEqual(
                    curation.type_hold(with_check(kind, "pending"), now=START)[0], "type_check_pending"
                )
                # No registry listing the address still leaves the type uncorroborated.
                self.assertIn(
                    "type_uncorroborated",
                    [rule for rule, _ in curation.gates(with_check(kind, "unavailable", code=None))],
                )
        self.assertEqual(
            [rule for rule, _ in curation.gates(with_check("first_type_here", "disagrees"))],
            ["type_disagrees"],
        )


# A fixed SR22 envelope so the gate does not depend on the shipped dataset's values.
SR22_ENVELOPE = {
    "type": "SR22",
    "basis": "type",
    "speed_bound_kt": 240,
    "wind_allowance_kt": 60,
    "wind_band_ft": [None, 10000],
    "ground_speed_cap_kt": 300,
    "ceiling_ft": 17500,
    "altitude_margin_ft": 5000,
    "altitude_cap_ft": 22500,
    "dataset_version": "test",
}


class ReadingImplausible(unittest.TestCase):
    """Type-dependent identity stories need a saved reading the listed type can produce."""

    def setUp(self):
        for patcher in (
            patch.object(reference, "envelope", return_value=SR22_ENVELOPE),
            patch.object(reference, "limits", return_value=None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def event(self, kind, **aircraft):
        base = {"hex": "a1b2c3", "r": "N283CP", "t": "SR22", "type": "adsb_icao", "gs": 160, "alt_baro": 8000}
        return {
            "kind": kind,
            "value": None,
            "hits": 5,
            "first_seen": 0,
            "last_seen": 60,
            "evidence": {"aircraft": {**base, **aircraft}},
        }

    def test_sr22_at_45000_ft_is_gated(self):
        rules_found = dict(curation.gates(self.event("first_type_here", alt_baro=45000)))
        self.assertIn("reading_implausible", rules_found)
        self.assertIn("45,000 ft", rules_found["reading_implausible"])
        self.assertIn("17,500 ft", rules_found["reading_implausible"])

    def test_sr22_at_420_knots_is_gated(self):
        rules_found = dict(curation.gates(self.event("watch_type", gs=420)))
        self.assertIn("reading_implausible", rules_found)
        self.assertIn("420 kn", rules_found["reading_implausible"])

    def test_only_a_direct_ads_b_speed_is_evidence(self):
        mlat = self.event("first_type_here", gs=420, mlat=["gs", "lat", "lon"])
        tisb = self.event("vintage", gs=420, tisb=["gs"])
        solved = self.event("foreign_military", gs=420, type="mlat")
        for event in (mlat, tisb, solved):
            with self.subTest(aircraft=event["evidence"]["aircraft"]):
                self.assertNotIn("reading_implausible", dict(curation.gates(event)))
        self.assertIn("reading_implausible", dict(curation.gates(self.event("first_type_here", gs=420))))
        # A position-derived speed leaves the altitude check in place.
        high = self.event("first_type_here", gs=420, alt_baro=45000, mlat=["gs"])
        self.assertIn("ceiling of 17,500 ft", dict(curation.gates(high))["reading_implausible"])

    def test_the_ceiling_has_a_margin(self):
        within = self.event("notable_variant", alt_baro=17500 + curation.CEILING_MARGIN_FT)
        above = self.event("notable_variant", alt_baro=17500 + curation.CEILING_MARGIN_FT + 100)
        self.assertNotIn("reading_implausible", dict(curation.gates(within)))
        self.assertIn("reading_implausible", dict(curation.gates(above)))

    def test_plausible_reading_passes_and_other_kinds_ignore_it(self):
        self.assertNotIn("reading_implausible", dict(curation.gates(self.event("vintage"))))
        self.assertNotIn(
            "reading_implausible", dict(curation.gates(self.event("rapid_descent", alt_baro=45000)))
        )

    def test_every_listed_kind_is_gated(self):
        for kind in sorted(curation.PLAUSIBILITY_KINDS):
            with self.subTest(kind=kind):
                self.assertIn("reading_implausible", dict(curation.gates(self.event(kind, alt_baro=45000))))

    def test_an_unknown_envelope_is_no_evidence(self):
        with patch.object(reference, "envelope", return_value=None):
            event = self.event("foreign_military", alt_baro=45000, gs=420)
            self.assertNotIn("reading_implausible", dict(curation.gates(event)))

    def test_a_missing_reading_is_no_evidence(self):
        event = self.event("first_type_here")
        del event["evidence"]["aircraft"]["gs"], event["evidence"]["aircraft"]["alt_baro"]
        self.assertNotIn("reading_implausible", dict(curation.gates(event)))


class StaleTypeChecks(unittest.TestCase):
    NOW = START + 40 * 86400

    def event(self, kind, state, age_days):
        event = finding(1, kind, value=394, address="abcdef")
        event["first_seen"] = self.NOW - age_days * 86400
        if state is None:
            del event["evidence"]["type_check"]
        else:
            event["evidence"]["type_check"] = {"state": state, "source": "adsbdb", "checked_at": START}
        return event

    def test_a_check_pending_for_thirty_days_stops_holding_and_counts_as_unavailable(self):
        self.assertEqual(curation.STALE_CHECK_SECONDS, 30 * 86400)
        for kind in ("type_speed_mismatch", "first_type_here"):
            for state in (None, "failed"):
                with self.subTest(kind=kind, state=state):
                    fresh, stale = self.event(kind, state, 2), self.event(kind, state, 31)
                    self.assertEqual(curation.type_hold(fresh, now=self.NOW)[0], "type_check_pending")
                    self.assertIsNone(curation.type_hold(stale, now=self.NOW))
                    self.assertTrue(curation.stale_check(stale, now=self.NOW))
                    self.assertFalse(curation.stale_check(fresh, now=self.NOW))
        # A stale check leaves a performance claim uncorroborated; a settled one is never stale.
        rules = [
            rule for rule, _ in curation.gates(self.event("type_speed_mismatch", None, 31), now=self.NOW)
        ]
        self.assertIn("type_uncorroborated", rules)
        settled = self.event("type_speed_mismatch", "agrees", 31)
        self.assertFalse(curation.stale_check({**settled, "first_seen": 0}, now=self.NOW))
        self.assertNotIn("type_uncorroborated", [rule for rule, _ in curation.gates(settled, now=self.NOW)])

    def test_an_unlisted_address_check_never_goes_stale(self):
        # A registry that never answered says nothing about whether it lists the address.
        for state in (None, "failed"):
            with self.subTest(state=state):
                event = self.event("unregistered_address", state, 31)
                if state == "failed":
                    # A failed block names no source (typecheck.failure_evidence).
                    event["evidence"]["type_check"]["source"] = None
                self.assertFalse(curation.stale_check(event, now=self.NOW))
                self.assertEqual(curation.type_hold(event, now=self.NOW)[0], "type_check_pending")
                self.assertIn("registry_unchecked", dict(curation.gates(event, now=self.NOW)))


class AerobaticContext(unittest.TestCase):
    def test_a_terminal_or_circuit_tag_makes_aerobatic_practice_context_only(self):
        self.assertFalse(curation.context_only("aerobatic_practice", {"maneuver": {"context_tags": []}}))
        for tag in ("terminal_area_context", "runway_circuit_context"):
            with self.subTest(tag=tag):
                evidence = {"maneuver": {"context_tags": [tag]}}
                self.assertTrue(curation.context_only("aerobatic_practice", evidence))
                self.assertFalse(curation.context_only("racetrack_orbit", evidence))


class UnlistedAddressGates(unittest.TestCase):
    """An unlisted-address finding passes only once a settled recheck finds no registry entry."""

    def event(self, **check):
        event = finding(1, "unregistered_address", value=None, address="a1b2c3")
        event["evidence"]["aircraft"]["t"] = ""
        if check:
            event["evidence"]["type_check"] = {
                "state": "unavailable",
                "source": None,
                "type_code": None,
                "registration": None,
                "listed_type": None,
                **check,
            }
        else:
            del event["evidence"]["type_check"]
        return event

    def rules(self, event):
        return [rule for rule, _ in curation.gates(event, now=START + 40 * 86400)]

    def test_a_registry_that_lists_the_address_blocks_it(self):
        week = START + typecheck.RECHECK_SECONDS
        for check in (
            {"source": "hexdb", "registration": "N123AB"},
            {"source": "adsbdb", "type_code": "C172", "listed_type": None},
            {"registration": "C-GKXM"},
        ):
            with self.subTest(check=check):
                found = self.rules(self.event(checked_at=week, **check))
                self.assertIn("registry_lists_address", found)
                self.assertNotIn("registry_unchecked", found)

    def test_only_a_settled_check_a_week_after_the_first_sighting_passes(self):
        self.assertIn("registry_unchecked", self.rules(self.event()))
        self.assertIn("registry_unchecked", self.rules(self.event(checked_at=START + 86400)))
        failed = self.event(state="failed", checked_at=START + typecheck.RECHECK_SECONDS)
        self.assertIn("registry_unchecked", self.rules(failed))
        settled = self.event(checked_at=START + typecheck.RECHECK_SECONDS)
        self.assertEqual([rule for rule in self.rules(settled) if rule.startswith("registry_")], [])


def operator_first(identifier, designator, *, day=0, address=None):
    event = finding(identifier, "first_operator_here", value=60, address=address, day=day)
    event["evidence"]["aircraft"]["flight"] = f"{designator}123"
    event["evidence"]["first_sighting"] = {
        "scope": "operator",
        "designator": designator,
        "operator": designator,
        "coverage_days": 60,
        "since": "2026-08-01",
    }
    return event


class OperatorFirsts(unittest.TestCase):
    """An operator first is keyed by its designator, never by the type that flies it."""

    def test_the_subtopic_is_the_designator(self):
        self.assertEqual(curation.facts(operator_first(1, "DAL"))["subtopic"], "operator:DAL")
        # Without the block, the callsign's own designator names it.
        event = operator_first(2, "DAL")
        del event["evidence"]["first_sighting"]
        with patch.object(reference, "operator_name", lambda code: "Delta" if code == "DAL" else None):
            self.assertEqual(curation.facts(event)["subtopic"], "operator:DAL")
        self.assertEqual(curation.facts(finding(3, "first_type_here"))["subtopic"], "type:C172")

    def test_only_a_second_first_for_the_same_designator_repeats(self):
        earlier = curation.choose([group(operator_first(1, "DAL", address="aaaaaa"))])
        history = [curation.story_entry(earlier["selected"][0])]
        for designator, disposition in (("UAL", "selected"), ("DAL", "first_repeat")):
            with self.subTest(designator=designator):
                later = group(operator_first(2, designator, day=40, address="bbbbbb"))
                row = curation.choose([later], history=history)["decisions"][0]
                self.assertEqual(row["disposition"], disposition)

    def test_a_routine_type_does_not_hold_an_operator_first(self):
        def routine(day):
            return {"coverage_days": 60, "type_days": {"C172": 40}}

        earlier = {
            "addresses": ["aaaaaa"],
            "kind": "foreign_military",
            "day": "2026-09-26",
            "start": START - 5 * 86400,
            "type": "C172",
            "subtopic": "type:C172",
            "routine_type": True,
        }
        candidate = group(operator_first(2, "UAL", address="bbbbbb"))
        result = curation.choose([candidate], history=[earlier], novelty=routine)
        row = result["decisions"][0]
        self.assertEqual(row["disposition"], "selected")
        self.assertFalse(row["routine_type"])
        # An operator first on a routine type does not hold a later type story either.
        operator_story = curation.story_entry(result["selected"][0])
        later = {**earlier, "addresses": ["cccccc"], "routine_type": False, "start": START + 86400}
        self.assertIsNone(curation.StoryLog([operator_story]).repeat(later))
        self.assertEqual(curation.StoryLog([earlier]).repeat(later), "routine_type")


class RacetrackNearRunway(unittest.TestCase):
    def test_a_racetrack_tagged_near_a_runway_scores_50_and_is_not_a_story(self):
        plain = finding(1, "racetrack_orbit", value=6)
        self.assertEqual(plain["selection"]["score"], 80)
        self.assertTrue(plain["selection"]["story_eligible"])
        for where in ("tag", "evidence"):
            with self.subTest(where=where):
                event = finding(2, "racetrack_orbit", value=6)
                if where == "tag":
                    event["evidence"]["maneuver"]["context_tags"] = ["airport_context"]
                else:
                    event["evidence"]["airport_context"] = {"runway": "09"}
                event["selection"] = selection.assess(event, {})
                self.assertEqual(event["selection"]["score"], 50)
                self.assertFalse(event["selection"]["story_eligible"])
                self.assertLessEqual(curation.rate(event)["interest"], 8)
                self.assertGreater(curation.rate(plain)["interest"], 8)


class EpisodeScores(unittest.TestCase):
    def test_an_episode_kind_with_its_own_score_keeps_it(self):
        self.assertEqual(finding(1, "holding_stack")["selection"]["score"], 78)
        self.assertEqual(finding(2, "winds_aloft_day")["selection"]["score"], 80)
        for kind in ("racetrack_orbit", "aerobatic_practice", "survey_grid", "descending_spiral"):
            with self.subTest(kind=kind):
                self.assertEqual(finding(3, kind, value=6)["selection"]["score"], 80)
        # An unconfirmed episode is still capped for review.
        self.assertEqual(finding(4, "holding_stack", confirmed=False)["selection"]["score"], 60)


if __name__ == "__main__":
    unittest.main()
