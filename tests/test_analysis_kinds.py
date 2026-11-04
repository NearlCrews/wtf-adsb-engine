"""The kind registry holds every kind and the properties the engine's tables derive from.

Ported from the live project's tests/test_kinds.py: the registry's shape and the engine-side
derived sets. The publisher's wording and format tables are checked in the publisher.
"""

import re
import unittest
from pathlib import Path

from wtf_adsb_engine.analysis import app, curation, kinds, maneuvers, rules, stories

ANALYSIS = Path(kinds.__file__).resolve().parent


class RegistryShape(unittest.TestCase):
    def test_story_kinds_are_the_registry_entries_flagged_story(self):
        self.assertEqual(kinds.where(story=True), frozenset(stories.STORY_KINDS))

    def test_the_registry_holds_the_story_and_episode_kinds_and_the_two_context_kinds(self):
        self.assertEqual(
            set(kinds.KINDS),
            set(stories.STORY_KINDS) | set(maneuvers.KINDS) | {"distant", "vfr_code_above_fl180"},
        )

    def test_the_two_context_kinds_are_registered(self):
        self.assertEqual(kinds.kind("distant").origin, "context")
        self.assertEqual(kinds.kind("vfr_code_above_fl180").origin, "context")

    def test_each_entry_is_registered_under_its_own_name(self):
        for name, entry in kinds.KINDS.items():
            with self.subTest(kind=name):
                self.assertEqual(entry.name, name)

    def test_where_matches_properties_by_equality(self):
        self.assertEqual(kinds.where(rated="altitude"), frozenset({"above_rated_altitude"}))
        self.assertEqual(
            kinds.where(origin="context", story=False), frozenset({"distant", "vfr_code_above_fl180"})
        )
        self.assertEqual(kinds.where(numeric_fields=("gs",)), frozenset({"fast", "type_speed_mismatch"}))
        self.assertEqual(kinds.where(fixed_score=(30, None)), frozenset({"distant"}))
        self.assertEqual(kinds.where(family="outlier", limit_dimension="speed"), frozenset({"outlier_gs"}))

    def test_an_unknown_property_is_refused(self):
        with self.assertRaises(TypeError):
            kinds.where(no_such_property=True)
        with self.assertRaises(TypeError):
            kinds.table("no_such_property")

    def test_unknown_kind_raises(self):
        with self.assertRaises(KeyError):
            kinds.kind("no_such_kind")

    def test_table_keeps_a_falsy_value_that_is_set(self):
        self.assertEqual(kinds.table("interest")["selected_altitude_overshoot"], 0)

    def test_table_refuses_an_order_that_does_not_name_exactly_its_kinds(self):
        with self.assertRaises(ValueError):
            kinds.table("rated_field", order=("above_rated_altitude",))
        with self.assertRaises(ValueError):
            kinds.table("rated_field", order=("above_rated_altitude", "above_rated_speed", "fast"))
        self.assertEqual(
            list(kinds.table("rated_field", order=("above_rated_speed", "above_rated_altitude"))),
            ["above_rated_speed", "above_rated_altitude"],
        )

    def test_the_squawk_families_are_the_status_tables(self):
        live = {kinds.LIVE_SQUAWK_PREFIX + status for status in kinds.STATUS_CODES | kinds.STATUS_NAMES}
        archived = {kinds.ARCHIVE_SQUAWK_PREFIX + code for code in kinds.STATUS_CODES}
        self.assertEqual(frozenset(stories.SPECIAL_KINDS), live | archived)
        self.assertEqual(kinds.where(origin="special"), live | archived)

    def test_the_status_tables_are_re_exported_unchanged(self):
        for module in (rules, maneuvers):
            with self.subTest(module=module.__name__):
                self.assertIs(module.STATUS_MEANINGS, kinds.STATUS_MEANINGS)
                self.assertIs(module.STATUS_CODES, kinds.STATUS_CODES)
                self.assertIs(module.STATUS_NAMES, kinds.STATUS_NAMES)
        self.assertIs(rules.SQUAWK_PREFIXES, kinds.SQUAWK_PREFIXES)
        self.assertIs(maneuvers.PRIORITY_STATUSES, kinds.PRIORITY_STATUSES)

    def test_property_values_are_data_only(self):
        def data(value):
            if isinstance(value, tuple):
                return all(data(item) for item in value)
            return value is None or isinstance(value, (bool, int, str))

        for name, entry in kinds.KINDS.items():
            for prop in kinds.PROPERTIES:
                with self.subTest(kind=name, prop=prop):
                    self.assertTrue(data(getattr(entry, prop)))

    def test_the_registry_imports_nothing_from_the_package(self):
        source = Path(kinds.__file__).read_text()
        self.assertNotRegex(source, r"(?m)^\s*from\s+\.")
        project = {path.stem for path in ANALYSIS.glob("*.py")} - {"kinds"}
        for name in sorted(project | {"wtf_adsb_engine"}):
            with self.subTest(module=name):
                self.assertNotRegex(source, rf"(?m)^\s*(from|import)\s+{re.escape(name)}\b")


class DerivedSetsKeepTheirRelations(unittest.TestCase):
    """What the replaced literals and unions stated, now that each derives from one property."""

    def test_status_exceptions_are_the_live_statuses_less_the_priority_declarations(self):
        statuses = kinds.STATUS_CODES | (kinds.STATUS_NAMES - kinds.PRIORITY_STATUSES)
        self.assertEqual(
            curation.STATUS_EXCEPTIONS,
            frozenset(kinds.LIVE_SQUAWK_PREFIX + status for status in statuses),
        )

    def test_episode_pattern_kinds_are_the_episode_kinds_outside_a_journal_category(self):
        self.assertEqual(kinds.where(origin="episode", pattern=True), frozenset(maneuvers.PATTERN_KINDS))
        self.assertEqual(stories.EPISODE_KINDS, maneuvers.KINDS)

    def test_the_unions_the_literals_spelled_out_still_hold(self):
        self.assertEqual(
            app.DAILY_KINDS,
            frozenset(curation.IDENTITY_KINDS | {"identity_conflict", "emitter_type_mismatch"}),
        )
        self.assertEqual(
            curation.SERIES_KINDS,
            frozenset({"identity_conflict", "winds_aloft_day"}) | curation.RECORD_KINDS | rules.RATED_KINDS,
        )
        self.assertLessEqual(stories.FIRST_SIGHTING_KINDS, set(kinds.where(cross_day=True)))
        self.assertEqual(curation.FIRST_KINDS, frozenset(stories.FIRST_SIGHTING_KINDS))

    def test_the_norm_kinds_are_the_ground_speed_type_stories(self):
        self.assertIs(curation.NORM_KINDS, rules.NORM_KINDS)
        self.assertEqual(rules.NORM_KINDS, frozenset({"seasonal_speed_record", "type_speed_mismatch"}))


class NewKindsAreRegistered(unittest.TestCase):
    def test_six_new_kinds_follow_above_rated_speed_in_order(self):
        names = list(kinds.KINDS)
        start = names.index("above_rated_speed") + 1
        self.assertEqual(
            names[start : start + 6],
            [
                "winds_aloft_day",
                "foreign_military",
                "first_operator_here",
                "aerobatic_practice",
                "unregistered_address",
                "racetrack_orbit",
            ],
        )

    def test_flags_and_tables(self):
        self.assertEqual(kinds.where(subjectless=True), frozenset({"winds_aloft_day"}))
        self.assertEqual(kinds.KINDS["winds_aloft_day"].fixed_score, (80, None))
        self.assertEqual(kinds.KINDS["foreign_military"].fixed_score, (80, None))
        self.assertTrue(kinds.KINDS["foreign_military"].type_dependent)
        self.assertTrue(kinds.KINDS["first_operator_here"].first_sighting)
        self.assertTrue(kinds.KINDS["first_operator_here"].callsign_first)
        self.assertEqual(
            kinds.KINDS["unregistered_address"].fixed_score,
            (75, "A registry gap can be lag; the story waits a week"),
        )
        self.assertEqual(
            kinds.table("absorbs")["aerobatic_practice"], ("altitude_oscillation", "course_reversal")
        )
        self.assertEqual(
            kinds.table("absorbs")["racetrack_orbit"],
            ("course_reversal", "parallel_passes", "prolonged_holding"),
        )
        self.assertEqual(kinds.KINDS["identity_conflict"].fixed_score[0], 70)
        self.assertEqual(kinds.KINDS["holding_stack"].fixed_score, (78, None))


if __name__ == "__main__":
    unittest.main()
