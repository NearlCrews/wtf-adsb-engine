"""Explainable episodes shared by live and archive analysis.

Thresholds are spotting heuristics. The evidence describes reports and geometry,
never a safety determination, a landing, a mission or a cause of signal trouble.
"""

import bisect
import math
import re
import statistics
from collections import defaultdict
from functools import lru_cache
from itertools import pairwise, product
from typing import NamedTuple

from . import airport_context, kinds, reference
from .kinds import PRIORITY_STATUSES as PRIORITY_STATUSES
from .kinds import STATUS_CODES as STATUS_CODES
from .kinds import STATUS_MEANINGS as STATUS_MEANINGS
from .kinds import STATUS_NAMES as STATUS_NAMES
from .rules import (
    Finding,
    distance_nm,
    movement_patterns,
    number,
    orbit_verdict,
    rotorcraft,
)
from .telemetry import (
    DIRECT,
    MODE_S_SOURCES,
    acas_report,
    advisory_sense,
    altitude_series,
    positions,
    quality,
    recent,
    source_for,
    spread_series,
)

VERSION = "maneuvers-v4"
HIGH_RATE_FPM = 7000


class Rule(NamedTuple):
    """A vertical-rate rule: from ``floor_ft`` of starting pressure altitude, a run of
    consecutive reports at ``threshold_fpm`` or faster, lasting ``seconds`` over at
    least ``reports`` reports."""

    floor_ft: float
    threshold_fpm: float
    seconds: float
    reports: int


# Rapid-descent rules by starting pressure altitude, highest floor first. Ordinary
# descents from 10,000 ft or higher reach higher rates, so they need a faster,
# longer run.
DESCENT_BANDS = (Rule(10000, 6000, 60, 6), Rule(0, 4500, 30, 4))
# Climbs keep one rule.
CLIMB_RULE = Rule(0, 4500, 30, 4)
# The exceptional descent window (curation.exceptional): it also confirms a
# descent whose altitude band asks for more, so no band demotes such a descent.
HIGH_RATE_RULE = Rule(0, HIGH_RATE_FPM, 30, 4)
# Reports at or beyond the lowest rule's rate open and hold a vertical episode;
# each finding then applies its own rule.
EPISODE_FPM = min(rule.threshold_fpm for rule in (*DESCENT_BANDS, CLIMB_RULE))
EXTREME_RATE_FPM = 12000
RATE_CONFLICT_FPM = 1500
# Transponder codes stay one episode across report gaps of up to a minute.
STATUS_GAP = 60
KINDS = set(kinds.where(origin="episode"))
# Journal categories, in their recorded order; every other kind is a flight pattern.
CATEGORIES = kinds.table(
    "journal_category",
    order=(
        "rapid_descent",
        "rapid_climb",
        "selected_altitude_overshoot",
        "correlated_data_anomaly",
        "gnss_integrity_loss",
        "gnss_integrity_cluster",
        "stratospheric_balloon",
        "tcas_ra",
        "special_use_code",
        "winds_aloft_day",
    ),
)
PATTERN_KINDS = KINDS - set(CATEGORIES)
METRICS = {
    "duration_seconds": ("Episode duration", "s"),
    "sample_count": ("Telemetry samples", "samples"),
    "peak_rate_fpm": ("Peak signed vertical rate", "ft/min"),
    "median_rate_fpm": ("Median rate of readings above the detection threshold", "ft/min"),
    "average_rate_fpm": ("Average rate from the episode altitude change", "ft/min"),
    "threshold_fpm": ("Detection threshold magnitude", "ft/min"),
    "qualifying_duration_seconds": ("Consecutive reports above the detection threshold", "s"),
    "qualifying_sample_count": ("Consecutive reports above the detection threshold", "samples"),
    "qualifying_seconds_required": ("Required duration of consecutive reports above the threshold", "s"),
    "qualifying_reports_required": ("Required consecutive reports above the threshold", "samples"),
    "sustained_rate_fpm": ("Slowest signed rate in the high-rate interval", "ft/min"),
    "sustained_duration_seconds": ("High-rate interval duration", "s"),
    "sustained_sample_count": ("High-rate interval reports", "samples"),
    "sustained_altitude_change_ft": ("High-rate interval altitude change", "ft"),
    "sustained_expected_altitude_change_ft": ("High-rate interval integrated altitude change", "ft"),
    "sustained_derived_rate_fpm": ("Altitude-derived rate in the high-rate interval", "ft/min"),
    "altitude_change_ft": ("Observed altitude change", "ft"),
    "expected_altitude_change_ft": ("Integrated reported altitude change", "ft"),
    "derived_rate_fpm": ("Altitude-derived vertical rate", "ft/min"),
    "altitude_excursion_ft": ("Altitude excursion", "ft"),
    "reversals": ("Altitude reversals", "reversals"),
    "span_nm": ("Track span", "NM"),
    "max_turn_rate_dps": ("Highest turn rate held for 20 seconds", "deg/s"),
    "racetrack_legs": ("Racetrack legs", "legs"),
    "axis_heading_deg": ("Racetrack axis heading", "degrees"),
    "lateral_spacing_nm": ("Spacing between the two racetrack lines", "NM"),
    "line_repeat_nm": ("Largest leg offset from its line", "NM"),
    "runway_within_nm": ("Nearest mapped runway end, up to 50 NM", "NM"),
    "turn_degrees": ("Course reversal", "degrees"),
    "closed_loops": ("Closed loops", "loops"),
    "complete_passes": ("Parallel passes", "passes"),
    "footprint_nm": ("Track footprint", "NM"),
    "aircraft_count": ("Distinct affected addresses", "addresses"),
    "start_altitude_ft": ("Episode start altitude", "ft"),
    "end_altitude_ft": ("Episode end altitude", "ft"),
    "level_off_altitude_ft": ("Level-off altitude", "ft"),
    "distinct_report_count": ("Distinct reports after collapsing repeats", "samples"),
    "min_separation_nm": ("Closest horizontal separation between the aircraft", "NM"),
    "vertical_separation_ft": ("Vertical separation at closest approach", "ft"),
    "participant_count": ("Aircraft involved", "aircraft"),
    "leg_count": ("Straight legs", "legs"),
    "leg_spacing_nm": ("Mean spacing between legs", "NM"),
    "leg_length_nm": ("Mean leg length", "NM"),
    "orbit_center_drift_nm": ("Orbit-centre drift per loop", "NM"),
    "climb_gain_ft": ("Altitude gained while circling", "ft"),
    "median_ground_speed_kt": ("Median ground speed", "kt"),
    "max_altitude_ft": ("Highest altitude", "ft"),
    "response_rate_change_fpm": ("Vertical-rate change after the advisory", "ft/min"),
}
# Non-blocking context; quality_flags stay the only blocking annotations.
CONTEXT_TAGS = frozenset(
    {
        "runway_circuit_context",
        "airport_descent_context",
        "terminal_area_context",
        "transient_code_selection",
        "status_field_disagreement",
        "ground_only_status",
        "status_from_mode_s_reply",
        "emergency_descent_profile",
        "isolated_rate_outlier",
        "refuelling_like",
        "aerotow_like",
        "ground_referenced_orbit",
        "drifting_orbit",
        "archive_field_freshness_unknown",
        "tanker_type",
        "military_flag",
        "airport_context",
        "airport_maneuver_hypothesis",
    }
)
# Archived fields have unknown freshness: a blocking flag, except for shapes the
# freshness cannot change, which carry it as context (owner decision, 2026-10-06).
# An advisory is robust only when measured on its own aircraft's saved trace.
ARCHIVE_FRESHNESS = "archive_field_freshness_unknown"
FRESHNESS_ROBUST_KINDS = kinds.where(freshness_robust=True)
# The transponder-status table (STATUS_CODES, STATUS_NAMES, STATUS_MEANINGS) and the
# priority statuses (PRIORITY_STATUSES) are defined once in kinds and re-exported here.
EMERGENCY_VALUES = {"none", "reserved", *STATUS_NAMES}
# US special-purpose codes (FAA JO 7110.66); the meaning is jurisdiction-specific.
SPECIAL_USE_CODES = {"1202", "1255", "1276", "1277", "4000", "7777"}
SENSITIVE_CODES = re.compile(r"44[0-7][0-7]")
# Four octal digits, or one to three for a shortened code (readsb's integer squawks).
OCTAL_CODE = re.compile(r"[0-7]{4}")
SHORT_OCTAL_CODE = re.compile(r"[0-7]{1,4}")
GLIDER_TYPES = {"GLID", "GLIM", "PISI"}
# Aerobatic designators. With the pinned type table, only the codes it lists count.
AEROBATIC_CANDIDATES = (
    "E300",
    "E330",
    "PTS1",
    "PTS2",
    "PTSS",
    "EDGE",
    "SU26",
    "SU29",
    "SU31",
    "YK52",
    "YK55",
    "CP10",
    "CP21",
    "CP23",
    "MX2",
    "G202",
    "LASR",
    "RV3",
    "RV4",
    "RV7",
    "RV8",
)
AEROBATIC_WINDOW_SECONDS = 600
AEROBATIC_STEP_FT = 800
AEROBATIC_MIN_REVERSALS = 5
AEROBATIC_MAX_SPAN_NM = 5.0
AEROBATIC_MIN_EXCURSION_FT = 1500
AEROBATIC_TURN_RATE_DPS = 6.0
AEROBATIC_TURN_SECONDS = 20
# Tanker designators. Airframes shared with transports (A330, 767, C-130J) count
# only as a curated tanker variant by database description (KC-46A, KC-130J, MRTT).
TANKER_TYPES = frozenset({"K35R", "K35E", "KC10", "KC46"})
# "Air tanker" names firefighting aircraft, so the word alone is not a tanker variant.
TANKER_DESCRIPTION = re.compile(r"\bKC-?[0-9]{1,3}[A-Z]?\b|\bMRTT\b", re.IGNORECASE)
BALLOON_CALLSIGNS = ("HBAL",)
BALLOON_FLOOR_FT = 50000
# Classes and emitter categories that contradict a balloon at that height.
BALLOON_COMPATIBLE_CLASSES = frozenset({"balloon", "airship", "uav"})
BALLOON_CONTRARY_CATEGORIES = frozenset({"A1", "A2", "A3", "A4", "A5", "A6", "A7", "B1", "B3", "B4"})
ALTITUDE_FIELDS = ("alt_baro", "alt_geom")
# An archived altitude without a known reference may still measure a range.
ARCHIVE_ALTITUDE_FIELDS = ("alt_baro", "alt_geom", "archive_alt")
MAX_PARTICIPANTS = 12
# A saved snapshot keeps the aircraft's identity and the readings at its timestamp.
SNAPSHOT_IDENTITY_FIELDS = ("hex", "r", "t", "desc", "year", "dbFlags")
SNAPSHOT_READING_FIELDS = (
    "lat",
    "lon",
    "gs",
    "alt_baro",
    "alt_geom",
    "archive_alt",
    "baro_rate",
    "geom_rate",
    "squawk",
    "emergency",
    "flight",
    "category",
    "mlat",
    "tisb",
    "nic",
    "nac_p",
    "label_at",
)
# Long-shape and multi-aircraft constants.
STRAIGHT_LEG_TOLERANCE = 10
STRAIGHT_LEG_MINIMUM_NM = 1.0
RACETRACK_MIN_LEGS = 4
RACETRACK_MIN_LEG_NM = 10.0
RACETRACK_AXIS_TOLERANCE = 10.0
RACETRACK_SPACING_NM = (2.0, 15.0)
RACETRACK_LINE_REPEAT_NM = 1.0
RACETRACK_MIN_SECONDS = 2400
RACETRACK_AIRPORT_NM = 10.0
RACETRACK_RUNWAY_CAP_NM = 50.0
GNSS_WINDOW_SECONDS = 600
GNSS_RADIUS_NM = 60
STACK_WINDOW_SECONDS = 1800
STACK_RADIUS_NM = 10
STACK_FLOOR_FT = 5000
RATE_FIELDS = ("baro_rate", "geom_rate")
MATCHING = {
    "baro_rate": "alt_baro",
    "geom_rate": "alt_geom",
    "derived_baro_rate": "alt_baro",
    "derived_geom_rate": "alt_geom",
}


def bounded_samples(points):
    count = min(96, len(points))
    return [points[round(i * (len(points) - 1) / max(1, count - 1))] for i in range(count)]


def merge_context(before, after):
    by_time = {point["ts"]: point for point in [*before, *after]}
    return bounded_samples([by_time[stamp] for stamp in sorted(by_time)])


def build(
    kind, points, title, value, unit, *, fields=(), confirmed=True, flags=(), tags=(), archive=None, **metrics
):
    """One episode finding. ``archive`` names the archive format ("traces" or
    "heatmap") of points read from saved archives, or None for live reports; see
    archived()."""
    issues = sorted(set(quality(points, fields)) | set(flags))
    summary = {
        "version": VERSION,
        "kind": kind,
        "started_at": points[0]["ts"],
        "ended_at": points[-1]["ts"],
        "evaluated_at": points[-1]["ts"],
        "duration_seconds": points[-1]["ts"] - points[0]["ts"],
        "sample_count": len(points),
        "confirmed": confirmed,
        "quality_review_required": bool(issues) or not confirmed,
        "quality_flags": issues,
        "context_tags": sorted({tag for tag in tags if tag in CONTEXT_TAGS}),
        "state": "active",
        "field_freshness_known": False,
        **metrics,
    }
    category = "signals" if kind.startswith("special_squawk_") else CATEGORIES.get(kind, "patterns")
    finding = Finding(
        kind,
        category,
        title,
        value,
        unit,
        why(title, value, unit, summary["duration_seconds"]),
        "This is a telemetry episode for review. Reported fields can have different ages and correlated errors. The observations do not establish distress, a mission, a completed landing, or the cause of a maneuver.",
        78,
        _confidence(summary),
        {"maneuver": summary, "maneuver_samples": bounded_samples(points)},
    )
    return archived(finding, archive) if archive else finding


def why(title, value, unit, seconds):
    """The measurement sentence an episode finding saves as its "why"."""
    measured = f"{title}: {value:,.1f} {unit} across" if value is not None else f"{title} across"
    return f"{measured} {seconds:.0f} seconds of retained reports."


def _confidence(info):
    return (
        "Episode evidence; review required"
        if info["quality_review_required"]
        else "Coherent episode; individual field ages unknown"
    )


def archived(finding, archive):
    """Apply an archive's unknown field freshness to an episode finding.

    It is a blocking flag, or a context tag for kinds whose shape the freshness
    cannot change (FRESHNESS_ROBUST_KINDS, and an advisory measured on its own
    aircraft's saved trace). ``archive`` is the archive format.
    """
    info = finding.evidence["maneuver"]
    flags, tags = set(info["quality_flags"]) - {ARCHIVE_FRESHNESS}, set(info["context_tags"])
    if finding.kind in FRESHNESS_ROBUST_KINDS or (finding.kind == "tcas_ra" and archive == "traces"):
        tags.add(ARCHIVE_FRESHNESS)
    else:
        flags.add(ARCHIVE_FRESHNESS)
    info.update(
        quality_flags=sorted(flags),
        context_tags=sorted(tags),
        quality_review_required=bool(flags) or info.get("confirmed") is not True,
    )
    finding.confidence = _confidence(info)
    return finding


def _tag(finding, *tags):
    info = finding.evidence["maneuver"]
    info["context_tags"] = sorted(set(info["context_tags"]) | {tag for tag in tags if tag in CONTEXT_TAGS})


def _flag(finding, *flags):
    info = finding.evidence["maneuver"]
    info["quality_flags"] = sorted(set(info["quality_flags"]) | set(flags))
    info["quality_review_required"] = True
    finding.confidence = _confidence(info)


def wrap_angle(degrees):
    return (degrees + 180) % 360 - 180


def altitude_field(point, fields=ALTITUDE_FIELDS):
    """(field, value) of the first reported altitude among ``fields``, else (None, None)."""
    for field in fields:
        if (value := number(point.get(field))) is not None:
            return field, value
    return None, None


def altitude(point, fields=ALTITUDE_FIELDS):
    """The first reported altitude among ``fields`` (pressure, then geometric), or None."""
    return altitude_field(point, fields)[1]


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def _overlaps(start, end, window):
    return window is not None and start <= window["ended_at"] and end >= window["started_at"]


# Aircraft classes. Pinned reference data decides when present; the type lists
# and emitter category are the fallback. rules.rotorcraft is the rotorcraft rule.


def glider(aircraft, points=()):
    known = airport_context.type_class(aircraft.get("t"))
    return (
        aircraft.get("category") == "B1"
        or known == "glider"
        or (known is None and aircraft.get("t") in GLIDER_TYPES)
        or any(squawk_code(p) == "1202" for p in points)
    )


def tanker(aircraft):
    """A tanker designator, or a tanker variant by its database description."""
    description = aircraft.get("desc")
    return aircraft.get("t") in TANKER_TYPES or (
        isinstance(description, str) and TANKER_DESCRIPTION.search(description) is not None
    )


# Airport context. Runway proximity is a hint; elevation is absent, so no
# altitude here is height above the ground.


def _near_runway(points, radius):
    sample = points[:: max(1, len(points) // 48)]
    return any(airport_context.nearby(p, radius) for p in sample)


def _nearest_runway_nm(point, radius=RACETRACK_RUNWAY_CAP_NM):
    """NM from ``point`` ([lat, lon]) to the nearest pinned runway end, at most ``radius``.

    None when the runway table is unavailable, or when ``point`` lies outside its
    coverage or within ``radius`` of the coverage edge.
    """
    if not airport_context.available():
        return None
    lat, lon = point
    # Near or beyond the table's edge, runways it does not hold could be closer.
    west, south, east, north = airport_context.coverage()
    scale = longitude_scale(lat)
    if min((lat - south) * 60, (north - lat) * 60, (lon - west) * scale, (east - lon) * scale) < radius:
        return None
    nearest = radius
    for runway in airport_context.runways_near(lat, lon, radius):
        for end_lon, end_lat in runway["ends"]:
            nearest = min(nearest, distance_nm((lat, lon), (end_lat, end_lon)))
    return nearest


def _centre(located):
    """The mean of positions, as a position."""
    coords = xy(located)
    origin = located[0]
    x, y = statistics.fmean(c[0] for c in coords), statistics.fmean(c[1] for c in coords)
    return {"lat": origin["lat"] + y / 60, "lon": origin["lon"] + x / longitude_scale(origin["lat"])}


def _airport_descent(points):
    """An orbit or descent centred within 3 NM of a pinned runway, or ending on an aligned approach."""
    located = positions(points)
    if not located:
        return False
    centre = _centre(located)
    return bool(airport_context.nearby(centre, 3)) or bool(airport_context.aligned_approach(located[-5:]))


def _terminal(points, approach=()):
    """Within 5 NM of a pinned runway at or below 5,000 ft, or leaving on an aligned approach."""
    low = [p for p in points if (height := altitude(p)) is not None and height <= 5000]
    if low and _near_runway(low, 5):
        return True
    return bool(approach) and bool(airport_context.aligned_approach(list(approach)[-5:]))


# Vertical-rate episodes.


def altitude_agrees(change, expected, direction):
    return (
        change is not None
        and expected is not None
        and change * direction >= 250
        and expected * direction > 0
        and abs(change - expected) <= max(500, abs(expected) * 0.3)
    )


def _rate_choice(point, preferred=None, previous=None, sticky=False):
    """Choose the rate by provenance: a direct report over MLAT or TIS-B values.

    A running episode keeps its field while it stays available (``sticky``);
    two direct rates that disagree are flagged rather than switched between.
    """
    values = {field: number(point.get(field)) for field in RATE_FIELDS}
    present = [field for field in RATE_FIELDS if values[field] is not None]
    direct = [field for field in present if source_for(point, field) in DIRECT]
    pool = direct or present
    if not pool:
        return None, None, False
    if len(pool) == 2 and previous is not None and not (sticky and preferred in pool):
        # readsb keeps a rate after its source switches; prefer the one still updating.
        changed = [
            field
            for field in pool
            if (values[field], number(point.get(MATCHING[field])))
            != (number(previous.get(field)), number(previous.get(MATCHING[field])))
        ]
        if len(changed) == 1:
            preferred = changed[0]
    field = preferred if preferred in pool else pool[0]
    conflict = len(direct) == 2 and abs(values["baro_rate"] - values["geom_rate"]) > RATE_CONFLICT_FPM
    return field, values[field], conflict


def _rate(point):
    """The vertical rate chosen by provenance (see _rate_choice), or None."""
    return _rate_choice(point)[1]


def _repeat(reports, point, field, rate):
    """A carried-forward rate with the same matching altitude is not a new report."""
    last = next(((p, r) for p, r, repeat in reversed(reports) if not repeat), None)
    if field not in RATE_FIELDS or last is None:
        return False
    previous, previous_rate = last
    height = number(point.get(MATCHING[field]))
    return (
        rate == previous_rate
        and height is not None
        and height == number(previous.get(MATCHING[field]))
        and abs(rate) * (point["ts"] - previous["ts"]) / 60 >= 100
    )


def _corroborated(pairs):
    """Whether each rate has an adjacent distinct report within 25% of it."""
    result = []
    for index, (point, rate) in enumerate(pairs):
        result.append(
            any(
                0 <= other < len(pairs)
                and abs(pairs[other][0]["ts"] - point["ts"]) <= 45
                and rate * pairs[other][1] > 0
                and abs(rate - pairs[other][1]) <= 0.25 * max(abs(rate), abs(pairs[other][1]))
                for other in (index - 1, index + 1)
            )
        )
    return result


def descent_rule(start_altitude_ft):
    """The DESCENT_BANDS rule for a descent starting at this pressure altitude."""
    for band in DESCENT_BANDS:
        if (start_altitude_ft or 0) >= band.floor_ft:
            return band
    return DESCENT_BANDS[-1]


def _vertical_rule(pairs, direction, start_altitude_ft):
    """(rule, run) one episode is judged by, with the rule's chosen run (_rate_run).

    A climb takes CLIMB_RULE. A descent takes the band of its start altitude;
    when the band's run does not hold over ``pairs`` (the usable (point, rate)
    reports) but HIGH_RATE_RULE's does, it takes HIGH_RATE_RULE instead.
    """
    rules = (CLIMB_RULE,) if direction > 0 else (descent_rule(start_altitude_ft), HIGH_RATE_RULE)
    runs = [_rate_run(pairs, direction, rule) for rule in rules]
    return next(
        ((rule, run) for rule, run in zip(rules, runs, strict=True) if _holds(run, rule)), (rules[0], runs[0])
    )


def _holds(run, rule):
    return len(run) >= rule.reports and run[-1][0]["ts"] - run[0][0]["ts"] >= rule.seconds


def _rate_run(pairs, direction, rule):
    """Choose a consecutive run at the rule's rate before evidence is downsampled to 96 reports."""
    threshold = rule.threshold_fpm
    runs, run = [], []
    for point, rate in pairs:
        if run and (rate * direction < threshold or not 0 < point["ts"] - run[-1][0]["ts"] <= 45):
            runs.append(run)
            run = []
        if rate * direction >= threshold:
            run.append((point, rate))
    if run:
        runs.append(run)

    def order(run):
        return _holds(run, rule), run[-1][0]["ts"] - run[0][0]["ts"], len(run)

    return max(runs, key=order, default=[])


def _vertical_change(pairs, reference):
    altitudes = [number(p.get(reference)) for p, _ in pairs]
    change = altitudes[-1] - altitudes[0] if all(v is not None for v in altitudes) else None
    expected = sum((a + b) / 2 * (q["ts"] - p["ts"]) / 60 for (p, a), (q, b) in pairwise(pairs))
    return change, expected


def _emergency_profile(points, observed, reference, after):
    """A descent from 25,000 ft or higher, then 60 s within ±500 ft between 8,000 and 15,000 ft.

    The hold starts after the high-rate part, and reported rates in it stay
    within 500 ft/min, so a steady slower descent is not a level-off.
    """
    start = number(observed[0].get(reference))
    if start is None or start < 25000:
        return None
    found, run = None, []
    for p in points:
        if p["ts"] <= after:
            continue
        altitude, rate = number(p.get(reference)), _rate(p)
        if altitude is None or not 8000 <= altitude <= 15000 or (rate is not None and abs(rate) > 500):
            run = []
            continue
        run.append((p["ts"], altitude))
        while max(a for _, a in run) - min(a for _, a in run) > 1000:
            run.pop(0)
        if run[-1][0] - run[0][0] >= 60:
            found = (round(statistics.median(a for _, a in run)), run[-1][0])
    return found


def _vertical_finding(episode, points, state="active", reason=None, at=None):
    """The rapid climb or descent finding for one rate episode, or None.

    ``episode["reports"]`` holds (point, rate, repeat) for each report in the
    episode. ``at`` is the report that ended it, so the saved state updates.
    The episode's own rule (_vertical_rule) applies, and the finding records
    its threshold and qualifying window.
    """
    reports = episode["reports"]
    observed = [p for p, _, _ in reports]
    if len(observed) < 2 or observed[-1]["ts"] - observed[0]["ts"] < 10:
        return None
    direction, field = episode["direction"], episode["field"]
    reference = MATCHING[field]
    distinct = [(p, r) for p, r, repeat in reports if not repeat]
    # A lone report beyond 12,000 ft/min is kept as evidence, not as a measurement.
    outliers, usable, trusted = [], [], []
    for pair, ok in zip(distinct, _corroborated(distinct), strict=True):
        if ok:
            trusted.append(pair)
        (usable if ok or abs(pair[1]) <= EXTREME_RATE_FPM else outliers).append(pair)
    rule, run = _vertical_rule(usable, direction, altitude(observed[0]))
    threshold = rule.threshold_fpm
    qualifying = [pair for pair in usable if pair[1] * direction >= threshold]
    if len(qualifying) < 2 or qualifying[-1][0]["ts"] - qualifying[0][0]["ts"] < 10:
        return None
    # Peak and high-rate metrics use only rates confirmed by a neighbouring report.
    supported = [pair for pair in trusted if pair[1] * direction >= threshold]
    peak_point, peak = max(supported or qualifying, key=lambda pair: abs(pair[1]))
    run_duration = run[-1][0]["ts"] - run[0][0]["ts"]
    confirmed = _holds(run, rule)
    if not confirmed and abs(peak) < HIGH_RATE_FPM:
        return None
    flags, tags = [], []
    change, expected = _vertical_change(usable, reference)
    if change is None:
        flags.append("matching_altitude_unavailable")
    elif not altitude_agrees(change, expected, direction):
        flags.append("rate_altitude_disagreement")
    if abs(peak) > EXTREME_RATE_FPM:
        flags.append("extreme_rate")
    if field.startswith("derived"):
        flags.append("rate_estimated_from_altitude")
    if episode["conflict"]:
        flags.append("rate_field_conflict")
    if not supported:
        flags.append("uncorroborated_rate")
    if outliers:
        tags.append("isolated_rate_outlier")
    if direction < 0:
        # Jump-plane and approach descents near runways; computed from the
        # episode's own reports so every ending keeps it.
        if not airport_context.available():
            flags.append("airport_context_unavailable")
        elif _airport_descent(observed):
            tags.append("airport_descent_context")
    high = _rate_run(trusted, direction, HIGH_RATE_RULE)
    sustained = {}
    if high:
        high_points = [p for p, _ in high]
        high_change, high_expected = _vertical_change(high, reference)
        seconds = high_points[-1]["ts"] - high_points[0]["ts"]
        sustained = {
            "sustained_rate_fpm": min((r for _, r in high), key=abs),
            "sustained_duration_seconds": seconds,
            "sustained_sample_count": len(high),
            "sustained_altitude_change_ft": high_change,
            "sustained_expected_altitude_change_ft": round(high_expected, 1),
            "sustained_started_at": high_points[0]["ts"],
            "sustained_ended_at": high_points[-1]["ts"],
        }
        if high_change is not None and seconds > 0:
            sustained["sustained_derived_rate_fpm"] = round(high_change * 60 / seconds, 1)
    extra = {}
    after = high[-1][0]["ts"] if high else peak_point["ts"]
    profile = _emergency_profile(points, observed, reference, after) if direction < 0 else None
    if profile:
        tags.append("emergency_descent_profile")
        extra["level_off_altitude_ft"] = profile[0]
    if outliers:
        extra["rate_outliers"] = [{"ts": p["ts"], "rate_fpm": r} for p, r in outliers]
    if reason:
        extra["end_reason"] = reason
    for key, point in (("start_altitude_ft", observed[0]), ("end_altitude_ft", observed[-1])):
        if (height := number(point.get(reference))) is not None:
            extra[key] = height
    span = usable[-1][0]["ts"] - usable[0][0]["ts"]
    finding = build(
        "rapid_climb" if direction > 0 else "rapid_descent",
        observed,
        "Rapid climb reports" if peak > 0 else "Rapid descent reports",
        peak,
        "ft/min",
        fields=(reference, field),
        confirmed=confirmed,
        flags=flags,
        tags=tags,
        peak_rate_fpm=peak,
        median_rate_fpm=statistics.median(r for _, r in qualifying),
        average_rate_fpm=round(change * 60 / span, 1) if change is not None and span > 0 else None,
        qualifying_duration_seconds=run_duration,
        qualifying_sample_count=len(run),
        qualifying_started_at=run[0][0]["ts"],
        qualifying_ended_at=run[-1][0]["ts"],
        altitude_change_ft=change,
        expected_altitude_change_ft=round(expected, 1),
        altitude_reference=reference,
        rate_field=field,
        threshold_fpm=threshold,
        qualifying_seconds_required=rule.seconds,
        qualifying_reports_required=rule.reports,
        distinct_report_count=len(distinct),
        state=state,
        episode_start=observed[0]["ts"],
        **extra,
        **sustained,
    )
    info = finding.evidence["maneuver"]
    # A level-off is observed after the descent; re-save the episode with it.
    info["evaluated_at"] = max(info["evaluated_at"], profile[1] if profile else 0, at or 0)
    # Keep before/after reports separately; they do not change peak timing.
    finding.evidence["context_samples"] = bounded_samples(
        [p for p in points if observed[0]["ts"] - 60 <= p["ts"] <= observed[-1]["ts"] + 60]
    )
    info["peak_at"] = peak_point["ts"]
    finding.evidence["peak_sample"] = peak_point
    return finding


def _end_reason(point, rate, signature, previous):
    if point.get("ground"):
        return "ground"
    if rate is None:
        return "rate_lost"
    if previous and signature != previous:
        return "segment_break"
    return None


def vertical(points):
    """Rapid climb and descent findings, each judged by its episode's rule
    (descent_rule, CLIMB_RULE). Reports at EPISODE_FPM or faster open an episode."""
    results, active, previous, preferred, prior = [], None, None, None, None

    def finish(episode, state="active", reason=None, at=None):
        if episode and (finding := _vertical_finding(episode, points, state, reason, at)):
            results.append(finding)

    derivation_start = 0
    for index, p in enumerate(points):
        while points[derivation_start]["ts"] < p["ts"] - 30:
            derivation_start += 1
        field, rate, conflict = _rate_choice(
            p, active["field"] if active else preferred, prior, sticky=active is not None
        )
        prior = p
        if rate is None:
            window = points[derivation_start : index + 1]
            ref, heights = altitude_series(window)
            if len(window) >= 3 and window[-1]["ts"] - window[0]["ts"] >= 20 and heights:
                rate = (heights[-1] - heights[0]) * 60 / (window[-1]["ts"] - window[0]["ts"])
                field = "derived_baro_rate" if ref == "alt_baro" else "derived_geom_rate"
        elif field in RATE_FIELDS:
            preferred = field
        signature = (field, source_for(p, field)) if field else None
        if reason := _end_reason(p, rate, signature, previous):
            finish(active, "ended", reason, p["ts"])
            active = None
        previous = signature
        if p.get("ground") or rate is None:
            continue
        direction = 1 if rate > 0 else -1
        if active and direction != active["direction"] and abs(rate) >= EPISODE_FPM:
            finish(active, "recovered", "recovered", p["ts"])
            active = None
        if active is None:
            if abs(rate) < EPISODE_FPM:
                continue
            active = {
                "reports": [],
                "direction": direction,
                "field": field,
                "conflict": False,
                "recovery": None,
            }
        active["reports"].append((p, rate, _repeat(active["reports"], p, field, rate)))
        active["conflict"] = active["conflict"] or conflict
        if rate * active["direction"] < EPISODE_FPM * 0.65:
            if active["recovery"] is None:
                active["recovery"] = p["ts"]
            if p["ts"] - active["recovery"] >= 20:
                finish(active, "recovered", "recovered")
                active = None
        else:
            active["recovery"] = None
    finish(active)

    def horizon(finding):
        # A high-altitude descent stays open long enough to observe a level-off.
        info = finding.evidence["maneuver"]
        return 150 if finding.kind == "rapid_descent" and info.get("start_altitude_ft", 0) >= 25000 else 60

    return [f for f in results if points[-1]["ts"] - f.evidence["maneuver"]["ended_at"] <= horizon(f)]


# Transponder codes.


def octal_code(value, *, padded=True):
    """A four-digit octal transponder code, or None; "0000" is an absent-code placeholder.

    ``padded`` also accepts one to three digits as a shortened code, as in
    readsb's integer squawks. The one code parser; episode_evidence wraps it.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    return _octal(value.strip(), padded) if isinstance(value, str) else None


@lru_cache(maxsize=4096)
def _octal(text, padded):
    if not (SHORT_OCTAL_CODE if padded else OCTAL_CODE).fullmatch(text):
        return None
    code = text.zfill(4)
    return None if code == "0000" else code


def squawk_code(point):
    """A report's four-digit octal code, or None; the "0000" placeholder is no report."""
    return octal_code(point.get("squawk"))


def status_meaning(point):
    code = squawk_code(point)
    if code in STATUS_CODES:
        return STATUS_MEANINGS[code]
    status = point.get("emergency")
    return status if isinstance(status, str) and status in STATUS_NAMES else None


def _status_source(point, field):
    source = source_for(point, field)
    return "adsb" if source in DIRECT else "mode_s" if source in MODE_S_SOURCES else "indirect"


def _field_disagreement(reports):
    """The aircraft's own emergency field contradicts its 7500/7600/7700 code."""
    agree = disagree = 0
    for p in reports:
        code, status = squawk_code(p), p.get("emergency")
        if code not in STATUS_MEANINGS or code == "7400" or not isinstance(status, str):
            continue
        if status not in EMERGENCY_VALUES:
            continue
        if status == STATUS_MEANINGS[code]:
            agree += 1
        else:
            disagree += 1
    return disagree > 0 and disagree >= agree


def _code_sequence(points, end):
    runs = []
    for p in points:
        code = squawk_code(p)
        if code is not None and (not runs or runs[-1][0] != code):
            runs.append((code, p["ts"]))
    return [
        {"code": code, "seconds": round((runs[i + 1][1] if i + 1 < len(runs) else end) - start, 1)}
        for i, (code, start) in enumerate(runs)
    ]


def _status_field(point):
    return "squawk" if squawk_code(point) in STATUS_CODES else "emergency"


def _persistent(reports, meaning):
    """When each meaning first persisted: a second report at least 8 s after its first."""
    first, persisted = {}, {}
    for p in reports:
        value = meaning(p)
        first.setdefault(value, p["ts"])
        if value not in persisted and p["ts"] - first[value] >= 8:
            persisted[value] = p["ts"]
    return persisted


def _status_kind(reports):
    """Name the episode by its first persistent status, else by its first report.

    A one-poll 7700 while dialling 7600 does not name a lasting 7600 sequence.
    The name is fixed when the status persists, so a later code cannot rename
    it. Returns the kind and the persistence time, or None before that.
    """
    persisted = _persistent(reports, status_meaning)
    if persisted:
        meaning = min(persisted, key=persisted.get)
        named = [p for p in reports if p["ts"] <= persisted[meaning] and status_meaning(p) == meaning]
    else:
        meaning, named = status_meaning(reports[0]), reports[:1]
    code = next((c for p in named if (c := squawk_code(p)) in STATUS_CODES), None)
    return "special_squawk_" + (code or meaning), persisted.get(meaning)


def _special_code(point):
    code = squawk_code(point)
    return code if code in SPECIAL_USE_CODES or (code and SENSITIVE_CODES.fullmatch(code)) else None


def _code_episodes(points, key, joined):
    """Keyed reports up to 60 s apart; an explicit other code afterwards is a reversion."""
    episodes, current = [], None
    for p in points:
        value = key(p)
        if value is None:
            if current is not None and current["reverted"] is None and squawk_code(p) is not None:
                current["reverted"] = p
            continue
        if current is not None:
            gap = p["ts"] - current["reports"][-1]["ts"]
            if gap > STATUS_GAP or not joined(current["key"], value):
                if gap <= STATUS_GAP and current["reverted"] is None:
                    current["reverted"] = p
                current = None
        if current is None:
            current = {"key": value, "reports": [], "reverted": None}
            episodes.append(current)
        current["reports"].append(p)
        current["reverted"] = None
    return episodes


def _code_finding(kind, title, episode, points, field, *, confirmed, flags=(), tags=(), **metrics):
    """Shared evidence for transponder-status and special-use code episodes."""
    reports, reverted = episode["reports"], episode["reverted"]
    first = reports[0]
    distinct = len({p["ts"] for p in reports})
    duration = reports[-1]["ts"] - first["ts"]
    ground = sum(bool(p.get("ground")) for p in reports)
    tags = list(tags)
    if ground == len(reports):
        tags.append("ground_only_status")
    if reverted is not None and (duration < 60 or distinct < 4):
        tags.append("transient_code_selection")
    # The transponder itself sends the code; MLAT supplies only the position.
    classes = [_status_source(p, field(p)) for p in reports]
    if all(c == "mode_s" for c in classes):
        tags.append("status_from_mode_s_reply")
    flags = [*flags, *(["uncertain_source"] if "indirect" in classes else [])]
    # Repeated inherited archive labels cannot confirm persistence.
    confirmed = confirmed and not any(p.get("archive") for p in reports)
    extra = {}
    if (code := squawk_code(first)) is not None:
        extra["reported_squawk"] = code
    if isinstance(first.get("emergency"), str) and first["emergency"] in EMERGENCY_VALUES:
        extra["reported_status"] = first["emergency"]
    if reverted is not None:
        extra["end_reason"] = "reverted"
        if (ending := squawk_code(reverted)) is not None:
            extra["ending_code"] = ending
    finding = build(
        kind,
        reports,
        title,
        None,
        "",
        confirmed=confirmed,
        flags=flags,
        tags=tags,
        code_sequence=_code_sequence(
            [p for p in points if first["ts"] <= p["ts"] <= reports[-1]["ts"]], reports[-1]["ts"]
        ),
        distinct_report_count=distinct,
        ground_report_share=round(ground / len(reports), 3),
        episode_start=first["ts"],
        state="ended" if reverted is not None else "active",
        **extra,
        **metrics,
    )
    finding.evidence["maneuver"]["evaluated_at"] = (
        reverted["ts"] if reverted is not None else reports[-1]["ts"]
    )
    finding.evidence["context_samples"] = bounded_samples(
        [p for p in points if first["ts"] - 60 <= p["ts"] <= reports[-1]["ts"] + 60]
    )
    return finding


def _lapsed(episode, points):
    """Skip old episodes; a code that stopped without a reversion ends once, 60 s later.

    Returns None for a current episode, "skip" or "stale".
    """
    quiet = points[-1]["ts"] - episode["reports"][-1]["ts"]
    if quiet <= STATUS_GAP:
        return None
    return "skip" if episode["reverted"] is not None or quiet > 3 * STATUS_GAP else "stale"


def _ended(finding, episode):
    info = finding.evidence["maneuver"]
    info.update(state="ended", end_reason="stale", evaluated_at=episode["reports"][-1]["ts"] + STATUS_GAP)
    return finding


def signals(points):
    """One status episode per continuous status run, plus special-use code episodes.

    A sequence such as 7700 then 7600, or a code followed by the aircraft's own
    matching emergency field, is one episode with a ``code_sequence``.
    """
    findings = []

    def joined(old, new):
        return (old in PRIORITY_STATUSES) == (new in PRIORITY_STATUSES)

    for episode in _code_episodes(points, status_meaning, joined):
        reports = episode["reports"]
        if (lapsed := _lapsed(episode, points)) == "skip":
            continue
        kind, persisted = _status_kind(reports)
        finding = _code_finding(
            kind,
            "Special transponder status",
            episode,
            points,
            _status_field,
            confirmed=persisted is not None,
            tags=["status_field_disagreement"] if _field_disagreement(reports) else [],
        )
        findings.append(_ended(finding, episode) if lapsed else finding)
        first, _ = _status_kind(reports[:1])
        if persisted is not None and first != kind:
            # Earlier evaluations saved this episode under its first report's
            # name; end that row instead of leaving it open.
            early = {"reports": [p for p in reports if p["ts"] < persisted], "reverted": None}
            superseded = _code_finding(
                first, "Special transponder status", early, points, _status_field, confirmed=False
            )
            superseded.evidence["maneuver"].update(
                state="ended", end_reason="superseded", superseded_by=kind, evaluated_at=persisted
            )
            findings.append(superseded)
    for episode in _code_episodes(points, _special_code, lambda old, new: old == new):
        if (lapsed := _lapsed(episode, points)) == "skip":
            continue
        finding = _code_finding(
            "special_use_code",
            "Special-use transponder code",
            episode,
            points,
            lambda p: "squawk",
            confirmed=bool(_persistent(episode["reports"], squawk_code)),
            flags=("sensitive_code_block",) if SENSITIVE_CODES.fullmatch(episode["key"]) else (),
            code=episode["key"],
        )
        findings.append(_ended(finding, episode) if lapsed else finding)
    return findings


# Geometry.


def longitude_scale(lat):
    """Nautical miles per degree of longitude at this latitude."""
    return math.cos(math.radians(lat)) * 60


def xy(points):
    """Positions as (east, north) NM from the first one."""
    origin = points[0]
    scale = longitude_scale(origin["lat"])
    return [
        (((p["lon"] - origin["lon"] + 180) % 360 - 180) * scale, (p["lat"] - origin["lat"]) * 60)
        for p in points
    ]


def bearing(a, b):
    """Degrees true from a to b, both (east, north) NM."""
    return math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) % 360


def _decimated(points, coords, minimum):
    """(point, coordinate) for each fix at least ``minimum`` NM after the previous kept one."""
    kept = [(points[0], coords[0])]
    for p, c in zip(points[1:], coords[1:], strict=True):
        if math.dist(c, kept[-1][1]) >= minimum:
            kept.append((p, c))
    return kept


def _leg(start, end):
    (p, a), (q, b) = start, end
    return {
        "t0": p["ts"],
        "t1": q["ts"],
        "heading": bearing(a, b),
        "length": math.dist(a, b),
        "a": a,
        "b": b,
        "p": p,
        "q": q,
    }


def track_legs(points, minimum=0.05, coords=None):
    """Position-derived legs between fixes at least ``minimum`` NM apart."""
    if len(points) < 2:
        return []
    return [_leg(a, b) for a, b in pairwise(_decimated(points, coords or xy(points), minimum))]


def _straight_back(legs, end, tolerance=15):
    reference = legs[end]["heading"]
    start = end
    while start > 0 and abs(wrap_angle(legs[start - 1]["heading"] - reference)) <= tolerance:
        start -= 1
    return start


def _chord(legs, start, end):
    return bearing(legs[start]["a"], legs[end]["b"])


def _reversal_at(legs, last):
    """A reversal whose straight exit ends at ``legs[last]``, walking back to its entry."""
    exit_start = _straight_back(legs, last)
    exit_seconds = legs[last]["t1"] - legs[exit_start]["t0"]
    if exit_seconds < 45 or sum(leg["length"] for leg in legs[exit_start : last + 1]) < 0.3:
        return None
    accumulated = 0.0
    for end in range(exit_start - 1, -1, -1):
        accumulated += wrap_angle(legs[end + 1]["heading"] - legs[end]["heading"])
        start = _straight_back(legs, end)
        seconds = legs[end]["t1"] - legs[start]["t0"]
        if seconds < 60 or sum(leg["length"] for leg in legs[start : end + 1]) < 0.3:
            continue
        entry, leave = _chord(legs, start, end), _chord(legs, exit_start, last)
        net = (
            accumulated
            + wrap_angle(legs[end]["heading"] - entry)
            + wrap_angle(leave - legs[exit_start]["heading"])
        )
        angle = abs(wrap_angle(leave - entry))
        turn_seconds = legs[exit_start]["t0"] - legs[end]["t1"]
        if angle >= 135 and 135 <= abs(net) <= 225 and turn_seconds <= 300:
            return {
                "entry_start": start,
                "entry_end": end,
                "exit_start": exit_start,
                "exit_end": last,
                "angle": angle,
                "net": net,
                "turn_started_at": legs[end]["t1"],
                "turn_ended_at": legs[exit_start]["t0"],
                "entry_seconds": seconds,
                "exit_seconds": exit_seconds,
            }
        return None
    return None


def _turned_before(legs, start):
    """Whether the 300 s before ``legs[start]`` hold a net turn of at least 135°."""
    accumulated = 0.0
    for index in range(start - 1, -1, -1):
        if legs[start]["t0"] - legs[index]["t1"] > 300:
            break
        accumulated += wrap_angle(legs[index + 1]["heading"] - legs[index]["heading"])
        if abs(accumulated) >= 135:
            return True
    return False


def course_reversal(pos, legs=None):
    """The current straight exit's reversal, unless it continues a racetrack or S-turn.

    ``legs`` are track_legs(pos), when the caller already has them.
    """
    legs = track_legs(pos) if legs is None else legs
    if len(legs) < 6:
        return None
    found = _reversal_at(legs, len(legs) - 1)
    if found is None:
        return None
    # A short entry leg that follows another reversal-sized turn is a racetrack,
    # lawnmower or S-turn chain; only its first reversal counts.
    if found["entry_seconds"] < 180 and _turned_before(legs, found["entry_start"]):
        return None
    start, end = legs[found["entry_start"]]["t0"], legs[found["exit_end"]]["t1"]
    found["points"] = [p for p in pos if start <= p["ts"] <= end]
    found["turn"] = [p for p in pos if found["turn_started_at"] <= p["ts"] <= end]
    found["exit"] = [p for p in pos if found["turn_ended_at"] <= p["ts"] <= end]
    return found


def _oscillations(heights, step=500):
    pivot, direction, reversals = heights[0], 0, 0
    for altitude in heights[1:]:
        delta = altitude - pivot
        if direction == 0 and abs(delta) >= step:
            direction, pivot = (1 if delta > 0 else -1), altitude
        elif direction != 0 and direction * delta >= 0:
            pivot = altitude
        elif abs(delta) >= step:
            reversals += 1
            direction, pivot = -direction, altitude
    return reversals


def aerobatic_types():
    """The aerobatic designators: those the pinned type table lists, or every candidate without it."""
    return _aerobatic_types(reference.GENERATION)


@lru_cache(maxsize=1)
def _aerobatic_types(generation):
    # Keyed on the reference generation, so reloaded reference data is read again.
    if reference.dataset("aircraft-types") is None:
        return frozenset(AEROBATIC_CANDIDATES)
    return frozenset(code for code in AEROBATIC_CANDIDATES if reference.type_info(code) is not None)


def _max_turn_rate(pos, coords=None):
    """The highest position-derived turn rate held for AEROBATIC_TURN_SECONDS, in deg/s, or None.

    A held rate is the slowest of consecutive leg pairs, each turning at least
    AEROBATIC_TURN_RATE_DPS either way, that together span AEROBATIC_TURN_SECONDS;
    the result is the highest held rate over every such span. ``coords`` are
    xy(pos), when the caller already has them.
    """
    legs = track_legs(pos, minimum=0.1, coords=coords)
    pairs = [(a["t0"], b["t1"], abs(_turn_rate(a, b))) for a, b in pairwise(legs)]
    best = None
    for index, (start, _, rate) in enumerate(pairs):
        if rate < AEROBATIC_TURN_RATE_DPS:
            continue
        slowest = rate
        for _, end, later in pairs[index:]:
            if later < AEROBATIC_TURN_RATE_DPS:
                break
            slowest = min(slowest, later)
            if end - start >= AEROBATIC_TURN_SECONDS:
                best = slowest if best is None else max(best, slowest)
                break
    return None if best is None else round(best, 2)


def aerobatic(pos, aircraft, coords=None):
    """Repeated large altitude reversals in a small area by an aerobatic type or a hard-turning light aircraft.

    The window is the last AEROBATIC_WINDOW_SECONDS of ``pos``; ``coords`` are
    xy(pos), when the caller already has them. Only a listed aerobatic type, or a
    light (A1) aircraft that also holds a hard turn, is measured at all.
    """
    typed = reference.clean(aircraft.get("t")) in aerobatic_types()
    if not typed and aircraft.get("category") != "A1":
        return None
    cutoff = pos[-1]["ts"] - AEROBATIC_WINDOW_SECONDS
    first = next(index for index, p in enumerate(pos) if p["ts"] >= cutoff)
    window = pos[first:]
    if len(window) < 10:
        return None
    ref, heights = altitude_series(window)
    if not heights:
        return None
    reversals = _oscillations(heights, step=AEROBATIC_STEP_FT)
    excursion = max(heights) - min(heights)
    local = coords[first:] if coords else xy(window)
    span = math.hypot(
        max(x for x, _ in local) - min(x for x, _ in local),
        max(y for _, y in local) - min(y for _, y in local),
    )
    if (
        reversals < AEROBATIC_MIN_REVERSALS
        or span > AEROBATIC_MAX_SPAN_NM
        or excursion < AEROBATIC_MIN_EXCURSION_FT
    ):
        return None
    turn = _max_turn_rate(window, local)
    if not typed and turn is None:
        return None
    basis = "type" if typed else "turn_rate"
    finding = build(
        "aerobatic_practice",
        window,
        "Repeated large altitude reversals in a small area",
        reversals,
        "reversals",
        fields=("lat", "lon", ref),
        reversals=reversals,
        altitude_excursion_ft=round(excursion),
        span_nm=round(span, 2),
        # Without a turn held for AEROBATIC_TURN_SECONDS there is no rate to report.
        **({} if turn is None else {"max_turn_rate_dps": turn}),
        basis=basis,
        altitude_reference=ref,
        episode_start=window[0]["ts"],
    )
    if _terminal(window):
        _tag(finding, "terminal_area_context")
    return finding


def _moved(side, ref, level):
    """Forward flight, or a clear climb or descent relative to the hover level."""
    return any((number(p.get("gs")) or 0) >= 30 for p in side) or any(
        (height := number(p.get(ref))) is not None and abs(height - level) >= 200 for p in side
    )


def _hover(pos, aircraft):
    """Near-stationary rotorcraft reports, airborne-verified by movement on both sides."""
    if not rotorcraft(aircraft):
        return None
    runs, current = [], []
    for p in pos:
        speed = number(p.get("gs"))
        slow = speed is not None and 0 <= speed <= 8
        # A slow vertical climb or descent of 200 ft also ends the near-stationary run.
        level = altitude(current[0], ARCHIVE_ALTITUDE_FIELDS) if current else None
        height = altitude(p, ARCHIVE_ALTITUDE_FIELDS)
        if slow and current and (level is None or height is None or abs(height - level) < 200):
            current.append(p)
            continue
        if current:
            runs.append(current)
        current = [p] if slow else []
    if current:
        runs.append(current)
    for run in reversed(runs):
        if pos[-1]["ts"] - run[-1]["ts"] > 300:
            return None
        window = [p for p in run if p["ts"] >= run[-1]["ts"] - 300]
        if len(window) < 13 or window[-1]["ts"] - window[0]["ts"] < 180:
            continue
        ref, heights = spread_series(window)
        coords = xy(window)
        span = math.hypot(
            max(x for x, _ in coords) - min(x for x, _ in coords),
            max(y for _, y in coords) - min(y for _, y in coords),
        )
        if not (
            heights
            and statistics.median(number(p["gs"]) for p in window) <= 5
            and span <= 0.12
            and max(heights) - min(heights) <= 300
        ):
            continue
        level = statistics.median(heights)
        before = [p for p in pos if p["ts"] < run[0]["ts"]]
        after = [p for p in pos if p["ts"] > run[-1]["ts"]]
        # Ten stationary minutes between flights look more like a pad stop than a hover.
        verified = (
            _moved(before, ref, level) and _moved(after, ref, level) and run[-1]["ts"] - run[0]["ts"] <= 600
        )
        finding = build(
            "hovering",
            run,
            "Near-stationary airborne reports",
            run[-1]["ts"] - run[0]["ts"],
            "s",
            fields=("lat", "lon", "gs", ref),
            flags=() if verified else ("ground_status_unverified",),
            footprint_nm=round(span, 3),
            altitude_reference=ref,
            episode_start=run[0]["ts"],
            # A run cut at the buffer start slides with it; staying open lets the
            # collector keep merging it into one row.
            state="ended" if after and run[0] is not pos[0] else "active",
        )
        finding.evidence["maneuver"]["evaluated_at"] = pos[-1]["ts"]
        return finding
    return None


def straight_legs(points, *, legs=None, summarize=True):
    """Straight-leg summaries for long-horizon shapes such as survey grids.

    The collector may keep these per encounter beyond the 20-minute buffer and
    pass them back to ``analyze(legs=...)``. Each summary holds only aircraft
    positions, times, length, altitude range and source, and with ``summarize``
    the share of its reports with an altitude and the quality flags of the
    position reports it spans (see summarize_legs). ``points`` are positions(),
    in time order; ``legs`` are track_legs(points), when the caller already has
    them. Without ``summarize`` only the geometry is measured, for a caller that
    summarizes just the legs it keeps.
    """
    legs = track_legs(points) if legs is None else legs
    result, index = [], len(legs) - 1
    while index >= 0:
        start = _straight_back(legs, index, STRAIGHT_LEG_TOLERANCE)
        length = sum(leg["length"] for leg in legs[start : index + 1])
        if length >= STRAIGHT_LEG_MINIMUM_NM:
            first, last = legs[start]["p"], legs[index]["q"]
            heights = [
                a
                for leg in legs[start : index + 1]
                for a in (altitude(leg["p"]), altitude(leg["q"]))
                if a is not None
            ]
            result.append(
                {
                    "started_at": first["ts"],
                    "ended_at": last["ts"],
                    "start": [first["lat"], first["lon"]],
                    "end": [last["lat"], last["lon"]],
                    "length_nm": round(length, 3),
                    "alt_min": min(heights) if heights else None,
                    "alt_max": max(heights) if heights else None,
                    "source": first.get("source", "unknown"),
                }
            )
        index = start - 1
    result.reverse()
    return summarize_legs(result, points) if summarize else result


def summarize_legs(legs, points):
    """Copies of ``legs`` with the altitude coverage and quality flags of every report they span.

    The summaries keep what the reports' provenance and integrity values would
    otherwise leave behind once the reports are gone. ``points`` are positions(),
    in time order. A leg that already carries a summary, such as an earlier one
    merged into a longer line, keeps the poorer of the two: the lower coverage
    and every flag. A leg without reports in ``points`` is returned as it is.
    """
    stamps = [p["ts"] for p in points]
    result = []
    for leg in legs:
        # Every report the leg spans, not only the decimated fixes that bound it.
        samples = points[
            bisect.bisect_left(stamps, leg["started_at"]) : bisect.bisect_right(stamps, leg["ended_at"])
        ]
        if not samples:
            result.append(leg)
            continue
        coverage = round(sum(altitude(p) is not None for p in samples) / len(samples), 3)
        known = number(leg.get("altitude_coverage"))
        flags = quality(samples, ("lat", "lon", *ALTITUDE_FIELDS))
        result.append(
            {
                **leg,
                "altitude_coverage": coverage if known is None else min(known, coverage),
                "quality_flags": _leg_flags(leg, {"quality_flags": flags}),
            }
        )
    return result


def _leg_flags(*legs):
    """The quality flags of every leg summary, merged; a summary without any carries none."""
    return sorted({flag for leg in legs for flag in leg.get("quality_flags", ())})


def _local(point, origin):
    """NM east and north of ``origin``; both are [lat, lon]."""
    return (point[1] - origin[1]) * longitude_scale(origin[0]), (point[0] - origin[0]) * 60


def merge_legs(legs):
    """Join pieces of one flown line: earlier summaries and the current buffer can
    each hold part of it, or overlapping copies of it. Legs are straight_legs()
    summaries, which never leave memory; the inputs are not modified."""
    merged = []
    for leg in sorted(legs or (), key=lambda leg: leg["started_at"]):
        vx, vy = _local(leg["end"], leg["start"])
        for other in merged:
            ex, ey = _local(other["end"], other["start"])
            length = math.hypot(ex, ey)
            if (
                leg["started_at"] > other["ended_at"] + 60
                or not length
                or not math.hypot(vx, vy)
                or abs(wrap_angle(math.degrees(math.atan2(vx, vy) - math.atan2(ex, ey)))) > 10
                or max(
                    abs(ex * y - ey * x) / length
                    for x, y in (_local(leg[k], other["start"]) for k in ("start", "end"))
                )
                > 0.15
            ):
                continue
            if leg["ended_at"] > other["ended_at"]:
                other.update(end=leg["end"], ended_at=leg["ended_at"])
            heights = [
                h
                for h in (other["alt_min"], other["alt_max"], leg["alt_min"], leg["alt_max"])
                if h is not None
            ]
            other.update(alt_min=min(heights, default=None), alt_max=max(heights, default=None))
            other["length_nm"] = round(math.hypot(*_local(other["end"], other["start"])), 3)
            # The joined line is as uncertain, and as sparsely measured, as any piece of it.
            other["quality_flags"] = _leg_flags(other, leg)
            coverage = [
                c for c in (other.get("altitude_coverage"), leg.get("altitude_coverage")) if c is not None
            ]
            if coverage:
                other["altitude_coverage"] = min(coverage)
            break
        else:
            merged.append({**leg, "length_nm": round(math.hypot(vx, vy), 3)})
    return merged


def survey_grid(legs, points=None, *, merged=False):
    """At least six alternating, evenly spaced straight legs over at least 30 minutes.

    Legs are at least 3 NM long and within 10° of the grid axis; successive lines
    move consistently 0.2–2 NM to one side (coefficient of variation at most 0.35),
    and the altitude spread is at most 300 ft. The spread is established only by
    legs that each carry altitude bounds from at least half their reports; a grid
    with a leg short of that is returned with the altitude_coverage_incomplete flag.
    With ``points`` (positions(), in time order), the legs that form a grid are
    summarized from those reports first, so a caller can pass geometry-only legs
    and summarize none when no grid forms. ``merged`` says the legs are already
    merge_legs() output.
    """
    # Merging only joins legs, so six grid legs need six inputs.
    if len(legs) < 6:
        return None
    legs = [leg for leg in (legs if merged else merge_legs(legs)) if leg["length_nm"] >= 3]
    if len(legs) < 6:
        return None
    origin = legs[0]["start"]
    ends = {id(leg): (_local(leg["start"], origin), _local(leg["end"], origin)) for leg in legs}
    vectors = [(b[0] - a[0], b[1] - a[1]) for a, b in ends.values()]
    headings = [math.degrees(math.atan2(dx, dy)) % 360 for dx, dy in vectors]
    axis = _axis_mean(headings, [math.hypot(*v) for v in vectors])
    best = _alternating_run(list(zip(legs, headings, strict=True)), axis, 10)
    if len(best) < 6:
        return None
    grid = [leg for leg, _, _ in best]
    normal = (-math.cos(math.radians(axis)), math.sin(math.radians(axis)))
    across = [statistics.fmean(x * normal[0] + y * normal[1] for x, y in ends[id(leg)]) for leg in grid]
    shifts = [b - a for a, b in pairwise(across)]
    spacing = [abs(s) for s in shifts]
    heights = [h for leg in grid for h in (leg.get("alt_min"), leg.get("alt_max")) if number(h) is not None]
    duration = grid[-1]["ended_at"] - grid[0]["started_at"]
    if (
        any(not 0.2 <= s <= 2 for s in spacing)
        or len({s > 0 for s in shifts}) != 1
        or statistics.pstdev(spacing) / statistics.fmean(spacing) > 0.35
        or len(heights) < 2
        or max(heights) - min(heights) > 300
        or duration < 1800
    ):
        return None
    if points:
        grid = summarize_legs(grid, points)
    # One measured leg must not stand in for the others: every leg needs both
    # altitude bounds, and a known coverage of at least half its reports.
    unmeasured = any(
        number(leg.get("alt_min")) is None
        or number(leg.get("alt_max")) is None
        or (number(leg.get("altitude_coverage")) is not None and leg["altitude_coverage"] < 0.5)
        for leg in grid
    )
    return {
        "legs": grid,
        "leg_count": len(grid),
        "leg_spacing_nm": round(statistics.fmean(spacing), 3),
        "leg_length_nm": round(statistics.fmean(leg["length_nm"] for leg in grid), 3),
        "duration_seconds": duration,
        "quality_flags": ["altitude_coverage_incomplete"] if unmeasured else [],
    }


def _leg_endpoints(legs):
    """Each leg summary's start and end as points: time, position, and source."""
    return [
        {"ts": leg[stamp], "lat": leg[edge][0], "lon": leg[edge][1], "source": leg.get("source", "unknown")}
        for leg in legs
        for edge, stamp in (("start", "started_at"), ("end", "ended_at"))
    ]


def _survey_finding(grid):
    # The endpoints give the shape's extent and sources; the reports between them
    # are gone, so each leg's own quality flags carry their provenance and integrity.
    points = _leg_endpoints(grid["legs"])
    return build(
        "survey_grid",
        points,
        "Evenly spaced parallel survey-like legs",
        grid["leg_count"],
        "legs",
        fields=("lat", "lon"),
        flags=[*_leg_flags(*grid["legs"]), *grid.get("quality_flags", ())],
        leg_count=grid["leg_count"],
        leg_spacing_nm=grid["leg_spacing_nm"],
        leg_length_nm=grid["leg_length_nm"],
        episode_start=points[0]["ts"],
    )


def _axis_mean(headings, weights=None):
    """Circular mean of headings modulo 180, in degrees, optionally weighted."""
    weights = weights or [1] * len(headings)
    x = sum(w * math.cos(math.radians(2 * h)) for h, w in zip(headings, weights, strict=True))
    y = sum(w * math.sin(math.radians(2 * h)) for h, w in zip(headings, weights, strict=True))
    return (math.degrees(math.atan2(y, x)) / 2) % 180


def _axis_offset(heading, axis):
    """Signed difference of heading from axis modulo 180, in [-90, 90)."""
    return (heading - axis + 90) % 180 - 90


def _alternating_run(entries, axis, tolerance=RACETRACK_AXIS_TOLERANCE):
    """The longest run of (leg, heading) entries within ``tolerance`` degrees of ``axis``
    whose direction along it alternates, as (leg, heading, sign) triples. Legs off
    the axis are skipped without ending the run."""
    run, best = [], []
    for leg, heading in entries:
        if abs(_axis_offset(heading, axis)) > tolerance:
            continue
        sign = 1 if abs(wrap_angle(heading - axis)) <= 90 else -1
        if run and sign == run[-1][2]:
            run = []
        run.append((leg, heading, sign))
        if len(run) > len(best):
            best = list(run)
    return best


def _racetrack_lines(run, axis):
    """Spacing and line repeat of a run over ``axis``, or None when it is no racetrack.

    Each leg's offset from its line is measured at both ends and the midpoint.
    """
    legs = [leg for leg, _, _ in run]
    signs = [sign for _, _, sign in run]
    duration = legs[-1]["ended_at"] - legs[0]["started_at"]
    if len(legs) < RACETRACK_MIN_LEGS or duration < RACETRACK_MIN_SECONDS:
        return None
    origin = legs[0]["start"]
    # The unit normal to the axis, as (east, north).
    normal = (-math.cos(math.radians(axis)), math.sin(math.radians(axis)))

    def lateral(point):
        east, north = _local(point, origin)
        return east * normal[0] + north * normal[1]

    centers = [[(leg["start"][i] + leg["end"][i]) / 2 for i in (0, 1)] for leg in legs]
    offsets = [
        (lateral(leg["start"]), lateral(center), lateral(leg["end"]))
        for leg, center in zip(legs, centers, strict=True)
    ]
    means = {
        side: statistics.fmean(o[1] for o, s in zip(offsets, signs, strict=True) if s == side)
        for side in (1, -1)
    }
    spacing = abs(means[1] - means[-1])
    repeat = max(abs(value - means[s]) for o, s in zip(offsets, signs, strict=True) for value in o)
    if not RACETRACK_SPACING_NM[0] <= spacing <= RACETRACK_SPACING_NM[1] or repeat > RACETRACK_LINE_REPEAT_NM:
        return None
    return {"legs": legs, "axis": axis, "spacing": spacing, "repeat": repeat, "centers": centers}


def racetrack(legs, pos, aircraft, *, merged=False):
    """A repeated racetrack of long reciprocal legs over the same two lines.

    ``legs`` are straight_legs() summaries, merged here as survey_grid merges
    them unless ``merged`` says they are merge_legs() output already. Each leg
    at least 10 NM long proposes its heading as the axis; the longest run of
    alternating legs within 10° of it is refitted to its own axis and checked
    again. A racetrack needs four legs over at least 40 minutes, its two lines
    2 to 15 NM apart, and every leg within 1 NM of its line. The longest run
    that passes wins, the earliest on a tie.
    """
    # Merging only joins legs, so four racetrack legs need four inputs.
    if len(legs) < RACETRACK_MIN_LEGS:
        return None
    long_legs = [
        leg
        for leg in (legs if merged else merge_legs(legs))
        if (leg.get("length_nm") or 0) >= RACETRACK_MIN_LEG_NM
    ]
    if len(long_legs) < RACETRACK_MIN_LEGS:
        return None
    # Straight-leg summaries carry no heading; it is the leg's own chord.
    entries = [(leg, math.degrees(math.atan2(*_local(leg["end"], leg["start"]))) % 360) for leg in long_legs]
    shape, tried = None, set()
    for candidate in sorted({round(heading % 180, 1) for _, heading in entries}):
        run = _alternating_run(entries, candidate)
        # Nearby candidate axes often select the same legs; each run is judged once.
        key = tuple(id(leg) for leg, _, _ in run)
        if len(run) < RACETRACK_MIN_LEGS or key in tried:
            continue
        tried.add(key)
        # Transit legs never set the axis: it is refitted on the run's own legs.
        axis = _axis_mean([heading for _, heading, _ in run])
        refit = _alternating_run([(leg, heading) for leg, heading, _ in run], axis)
        if len(refit) != len(run):
            continue
        found = _racetrack_lines(refit, axis)
        if found and (
            shape is None
            or (len(found["legs"]), -found["legs"][0]["started_at"])
            > (len(shape["legs"]), -shape["legs"][0]["started_at"])
        ):
            shape = found
    if shape is None:
        return None
    aligned = summarize_legs(shape["legs"], pos)
    # The legs can outlast the buffer: their endpoints give the shape's extent,
    # and the buffered reports inside it fill in the track.
    by_time = {point["ts"]: point for point in _leg_endpoints(aligned)}
    by_time.update(
        {p["ts"]: p for p in pos if aligned[0]["started_at"] <= p["ts"] <= aligned[-1]["ended_at"]}
    )
    points = [by_time[stamp] for stamp in sorted(by_time)]
    centers = shape["centers"]
    runway = _nearest_runway_nm(
        [statistics.fmean(c[0] for c in centers), statistics.fmean(c[1] for c in centers)]
    )
    tags = []
    if tanker(aircraft):
        tags.append("tanker_type")
    if int(number(aircraft.get("dbFlags")) or 0) & 1:
        tags.append("military_flag")
    if runway is not None and runway <= RACETRACK_AIRPORT_NM:
        tags.append("airport_context")
    return build(
        "racetrack_orbit",
        points,
        "Repeated racetrack over the same two lines",
        len(aligned),
        "legs",
        fields=("lat", "lon"),
        flags=_leg_flags(*aligned),
        tags=tags,
        racetrack_legs=len(aligned),
        leg_length_nm=round(statistics.fmean(leg["length_nm"] for leg in aligned), 2),
        axis_heading_deg=round(shape["axis"], 1),
        lateral_spacing_nm=round(shape["spacing"], 2),
        line_repeat_nm=round(shape["repeat"], 2),
        # An unknown distance (no runway table, or near its edge) is no measurement.
        **({} if runway is None else {"runway_within_nm": round(runway, 1)}),
        episode_start=aligned[0]["started_at"],
    )


def parallel(points, coords=None):
    if len(points) < 12 or points[-1]["ts"] - points[0]["ts"] < 180:
        return None
    coords = coords or xy(points)
    mx, my = statistics.fmean(x for x, _ in coords), statistics.fmean(y for _, y in coords)
    xx, yy = sum((x - mx) ** 2 for x, _ in coords), sum((y - my) ** 2 for _, y in coords)
    cross = sum((x - mx) * (y - my) for x, y in coords)
    angle = math.atan2(2 * cross, xx - yy) / 2
    along = [x * math.cos(angle) + y * math.sin(angle) for x, y in coords]
    across = [-x * math.sin(angle) + y * math.cos(angle) for x, y in coords]
    legs, active = [], None
    for index in range(1, len(points)):
        dx, dy = along[index] - along[index - 1], across[index] - across[index - 1]
        straight = abs(dx) >= 0.05 and abs(dy) <= abs(dx) * 0.25
        direction = 1 if dx > 0 else -1
        if active and (not straight or direction != active[0]):
            legs.append(active)
            active = None
        if straight:
            if active is None:
                active = [direction, index - 1, index]
            active[2] = index
    if active:
        legs.append(active)
    legs = [
        leg
        for leg in legs
        if abs(along[leg[2]] - along[leg[1]]) >= 1 and points[leg[2]]["ts"] - points[leg[1]]["ts"] >= 30
    ]
    if len(legs) < 3:
        return None
    centers = [statistics.median(across[a : b + 1]) for _, a, b in legs]
    shifts = [b - a for a, b in zip(centers, centers[1:], strict=False)]
    common = min(max(along[a : b + 1]) for _, a, b in legs) - max(min(along[a : b + 1]) for _, a, b in legs)
    if (
        common < 0.7
        or any(not 0.15 <= abs(s) <= 2 for s in shifts)
        or len({s > 0 for s in shifts}) != 1
        or any(a[0] == b[0] for a, b in zip(legs, legs[1:], strict=False))
    ):
        return None
    return build(
        "parallel_passes",
        points,
        "Offset parallel flight legs",
        len(legs),
        "passes",
        fields=("lat", "lon"),
        complete_passes=len(legs),
    )


def go_around(points):
    window = [p for p in points if p["ts"] >= points[-1]["ts"] - 300]
    ref, heights = altitude_series(window)
    if len(window) < 7 or not heights:
        return None
    floor = min(range(len(heights)), key=heights.__getitem__)
    if (
        floor < 3
        or floor > len(heights) - 4
        or heights[floor] > 6000
        or heights[0] - heights[floor] < 400
        or heights[-1] - heights[floor] < 400
    ):
        return None
    before, after = window[: floor + 1], window[floor:]
    if min(before[-1]["ts"] - before[0]["ts"], after[-1]["ts"] - after[0]["ts"]) < 30:
        return None
    if any(p.get("ground") for p in window):
        return None
    runway = airport_context.aligned_approach(before[-5:])
    if not runway:
        return None
    finding = build(
        "possible_go_around",
        window,
        "Possible go-around sequence",
        heights[-1] - heights[floor],
        "ft regained",
        fields=("lat", "lon", ref),
        tags=("airport_maneuver_hypothesis",),
        altitude_excursion_ft=heights[-1] - heights[floor],
        altitude_reference=ref,
    )
    finding.evidence["airport_context"] = airport_context.annotation(runway)
    return finding


def departure_return(points):
    ground = [
        i
        for i, p in enumerate(points)
        if p.get("ground") and number(p.get("lat")) is not None and number(p.get("lon")) is not None
    ]
    if not ground:
        return None
    last_ground = ground[-1]
    air = positions(points[last_ground + 1 :])
    if len(air) < 10 or air[-1]["ts"] - air[0]["ts"] < 180:
        return None
    ref, heights = altitude_series(air)
    if not heights or max(heights) - heights[0] < 500 or max(heights) - heights[-1] < 500:
        return None
    runway = airport_context.aligned_approach(air[-5:])
    if (
        not runway
        or airport_context.runway_distance(points[last_ground], runway) > 0.6
        or max(airport_context.runway_distance(p, runway) for p in air) < 3
    ):
        return None
    finding = build(
        "departure_return",
        points[last_ground:],
        "Return toward observed departure airport",
        air[-1]["ts"] - air[0]["ts"],
        "s",
        fields=("lat", "lon", ref),
        flags=("airport_maneuver_hypothesis",),
        altitude_reference=ref,
    )
    finding.evidence["airport_context"] = airport_context.annotation(runway)
    return finding


def _since(points, cutoff):
    """The points at or after ``cutoff``, newest first."""
    for point in reversed(points):
        if point["ts"] < cutoff:
            return
        yield point


def _turn_rate(a, b):
    """Degrees per second between consecutive legs; 0 across a gap over 45 s."""
    elapsed = ((b["t0"] + b["t1"]) - (a["t0"] + a["t1"])) / 2
    return wrap_angle(b["heading"] - a["heading"]) / elapsed if 0 < elapsed <= 45 else 0


def extreme_turn(pos, aircraft, coords=None):
    """Position-derived turn rate above 6°/s for at least 20 s by a large aircraft.

    A run is consecutive leg pairs turning above 6°/s one way; the latest run
    lasting 20 s must end within 60 s of the newest fix. Legs are built from the
    newest fix backward only until that is settled.
    """
    if aircraft.get("category") not in {"A3", "A4", "A5"} or len(pos) < 2:
        return None
    kept = _decimated(pos, coords or xy(pos), 0.1)
    legs = {}

    def leg(index):
        if index not in legs:
            legs[index] = _leg(kept[index], kept[index + 1])
        return legs[index]

    def lasted(run):
        # ``run`` holds (pair index, rate), newest first.
        first, last = leg(run[-1][0]), leg(run[0][0] + 1)
        return (last["t0"] + last["t1"] - first["t0"] - first["t1"]) / 2 >= 20

    newest, best, run = pos[-1]["ts"], None, []
    for index in range(len(kept) - 3, -1, -1):
        rate = _turn_rate(leg(index), leg(index + 1))
        if abs(rate) > 6 and run and rate * run[-1][1] > 0:
            run.append((index, rate))
            continue
        if run:
            if lasted(run):
                best = run
                break
            run = []
        if newest - leg(index + 1)["t1"] > 60:
            # Any run from here back ends too early.
            return None
        if abs(rate) > 6:
            run = [(index, rate)]
    else:
        if run and lasted(run):
            best = run
    if best is None:
        return None
    best = [(leg(index), leg(index + 1), rate) for index, rate in reversed(best)]
    start, end = best[0][0]["t0"], best[-1][1]["t1"]
    window = [p for p in pos if start <= p["ts"] <= end]
    finding = build(
        "extreme_turn_rate",
        window,
        "Very tight position-derived turn",
        round(statistics.median(abs(r) for _, _, r in best), 1),
        "deg/s",
        fields=("lat", "lon"),
        flags=("extreme_turn_review",),
        turn_degrees=round(abs(sum(wrap_angle(b["heading"] - a["heading"]) for a, b, _ in best)), 1),
        median_ground_speed_kt=_median(number(p.get("gs")) for p in window),
        episode_start=start,
    )
    if _terminal(window):
        _tag(finding, "terminal_area_context")
    return finding


def _balloon_contrary(aircraft):
    """A declared category or listed type class that contradicts a balloon."""
    known = airport_context.type_class(aircraft.get("t"))
    return aircraft.get("category") in BALLOON_CONTRARY_CATEGORIES or (
        known is not None and known not in BALLOON_COMPATIBLE_CLASSES
    )


def stratospheric_balloon(pos, aircraft):
    """A slow target at 50,000 ft or higher for at least ten minutes, with corroboration.

    Pressure and geometric altitudes more than 3,000 ft apart, or a declared
    category or listed type that is not a balloon, are contrary evidence and
    block the finding.
    """
    # The high run must end within 60 s of the newest fix.
    if not pos or not any(
        (height := altitude(p, ARCHIVE_ALTITUDE_FIELDS)) is not None and height >= BALLOON_FLOOR_FT
        for p in _since(pos, pos[-1]["ts"] - 60)
    ):
        return None
    run = []
    for p in pos:
        ref, height = altitude_field(p, ARCHIVE_ALTITUDE_FIELDS)
        if ref is None or height < BALLOON_FLOOR_FT:
            run = []
            continue
        if run and run[-1][1] != ref:
            run = []
        run.append((p, ref))
    if not run or pos[-1]["ts"] - run[-1][0]["ts"] > 60:
        return None
    points, ref = [p for p, _ in run], run[0][1]
    duration = points[-1]["ts"] - points[0]["ts"]
    if len(points) < 20 or duration < 600:
        return None
    heights = [number(p[ref]) for p in points]
    speeds = [number(p.get("gs")) for p in points]
    if all(s is None for s in speeds):
        speeds = [
            distance_nm((a["lat"], a["lon"]), (b["lat"], b["lon"])) * 3600 / (b["ts"] - a["ts"])
            for a, b in pairwise(points)
        ]
    rates = [_rate(p) for p in points]
    if all(r is None for r in rates):
        rates = [
            (b - a) * 60 / (q["ts"] - p["ts"])
            for (p, a), (q, b) in pairwise(zip(points, heights, strict=True))
        ]
    speed, climb = _median(speeds), _median(abs(r) for r in rates if r is not None)
    if (
        speed is None
        or speed > 80
        or climb is None
        or climb > 1000
        or any(abs(b - a) > 2000 for a, b in pairwise(heights))
    ):
        return None
    corroboration, flags = [], []
    differences = [
        abs(number(p.get("alt_geom")) - number(p.get("alt_baro")))
        for p in points
        if number(p.get("alt_geom")) is not None and number(p.get("alt_baro")) is not None
    ]
    if differences:
        if statistics.median(differences) <= 3000:
            corroboration.append("geometric_altitude_agrees")
        else:
            flags.append("altitude_reference_conflict")
    if _balloon_contrary(aircraft):
        flags.append("balloon_class_conflict")
    if aircraft.get("category") == "B2" or any(p.get("category") == "B2" for p in points):
        corroboration.append("balloon_category")
    if aircraft.get("t") == "BALL" or airport_context.type_class(aircraft.get("t")) == "balloon":
        corroboration.append("balloon_type")
    callsigns = {str(p.get("flight", aircraft.get("flight", ""))).strip().upper() for p in points}
    if any(c.startswith(BALLOON_CALLSIGNS) for c in callsigns):
        corroboration.append("balloon_callsign")
    if any(SENSITIVE_CODES.fullmatch(squawk_code(p) or "") for p in points):
        corroboration.append("high_altitude_code_block")
    return build(
        "stratospheric_balloon",
        points,
        "High-altitude balloon-like reports",
        round(statistics.median(heights)),
        "ft",
        fields=("lat", "lon", ref),
        confirmed=bool(corroboration),
        flags=flags,
        max_altitude_ft=max(heights),
        median_ground_speed_kt=round(speed, 1),
        altitude_reference=ref,
        corroboration=corroboration,
        episode_start=points[0]["ts"],
    )


def _orbit(orbit, pos, aircraft):
    """The circling verdict as the episode rules read it, or None.

    ``orbit`` is a rules.orbit_verdict() dict, False for none, or None to
    evaluate this buffer's circling here. A verdict that already holds an
    "airport_context" key (None when away from runways) is not classified again.
    """
    if orbit is None:
        orbit = orbit_verdict(movement_patterns(pos)) if pos else False
    if not orbit or orbit.get("shape_verified") is False:
        return None
    started, ended = number(orbit.get("started_at")), number(orbit.get("ended_at"))
    if started is None or ended is None or ended < started:
        return None
    samples = [
        sample
        for sample in orbit.get("pattern_samples") or []
        if all(number(sample.get(k)) is not None for k in ("ts", "lat", "lon"))
    ]
    if "airport_context" in orbit:
        context = orbit["airport_context"]
    else:
        context = airport_context.window_context(orbit, samples, aircraft) if samples else None
    return {
        "started_at": started,
        "ended_at": ended,
        "duration": number(orbit.get("duration_seconds")) or ended - started,
        "closed_loops": int(number(orbit.get("closed_loops")) or 0),
        "uncertain": orbit.get("quality_review_required") is not False,
        "airport_context": context,
        "samples": samples,
        "tags": [
            tag
            for tag in orbit.get("context_tags") or []
            if tag in ("ground_referenced_orbit", "drifting_orbit")
        ],
    }


def _holding_like(circle):
    return circle["duration"] >= 600 and circle["closed_loops"] >= 3


def _descending(heights):
    """At least 1,500 ft lost, falling between at least 70% of consecutive reports."""
    return heights[-1] - heights[0] <= -1500 and sum(b < a for a, b in pairwise(heights)) >= 0.7 * (
        len(heights) - 1
    )


def circuit_attributes(samples, aircraft, orbit=None):
    """Holding, oscillation and descent properties of a runway-circuit circling episode.

    For the collector: when a circling finding has a runway-circuit annotation,
    analyze() no longer saves prolonged_holding, altitude_oscillation or
    descending_spiral for that window. Merge ``attributes`` into the circling
    evidence and add ``context_tags``. Returns None without that annotation.
    ``orbit`` is the circling finding or its verdict.
    """
    pos = positions(list(samples))
    if isinstance(orbit, Finding):
        orbit = orbit_verdict([orbit])
    circle = _orbit(orbit, pos, aircraft)
    if not circle or not circle["airport_context"]:
        return None
    window = [p for p in pos if circle["started_at"] <= p["ts"] <= circle["ended_at"]]
    attributes = {
        "duration_seconds": circle["duration"],
        "closed_loops": circle["closed_loops"],
        "holding_like": _holding_like(circle),
    }
    ref, heights = altitude_series(window)
    if heights:
        reversals = _oscillations(heights)
        attributes.update(
            altitude_reference=ref,
            altitude_change_ft=heights[-1] - heights[0],
            altitude_excursion_ft=max(heights) - min(heights),
            reversals=reversals,
            oscillating=reversals >= 3,
            descending=_descending(heights),
        )
    return {"context_tags": ["runway_circuit_context"], "attributes": attributes}


def _orbit_findings(circle, pos, aircraft, circuit):
    """Holding, descending-spiral and soaring findings from one circling verdict.

    A runway-circuit orbit keeps its holding and descent as circling attributes.
    """
    findings = []
    fields = ("lat", "lon")
    window = [p for p in pos if circle["started_at"] <= p["ts"] <= circle["ended_at"]]
    flags = ["uncertain_geometry"] if circle["uncertain"] else []
    ref, heights = altitude_series(window)
    # The verdict's own samples describe the same interval as its duration.
    holding = circle["samples"] or window
    if holding and not circuit and _holding_like(circle):
        findings.append(
            build(
                "prolonged_holding",
                holding,
                "Prolonged holding-like track",
                circle["duration"] / 60,
                "minutes",
                fields=fields,
                flags=flags,
                tags=circle["tags"],
                closed_loops=circle["closed_loops"],
            )
        )
    if heights and not circuit and _descending(heights):
        spiral = build(
            "descending_spiral",
            window,
            "Circling with sustained altitude loss",
            heights[-1] - heights[0],
            "ft",
            fields=(*fields, ref),
            flags=flags,
            tags=circle["tags"],
            altitude_change_ft=heights[-1] - heights[0],
            altitude_reference=ref,
            closed_loops=circle["closed_loops"],
            start_altitude_ft=heights[0],
            end_altitude_ft=heights[-1],
        )
        if not airport_context.available():
            _flag(spiral, "airport_context_unavailable")
        elif _airport_descent(window):
            _tag(spiral, "airport_descent_context")
        findings.append(spiral)
    if heights and glider(aircraft, window):
        gain, seconds = heights[-1] - heights[0], window[-1]["ts"] - window[0]["ts"]
        speed = _median(number(p.get("gs")) for p in window)
        if gain >= 500 and seconds > 0 and 200 <= gain * 60 / seconds <= 1000 and speed and 40 <= speed <= 80:
            findings.append(
                build(
                    "thermal_soaring",
                    window,
                    "Circling climb consistent with soaring",
                    gain,
                    "ft",
                    fields=(*fields, ref),
                    flags=flags,
                    tags=circle["tags"],
                    climb_gain_ft=gain,
                    median_ground_speed_kt=round(speed, 1),
                    closed_loops=circle["closed_loops"],
                    altitude_reference=ref,
                )
            )
    return findings


def trajectory(points, aircraft, *, orbit=None, track=None, legs=None):
    pos = positions(points) if track is None else track
    if len(pos) < 4:
        return []
    fields = ("lat", "lon")
    # One projection and one set of fine legs serve every shape below.
    coords = xy(pos)
    fine = track_legs(pos, coords=coords)
    circle = _orbit(orbit, pos, aircraft)
    # A runway-circuit orbit keeps its holding, oscillation and descent as attributes.
    circuit = circle if circle and circle["airport_context"] else None
    findings = _orbit_findings(circle, pos, aircraft, circuit) if circle else []
    reversal = course_reversal(pos, fine)
    if reversal and not _overlaps(reversal["turn_started_at"], reversal["turn_ended_at"], circle):
        finding = build(
            "course_reversal",
            reversal["points"],
            "Substantial course reversal",
            round(reversal["angle"], 1),
            "degrees",
            fields=fields,
            turn_degrees=round(reversal["angle"], 1),
            net_turn_degrees=round(reversal["net"], 1),
            turn_started_at=reversal["turn_started_at"],
            turn_ended_at=reversal["turn_ended_at"],
            entry_seconds=reversal["entry_seconds"],
            exit_seconds=reversal["exit_seconds"],
            episode_start=reversal["turn_started_at"],
            state="ended",
        )
        if _terminal(reversal["turn"], reversal["exit"]):
            _tag(finding, "terminal_area_context")
        findings.append(finding)
    ref, heights = altitude_series(pos)
    if heights and len(pos) >= 10 and (reversals := _oscillations(heights)) >= 3:
        if not (circuit and _overlaps(pos[0]["ts"], pos[-1]["ts"], circuit)):
            finding = build(
                "altitude_oscillation",
                pos,
                "Repeated altitude reversals",
                reversals,
                "reversals",
                fields=(*fields, ref),
                reversals=reversals,
                altitude_excursion_ft=max(heights) - min(heights),
                altitude_reference=ref,
            )
            if _terminal(pos):
                _tag(finding, "terminal_area_context")
            findings.append(finding)
    if match := aerobatic(pos, aircraft, coords):
        info = match.evidence["maneuver"]
        if not (circuit and _overlaps(info["started_at"], info["ended_at"], circuit)):
            findings.append(match)
    if match := _hover(pos, aircraft):
        findings.append(match)
    # This runs for every fresh report, so the buffer's legs are measured, and
    # their reports summarized only once they form a grid.
    lines = [*(legs or []), *straight_legs(pos, legs=fine, summarize=False)]
    # Both long shapes read the same merged lines; too few lines for either skip the merge.
    merged = merge_legs(lines) if len(lines) >= RACETRACK_MIN_LEGS else []
    grid = survey_grid(merged, pos, merged=True)
    if grid:
        findings.append(_survey_finding(grid))
    elif match := parallel(pos, coords):
        if _terminal(pos):
            _tag(match, "terminal_area_context")
        findings.append(match)
    if not grid and (match := racetrack(merged, pos, aircraft, merged=True)):
        findings.append(match)
    if match := go_around(pos):
        findings.append(match)
    if match := departure_return(points):
        findings.append(match)
    if match := extreme_turn(pos, aircraft, coords):
        findings.append(match)
    if match := stratospheric_balloon(pos, aircraft):
        findings.append(match)
    if circuit:
        for finding in findings:
            info = finding.evidence["maneuver"]
            if _overlaps(info["started_at"], info["ended_at"], circuit):
                _tag(finding, "runway_circuit_context")
    return findings


# Navigation integrity.


def _integrity(point):
    """'good' or 'lost' for an airborne ADS-B version 2 report; None otherwise."""
    if point.get("ground") or point.get("source") != "adsb_icao":
        return None
    version = number(point.get("version"))
    if (version is not None and version < 2) or any(
        source_for(point, field) != "adsb_icao" for field in ("nic", "nac_p")
    ):
        return None
    nic, accuracy = number(point.get("nic")), number(point.get("nac_p"))
    if (
        number(point.get("gpsOkBefore")) is not None
        or (nic is not None and nic <= 1)
        or (accuracy is not None and accuracy <= 1)
    ):
        return "lost"
    return "good" if nic is not None and accuracy is not None and nic >= 6 and accuracy >= 8 else None


def gnss_integrity_loss(points):
    """NIC/NACp fall to 1 or less after at least 60 s at NIC 6+ and NACp 8+.

    The loss needs at least three consecutive airborne reports over 20 s with a
    pressure altitude, all from one ADS-B track. readsb's ``gpsOkBefore`` marker
    counts as a loss. This describes integrity reports, never a cause.
    """
    # The loss must end within 60 s of the newest report; most buffers have none there.
    if not points or not any(
        number(p.get("alt_baro")) is not None and _integrity(p) == "lost"
        for p in _since(points, points[-1]["ts"] - 60)
    ):
        return None
    states = [(p, _integrity(p)) for p in points]
    runs, run = [], []
    for index, (p, state) in enumerate(states):
        if state == "lost" and number(p.get("alt_baro")) is not None:
            run.append(index)
        elif run:
            runs.append(run)
            run = []
    if run:
        runs.append(run)
    for run in reversed(runs):
        loss = [states[index][0] for index in run]
        if points[-1]["ts"] - loss[-1]["ts"] > 60:
            return None
        if len(loss) < 3 or loss[-1]["ts"] - loss[0]["ts"] < 20:
            continue
        # Intermediate integrity reports may sit between the baseline and the drop.
        index = run[0] - 1
        while index >= 0 and states[index][1] is None and loss[0]["ts"] - states[index][0]["ts"] <= 30:
            index -= 1
        baseline = []
        while index >= 0 and states[index][1] == "good":
            baseline.insert(0, states[index][0])
            index -= 1
        if (
            not baseline
            or baseline[-1]["ts"] - baseline[0]["ts"] < 60
            or loss[0]["ts"] - baseline[-1]["ts"] > 30
        ):
            continue
        after = [p for p, _ in states[run[-1] + 1 :]][:2]
        recovered = len(after) == 2 and all((number(p.get("nic")) or 0) >= 6 for p in after)
        observed = after[-1]["ts"] if after else loss[-1]["ts"]
        state = "recovered" if recovered else "ended" if after else "active"
        last_good = next(
            (
                {"last_good_lat": p["gpsOkLat"], "last_good_lon": p["gpsOkLon"]}
                for p in loss
                if number(p.get("gpsOkLat")) is not None and number(p.get("gpsOkLon")) is not None
            ),
            {"last_good_lat": baseline[-1].get("lat"), "last_good_lon": baseline[-1].get("lon")},
        )
        finding = build(
            "gnss_integrity_loss",
            loss,
            "Navigation integrity reports dropped",
            loss[-1]["ts"] - loss[0]["ts"],
            "s",
            fields=("nic", "nac_p"),
            onset_at=loss[0]["ts"],
            baseline_seconds=baseline[-1]["ts"] - baseline[0]["ts"],
            episode_start=loss[0]["ts"],
            state=state,
            **({"end_reason": "recovered" if recovered else "integrity_changed"} if after else {}),
            **{key: value for key, value in last_good.items() if number(value) is not None},
        )
        finding.evidence["maneuver"]["evaluated_at"] = observed
        return finding
    return None


# Multi-aircraft helpers. The collector holds every address; these stay pure.


def participant(address, role, *, aircraft=None, source=None, measurements=None):
    """One entry of ``evidence["participants"]``; the primary aircraft is never listed."""
    meta = aircraft or {}
    entry = {
        "hex": address,
        "role": role,
        "source": source or meta.get("type") or meta.get("source") or "unknown",
        "measurements": {
            key: value for key, value in (measurements or {}).items() if number(value) is not None
        },
    }
    for key, name in (("r", "registration"), ("t", "type"), ("flight", "callsign")):
        if isinstance(meta.get(key), str) and meta[key].strip():
            entry[name] = meta[key].strip()
    return entry


def gnss_member(address, finding, aircraft=None):
    """Cluster input from one gnss_integrity_loss finding."""
    info = finding.evidence["maneuver"]
    return {
        **{key: value for key, value in (aircraft or {}).items() if key in ("r", "t", "flight")},
        "hex": address,
        "onset_at": info.get("onset_at", info["started_at"]),
        "last_good_lat": info.get("last_good_lat"),
        "last_good_lon": info.get("last_good_lon"),
        "source": finding.evidence["maneuver_samples"][-1].get("source", "unknown"),
    }


def _groups(members, window, radius, center, joins=lambda group, member: True):
    """Greedy, deterministic groups of at least three addresses near in time and space."""
    first = {}
    for member in sorted(members, key=lambda m: (m["at"], m["hex"])):
        first.setdefault(member["hex"], member)
    ordered = sorted(first.values(), key=lambda m: (m["at"], m["hex"]))
    used, groups = set(), []
    for anchor in ordered:
        if anchor["hex"] in used:
            continue
        group = [anchor]
        for other in ordered:
            if (
                other["hex"] in used
                or other is anchor
                or not 0 <= other["at"] - anchor["at"] <= window
                or any(distance_nm(center(other), center(m)) > radius for m in group)
                or not joins(group, other)
            ):
                continue
            group.append(other)
        if len(group) >= 3:
            used.update(m["hex"] for m in group)
            groups.append(group)
    return groups


def _uncertain_sources(sources):
    return ["participant_uncertain_source"] if any(source != "adsb_icao" for source in sources) else []


def _group_finding(kind, title, group, points, measurement, *, flags=(), archive=None):
    """A multi-aircraft finding saved on the group's first member; up to twelve
    others are its participants, each with ``measurement(member)``."""
    finding = build(
        kind,
        points,
        title,
        len(group),
        "aircraft",
        flags=[*_uncertain_sources(m.get("source") for m in group), *flags],
        participant_count=len(group),
        episode_start=group[0]["at"],
        archive=archive or next((m["archive"] for m in group if m.get("archive")), None),
    )
    finding.evidence["participants"] = [
        participant(m["hex"], "member", aircraft=m, measurements=measurement(m))
        for m in group[1 : 1 + MAX_PARTICIPANTS]
    ]
    return group[0]["hex"], finding


def gnss_clusters(members, *, archive=None):
    """At least three addresses losing integrity within 10 minutes and 60 NM.

    ``members`` come from ``gnss_member``. Distance uses each aircraft's last
    good position, never the receiver. Returns ``(primary_hex, finding)`` pairs;
    the earliest onset is primary and the others are listed as participants.
    A member's "archive" (or ``archive``) names the archive format it was read from.
    """
    usable = []
    for m in members:
        values = {k: number(m.get(k)) for k in ("onset_at", "last_good_lat", "last_good_lon")}
        if None not in values.values():
            usable.append({**m, **values, "at": values["onset_at"]})
    return [
        _group_finding(
            "gnss_integrity_cluster",
            "Integrity reports dropped on several aircraft",
            group,
            [{"ts": m["at"], "source": m.get("source", "unknown")} for m in group],
            lambda m, group=group: {"onset_offset_seconds": m["at"] - group[0]["at"]},
            archive=archive,
        )
        for group in _groups(
            usable, GNSS_WINDOW_SECONDS, GNSS_RADIUS_NM, lambda m: (m["last_good_lat"], m["last_good_lon"])
        )
    ]


def holding_stacks(episodes, *, archive=None):
    """At least three aircraft circling at the same time, within 10 NM, 500 ft apart.

    ``episodes`` hold ``hex``, ``started_at``, ``ended_at``, the orbit centre
    ``lat``/``lon`` and ``altitude_ft`` at or above 5,000 ft; optional ``source``,
    r/t/flight and "archive". Callers leave out orbits whose shape needs review.
    Every pair overlaps in time and is at least 500 ft apart vertically. Returns
    ``(primary_hex, finding)`` pairs.
    """
    usable = []
    for e in episodes:
        values = {k: number(e.get(k)) for k in ("started_at", "ended_at", "lat", "lon", "altitude_ft")}
        if (
            None not in values.values()
            and values["altitude_ft"] >= STACK_FLOOR_FT
            and values["ended_at"] >= values["started_at"]
        ):
            usable.append({**e, **values, "at": values["started_at"]})

    def stacked(group, member):
        return all(
            m["started_at"] <= member["ended_at"]
            and member["started_at"] <= m["ended_at"]
            and abs(member["altitude_ft"] - m["altitude_ft"]) >= 500
            for m in group
        )

    results = []
    for group in _groups(
        usable, STACK_WINDOW_SECONDS, STACK_RADIUS_NM, lambda m: (m["lat"], m["lon"]), stacked
    ):
        points = sorted(
            (
                {"ts": m[key], "source": m.get("source", "unknown")}
                for m in group
                for key in ("started_at", "ended_at")
            ),
            key=lambda p: p["ts"],
        )
        results.append(
            _group_finding(
                "holding_stack",
                "Several aircraft circling at different levels",
                group,
                points,
                lambda m: {"altitude_ft": m["altitude_ft"]},
                archive=archive,
            )
        )
    return results


def _headings(pos):
    """Reported track, or the position-derived heading to each fix."""
    result = [number(pos[0].get("track"))]
    for a, b in pairwise(pos):
        track = number(b.get("track"))
        if track is None:
            c, d = xy([a, b])
            track = bearing(c, d) if math.dist(c, d) >= 0.02 else None
        result.append(track)
    return result


class _Match(NamedTuple):
    """A fix of the first aircraft and the second's nearest report within 5 s."""

    p: dict
    q: dict
    good: bool
    horizontal: float
    vertical: float
    reference: str


def _formation_ok(p, q, hp, hq):
    references = [
        field
        for field in ("alt_baro", "alt_geom")
        if number(p.get(field)) is not None and number(q.get(field)) is not None
    ]
    if not references:
        return None
    altitude_p, altitude_q = number(p[references[0]]), number(q[references[0]])
    horizontal = distance_nm((p["lat"], p["lon"]), (q["lat"], q["lon"]))
    vertical = abs(altitude_p - altitude_q)
    speeds = number(p.get("gs")), number(q.get("gs"))
    low = min(altitude_p, altitude_q)
    good = (
        horizontal <= 0.5
        and vertical <= 400
        and hp is not None
        and hq is not None
        and abs(wrap_angle(hp - hq)) <= 20
        and None not in speeds
        and abs(speeds[0] - speeds[1]) <= 25
        and (
            low >= 3000
            or (
                low >= 1500
                and airport_context.available()
                and not airport_context.nearby(p, 3)
                and not airport_context.nearby(q, 3)
            )
        )
    )
    return good, horizontal, vertical, references[0]


def _aligned_gaps(match, second, times):
    """Gaps between a fix and the second track interpolated to its time: horizontal
    NM, vertical ft and ground speed kn (None when not reported), or None."""
    q = _threat_at(second, times, match.p["ts"], (match.reference, "gs"))
    if q is None:
        return None
    heights = number(match.p.get(match.reference)), number(q.get(match.reference))
    speeds = number(match.p.get("gs")), number(q.get("gs"))
    return (
        distance_nm((match.p["lat"], match.p["lon"]), (q["lat"], q["lon"])),
        match.vertical if None in heights else abs(heights[0] - heights[1]),
        None if None in speeds else abs(speeds[0] - speeds[1]),
    )


def _duplicate(best, second, times):
    """Whether two addresses look like one aircraft: once the second track is
    aligned to the first's fix times, the positions coincide (median under 0.01
    NM) at the same altitude (under 25 ft) and ground speed (within 2 kn)."""
    gaps = [found for match in best if (found := _aligned_gaps(match, second, times))]
    speeds = [speed for _, _, speed in gaps if speed is not None]
    return (
        bool(gaps)
        and statistics.median(horizontal for horizontal, _, _ in gaps) < 0.01
        and statistics.median(vertical for _, vertical, _ in gaps) < 25
        and (not speeds or statistics.median(speeds) <= 2)
    )


def _pair(a, first, b, second, meta):
    times = [q["ts"] for q in second]
    headings_a, headings_b = _headings(first), _headings(second)
    matches = []
    for index, p in enumerate(first):
        nearest = bisect.bisect_left(times, p["ts"])
        candidates = [j for j in (nearest - 1, nearest) if 0 <= j < len(second)]
        if not candidates:
            continue
        j = min(candidates, key=lambda k: abs(second[k]["ts"] - p["ts"]))
        if abs(second[j]["ts"] - p["ts"]) > 5:
            continue
        result = _formation_ok(p, second[j], headings_a[index], headings_b[j])
        if result:
            matches.append(_Match(p, second[j], *result))
    best, run = [], []
    for match in matches:
        if not match.good or (run and match.p["ts"] - run[-1].p["ts"] > 45):
            run = []
        if match.good:
            run.append(match)
            if len(run) >= 8 and run[-1].p["ts"] - run[0].p["ts"] >= 120:
                best = list(run)
    if not best or max(first[-1]["ts"], second[-1]["ts"]) - best[-1].p["ts"] > 60:
        return None
    closest = min(best, key=lambda m: m.horizontal)

    def names(address, side):
        # Registration and callsigns; one aircraft echoed under two addresses shares one.
        values = {(meta.get(address) or {}).get("r"), *(p.get("flight") for p in side)}
        return {v.strip().upper() for v in values if isinstance(v, str) and v.strip()}

    duplicate = bool(names(a, [m.p for m in best]) & names(b, [m.q for m in best])) or _duplicate(
        best, second, times
    )
    gains = [
        number(getattr(best[-1], side).get(best[-1].reference))
        - number(getattr(best[0], side).get(best[0].reference))
        for side in ("p", "q")
        if best[0].reference == best[-1].reference
    ]
    samples = {a: [m.p for m in best], b: [m.q for m in best]}
    # The geometry rests on both tracks: each aircraft's matched reports are checked
    # for every field the separations and motion comparison read.
    fields = ("lat", "lon", "gs", "track", *sorted({m.reference for m in best}))
    flags = sorted({flag for side in samples.values() for flag in quality(side, fields)})
    return {
        "hexes": (a, b),
        "started_at": best[0].p["ts"],
        "ended_at": best[-1].p["ts"],
        "pair_count": len(best),
        "min_separation_nm": round(closest.horizontal, 3),
        "vertical_separation_ft": round(closest.vertical),
        "median_separation_nm": round(statistics.median(m.horizontal for m in best), 3),
        "samples": samples,
        "sources": {a: first[-1].get("source", "unknown"), b: second[-1].get("source", "unknown")},
        "quality_flags": flags,
        "climbing": len(gains) == 2 and min(gains) >= 200,
        "duplicate": duplicate,
    }


def near_pairs(locations, cell):
    """Sorted pairs of keys whose positions fall in the same or adjacent grid cells.

    ``locations`` maps keys to (lat, lon); ``cell(lat, lon)`` gives an integer
    (row, column). A coarse neighbour search before exact distance checks.
    """
    cells = defaultdict(list)
    for key, (lat, lon) in sorted(locations.items()):
        cells[cell(lat, lon)].append(key)
    pairs = set()
    for (row, column), keys in cells.items():
        for dy, dx in product((-1, 0, 1), repeat=2):
            for a in keys:
                for b in cells.get((row + dy, column + dx), ()):
                    if a < b:
                        pairs.add((a, b))
    return sorted(pairs)


def formation_pairs(history, aircraft=None):
    """Pairs of aircraft flying together, from each address's recent samples.

    ``history`` maps addresses to chronological telemetry (or track) samples;
    ``aircraft`` optionally maps addresses to r/t/flight/category/dbFlags. Both
    aircraft must report direct ADS-B positions (no ``~`` address, MLAT or
    TIS-B) at or above 3,000 ft, or 1,500 ft and more than 3 NM from any pinned
    runway; within 0.5 NM and 400 ft, tracks within 20° and ground speeds within
    25 kn, for at least 120 s and 8 matched position pairs. Pass the result to
    ``formation_findings``.
    """
    meta = aircraft or {}
    tracks = {}
    for address, samples in history.items():
        if address.startswith("~"):
            continue
        pos = [p for p in positions(list(samples)) if p.get("source") in DIRECT and altitude(p) is not None]
        if len(pos) >= 8:
            tracks[address] = pos
    latest = {address: (pos[-1]["lat"], pos[-1]["lon"]) for address, pos in tracks.items()}
    candidates = near_pairs(latest, lambda lat, lon: (math.floor(lat * 10), math.floor(lon * 10)))
    return [record for a, b in candidates if (record := _pair(a, tracks[a], b, tracks[b], meta))]


def _pair_measurements(address, members, primary):
    """The separation a participant reports: its pair with the primary, else its closest pair."""
    pair = next((p for p in members if set(p["hexes"]) == {primary, address}), None) or min(
        (p for p in members if address in p["hexes"]), key=lambda p: p["min_separation_nm"]
    )
    return {key: pair[key] for key in ("min_separation_nm", "vertical_separation_ft")}


def formation_findings(pairs, aircraft=None, *, archive=None):
    """One formation_flight finding per connected group of pairs.

    Returns ``(primary_hex, finding)``; the primary has the most partners (then
    the lowest address) and every other aircraft is a participant. ``archive``
    names the archive format the tracks were read from, or None when live.
    """
    meta = aircraft or {}
    parent = {}

    def root(address):
        parent.setdefault(address, address)
        while parent[address] != address:
            parent[address] = parent[parent[address]]
            address = parent[address]
        return address

    for pair in pairs:
        a, b = pair["hexes"]
        parent[root(a)] = root(b)
    groups = {}
    for pair in pairs:
        groups.setdefault(root(pair["hexes"][0]), []).append(pair)
    results = []
    for members in groups.values():
        degree = {}
        for pair in members:
            for address in pair["hexes"]:
                degree[address] = degree.get(address, 0) + 1
        primary = min(degree, key=lambda address: (-degree[address], address))
        others = sorted(address for address in degree if address != primary)
        own = {p["ts"]: p for pair in members if primary in pair["hexes"] for p in pair["samples"][primary]}
        points = [own[stamp] for stamp in sorted(own)]
        closest = min(members, key=lambda pair: pair["min_separation_nm"])
        sources = {address: source for pair in members for address, source in pair["sources"].items()}
        # Every participant's matched reports count, not only the primary's own points.
        flags = [
            *_uncertain_sources(sources.values()),
            *{flag for pair in members for flag in pair.get("quality_flags", ())},
        ]
        if any(pair["duplicate"] for pair in members):
            flags.append("possible_duplicate_target")
        tags = []
        if any(tanker(meta.get(address, {})) for address in degree):
            tags.append("refuelling_like")
        for pair in members:
            gliders = [glider(meta.get(address, {}), pair["samples"][address]) for address in pair["hexes"]]
            if sum(gliders) == 1 and pair["min_separation_nm"] <= 0.2 and pair["climbing"]:
                tags.append("aerotow_like")
        finding = build(
            "formation_flight",
            points,
            "Aircraft flying in close company",
            points[-1]["ts"] - points[0]["ts"],
            "s",
            fields=("lat", "lon"),
            flags=flags,
            tags=tags,
            archive=archive,
            min_separation_nm=closest["min_separation_nm"],
            vertical_separation_ft=closest["vertical_separation_ft"],
            participant_count=len(degree),
            episode_start=min(pair["started_at"] for pair in members),
        )
        finding.evidence["participants"] = [
            participant(
                address,
                "member",
                aircraft=meta.get(address),
                source=sources.get(address),
                measurements=_pair_measurements(address, members, primary),
            )
            for address in others[:MAX_PARTICIPANTS]
        ]
        if _terminal(points):
            _tag(finding, "terminal_area_context")
        results.append((primary, finding))
    return results


# TCAS resolution advisories. readsb decodes them; the evidence never claims a
# near miss or a loss of separation.


# The advised response compares the latest rate in the 30 s before the first active
# message with the latest in the 20 s after it.
RESPONSE_WINDOW = (30, 20)


# A vertical rate within this of zero is level flight.
LEVEL_FPM = 500


class _RateReport(NamedTuple):
    """A vertical rate with the report and field it was read from."""

    rate: float
    field: str
    point: dict


def _last_rate(points, low, high, *, inclusive=True):
    """The latest reported vertical rate at a time in [low, high] ((low, high] if
    not inclusive), with its report and field, or None."""
    for p in reversed(points):
        if (low <= p["ts"] if inclusive else low < p["ts"]) and p["ts"] <= high:
            field, rate, _ = _rate_choice(p)
            if rate is not None:
                return _RateReport(rate, field, p)
    return None


def _response(sense, before, after):
    """Whether the rate after the advisory is the advised response to the rate before it.

    A level-off is answered by level flight, or by at least halving the rate
    towards it; a climb or descent that starts or grows after the advisory is not.
    """
    if sense in {"maintain", "preventive"}:
        return True
    if before is None or after is None:
        return False
    if sense == "up":
        return after >= 1000 or before < 0 < after
    if sense == "down":
        return after <= -1000 or before > 0 > after
    if sense == "level":
        return abs(after) <= LEVEL_FPM or abs(after) <= abs(before) * 0.5
    return False


def _provenance(a, b):
    """What two fixes say about how they were measured, merged for a position
    between them: the fields either lists as MLAT or TIS-B derived, the lower
    integrity and accuracy values, and the archive either came from."""
    merged = {}
    for key in ("mlat", "tisb"):
        listed = [p[key] for p in (a, b) if isinstance(p.get(key), (list, tuple))]
        if listed:
            merged[key] = sorted({name for names in listed for name in names if isinstance(name, str)})
    for key in ("nic", "nac_p"):
        values = [value for p in (a, b) if (value := number(p.get(key))) is not None]
        if values:
            merged[key] = min(values)
    if archive := a.get("archive") or b.get("archive"):
        merged["archive"] = archive
    return merged


def _threat_at(others, times, stamp, fields=("alt_baro",)):
    """The other aircraft's position at ``stamp``: interpolated between fixes up to
    30 s apart (with ``fields`` when both fixes report them), else its nearest fix
    within 2 s. An interpolated position keeps both fixes' provenance."""
    j = bisect.bisect_left(times, stamp)
    if 0 < j < len(others) and 0 < others[j]["ts"] - others[j - 1]["ts"] <= 30:
        a, b = others[j - 1], others[j]
        fraction = (stamp - a["ts"]) / (b["ts"] - a["ts"])
        result = {
            "ts": stamp,
            "lat": a["lat"] + fraction * (b["lat"] - a["lat"]),
            "lon": a["lon"] + fraction * (b["lon"] - a["lon"]),
            "source": b.get("source", "unknown"),
            **_provenance(a, b),
        }
        for field in fields:
            values = number(a.get(field)), number(b.get(field))
            result[field] = None if None in values else values[0] + fraction * (values[1] - values[0])
        return result
    near = [others[k] for k in (j - 1, j) if 0 <= k < len(others) and abs(others[k]["ts"] - stamp) <= 2]
    return near[0] if near else None


def _closest_approach(first, second, start, end, fields=("alt_baro",)):
    """Closest horizontal approach between two aircraft, with the vertical gap then.

    Returns (NM, ft or None, the other aircraft's interpolated position); the
    vertical gap uses the first of ``fields`` both aircraft report.
    """
    others = positions(list(second))
    times = [q["ts"] for q in others]
    best = None
    for p in positions(list(first)):
        if not start <= p["ts"] <= end or (q := _threat_at(others, times, p["ts"], fields)) is None:
            continue
        horizontal = distance_nm((p["lat"], p["lon"]), (q["lat"], q["lon"]))
        if best is None or horizontal < best[0]:
            field = next(
                (f for f in fields if number(p.get(f)) is not None and number(q.get(f)) is not None), None
            )
            vertical = abs(number(p[field]) - number(q[field])) if field else None
            best = (horizontal, vertical, q)
    return best


def _separation_flags(own_points, threat_points, start, end, fields=("alt_baro",)):
    """Quality flags of the reports a closest approach is measured from: the
    aircraft's own fixes in the window and the other aircraft's fixes that bound
    it, including the pair an interpolated position lies between (up to 30 s
    outside the window). The other aircraft's indirect sources are its
    participant flag; its integrity values count like the aircraft's own."""
    checked = ("lat", "lon", *fields)
    own = [p for p in positions(list(own_points)) if start <= p["ts"] <= end]
    others = [q for q in positions(list(threat_points)) if start - 30 <= q["ts"] <= end + 30]
    flags = set(quality(own, checked)) if own else set()
    for flag in quality(others, checked) if others else ():
        flags.add("participant_uncertain_source" if flag == "uncertain_source" else flag)
    return sorted(flags)


def advisory_runs(reports):
    """Advisory episodes from readsb ``acas_ra`` reports, oldest first.

    Messages are allowlisted and deduplicated by their own timestamp (the last
    copy wins). An episode ends after 60 s without messages or at a terminated
    advisory (RAT=1) followed by an active one.
    """
    messages = {}
    for raw in reports:
        if (report := acas_report(raw)) is not None:
            messages[report["unix_timestamp"]] = report
    run = []
    for stamp in sorted(messages):
        report = messages[stamp]
        if run and (
            stamp - run[-1]["unix_timestamp"] > 60 or (run[-1].get("RAT") == 1 and report.get("RAT") != 1)
        ):
            yield run
            run = []
        run.append(report)
    if run:
        yield run


def advisory_threat(episode, own_address=None):
    """The threat an advisory episode names: the first active message (RAT=0) with
    a threat identity (TTI=1). Never the aircraft itself."""
    threat = next(
        (
            m["threat_id_hex"]
            for m in episode
            if m.get("RAT") == 0 and m.get("TTI") == 1 and m.get("threat_id_hex")
        ),
        None,
    )
    return None if threat == own_address else threat


def tcas_episode(acas_reports, own_points, threat_points=(), *, own, threat=None, archive=None):
    """The latest TCAS resolution-advisory episode from readsb ``acas_ra`` reports.

    Episodes come from advisory_runs(). An episode needs at least two active
    (RAT=0) messages within 30 s while the aircraft is airborne and ends at
    RAT=1 or after 60 s without messages. Only ADS-B (DF17/18) decoding is
    non-experimental; the threat's track (``threat_points``, for the threat
    advisory_threat() names), the advised response and a direct threat source
    are required for unblocked evidence. ``archive`` names the archive format of
    the aircraft's own saved track, or None for live reports.
    """
    runs = list(advisory_runs(acas_reports))
    episode = runs[-1] if runs else []
    active = [m for m in episode if m.get("RAT") == 0]
    if not active:
        return None
    first, last = active[0]["unix_timestamp"], episode[-1]["unix_timestamp"]
    own_points = sorted(own_points, key=lambda p: p["ts"])
    nearby_points = [p for p in own_points if abs(p["ts"] - first) <= 30]
    airborne = (
        bool(nearby_points)
        and not any(p.get("ground") for p in nearby_points)
        and any(altitude(p) is not None for p in nearby_points)
    )
    confirmed = airborne and any(
        0 < b["unix_timestamp"] - a["unix_timestamp"] <= 30 for a, b in pairwise(active)
    )
    advisory = next((m["advisory"] for m in active if m.get("advisory")), None)
    sense = advisory_sense(advisory)
    rates = (
        _last_rate(own_points, first - RESPONSE_WINDOW[0], first),
        _last_rate(own_points, first, first + RESPONSE_WINDOW[1], inclusive=False),
    )
    before, after = (report.rate if report else None for report in rates)
    flags = []
    if any(m.get("df_type") not in (17, 18) for m in episode):
        flags.append("acas_decoder_experimental")
    if not _response(sense, before, after):
        flags.append("no_advised_response")
    # The response rests on two rate reports, whose provenance is judged like the
    # separation's: a rate from MLAT or TIS-B does not establish it.
    flags.extend(flag for report in rates if report for flag in quality([report.point], (report.field,)))
    threat_hex = advisory_threat(episode, own.get("hex"))
    approach = _closest_approach(own_points, threat_points, first - 60, last + 60) if threat_hex else None
    if approach is None:
        flags.append("threat_track_missing")
    threat_source = approach[2].get("source", "unknown") if approach else (threat or {}).get("type")
    if threat_hex and threat_source not in {"adsb_icao", "adsb_other"}:
        flags.append("participant_uncertain_source")
    # The advisory is the aircraft's own report; the separation is measured from both
    # tracks' fixes, whose provenance and integrity are judged separately from it.
    separation = _separation_flags(own_points, threat_points, first - 60, last + 60) if approach else []
    flags.extend(separation)
    window = [p for p in own_points if first - 30 <= p["ts"] <= last + 30]
    flags.extend(quality(window, ("alt_baro",)) if window else ["matching_altitude_unavailable"])
    points = [{"ts": m["unix_timestamp"], "source": own.get("type", "unknown")} for m in episode]
    complement = next((m["advisory_complement"] for m in active if m.get("advisory_complement")), None)
    terminated = episode[-1].get("RAT") == 1
    # Without a captured RAT=1, a minute of own reports with no new message ends it.
    silent = bool(own_points) and own_points[-1]["ts"] - last > 60
    metrics = {
        "distinct_report_count": len(active),
        "participant_count": 2 if threat_hex else 1,
        "episode_start": first,
        "state": "ended" if terminated or silent else "active",
        "advisory_sequence": [m["advisory"] for m in episode if m.get("advisory")],
    }
    if terminated or silent:
        metrics["end_reason"] = "terminated" if terminated else "no_messages"
    if advisory:
        metrics["advisory"] = advisory
    if complement:
        metrics["advisory_complement"] = complement
    if before is not None and after is not None:
        metrics["response_rate_change_fpm"] = after - before
    if approach:
        metrics["min_separation_nm"] = round(approach[0], 2)
        if approach[1] is not None:
            metrics["vertical_separation_ft"] = round(approach[1])
        if separation:
            # The separation figures stay, marked by the limits of the fixes behind them.
            metrics["separation_quality_flags"] = separation
    finding = build(
        "tcas_ra",
        points,
        "TCAS resolution advisory reported",
        None,
        "",
        confirmed=confirmed,
        flags=flags,
        archive=archive,
        **metrics,
    )
    # Later own reports supply the response; stop updating a minute after the last message.
    if own_points:
        finding.evidence["maneuver"]["evaluated_at"] = max(last, min(own_points[-1]["ts"], last + 60))
    finding.evidence["context_samples"] = bounded_samples(window)
    if threat_hex:
        finding.evidence["participants"] = [
            participant(
                threat_hex,
                "threat",
                aircraft=threat,
                source=threat_source,
                measurements={
                    key: metrics[key]
                    for key in ("min_separation_nm", "vertical_separation_ft")
                    if key in metrics
                },
            )
        ]
    return finding


def _tcas(samples, aircraft, threats):
    """The latest advisory episode, with the buffered track of the threat it names."""
    reports = [p["acas_ra"] for p in samples if isinstance(p.get("acas_ra"), dict)]
    if not reports:
        return []
    runs = list(advisory_runs(reports))
    threat = advisory_threat(runs[-1], aircraft.get("hex")) if runs else None
    # ``threats`` maps addresses to {"samples", "aircraft"}.
    entry = (threats or {}).get(threat) or {}
    episode = tcas_episode(
        reports, samples, entry.get("samples", ()), own=aircraft, threat=entry.get("aircraft")
    )
    return [episode] if episode else []


# Selected-altitude overshoot. A selected altitude is not an ATC clearance.


def _indicated(point):
    altitude = number(point.get("alt_baro"))
    if altitude is None or altitude >= 18000:
        return altitude
    # Below the US transition altitude the selection is set against local pressure.
    qnh = number(point.get("nav_qnh"))
    return altitude + 27 * (qnh - 1013.25) if qnh is not None else None


def _overshoot(run):
    selected = number(run[0]["nav_altitude_mcp"])
    approach, beyond = None, []
    for p in run:
        height = _indicated(p)
        if height is None:
            continue
        offset = height - selected
        if approach is None:
            if p["ts"] - run[0]["ts"] >= 60:
                rate = _rate(p)
                if rate is not None and abs(rate) >= 1000 and offset * rate < 0:
                    approach = (p, 1 if rate > 0 else -1)
            continue
        rate = _rate(p)
        direction = approach[1]
        if offset * direction >= 300:
            beyond.append((p, offset * direction))
        enough = len(beyond) >= 3 and beyond[-1][0]["ts"] - beyond[0][0]["ts"] >= 20
        turned = (rate is not None and rate * direction < -200) or offset * direction < 300
        if enough and turned:
            return approach[0], beyond, p, selected
        if offset * direction < 300 and beyond:
            approach, beyond = None, []
    return None


def _overshoot_possible(points):
    """Whether a report in the last 60 s, or the one with an altitude before them, is
    300 ft or more from its selected altitude. A reportable overshoot turns back
    within 60 s of the newest report, right after its last report beyond the
    selection, so this is the cheap test most buffers fail."""
    if not points:
        return False
    cutoff = points[-1]["ts"] - 60
    for p in reversed(points):
        height, selected = _indicated(p), number(p.get("nav_altitude_mcp"))
        if height is None or selected is None:
            continue
        if abs(height - selected) >= 300:
            return True
        if p["ts"] < cutoff:
            return False
    return False


def selected_altitude_overshoot(points):
    """Passing an unchanged selected altitude by 300 ft for 20 s, then turning back."""
    if not _overshoot_possible(points):
        return None
    runs, run = [], []
    for p in points:
        selected = number(p.get("nav_altitude_mcp"))
        if selected is None or (run and number(run[-1].get("nav_altitude_mcp")) != selected):
            if run:
                runs.append(run)
            run = []
        if selected is not None:
            run.append(p)
    if run:
        runs.append(run)
    for run in reversed(runs):
        if (found := _overshoot(run)) is None:
            continue
        start, beyond, turned, selected = found
        if points[-1]["ts"] - turned["ts"] > 60:
            return None
        window = [p for p in run if start["ts"] <= p["ts"] <= turned["ts"]]
        excess = max(value for _, value in beyond)
        return build(
            "selected_altitude_overshoot",
            window,
            "Passed the selected altitude before turning back",
            round(excess),
            "ft",
            fields=("alt_baro",),
            flags=("selected_altitude_not_clearance",),
            altitude_excursion_ft=round(excess),
            selected_altitude_ft=selected,
            episode_start=start["ts"],
            state="ended",
        )
    return None


# Whole-buffer analysis.


def _closed(findings, reason, evaluated_at):
    """Open episodes whose data stopped: a segment break or no report for 60 s."""
    closed = []
    for finding in findings:
        info = finding.evidence["maneuver"]
        if info.get("state") != "active":
            continue
        info.update(state="ended", end_reason=reason, evaluated_at=max(info["evaluated_at"], evaluated_at))
        closed.append(finding)
    return closed


def _close_previous(samples, segment, gap, split, evaluate):
    """Close episodes of the segment that ended just before the current one began."""
    if not segment or segment[-1]["ts"] - segment[0]["ts"] > STATUS_GAP:
        return []
    index = next((i for i, p in enumerate(samples) if p is segment[0]), 0)
    previous = recent(samples[:index], gap, split_sources=split) if index else []
    return _closed(evaluate(previous), "segment_break", segment[0]["ts"]) if previous else []


def analyze(
    samples,
    aircraft,
    *,
    orbit=None,
    now=None,
    legs=None,
    threats=None,
    archive=None,
    positions_cache=None,
):
    """Evaluate one aircraft's buffered reports with the shared episode rules.

    ``samples`` may be the buffer itself; it is copied once. Optional inputs:
    - ``orbit``: this evaluation's circling verdict (rules.orbit_verdict; False
      for none, None to evaluate it here). It decides holding, spiral and
      soaring precedence and suppresses reversals inside the orbit.
    - ``now``: once the newest report is over 60 s old, every open episode ends
      ("stale") and nothing new is reported.
    - ``legs``: earlier ``straight_legs`` summaries for long survey grids.
    - ``threats``: TCAS threat address -> {"samples", "aircraft"}.
    - ``archive``: the archive format of saved reports ("traces" or "heatmap"),
      or None for live reports; see archived().
    - ``positions_cache``: a telemetry.PositionCache the caller keeps for this buffer.
    """
    samples = list(samples)
    points = recent(samples)
    if not points:
        return []
    status = recent(samples, STATUS_GAP, split_sources=False)
    newest = max(points[-1]["ts"], status[-1]["ts"])
    findings = vertical(points) + signals(status)
    track = positions(samples, cache=positions_cache)
    findings += trajectory(points, aircraft, orbit=orbit, track=track, legs=legs)
    findings += [f for f in (gnss_integrity_loss(points), selected_altitude_overshoot(points)) if f]
    findings += _tcas(samples, aircraft, threats)
    if now is not None and now - newest > STATUS_GAP:
        findings = _closed(findings, "stale", newest + STATUS_GAP)
    else:
        findings += _close_previous(samples, points, 45, True, vertical)
        findings += _close_previous(samples, status, STATUS_GAP, False, signals)
    return [archived(finding, archive) for finding in findings] if archive else findings


def anomaly_tags(previous, current):
    """Private diagnostics only. One address never establishes a shared anomaly.

    Only consecutive airborne reports from one direct ADS-B source compare, and
    MLAT- or TIS-B-attributed integrity values are ignored, so a change of source
    cannot look like a GNSS or position fault.
    """
    tags = set()
    if (
        not previous
        or not 0 < current["ts"] - previous["ts"] <= 45
        or current.get("source") != previous.get("source")
        or current.get("source") not in DIRECT
        or previous.get("ground")
        or current.get("ground")
    ):
        return tags
    pair = [previous, current]
    for field in ("nic", "nac_p"):
        if any(source_for(p, field) not in DIRECT for p in pair):
            continue
        old, new = number(previous.get(field)), number(current.get(field))
        if old is not None and new is not None and old >= 5 and new <= 1:
            tags.add("position_quality_drop")
    if all(all(number(p.get(k)) is not None for k in ("lat", "lon")) for p in pair) and all(
        source_for(p, "lat") in DIRECT for p in pair
    ):
        dt = current.get("position_at", current["ts"]) - previous.get("position_at", previous["ts"])
        distance = distance_nm((previous["lat"], previous["lon"]), (current["lat"], current["lon"]))
        if 2 <= dt <= 45 and distance >= 12 and distance * 3600 / dt >= 1200:
            tags.add("position_jump")
    return tags


def correlated(rows, now):
    groups = {}
    for address, stamp, source, tag in rows:
        if 0 <= now - stamp <= 60:
            groups.setdefault((source, tag), {})[address] = stamp
    return [(source, tag, addresses) for (source, tag), addresses in groups.items() if len(addresses) >= 3]


def correlation_finding(source, tag, addresses, *, archive=None):
    """``archive`` names the archive format the reports came from (any true value
    marks archived reports), or None when live."""
    points = [{"ts": stamp, "source": source} for stamp in sorted(addresses.values())]
    return build(
        "correlated_data_anomaly",
        points,
        "Related telemetry anomalies across aircraft",
        len(addresses),
        "addresses",
        flags=("shared_source_cause_unknown",),
        archive=archive,
        aircraft_count=len(addresses),
        anomaly_type=tag,
    )


def saved_snapshot(aircraft, finding):
    """Pair the saved readings with their actual endpoint or peak timestamp."""
    info = finding.evidence["maneuver"]
    stamp = info.get("peak_at", info["ended_at"])
    point = finding.evidence.get("peak_sample") or next(
        (p for p in finding.evidence.get("maneuver_samples", []) if p["ts"] == stamp), {}
    )
    identity = {k: aircraft[k] for k in SNAPSHOT_IDENTITY_FIELDS if k in aircraft}
    readings = {k: point[k] for k in SNAPSHOT_READING_FIELDS if k in point}
    return {**identity, **readings, "type": point.get("source", aircraft.get("type", "unknown"))}, stamp
