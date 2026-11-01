"""The kind registry: every finding kind and the named properties the kind-keyed tables derive from.

A kind's properties are data only (strings, booleans, integers, tuples, and None). Wording
and behaviour stay in the modules that own them: story angles, notes, limitations,
hypotheses, required sentences, and fixed headlines keep their own tables, keyed by the
kinds registered here. This module imports nothing from the project, so the collector
and the publisher can both load it.

The entries are declared in the order that the derived dicts iterating the registry keep
(curation.INTEREST first among them); tests/test_kinds_golden.py pins every derived table.
"""

from dataclasses import KW_ONLY, dataclass, fields

# The one transponder-status table. A code and the aircraft's own emergency field
# describe one status; rules and maneuvers re-export these names.
STATUS_MEANINGS = {"7400": "lost_link", "7500": "unlawful", "7600": "nordo", "7700": "general"}
STATUS_CODES = frozenset(STATUS_MEANINGS)
STATUS_NAMES = frozenset({"general", "lifeguard", "minfuel", "nordo", "unlawful", "downed"})
# Priority declarations are separate episodes, so they never hide a later emergency.
PRIORITY_STATUSES = {"lifeguard", "minfuel"}
# Kind prefixes of the live and episode-archive transponder-code findings.
LIVE_SQUAWK_PREFIX = "special_squawk_"
ARCHIVE_SQUAWK_PREFIX = "archive_squawk_"
SQUAWK_PREFIXES = (LIVE_SQUAWK_PREFIX, ARCHIVE_SQUAWK_PREFIX)


@dataclass(frozen=True)
class Kind:
    """One kind's properties. Each comment names the table the property is read from;
    `story` and `family` follow from the origin and the name."""

    name: str
    _: KW_ONLY
    # "story" (stories.KINDS), "episode" (maneuvers.KINDS), "special" (the transponder-status
    # families), or "context" (named only by collector or curation tables).
    origin: str
    # maneuvers.CATEGORIES: the journal category; None is a flight pattern.
    journal_category: str | None = None
    # stories.PATTERN_KINDS.
    pattern: bool = False
    # public_events.KIND_TYPES: the public section.
    section: str | None = None
    # data/topics.json: the site topic whose kinds list holds the kind (not curation.topic()).
    site_topic: str | None = None
    # story_urls.PHENOMENA: the slug's phenomenon and its short form.
    slug: tuple[str, str] | None = None
    # story_urls.SUBJECT_KINDS, PAIR_KINDS, and CALLSIGN_FIRST.
    slug_subject: bool = False
    pair: bool = False
    callsign_first: bool = False
    # curation.IDENTITY_KINDS.
    identity: bool = False
    # rules.IDENTITY_LED_KINDS.
    identity_led: bool = False
    # app.DAILY_KINDS: saved once per aircraft per UTC day.
    daily_row: bool = False
    # stories.FIRST_SIGHTING_KINDS and curation.FIRST_KINDS.
    first_sighting: bool = False
    # history_backfill.CROSS_DAY_KINDS.
    cross_day: bool = False
    # enrichment_rules.TYPE_DEPENDENT_KINDS and REGISTRY_TYPE_KINDS.
    type_dependent: bool = False
    registry_type: bool = False
    # curation.RECORD_KINDS, rules.NORM_KINDS, and curation.SERIES_KINDS.
    record: bool = False
    norm: bool = False
    series: bool = False
    # curation.CONTEXT_KINDS, DATA_QUALITY_KINDS, CIRCUIT_KINDS, BALLOON_FOLDS, and STATUS_EXCEPTIONS.
    context_only: bool = False
    data_quality: bool = False
    circuit_shape: bool = False
    balloon_fold: bool = False
    status_exception: bool = False
    # app.BEST_WINDOW_KINDS.
    best_window: bool = False
    # maneuvers.FRESHNESS_ROBUST_KINDS.
    freshness_robust: bool = False
    # episode_evidence.MLAT_ESTIMATE_KINDS.
    mlat_estimate: bool = False
    # public_events.RATE_KINDS.
    rate_reading: bool = False
    # app.Detector.WIDE_SPREAD_KINDS and SIGNED_KINDS.
    wide_spread: bool = False
    signed: bool = False
    # episode_replay.DETAIL_KINDS and ORBIT_KINDS.
    detail: bool = False
    orbit: bool = False
    # explain.PRIVATE_NOTE_KINDS.
    private_note: bool = False
    # selection.NUMERIC_KINDS and NOVELTY_KINDS.
    numeric: bool = False
    novelty: bool = False
    # rules.GROUND_SPEED_KINDS: findings whose reading is a ground speed (the wind worker flags them).
    ground_speed_reading: bool = False
    # public_prose.SPEED_PROSE_KINDS and editorial.SPEED_KINDS: prose that names a ground speed.
    speed_prose: bool = False
    # public_prose.CAUSELESS_KINDS.
    causeless: bool = False
    # editorial.DURATIVE_SHAPES and EMITTER_KINDS.
    durative: bool = False
    emitter: bool = False
    # public_events.DATABASE_KINDS.
    database_basis: bool = False
    # story_facts.AIRPORT_EXCLUDED_KINDS, with the transponder-status kinds: climbs and descents,
    # go-arounds and returns, holds and reversals, and collision advisories.
    landing_shaped: bool = False
    # "altitude" or "speed": rules.RATED_KINDS, public_events.ALTITUDE_KINDS and SPEED_KINDS.
    rated: str | None = None
    # stories.RATED_FIELDS.
    rated_field: str | None = None
    # rules.MEASURED_FIELDS.
    measured_field: str | None = None
    # app.Detector.NUMERIC_FIELDS.
    numeric_fields: tuple[str, ...] = ()
    # app.ABSORBED_KINDS.
    absorbs: tuple[str, ...] = ()
    # curation.INTEREST: an editorial weight, not a probability or an aircraft operating limit.
    interest: int | None = None
    # selection.FIXED_SCORES: (score, reason).
    fixed_score: tuple[int, str | None] | None = None
    # story_facts.RATED_LIMIT_KINDS: the story may carry the type's certified-limit facts, the
    # airspeed limits ("speed") or the ceiling ("altitude").
    limit_dimension: str | None = None
    # story_facts.FACT_NEIGHBOR_FIELDS; public_events.NEIGHBOR_FIELDS is its outlier family.
    neighbor_field: str | None = None
    # stories.KIND_READINGS: the group of extra readings the kind's card shows.
    reading: str | None = None

    @property
    def story(self):
        """stories.STORY_KINDS: a kind the public story policy covers, every one but the context kinds."""
        return self.origin != "context"

    @property
    def family(self):
        """The family by name prefix: "squawk", "outlier", or None."""
        if self.name.startswith(SQUAWK_PREFIXES):
            return "squawk"
        return "outlier" if self.name.startswith("outlier_") else None


PROPERTIES = frozenset({field.name for field in fields(Kind)} - {"name"} | {"story", "family"})
# The transponder code of each named status that has one.
_STATUS_CODE = {meaning: code for code, meaning in STATUS_MEANINGS.items()}


def _status_kind(prefix, status, **props):
    """A transponder-status kind; a live status other than a priority declaration is an exception.

    Its slug names its transponder code unless `slug` is given.
    """
    code = _STATUS_CODE.get(status, status)
    props.setdefault("slug", (f"squawk-{code}", code))
    return Kind(
        prefix + status,
        origin="special",
        section="transponder-reports",
        site_topic="squawk-codes",
        status_exception=prefix == LIVE_SQUAWK_PREFIX and status not in PRIORITY_STATUSES,
        **props,
    )


# Declared in curation.INTEREST's order, with the kinds it does not rate placed beside their
# neighbours; the derived dicts that iterate the registry keep their recorded order this way.
KINDS = {
    entry.name: entry
    for entry in (
        Kind(
            "rapid_descent",
            origin="episode",
            journal_category="performance",
            section="performance",
            site_topic="rapid-descents",
            slug=("rapid-descent", "descent"),
            rate_reading=True,
            landing_shaped=True,
            interest=40,
        ),
        Kind(
            "rapid_climb",
            origin="episode",
            journal_category="performance",
            section="performance",
            site_topic="speed-and-altitude",
            slug=("rapid-climb", "climb"),
            context_only=True,
            rate_reading=True,
            landing_shaped=True,
            interest=34,
        ),
        Kind(
            "descending_spiral",
            origin="episode",
            pattern=True,
            section="flight-patterns",
            site_topic="flight-shapes",
            slug=("descending-spiral", "spiral"),
            circuit_shape=True,
            best_window=True,
            durative=True,
            interest=42,
        ),
        Kind(
            "possible_go_around",
            origin="episode",
            pattern=True,
            section="flight-patterns",
            site_topic="flight-shapes",
            slug=("go-around", "go-around"),
            landing_shaped=True,
            interest=34,
        ),
        Kind(
            "departure_return",
            origin="episode",
            pattern=True,
            section="flight-patterns",
            site_topic="flight-shapes",
            slug=("departure-return", "return"),
            landing_shaped=True,
            interest=36,
        ),
        Kind(
            "altitude_oscillation",
            origin="episode",
            pattern=True,
            section="flight-patterns",
            site_topic="flight-shapes",
            slug=("altitude-oscillation", "oscillation"),
            context_only=True,
            best_window=True,
            durative=True,
            interest=34,
        ),
        Kind(
            "parallel_passes",
            origin="episode",
            pattern=True,
            section="flight-patterns",
            site_topic="survey-flights",
            slug=("parallel-passes", "passes"),
            context_only=True,
            best_window=True,
            durative=True,
            interest=36,
        ),
        Kind(
            "repeated_passes",
            origin="story",
            pattern=True,
            section="flight-patterns",
            site_topic="flight-shapes",
            slug=("repeated-passes", "passes"),
            context_only=True,
            durative=True,
            interest=32,
        ),
        Kind(
            "prolonged_holding",
            origin="episode",
            pattern=True,
            section="flight-patterns",
            site_topic="holding-patterns",
            slug=("holding", "holding"),
            context_only=True,
            circuit_shape=True,
            best_window=True,
            orbit=True,
            durative=True,
            landing_shaped=True,
            interest=30,
        ),
        Kind(
            "hovering",
            origin="episode",
            pattern=True,
            section="flight-patterns",
            site_topic="hovering",
            slug=("hovering", "hover"),
            best_window=True,
            durative=True,
            interest=28,
        ),
        Kind(
            "circling",
            origin="story",
            pattern=True,
            section="flight-patterns",
            site_topic="flight-shapes",
            slug=("circling", "circling"),
            context_only=True,
            circuit_shape=True,
            orbit=True,
            durative=True,
            absorbs=("course_reversal", "loitering"),
            interest=20,
        ),
        Kind(
            "loitering",
            origin="story",
            pattern=True,
            section="flight-patterns",
            site_topic="flight-shapes",
            slug=("loitering", "loiter"),
            context_only=True,
            circuit_shape=True,
            durative=True,
            interest=16,
        ),
        Kind(
            "course_reversal",
            origin="episode",
            pattern=True,
            section="flight-patterns",
            site_topic="flight-shapes",
            slug=("course-reversal", "reversal"),
            context_only=True,
            best_window=True,
            landing_shaped=True,
            interest=8,
        ),
        Kind(
            "confined_activity",
            origin="story",
            pattern=True,
            section="flight-patterns",
            site_topic="flight-shapes",
            slug=("confined-flight", "confined"),
            context_only=True,
            circuit_shape=True,
            durative=True,
            interest=4,
        ),
        Kind(
            "watch_type",
            origin="story",
            section="unusual-aircraft",
            site_topic="notable-aircraft",
            slug_subject=True,
            identity=True,
            identity_led=True,
            daily_row=True,
            type_dependent=True,
            database_basis=True,
            interest=32,
        ),
        Kind(
            "watchlist_match",
            origin="story",
            section="unusual-aircraft",
            site_topic="watchlisted-aircraft",
            slug_subject=True,
            identity=True,
            daily_row=True,
            interest=34,
        ),
        Kind(
            "vintage",
            origin="story",
            section="unusual-aircraft",
            site_topic="vintage-aircraft",
            slug_subject=True,
            identity=True,
            identity_led=True,
            daily_row=True,
            type_dependent=True,
            database_basis=True,
            # Plus age and local type rarity (curation.vintage_interest()).
            interest=18,
        ),
        Kind(
            "rare_here",
            origin="story",
            section="unusual-aircraft",
            site_topic="notable-aircraft",
            slug_subject=True,
            identity=True,
            identity_led=True,
            daily_row=True,
            type_dependent=True,
            database_basis=True,
            interest=30,
        ),
        Kind(
            "military",
            origin="story",
            section="unusual-aircraft",
            site_topic="notable-aircraft",
            slug_subject=True,
            identity=True,
            identity_led=True,
            daily_row=True,
            database_basis=True,
            interest=6,
            fixed_score=(55, "Military classification alone is a routine identity tag"),
        ),
        Kind(
            "database_watch",
            origin="story",
            section="unusual-aircraft",
            site_topic="notable-aircraft",
            slug_subject=True,
            identity=True,
            daily_row=True,
            database_basis=True,
            interest=6,
            fixed_score=(45, None),
        ),
        Kind(
            "unusual_emitter",
            origin="story",
            section="data-quality",
            site_topic="ads-b-glitches",
            slug=("emitter-category", "emitter"),
            identity=True,
            daily_row=True,
            emitter=True,
            database_basis=True,
            interest=18,
            fixed_score=(65, None),
        ),
        Kind(
            "fast",
            origin="story",
            section="performance",
            site_topic="speed-and-altitude",
            slug=("ground-speed", "speed"),
            mlat_estimate=True,
            numeric=True,
            ground_speed_reading=True,
            speed_prose=True,
            measured_field="gs",
            numeric_fields=("gs",),
            interest=28,
            neighbor_field="gs",
        ),
        Kind(
            "high",
            origin="story",
            section="performance",
            site_topic="speed-and-altitude",
            slug=("high-altitude", "altitude"),
            balloon_fold=True,
            wide_spread=True,
            numeric=True,
            numeric_fields=("alt_baro",),
            interest=26,
            limit_dimension="altitude",
            neighbor_field="alt_baro",
        ),
        Kind(
            "vertical",
            origin="story",
            section="performance",
            site_topic="rapid-descents",
            slug=("vertical-rate", "rate"),
            rate_reading=True,
            signed=True,
            numeric=True,
            landing_shaped=True,
            measured_field="baro_rate",
            numeric_fields=("baro_rate",),
            interest=30,
            fixed_score=(75, None),
        ),
        Kind(
            "wind_boost",
            origin="story",
            section="performance",
            site_topic="speed-and-altitude",
            slug=("speed-difference", "speed"),
            mlat_estimate=True,
            numeric=True,
            measured_field="gs",
            numeric_fields=("gs", "tas"),
            interest=24,
            fixed_score=(75, None),
        ),
        Kind(
            "altitude_gap",
            origin="story",
            section="data-quality",
            site_topic="ads-b-glitches",
            slug=("altitude-gap", "gap"),
            data_quality=True,
            wide_spread=True,
            numeric=True,
            numeric_fields=("alt_baro", "alt_geom"),
            interest=16,
            fixed_score=(75, None),
        ),
        Kind(
            "distant",
            origin="context",
            private_note=True,
            numeric=True,
            numeric_fields=("lat", "lon"),
            fixed_score=(30, None),
        ),
        Kind(
            "correlated_data_anomaly",
            origin="episode",
            journal_category="oddities",
            section="data-quality",
            site_topic="ads-b-glitches",
            slug=("data-anomaly", "data"),
            data_quality=True,
            interest=34,
            reading="integrity",
        ),
        Kind(
            "position_jump",
            origin="story",
            section="data-quality",
            site_topic="ads-b-glitches",
            slug=("position-jump", "jump"),
            data_quality=True,
            mlat_estimate=True,
            interest=24,
            neighbor_field="gs",
        ),
        Kind(
            "outlier_gs",
            origin="story",
            section="data-quality",
            site_topic="ads-b-glitches",
            slug=("ground-speed-spike", "speed-spike"),
            balloon_fold=True,
            mlat_estimate=True,
            speed_prose=True,
            measured_field="gs",
            limit_dimension="speed",
            neighbor_field="gs",
        ),
        Kind(
            "outlier_alt_baro",
            origin="story",
            section="data-quality",
            site_topic="ads-b-glitches",
            slug=("altitude-spike", "altitude"),
            balloon_fold=True,
            limit_dimension="altitude",
            neighbor_field="alt_baro",
        ),
        Kind(
            "outlier_baro_rate",
            origin="story",
            section="data-quality",
            site_topic="ads-b-glitches",
            slug=("vertical-rate-spike", "rate-spike"),
            rate_reading=True,
            measured_field="baro_rate",
            neighbor_field="baro_rate",
        ),
        Kind(
            "outlier_archive_alt",
            origin="story",
            section="data-quality",
            site_topic="ads-b-glitches",
            slug=("altitude-spike", "altitude"),
            balloon_fold=True,
            limit_dimension="altitude",
            neighbor_field="archive_alt",
        ),
        Kind(
            "type_speed_mismatch",
            origin="story",
            section="data-quality",
            site_topic="ads-b-glitches",
            slug=("speed-mismatch", "speed"),
            type_dependent=True,
            registry_type=True,
            norm=True,
            data_quality=True,
            mlat_estimate=True,
            speed_prose=True,
            causeless=True,
            measured_field="gs",
            numeric_fields=("gs",),
            interest=26,
            limit_dimension="speed",
            neighbor_field="gs",
        ),
        Kind(
            "type_altitude_mismatch",
            origin="story",
            section="data-quality",
            site_topic="ads-b-glitches",
            slug=("altitude-mismatch", "altitude"),
            type_dependent=True,
            registry_type=True,
            data_quality=True,
            wide_spread=True,
            causeless=True,
            numeric_fields=("alt_baro",),
            interest=26,
            limit_dimension="altitude",
            neighbor_field="alt_baro",
        ),
        Kind(
            "space_category",
            origin="story",
            section="data-quality",
            site_topic="ads-b-glitches",
            slug=("emitter-category", "emitter"),
            data_quality=True,
            emitter=True,
            database_basis=True,
            interest=20,
        ),
        Kind(
            "tcas_ra",
            origin="episode",
            journal_category="signals",
            section="transponder-reports",
            site_topic="tcas-advisories",
            slug=("tcas-advisory", "tcas"),
            pair=True,
            rate_reading=True,
            landing_shaped=True,
            interest=40,
        ),
        _status_kind(LIVE_SQUAWK_PREFIX, "7400", interest=45),
        _status_kind(LIVE_SQUAWK_PREFIX, "7500"),
        _status_kind(LIVE_SQUAWK_PREFIX, "7600"),
        _status_kind(LIVE_SQUAWK_PREFIX, "7700"),
        _status_kind(LIVE_SQUAWK_PREFIX, "downed", slug=("downed-status", "downed")),
        _status_kind(LIVE_SQUAWK_PREFIX, "general"),
        _status_kind(LIVE_SQUAWK_PREFIX, "lifeguard", slug=("lifeguard-status", "lifeguard")),
        _status_kind(LIVE_SQUAWK_PREFIX, "minfuel", slug=("minimum-fuel-status", "fuel-status")),
        _status_kind(LIVE_SQUAWK_PREFIX, "nordo"),
        _status_kind(LIVE_SQUAWK_PREFIX, "unlawful"),
        _status_kind(ARCHIVE_SQUAWK_PREFIX, "7400"),
        _status_kind(ARCHIVE_SQUAWK_PREFIX, "7500"),
        _status_kind(ARCHIVE_SQUAWK_PREFIX, "7600"),
        _status_kind(ARCHIVE_SQUAWK_PREFIX, "7700"),
        Kind(
            "special_use_code",
            origin="episode",
            journal_category="signals",
            section="transponder-reports",
            site_topic="squawk-codes",
            slug=("special-code", "code"),
            interest=30,
        ),
        Kind(
            "stratospheric_balloon",
            origin="episode",
            journal_category="airframes",
            section="unusual-aircraft",
            site_topic="high-altitude-balloons",
            slug=("high-altitude-balloon", "balloon"),
            callsign_first=True,
            freshness_robust=True,
            interest=40,
            neighbor_field="gs",
        ),
        Kind(
            "formation_flight",
            origin="episode",
            pattern=True,
            section="flight-patterns",
            site_topic="formation-flights",
            slug=("formation", "formation"),
            pair=True,
            freshness_robust=True,
            interest=36,
        ),
        Kind(
            "holding_stack",
            origin="episode",
            pattern=True,
            section="flight-patterns",
            site_topic="holding-patterns",
            slug=("circling-group", "circling"),
            freshness_robust=True,
            landing_shaped=True,
            interest=30,
        ),
        Kind(
            "gnss_integrity_loss",
            origin="episode",
            journal_category="oddities",
            section="data-quality",
            site_topic="gps-integrity",
            slug=("gps-integrity", "gps"),
            context_only=True,
            data_quality=True,
            detail=True,
            interest=10,
            reading="integrity",
        ),
        Kind(
            "gnss_integrity_cluster",
            origin="episode",
            journal_category="oddities",
            section="data-quality",
            site_topic="gps-integrity",
            slug=("gps-integrity", "gps"),
            data_quality=True,
            interest=32,
            reading="integrity",
        ),
        Kind(
            "survey_grid",
            origin="episode",
            pattern=True,
            section="flight-patterns",
            site_topic="survey-flights",
            slug=("survey-lines", "lines"),
            best_window=True,
            freshness_robust=True,
            absorbs=("course_reversal", "parallel_passes"),
            interest=32,
        ),
        Kind(
            "thermal_soaring",
            origin="episode",
            pattern=True,
            section="flight-patterns",
            site_topic="soaring",
            slug=("soaring", "soaring"),
            best_window=True,
            interest=22,
        ),
        Kind(
            "selected_altitude_overshoot",
            origin="episode",
            journal_category="performance",
            section="performance",
            site_topic="speed-and-altitude",
            slug=("selected-altitude", "altitude"),
            detail=True,
            interest=0,
            reading="selected_altitude",
        ),
        Kind(
            "extreme_turn_rate",
            origin="episode",
            pattern=True,
            section="flight-patterns",
            site_topic="flight-shapes",
            slug=("tight-turn", "turn"),
            best_window=True,
            interest=0,
        ),
        Kind(
            "identity_conflict",
            origin="story",
            section="data-quality",
            site_topic="ads-b-glitches",
            slug=("identity-conflict", "identity"),
            daily_row=True,
            series=True,
            data_quality=True,
            database_basis=True,
            interest=24,
            fixed_score=(
                78,
                "Deterministic identity checks disagree; this is a data finding, not an identity claim",
            ),
        ),
        Kind(
            "first_type_here",
            origin="story",
            section="unusual-aircraft",
            site_topic="first-sightings",
            slug=("first-sighting", "first"),
            identity=True,
            identity_led=True,
            daily_row=True,
            first_sighting=True,
            cross_day=True,
            type_dependent=True,
            novelty=True,
            database_basis=True,
            interest=34,
        ),
        Kind(
            "first_country_here",
            origin="story",
            section="unusual-aircraft",
            site_topic="first-sightings",
            slug=("first-sighting", "first"),
            identity=True,
            identity_led=True,
            daily_row=True,
            first_sighting=True,
            cross_day=True,
            novelty=True,
            database_basis=True,
            interest=26,
        ),
        Kind(
            "notable_variant",
            origin="story",
            section="unusual-aircraft",
            site_topic="notable-aircraft",
            slug_subject=True,
            identity=True,
            identity_led=True,
            daily_row=True,
            type_dependent=True,
            novelty=True,
            database_basis=True,
            interest=34,
        ),
        Kind(
            "emitter_type_mismatch",
            origin="story",
            section="data-quality",
            site_topic="ads-b-glitches",
            slug=("emitter-mismatch", "emitter"),
            daily_row=True,
            context_only=True,
            data_quality=True,
            emitter=True,
            database_basis=True,
            interest=8,
        ),
        Kind("vfr_code_above_fl180", origin="context", context_only=True, interest=8),
        Kind(
            "seasonal_speed_record",
            origin="story",
            section="performance",
            site_topic="speed-and-altitude",
            slug=("speed-record", "speed"),
            identity_led=True,
            cross_day=True,
            type_dependent=True,
            record=True,
            norm=True,
            series=True,
            mlat_estimate=True,
            ground_speed_reading=True,
            speed_prose=True,
            # A tailwind usually explains a record, so it ranks below most stories.
            interest=20,
        ),
        Kind(
            "above_rated_altitude",
            origin="story",
            section="performance",
            site_topic="speed-and-altitude",
            slug=("above-certified-ceiling", "ceiling"),
            identity_led=True,
            type_dependent=True,
            series=True,
            wide_spread=True,
            causeless=True,
            rated="altitude",
            rated_field="alt_baro",
            numeric_fields=("alt_baro",),
            # A sustained reading past a certified limit is rarer than a tailwind record.
            interest=26,
        ),
        Kind(
            "above_rated_speed",
            origin="story",
            section="performance",
            site_topic="speed-and-altitude",
            slug=("above-maximum-speed", "max-speed"),
            identity_led=True,
            type_dependent=True,
            series=True,
            mlat_estimate=True,
            causeless=True,
            rated="speed",
            rated_field="airspeed",
            measured_field="gs",
            # Every field the airspeed estimate reads: ground speed and track against the wind,
            # pressure altitude for the conversion, and a reported true airspeed when the
            # aircraft sends one. An indirect source for any of them flags the confirmation.
            numeric_fields=("gs", "track", "alt_baro", "tas"),
            interest=24,
        ),
    )
}


def kind(name):
    """The registered kind; KeyError when unknown."""
    return KINDS[name]


def _property(name):
    if name not in PROPERTIES:
        raise TypeError(f"no kind property {name!r}")
    return name


def where(**props):
    """Names of the kinds whose properties all equal the values given."""
    for name in props:
        _property(name)
    return frozenset(
        entry.name
        for entry in KINDS.values()
        if all(getattr(entry, name) == wanted for name, wanted in props.items())
    )


def _set(value):
    return not (value is None or value is False or value == ())


def table(prop, order=None):
    """{name: value} for the kinds whose `prop` is set (not None, False, or empty).

    The registry's order unless `order` is given; `order` must name exactly those kinds.
    """
    names = [entry.name for entry in KINDS.values() if _set(getattr(entry, _property(prop)))]
    if order is not None:
        if len(set(order)) != len(order) or set(order) != set(names):
            raise ValueError(f"the order given for {prop!r} does not name exactly its kinds")
        names = order
    return {name: getattr(KINDS[name], prop) for name in names}
