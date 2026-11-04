"""Choose a small daily reading list without approving evidence or publishing it."""

import re
import time
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from typing import NamedTuple

from . import enrichment_rules, kinds, limits, reference, typecheck
from .enrichment_rules import REGISTRY_TYPE_KINDS, SETTLED, TYPE_DEPENDENT_KINDS
from .maneuvers import HIGH_RATE_FPM, altitude_agrees
from .rules import (
    MIN_REPORT_SECONDS,
    MIN_REPORTS,
    NORM_KINDS,  # noqa: F401 - autopilot reads it here with the other kind sets
    RATED_KINDS,
    SQUAWK_PREFIXES,
    callsign_designator,
    number,
    speed_plausible,
    type_envelope,
)
from .telemetry import direct_value

VERSION = 11
MAX_PER_DAY = 9
MIN_SCORE = 65
IDENTITY_COOLDOWN = 30 * 86400
# One airframe takes one story of a kind (kind_family) per 30 days.
STORY_COOLDOWN = 30 * 86400
TOPIC_COOLDOWN = 7 * 86400
# Local type novelty. A watch type repeats at most every 14 days on any airframe.
# A type seen on 40% of the last 60 coverage days gets one identity story per 30 days.
TYPE_COOLDOWN = 14 * 86400
ROUTINE_COOLDOWN = 30 * 86400
ROUTINE_WINDOW = 60
ROUTINE_DAYS = 24
VARIANT_COOLDOWN = 7 * 86400
# A data-conflict story, a tailwind speed record and a rated-limit exceedance
# read alike on any aircraft, so each series takes one story per seven days.
SERIES_COOLDOWN = 7 * 86400
# Readings above a listed type's envelope: a wrong type or bad data, not performance. They are
# the kinds that name a disagreeing registry's type (enrichment_rules.REGISTRY_TYPE_KINDS).
ENVELOPE_KINDS = REGISTRY_TYPE_KINDS
# A type's fastest sustained ground speed this season (selection imports this one set).
RECORD_KINDS = kinds.where(record=True)
# The data-conflict, season-record, and rated-limit kinds (RECORD_KINDS and RATED_KINDS among them).
SERIES_KINDS = kinds.where(series=True)
# Type-dependent kinds whose claim is a performance reading against the listed
# type: they need a registry to corroborate the type. The identity kinds
# publish when no registry lists the address, since the database is then the
# only record, but never against a disagreeing one.
PERFORMANCE_TYPE_KINDS = ENVELOPE_KINDS | RECORD_KINDS | RATED_KINDS
# Identity stories that name the listed type: the saved reading must be one the type can
# produce (gates' reading_implausible), or the story is about bad data or a wrong type.
PLAUSIBILITY_KINDS = frozenset(
    {"first_type_here", "watch_type", "vintage", "notable_variant", "foreign_military"}
)
# Pressure altitude allowed above the type envelope's ceiling before the reading is implausible.
CEILING_MARGIN_FT = 2000
# Selection and curation share one vintage story age.
VINTAGE_YEARS = 80
# Vintage airframes share one sub-topic per day, and an airframe with a vintage
# story waits 90 days for another identity story.
VINTAGE_COOLDOWN = 90 * 86400
# Vintage interest adds a point per year beyond the story age (up to 12) and a
# bonus for a type seen on at most N of the 60 coverage days before the story.
# A story then needs seven points: an unseen type at 80 years, one seen on 1-2
# days at 83, on 3-6 days at 85, and any type at 87. Calibrated to keep vintage
# near a quarter of the history curation instead of over half.
VINTAGE_AGE_POINTS = 12
VINTAGE_RARITY = ((0, 7), (2, 4), (6, 2))
# Later exceptional episodes of the same address and UTC date compete as ordinary ones.
EXCEPTIONAL_PER_AIRCRAFT = 2
# Why a finding is ordinary, in the order exception_decision() reports them.
EXCEPTION_REASONS = ("no_grade", "ineligible", "review", "crewed_7400", "jump_type", "repeated_descent")
# These observations can describe ordinary activity. They add
# context to an independently interesting finding, never qualify a story alone.
CONTEXT_KINDS = kinds.where(context_only=True)
# Glider VFR code: context for a soaring story, never a story alone.
CONTEXT_CODES = frozenset({"1202"})
SUSTAINED_FIELDS = (
    "sustained_rate_fpm",
    "sustained_duration_seconds",
    "sustained_sample_count",
    "sustained_altitude_change_ft",
    "sustained_expected_altitude_change_ft",
)
IDENTITY_KINDS = set(kinds.where(identity=True))
# Identity kinds about the airframe's type: an operator first is keyed by the operator,
# so a locally routine type never holds it.
TYPE_IDENTITY_KINDS = frozenset(IDENTITY_KINDS - {"first_operator_here"})
DATA_QUALITY_KINDS = kinds.where(data_quality=True)
# Orbit shapes whose interest falls to context near a runway.
CIRCUIT_KINDS = kinds.where(circuit_shape=True)
AIRPORT_TAGS = frozenset({"runway_circuit_context", "airport_descent_context"})
# A racetrack centered near a mapped runway is routine holding or approach practice.
RACETRACK_AIRPORT_TAGS = frozenset({"airport_context"})
# A balloon episode explains these same-encounter readings.
BALLOON_FOLDS = kinds.where(balloon_fold=True)
# Live status kinds by code or emergency field; priority declarations (kinds.PRIORITY_STATUSES)
# are not exceptions.
STATUS_EXCEPTIONS = kinds.where(status_exception=True)
BLOCKING_STATUS_TAGS = frozenset(
    {"transient_code_selection", "status_field_disagreement", "ground_only_status"}
)
PERSISTENT_STATUS_SECONDS = 60
PERSISTENT_STATUS_REPORTS = 4
# End reasons that mean the reports stopped, not that the code was changed.
STILL_SENDING = frozenset({"stale", "segment_break"})
EXCEPTIONAL_DERIVED_FPM = 6000
REPEATED_DESCENT_SECONDS = 6 * 3600
SAME_DESCENT_SECONDS = 600
EMERGENCY_PROFILE_FT = 25000
JUMP_TYPES = frozenset(
    {"DHC6", "C208", "BE9L", "BE99", "PC6T", "SC7", "L410", "AN2", "C182", "DHC2", "B190", "BE20"}
)
MAX_PARTICIPANTS = 12
ADDRESS = re.compile(r"~?[0-9a-f]{6}")
# Editorial weights, not probabilities or aircraft operating limits (each kind's weight and
# its reason are in kinds).
INTEREST = kinds.table("interest")
# One story ever per first-sighting kind and subtopic (type, allocation State, or operator).
FIRST_KINDS = kinds.where(first_sighting=True)


def topic(kind):
    if kind in IDENTITY_KINDS:
        return "aircraft"
    if kind in CIRCUIT_KINDS:
        return "circling"
    if kind in {"parallel_passes", "repeated_passes", "survey_grid"}:
        return "passes"
    if kind in {
        "rapid_descent",
        "rapid_climb",
        "vertical",
        "altitude_oscillation",
        "selected_altitude_overshoot",
    }:
        return "vertical-motion"
    if kind.startswith(SQUAWK_PREFIXES) or kind in {
        "special_use_code",
        "vfr_code_above_fl180",
    }:
        return "transponder"
    if kind.startswith("outlier_") or kind in DATA_QUALITY_KINDS:
        return "data-quality"
    if kind in {"fast", "high", "wind_boost", "seasonal_speed_record"} or kind in RATED_KINDS:
        return "performance"
    return kind


def _measurement(metrics, key):
    return number(metrics.get(key)) or 0


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _strings(value):
    return {item for item in value if isinstance(item, str)} if isinstance(value, list) else set()


def utc_day(stamp):
    """The UTC date of a timestamp, as YYYY-MM-DD."""
    return datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()


def absorbed(event):
    """A shape row a later circle or survey grid covers (the collector marks it)."""
    return bool(_mapping(_mapping(event.get("evidence")).get("absorbed_by")))


def context_tags(evidence):
    """Non-blocking annotations from the top-level, episode and pattern blocks."""
    evidence = _mapping(evidence)
    tags = set()
    for block in (evidence, evidence.get("maneuver"), evidence.get("pattern")):
        tags |= _strings(_mapping(block).get("context_tags"))
    return tags


def near_runway(kind, evidence, tags=None):
    """An orbit shape whose interest falls to routine context near a runway."""
    evidence = _mapping(evidence)
    tags = context_tags(evidence) if tags is None else tags
    if kind in CIRCUIT_KINDS:
        return bool(evidence.get("airport_context") or tags & AIRPORT_TAGS)
    return kind == "racetrack_orbit" and bool(
        evidence.get("airport_context") or tags & RACETRACK_AIRPORT_TAGS
    )


def _code(evidence):
    code = _mapping(_mapping(evidence).get("maneuver")).get("code")
    return code if isinstance(code, str) else None


# Aerobatic-style practice near an airport or over a runway circuit is context only:
# training circuits and approaches also reverse altitude often.
AEROBATIC_CONTEXT_TAGS = frozenset({"terminal_area_context", "runway_circuit_context"})


def context_only(kind, evidence=None):
    # Legacy scalar values are absolute. Only a saved negative barometric rate
    # establishes a descent; missing direction cannot bypass the climb policy.
    evidence = _mapping(evidence)
    rate = number(_mapping(evidence.get("aircraft")).get("baro_rate"))
    return (
        kind in CONTEXT_KINDS
        or (kind == "vertical" and (rate is None or rate >= 0))
        or (kind == "special_use_code" and _code(evidence) in CONTEXT_CODES)
        or (kind == "aerobatic_practice" and bool(context_tags(evidence) & AEROBATIC_CONTEXT_TAGS))
    )


def report_count(event):
    """(qualifying reports, seconds they span) from the confirmation window, else the row."""
    evidence = _mapping(event.get("evidence"))
    confirmation = _mapping(evidence.get("confirmation"))
    samples = confirmation.get("samples")
    reports = max(int(number(event.get("hits")) or 0), len(samples) if isinstance(samples, list) else 0)
    span = number(confirmation.get("duration_seconds"))
    if span is None:
        first, last = number(event.get("first_seen")), number(event.get("last_seen"))
        span = last - first if first is not None and last is not None else 0
    return reports, max(0, span)


# A second-source check still unsettled this long after the finding's first sighting
# no longer holds it: the check counts as unavailable (stale_check).
STALE_CHECK_SECONDS = 30 * 86400


def stale_check(event, now=None):
    """Whether a finding's unsettled second-source check has gone stale.

    A pending or failed check (enrichment_rules.type_check_state) on a finding
    first seen more than STALE_CHECK_SECONDS before `now` (default: the current
    time) is stale: it no longer holds selection and counts as unavailable. A
    typecheck.RECHECK_KINDS finding never goes stale, since a registry that never
    answered says nothing about whether it lists the address.
    """
    if event.get("kind") in typecheck.RECHECK_KINDS:
        return False
    first = number(event.get("first_seen"))
    if first is None or enrichment_rules.type_check_state(event.get("evidence")) not in {"pending", "failed"}:
        return False
    return (time.time() if now is None else now) - first > STALE_CHECK_SECONDS


def type_hold(event, *, now=None):
    """Why a type-dependent finding is not yet a story candidate, as (rule, reason), or None.

    A pending or failed second-source check holds selection until the worker's
    next answer, for at most STALE_CHECK_SECONDS after the finding's first
    sighting (stale_check). The hold is not a review flag and never
    unpublishes a released story: the publisher's next pass selects the finding
    once the check arrives. A disagreeing check, or no registry record for a
    performance kind, is a blocking gate instead (gates()).
    """
    kind = event["kind"]
    if kind not in TYPE_DEPENDENT_KINDS:
        return None
    state = enrichment_rules.type_check_state(event.get("evidence"))
    if state in {"pending", "failed"} and not stale_check(event, now):
        return (
            "type_check_pending",
            "The second-source type check has not completed; selection waits for it"
            + (
                " (the registries could not be asked; retried after the backoff)" if state == "failed" else ""
            ),
        )
    return None


def _direct_speed(aircraft):
    """Whether a saved aircraft's own direct ADS-B source supplied its ground speed.

    A snapshot names its source in "type"; an MLAT or TIS-B solution listed for
    "gs" is a position-derived speed, never evidence against the listed type.
    """
    return direct_value(aircraft, "gs") is not None


def _registry_gates(event, evidence):
    """registry_lists_address and registry_unchecked for an unlisted-address finding (gates)."""
    check = _mapping(evidence.get("type_check"))
    if check.get("source") is not None or check.get("registration") is not None:
        return [
            (
                "registry_lists_address",
                f"A second source ({check.get('source')}) lists this address"
                + (f" as {check['registration']}" if check.get("registration") else "")
                + ", so the finding's premise does not hold",
            )
        ]
    checked, first = number(check.get("checked_at")), number(event.get("first_seen"))
    if (
        enrichment_rules.type_check_state(evidence) not in SETTLED
        or checked is None
        or first is None
        or checked - first < typecheck.RECHECK_SECONDS
    ):
        return [
            (
                "registry_unchecked",
                f"The registries have not answered at least {typecheck.RECHECK_SECONDS // 86400} days "
                "after the first sighting; registries lag new registrations",
            )
        ]
    return []


def gates(event, *, now=None):
    """Blocking evidence rules for one finding, as (rule, reason) pairs.

    type_disagrees: a second-source registry lists another type, so a
    type-dependent premise is wrong (typecheck); a mismatch story names the
    registry's type instead (REGISTRY_TYPE_KINDS). type_uncorroborated: no
    registry lists the address, and a performance claim against the listed type
    needs corroboration. single_report: a spike, envelope, or rated finding with
    fewer than rules.MIN_REPORTS qualifying reports or spanning less than
    rules.MIN_REPORT_SECONDS (no one-report stories). rated_verdict: a rated-limit finding
    publishes only with the verdict "exceeded"; pending, unknown, and within
    readings stay private. rated_incomplete: an exceeded verdict whose reports over
    the limit, or whose airspeed's origin, were not kept (limits.comparison), so
    no story could state it. speed_implausible: a season record whose sustained
    ground speed the current plausibility gate (rules.speed_plausible) refuses for
    the listed type at its altitude; records saved under older rules, with a flat
    wind allowance and no rated-limit check, are judged again here.
    reading_implausible: an identity story about the listed type
    (PLAUSIBILITY_KINDS) whose saved aircraft reports a ground speed
    rules.speed_plausible refuses, when its own direct ADS-B source supplied
    that speed (_direct_speed), or a pressure altitude more than
    CEILING_MARGIN_FT above the type envelope's ceiling. Without a known
    envelope the envelope's cap and ceiling are skipped, while the absolute
    speed bound and a rated limit from reference.limits still apply; a missing
    reading is no evidence and no gate. registry_lists_address: an unlisted-address
    finding (typecheck.RECHECK_KINDS) whose second source named a registration or
    a type for the address. registry_unchecked: such a finding without a settled
    check made at least typecheck.RECHECK_SECONDS after its first sighting, since
    registries lag new registrations. A check still pending
    is a selection hold that resolves on its own: see type_hold(). One left
    unsettled for STALE_CHECK_SECONDS counts as unavailable at `now` (stale_check).
    """
    kind = event["kind"]
    evidence = _mapping(event.get("evidence"))
    found = []
    if kind in TYPE_DEPENDENT_KINDS:
        stale = stale_check(event, now)
        state = "unavailable" if stale else enrichment_rules.type_check_state(evidence)
        check = _mapping(evidence.get("type_check"))
        if state == "disagrees" and kind not in REGISTRY_TYPE_KINDS:
            found.append(
                (
                    "type_disagrees",
                    f"A second source lists a different type: {check.get('type_code')} ({check.get('source')})",
                )
            )
        elif state == "unavailable" and kind in PERFORMANCE_TYPE_KINDS:
            found.append(
                (
                    "type_uncorroborated",
                    f"The second-source type check did not complete within {STALE_CHECK_SECONDS // 86400} days; a performance claim needs corroboration"
                    if stale
                    else "No registry lists this address's type; a performance claim needs corroboration",
                )
            )
    if kind in typecheck.RECHECK_KINDS:
        found.extend(_registry_gates(event, evidence))
    if kind.startswith("outlier_") or kind in ENVELOPE_KINDS or kind in RATED_KINDS:
        reports, span = report_count(event)
        if reports < MIN_REPORTS or span < MIN_REPORT_SECONDS:
            found.append(
                (
                    "single_report",
                    f"Only {reports} qualifying reports over {span:,.0f} s; a story needs at least "
                    f"{MIN_REPORTS} reports spanning {MIN_REPORT_SECONDS} s",
                )
            )
    if kind in RATED_KINDS:
        verdict = _mapping(evidence.get("rated_limit")).get("verdict")
        if verdict != "exceeded":
            found.append(
                (
                    "rated_verdict",
                    f"The rated-limit verdict is {verdict or 'missing'}; only an exceeded verdict can publish",
                )
            )
        elif limits.comparison(kind, evidence) is None:
            # The story states this comparison; without it no story can be written.
            found.append(
                (
                    "rated_incomplete",
                    "The reports over the certified limit, or how their airspeed was obtained, were not kept",
                )
            )
    if kind in RECORD_KINDS and not speed_plausible(_mapping(evidence.get("aircraft")), event.get("value")):
        found.append(
            (
                "speed_implausible",
                "The sustained ground speed is beyond the listed type's envelope or certified limit "
                "plus the wind allowance at its altitude: bad data or a wrong type, not a record",
            )
        )
    if kind in PLAUSIBILITY_KINDS:
        aircraft = _mapping(evidence.get("aircraft"))
        gs, alt = number(aircraft.get("gs")), number(aircraft.get("alt_baro"))
        ceiling = number((type_envelope(aircraft) or {}).get("ceiling_ft"))
        if gs is not None and _direct_speed(aircraft) and not speed_plausible(aircraft, gs):
            found.append(
                (
                    "reading_implausible",
                    f"A ground speed of {gs:,.0f} kn is beyond the listed type's envelope: "
                    "bad data or a wrong type, not a sighting of that type",
                )
            )
        elif alt is not None and ceiling is not None and alt > ceiling + CEILING_MARGIN_FT:
            found.append(
                (
                    "reading_implausible",
                    f"A pressure altitude of {alt:,.0f} ft is above the listed type's ceiling of "
                    f"{ceiling:,.0f} ft: bad data or a wrong type, not a sighting of that type",
                )
            )
    return found


def _derived_descent(metrics):
    """The high-rate interval's own altitude change must also be steep."""
    duration = number(metrics.get("sustained_duration_seconds"))
    change = number(metrics.get("sustained_altitude_change_ft"))
    if not duration or duration <= 0 or change is None or change * 60 / duration > -EXCEPTIONAL_DERIVED_FPM:
        return False
    # The detector's rate collapses repeated identical reports. A proof without
    # it still carries the interval change and duration checked above.
    reported = number(metrics.get("sustained_derived_rate_fpm"))
    return reported is None or abs(reported) >= EXCEPTIONAL_DERIVED_FPM


def exceptional(kind, value, metrics):
    """An exception requires confirmed, coherent evidence, not an editorial flag.

    The same rule checks raw episodes, public packets and their small proofs, so
    a condition applies only when its field is present. Raw episodes always
    carry the fields; the proof then carries their verified result.
    """
    if metrics.get("confirmed") is not True or metrics.get("quality_review_required") is not False:
        return False
    if metrics.get("quality_flags"):
        return False
    if kind in STATUS_EXCEPTIONS:
        duration, samples = _measurement(metrics, "duration_seconds"), _measurement(metrics, "sample_count")
        if duration < 8 or samples < 2 or _strings(metrics.get("context_tags")) & BLOCKING_STATUS_TAGS:
            return False
        if "state" not in metrics:
            return True
        reports = number(metrics.get("distinct_report_count"))
        reports = samples if reports is None else reports
        # Either a persistent status, or one still being sent when reports ended:
        # an episode that ended only because reports stopped or its segment broke.
        return (
            (duration >= PERSISTENT_STATUS_SECONDS and reports >= PERSISTENT_STATUS_REPORTS)
            or metrics.get("state") == "active"
            or metrics.get("end_reason") in STILL_SENDING
        )
    rate = number(value)
    sustained = _measurement(metrics, "sustained_rate_fpm")
    duration = _measurement(metrics, "sustained_duration_seconds")
    expected = number(metrics.get("sustained_expected_altitude_change_ft"))
    return bool(
        kind == "rapid_descent"
        and rate is not None
        and -12000 <= rate <= sustained <= -HIGH_RATE_FPM
        and 30 <= duration <= _measurement(metrics, "duration_seconds")
        and 4 <= _measurement(metrics, "sustained_sample_count") <= _measurement(metrics, "sample_count")
        and expected is not None
        # The detector rounds integrated altitude change to a tenth of a foot.
        and rate * duration / 60 - 0.1 <= expected <= sustained * duration / 60 + 0.1
        and altitude_agrees(
            number(metrics.get("sustained_altitude_change_ft")),
            expected,
            -1,
        )
        and _derived_descent(metrics)
    )


def uncrewed(aircraft):
    """Uncrewed metadata: emitter category B6 or the reference type class."""
    aircraft = _mapping(aircraft)
    return aircraft.get("category") == "B6" or reference.type_class(aircraft.get("t")) == "uav"


def _start(event):
    start = number(_mapping(_mapping(event.get("evidence")).get("maneuver")).get("started_at"))
    return number(event.get("first_seen")) if start is None else start


def descent_starts(groups):
    """Every saved descent episode per address, including uncertain ones."""
    starts = defaultdict(list)
    for group in groups:
        for event in group["events"]:
            if event.get("kind") == "rapid_descent" and (start := _start(event)) is not None:
                starts[event["hex"]].append(start)
    return {address: sorted(times) for address, times in starts.items()}


def repeated_descent(event, descents):
    """Another descent cycle by the same address within six hours, such as a jump plane."""
    start = _start(event)
    return start is not None and any(
        SAME_DESCENT_SECONDS <= abs(other - start) <= REPEATED_DESCENT_SECONDS
        for other in (descents or {}).get(event.get("hex"), ())
    )


def _ordinary_reason(event, descents=None):
    """Why one saved finding is not exceptional on its evidence and context, or None when it is."""
    kind = event["kind"]
    evidence = _mapping(event.get("evidence"))
    metrics = _mapping(evidence.get("maneuver"))
    if not exceptional(kind, event.get("value"), metrics):
        return "no_grade"
    aircraft = _mapping(evidence.get("aircraft"))
    if kind == "special_squawk_7400":
        # A crewed aircraft's lost-link code is exceptional only when it moved to an emergency code.
        sequence = metrics.get("code_sequence")
        codes = {_mapping(item).get("code") for item in sequence} if isinstance(sequence, list) else set()
        return None if uncrewed(aircraft) or codes & {"7500", "7600", "7700"} else "crewed_7400"
    if kind != "rapid_descent":
        return None
    tags = context_tags(evidence)
    if aircraft.get("t") in JUMP_TYPES and "airport_descent_context" in tags:
        return "jump_type"
    start = number(metrics.get("start_altitude_ft"))
    emergency = "emergency_descent_profile" in tags and (start is None or start >= EMERGENCY_PROFILE_FT)
    return None if emergency or not repeated_descent(event, descents) else "repeated_descent"


def exceptional_event(event, descents=None):
    """Exceptional status of one saved finding, before the per-aircraft daily cap."""
    return _ordinary_reason(event, descents) is None


def exception_decision(event, descents=None, *, eligible=True, review_required=False):
    """The selection's exceptional decision for one finding, before the per-aircraft daily cap.

    `grade` is whether the evidence passes exceptional(); `reason` says why the finding
    is ordinary, the first of EXCEPTION_REASONS that applies, or None when it is exceptional.
    """
    reason = _ordinary_reason(event, descents)
    if reason != "no_grade" and not eligible:
        reason = "ineligible"
    elif reason != "no_grade" and review_required:
        reason = "review"
    return {"grade": reason != "no_grade", "exceptional": reason is None, "reason": reason}


def selected_exception(event, row, descents=None):
    """The stored decision for one selected row: exception_decision() with the row's final state.

    The per-aircraft cap demotes a row (`aircraft_cap`); a live story's own finding keeps
    its exceptional standing (no reason). The row's `exceptional` is always the result.
    """
    selection_decision = _mapping(event.get("selection"))
    decision = exception_decision(
        event,
        descents,
        eligible=bool(row["eligible"]),
        review_required=bool(selection_decision.get("review_required")),
    )
    if bool(row["exceptional"]) != decision["exceptional"]:
        decision.update(
            exceptional=bool(row["exceptional"]), reason=None if row["exceptional"] else "aircraft_cap"
        )
    return {**decision, "curation_version": VERSION}


def priority_evidence(kind, value, metrics):
    """The small public evidence record needed to validate a daily-limit exception."""
    if not exceptional(kind, value, metrics):
        return None
    proof = {
        "confirmed": True,
        "quality_review_required": False,
        "duration_seconds": metrics["duration_seconds"],
        "sample_count": metrics["sample_count"],
        "value": number(value),
    }
    if kind == "rapid_descent":
        proof.update({key: metrics[key] for key in SUSTAINED_FIELDS})
    return proof


def participants(event):
    """Other aircraft addresses named by a multi-aircraft finding."""
    evidence = _mapping(event.get("evidence"))
    found = set()
    for block in (evidence, evidence.get("maneuver")):
        items = _mapping(block).get("participants")
        for item in items if isinstance(items, list) else ():
            address = _mapping(item).get("hex")
            if isinstance(address, str) and ADDRESS.fullmatch(address.lower()):
                found.add(address.lower())
    found.discard(str(event.get("hex", "")).lower())
    return sorted(found)[:MAX_PARTICIPANTS]


def facts(event):
    """Repeat-policy facts: addresses, type, variant, the identity sub-topic and the story series."""
    evidence = _mapping(event.get("evidence"))
    aircraft = _mapping(evidence.get("aircraft"))
    kind = event["kind"]
    typecode = aircraft.get("t") if isinstance(aircraft.get("t"), str) and aircraft.get("t") else None
    variant = None
    if kind == "notable_variant":
        # One label covers several database descriptions of the same variant.
        raw = evidence.get("variant")
        raw = (_mapping(raw).get("label") or _mapping(raw).get("name")) if isinstance(raw, dict) else raw
        variant = raw if isinstance(raw, str) and raw else aircraft.get("desc")
        variant = variant if isinstance(variant, str) and variant else None
    country = None
    if kind == "first_country_here":
        raw = (
            _mapping(evidence.get("first_sighting")).get("country")
            or evidence.get("country")
            or _mapping(evidence.get("identity_checks")).get("allocation_country")
        )
        country = raw if isinstance(raw, str) and raw else None
    address = str(event.get("hex", "")).lower()
    subtopic = None
    if kind == "vintage":
        # Old airframes of different types still read as one kind of story.
        subtopic = "vintage"
    elif kind == "first_operator_here":
        # One first per operator, whatever type flies it.
        raw = _mapping(evidence.get("first_sighting")).get("designator") or callsign_designator(aircraft)
        subtopic = "operator:" + raw if isinstance(raw, str) and raw else "aircraft:" + address
    elif kind in IDENTITY_KINDS:
        subtopic = (
            "variant:" + variant
            if variant
            else "country:" + country
            if country
            else "type:" + typecode
            if typecode
            else "aircraft:" + address
        )
    series = kind if kind in SERIES_KINDS else None
    return {
        "addresses": sorted({address, *participants(event)}),
        "type": typecode,
        "variant": variant,
        "subtopic": subtopic,
        "series": series,
    }


def story_key(event, episode):
    """One phenomenon is one story, whichever participant's encounter holds it."""
    others = participants(event)
    if not others:
        return episode
    start = _start(event)
    return f"{event['kind']}:{'+'.join(sorted({str(event['hex']).lower(), *others}))}:{start:.6f}"


def type_day_counts(presence, day):
    """Distinct coverage days per type within the 60 coverage days before `day`."""
    coverage = presence.get("coverage", [])
    end = bisect_left(coverage, day)
    window = set(coverage[max(0, end - ROUTINE_WINDOW) : end])
    counts = {}
    for typecode, days in presence.get("types", {}).items():
        if seen := len(window.intersection(days)):
            counts[typecode] = seen
    return {"coverage_days": len(window), "type_days": counts}


def routine_types(counts):
    """Locally routine types from precomputed type-day counts."""
    return frozenset(
        typecode
        for typecode, days in _mapping(_mapping(counts).get("type_days")).items()
        if days >= ROUTINE_DAYS
    )


def _identity_pair(row, other, routine):
    return (row["kind"] in IDENTITY_KINDS and other["kind"] in IDENTITY_KINDS) or (
        row["kind"] == other["kind"] == "identity_conflict"
    )


def kind_family(kind):
    """Kinds that tell the same story about one airframe: every transponder code is one family."""
    return "squawk" if kind.startswith(SQUAWK_PREFIXES) else kind


def _kind_pair(row, other, routine):
    # Identity kinds keep their own cooldowns above and below.
    return row["kind"] not in IDENTITY_KINDS and kind_family(row["kind"]) == kind_family(other["kind"])


def _vintage_pair(row, other, routine):
    return other["kind"] in IDENTITY_KINDS and "vintage" in (row["kind"], other["kind"])


def _routine_pair(row, other, routine):
    # A type routine on either story's own date holds both, so earlier and later
    # stories agree whichever is checked first.
    return other["kind"] in TYPE_IDENTITY_KINDS and (row["type"] in routine or other.get("routine_type"))


# The repeat policy, checked in order: (reason, index, row kinds it applies to (None
# for all), whether another story matches, and the window in seconds (None: any time)).
# The airframe rules come before the series rule, so a same-airframe repeat of a series kind
# (SERIES_KINDS) within 30 days is held as identity_repeat (identity_conflict) or as
# kind_repeat (seasonal_speed_record and the certified-limit kinds, RATED_KINDS), not as
# series_repeat.
REPEAT_RULES = (
    ("same_aircraft_day", "address", None, lambda row, other, routine: other["day"] == row["day"], None),
    ("identity_repeat", "address", None, _identity_pair, IDENTITY_COOLDOWN),
    ("kind_repeat", "address", None, _kind_pair, STORY_COOLDOWN),
    ("vintage_repeat", "address", IDENTITY_KINDS, _vintage_pair, VINTAGE_COOLDOWN),
    ("routine_type", "type", TYPE_IDENTITY_KINDS, _routine_pair, ROUTINE_COOLDOWN),
    (
        "type_repeat",
        "type",
        {"watch_type"},
        lambda row, other, routine: other["kind"] == "watch_type",
        TYPE_COOLDOWN,
    ),
    (
        "variant_repeat",
        "variant",
        {"notable_variant"},
        lambda row, other, routine: other["kind"] == "notable_variant",
        VARIANT_COOLDOWN,
    ),
    # A second "first" of one type, allocation State, or operator would be false.
    ("first_repeat", "subtopic", FIRST_KINDS, lambda row, other, routine: other["kind"] == row["kind"], None),
    ("series_repeat", "series", None, lambda row, other, routine: True, SERIES_COOLDOWN),
)


def _days(seconds):
    return f"{seconds // 86400} days"


REPEAT_REASONS = {
    "same_aircraft_day": "This aircraft already has a story on this UTC date",
    "identity_repeat": f"This aircraft had an identity story within {_days(IDENTITY_COOLDOWN)}",
    "kind_repeat": f"This aircraft had a story of this kind within {_days(STORY_COOLDOWN)}",
    "type_repeat": f"This aircraft type had a type-match story within {_days(TYPE_COOLDOWN)}",
    "routine_type": f"This locally routine type had an identity story within {_days(ROUTINE_COOLDOWN)}",
    "variant_repeat": f"This aircraft variant had a story within {_days(VARIANT_COOLDOWN)}",
    "vintage_repeat": f"This vintage airframe had an identity story within {_days(VINTAGE_COOLDOWN)}",
    "first_repeat": "A first sighting of this type, allocation State, or operator already had its story",
    "series_repeat": f"A story that reads the same way on any aircraft ran within {_days(SERIES_COOLDOWN)}",
}


# Single-valued repeat-policy facts that StoryLog indexes beside the addresses.
FACT_INDEXES = ("type", "variant", "subtopic", "series")


class StoryLog:
    """Prior stories for the shared repeat policy, at exact episode-start times."""

    def __init__(self, entries=()):
        self.indexes = {name: defaultdict(list) for name in ("address", *FACT_INDEXES)}
        for entry in entries:
            self.add(entry)

    @property
    def by_address(self):
        return self.indexes["address"]

    def add(self, entry):
        # An in-place withdrawal retracts the story; it never holds later candidates.
        if entry.get("withdrawn"):
            return
        for name, keys in self._keys(entry).items():
            for key in keys:
                self.indexes[name][key].append(entry)

    @staticmethod
    def _keys(entry):
        return {
            "address": entry["addresses"],
            **{name: [entry[name]] if entry.get(name) else [] for name in FACT_INDEXES},
        }

    def repeat(self, row, routine=frozenset()):
        """Why an ordinary candidate repeats an earlier or later story, or None."""
        keys = self._keys(row)
        for reason, index, rule_kinds, matches, window in REPEAT_RULES:
            if rule_kinds is not None and row["kind"] not in rule_kinds:
                continue
            for key in keys[index]:
                for other in self.indexes[index].get(key, ()):
                    if (
                        (other.get("id") is None or other["id"] != row.get("id"))
                        and (window is None or abs(row["start"] - other["start"]) < window)
                        and matches(row, other, routine)
                    ):
                        return reason
        return None

    def novelty(self, row):
        """Zero for the same aircraft and topic within seven days; eight for another topic."""
        score = 15
        for address in row["addresses"]:
            for other in self.by_address.get(address, ()):
                if other.get("group_key") is not None and other.get("group_key") == row.get("episode_key"):
                    continue
                if abs(row["start"] - other["start"]) < TOPIC_COOLDOWN:
                    if topic(other["kind"]) == topic(row["kind"]):
                        return 0
                    score = 8
        return score


class Slot(NamedTuple):
    """The UTC date and addresses a story's current projection would occupy."""

    day: str
    addresses: frozenset


class LiveSlot(NamedTuple):
    """The date and addresses a live article holds, and whether it was released as exceptional."""

    day: str
    addresses: frozenset
    published_standing: bool


class Claim(NamedTuple):
    """One story asking for a publication slot.

    `current` is None for a record that no longer projects, and `live` is the slot
    its live article holds, if any: the publisher passes the same LiveSlot it seeded
    the quota with. `standing` is the story's exceptional standing, which orders the
    claims and, while the story holds no exceptional slot, decides its priority;
    `sticky_standing` is the standing it keeps while it holds one (read only then).
    `rank` is the curation rank and `start_ts` the episode start.
    """

    id: str
    current: Slot | None
    live: LiveSlot | None = None
    standing: bool = False
    sticky_standing: bool = False
    rank: int | None = None
    start_ts: float | None = None

    @property
    def was_ordinary(self):
        """The live article counts toward its date's ordinary stories."""
        return self.live is not None and not self.live.published_standing


class Decision(NamedTuple):
    """Whether a story may take its slot, and with exceptional priority.

    `reason` is "daily_limit" for a refused story, and "aircraft_cap" for one that
    stands as exceptional but competes as ordinary because its aircraft's exceptional
    slots for the date are full.
    """

    admit: bool
    priority: bool
    reason: str | None


def _held(slot):
    return [(address, slot.day) for address in slot.addresses]


class DayQuota:
    """Publication slots per UTC date: MAX_PER_DAY stories, and exceptional ones beyond it.

    Seeded from the live articles only, {record id: LiveSlot} in the order given (the
    publisher passes them by observation start): each counts toward its date's total,
    an article released as ordinary toward the ordinary count, and one released as
    exceptional holds an exceptional slot for each of its addresses and the date, up
    to EXCEPTIONAL_PER_AIRCRAFT. A live exception over that cap is `displaced`: it
    holds no exceptional slot and gives way first. A new story checks the date's
    total and a live one on the same date the ordinary count, so a live story is
    never displaced by the exceptions published beside it. The caller applies each
    decision and reports the outcome through commit(), release(), or
    hold_conservatively().
    """

    def __init__(self, live_slots):
        self.counts, self.ordinary, self.exceptions = Counter(), Counter(), Counter()
        self.granted, self.displaced = set(), set()
        for key, slot in live_slots.items():
            self.counts[slot.day] += 1
            if not slot.published_standing:
                self.ordinary[slot.day] += 1
            elif all(self.exceptions[held] < EXCEPTIONAL_PER_AIRCRAFT for held in _held(slot)):
                self.exceptions.update(_held(slot))
                self.granted.add(key)
            else:
                # Like a corrected exception, a story over the per-aircraft cap gives way first.
                self.displaced.add(key)

    @staticmethod
    def _order_key(claim):
        if claim.current is None:
            return (2, 0, False, 0, 0, claim.id)
        live = claim.live is not None
        rank = claim.rank
        # Within a date, an over-full date gives up its worst-ranked live story
        # first, and scarce new slots follow the curation rank.
        return (
            not claim.standing,
            -date.fromisoformat(claim.current.day).toordinal(),
            not live,
            (-rank if rank is not None else 0) if live else (rank if rank is not None else float("inf")),
            -claim.start_ts,
            claim.id,
        )

    def order(self, claims):
        """Exceptional first, newest date first, live before new, then by rank and start."""
        return sorted(claims, key=self._order_key)

    def decide(self, claim):
        """Whether a projectable story may take its current slot.

        A live exception whose date was corrected first gives up the exceptional slot it
        held on the old date, so it takes its own place under the per-aircraft cap.
        """
        day = claim.current.day
        previous_day = claim.live.day if claim.live is not None else day
        if claim.id in self.granted and previous_day != day:
            self.granted.discard(claim.id)
            self.exceptions.subtract(_held(claim.live))
        granted = claim.id in self.granted
        standing = claim.sticky_standing if granted else claim.standing
        room = granted or all(
            self.exceptions[held] < EXCEPTIONAL_PER_AIRCRAFT for held in _held(claim.current)
        )
        priority = standing and room
        overflow = not priority and (
            self.ordinary[day] - claim.was_ordinary >= MAX_PER_DAY
            if claim.live is not None and previous_day == day
            else self.counts[day] >= MAX_PER_DAY
        )
        reason = "daily_limit" if overflow else "aircraft_cap" if standing and not room else None
        return Decision(not overflow, priority, reason)

    def commit(self, claim, decision):
        """The story is live in its current slot, with the decision's priority."""
        day, live = claim.current.day, claim.live
        previous_day = live.day if live is not None else day
        if live is None or previous_day != day:
            self.counts[day] += 1
            if live is not None:
                self.counts[previous_day] -= 1
        self.ordinary[previous_day] -= claim.was_ordinary
        self.ordinary[day] += not decision.priority
        if claim.id in self.granted and not decision.priority:
            self.granted.discard(claim.id)
            self.exceptions.subtract(_held(live))
        elif decision.priority and claim.id not in self.granted:
            self.granted.add(claim.id)
            self.exceptions.update(_held(claim.current))

    def release(self, claim):
        """The story's live article was suspended; its previous slot is free."""
        live = claim.live
        if live is None:
            return
        self.counts[live.day] -= 1
        self.ordinary[live.day] -= claim.was_ordinary
        if claim.id in self.granted:
            self.granted.discard(claim.id)
            self.exceptions.subtract(_held(live))

    def hold_conservatively(self, claim):
        """A displaced live story whose write failed counts as ordinary for the rest of the pass."""
        if claim.id in self.displaced and claim.live is not None:
            self.ordinary[claim.live.day] += 1


def vintage_interest(age, type_days=None):
    """Rank vintage airframes by age and by how rarely their type is seen here.

    `type_days` counts the type's days among the last 60 coverage days; None
    means no local counts, which never lowers the rank.
    """
    if age is None or age < VINTAGE_YEARS:
        return INTEREST["vintage"]
    points = min(VINTAGE_AGE_POINTS, int(age) - VINTAGE_YEARS)
    rarity = next((bonus for days, bonus in VINTAGE_RARITY if type_days is None or type_days <= days), 0)
    return INTEREST["vintage"] + points + rarity


def rate(event, *, descents=None, rarity=None):
    """Score one finding using its existing factual and editorial assessment.

    `rarity` is the type's recent coverage-day count, used to rank vintage airframes.
    """
    evidence, decision = event["evidence"], event["selection"]
    kind = event["kind"]
    maneuver = evidence.get("maneuver") or {}
    pattern = evidence.get("pattern") or {}
    metrics = maneuver or pattern
    confirmed = maneuver.get("confirmed") is True if maneuver else pattern.get("shape_verified") is True
    flags = _strings(metrics.get("quality_flags")) | _strings(evidence.get("quality_flags"))
    tags = context_tags(evidence)
    # Archives can nominate an exceptional review candidate without pretending
    # that repeated labels or unknown field freshness confirm an aircraft event.
    coherent = (
        confirmed
        and not (flags - {"archive_field_freshness_unknown"})
        and (metrics.get("quality_review_required") is False or flags == {"archive_field_freshness_unknown"})
    )
    loops = _measurement(metrics, "closed_loops")
    passes = _measurement(metrics, "complete_passes")
    duration = _measurement(metrics, "duration_seconds")
    interest = INTEREST.get(kind, 28 if kind.startswith("outlier_") else 0)
    if coherent and kind in {"rapid_descent", "rapid_climb"}:
        interest += min(5, int(max(0, abs(number(event.get("value")) or 0) - 4500) / 1500))
    if kind == "rapid_descent" and "emergency_descent_profile" in tags:
        interest += 5
    if kind.startswith(SQUAWK_PREFIXES):
        interest = 45
    elif kind == "special_use_code" and _code(evidence) in CONTEXT_CODES:
        interest = 8
    if (kind.startswith(SQUAWK_PREFIXES) or kind == "special_use_code") and (tags & BLOCKING_STATUS_TAGS):
        # A transient, ground-only or contradicted code does not lead the day.
        interest = 8
    elif kind == "vintage":
        interest = vintage_interest(number(event.get("value")), rarity)
    elif topic(kind) in {"circling", "passes"}:
        interest += min(12, max(0, int(max(loops, passes) - 3) * 2))
    if near_runway(kind, evidence, tags):
        interest = min(interest, 8)
    if event.get("verdict") == "keep":
        interest = max(40, interest)
    interest = min(45, interest)
    exceptional_candidate = coherent and (
        kind in STATUS_EXCEPTIONS
        or (
            kind in {"rapid_descent", "rapid_climb"}
            and abs(number(event.get("value")) or 0) >= 7000
            and duration >= 30
            and _measurement(metrics, "sample_count") >= 4
        )
    )
    # assess() evaluated the type-check hold with the gates.
    hold = decision.get("publication_hold")
    eligible = bool(decision["story_eligible"]) and hold is None
    evidence_points = (20 if kind in IDENTITY_KINDS else 30) if eligible else 10
    if decision["review_required"]:
        evidence_points = min(evidence_points, 10)
    depth = (
        10
        if (
            (confirmed and duration >= 30 and _measurement(metrics, "sample_count") >= 4)
            or (kind.startswith("outlier_") and eligible and not decision["review_required"])
        )
        else 5
        if evidence.get("aircraft", {}).get("t")
        else 0
    )
    return {
        "finding_id": event["id"],
        "kind": kind,
        "topic": topic(kind),
        "interest": interest,
        "evidence": evidence_points,
        "depth": depth,
        "eligible": eligible,
        "exceptional": bool(
            eligible and not decision["review_required"] and exceptional_event(event, descents)
        ),
        "priority_review": bool(
            not eligible
            and not context_only(kind, evidence)
            and event.get("verdict") not in {"routine", "bad_data"}
            and coherent
            and (exceptional_candidate or interest >= 34)
        ),
        "quality_flags": sorted(flags),
        "context_only": context_only(kind, evidence),
        "hold": hold,
        "reasons": list(decision["reasons"]),
        **facts(event),
    }


def _order(candidate):
    rarity = candidate.get("rarity")
    return (
        -candidate["exceptional"],
        -candidate.get("pinned", False),
        -candidate.get("adjusted_score", candidate["score"]),
        -candidate["interest"],
        -candidate["evidence"],
        # Ties go to the locally rarer type.
        float("inf") if rarity is None else rarity,
        candidate["first_seen"],
        candidate["episode_key"],
        str(candidate["finding_id"]),
    )


def _penalty_key(row):
    # Distinct aircraft types or variants do not compete for one identity slot.
    return row["subtopic"] if row["kind"] in IDENTITY_KINDS and row.get("subtopic") else topic(row["kind"])


def episode_key(group):
    """Use the collector's encounter boundary even when early rows expire."""
    start = group.get("start")
    if start is None:
        start = group["first_seen"]
    return str(group.get("id") or f"{group['events'][0]['hex']}:{start:.6f}")


def _novelty(novelty, day):
    return novelty(day) if novelty else None


def _rarity(group):
    """First sightings and private rarity leads; a lower count is rarer."""
    if any(event["kind"] == "first_type_here" for event in group["events"]):
        return 0
    counts = [
        number(_mapping(_mapping(event.get("evidence")).get("rarity")).get("type_days"))
        for event in group["events"]
        if event["kind"] == "rare_here"
    ]
    counts = [value for value in counts if value is not None]
    return min(counts) if counts else None


def story_entry(row):
    """A selected row as a prior story for later repeat checks."""
    return {
        "id": None,
        "episode_key": row["story_key"],
        "addresses": list(row["addresses"]),
        "kind": row["kind"],
        "start": row["start"],
        "day": row["day"],
        "type": row["type"],
        "variant": row["variant"],
        "subtopic": row["subtopic"],
        "series": row["series"],
        "exceptional": row["exceptional"],
        "routine_type": row["routine_type"],
        "live": True,
    }


def _score(row):
    return row["interest"] + row["evidence"] + row["depth"] + row["novelty"]


def _adjust(row, topics):
    """The diversity penalty: eight points per earlier story of this topic that day."""
    row["diversity_penalty"] = 0 if row["exceptional"] or row["pinned"] else 8 * topics[_penalty_key(row)]
    row["adjusted_score"] = row["score"] - row["diversity_penalty"]


def _rate_group(key, day, first, group, *, counts, routine, descents, removed, pinned):
    """Rated rows for every finding of one observation episode."""
    events = group["events"]
    type_days = _mapping(_mapping(counts).get("type_days"))
    # Collection passes only eligible findings; it flags a balloon under review.
    folded = group.get("balloon") or any(event["kind"] == "stratospheric_balloon" for event in events)
    rarity = _rarity(group)
    common = {
        "episode_key": key,
        "day": day,
        "first_seen": first,
        "start": first,
        "last_seen": group["last_seen"],
        "hex": events[0]["hex"],
    }
    rated = []
    for event in events:
        typecode = facts(event)["type"]
        type_rarity = rarity if rarity == 0 or counts is None or not typecode else type_days.get(typecode, 0)
        row = {**common, **rate(event, descents=descents, rarity=type_rarity)}
        source = (event["hex"], event["kind"], event["first_seen"])
        row["owner_removed"] = source in removed
        row["pinned"] = source in pinned.get(key, {})
        if (
            row["pinned"]
            and pinned[key][source]
            and row["eligible"]
            and not event["selection"]["review_required"]
            and exceptional_event(event)
        ):
            # A published exception keeps its standing while its own evidence holds.
            row["exceptional"] = True
        row["story_key"] = story_key(event, key)
        row["routine_type"] = bool(row["type"] in routine and row["kind"] in TYPE_IDENTITY_KINDS)
        row["rarity"] = type_rarity
        if folded and row["kind"] in BALLOON_FOLDS:
            row["context_only"] = True
            row["reasons"].append("Part of the balloon episode; not a separate story")
        row["novelty"] = 15
        row["score"] = _score(row)
        rated.append(row)
    return rated


def _available(row, log, routine):
    """Give the row its disposition; whether it can still be selected."""
    row.update(novelty=15, diversity_penalty=0)
    if row["owner_removed"]:
        row["disposition"] = "owner_removed"
        row["reasons"].append("The owner removed the story about this finding")
    elif row["context_only"]:
        row["disposition"] = "context_only"
        row["reasons"].append(
            "This observation needs another independently qualifying finding in this episode"
        )
    elif row.get("hold"):
        # Waits for the second-source type check; the next pass selects it unattended.
        row["disposition"] = row["hold"]
    elif not row["eligible"]:
        row["disposition"] = "needs_review" if row["priority_review"] else "not_story_eligible"
    elif not row["exceptional"] and not row["pinned"] and (reason := log.repeat(row, routine)):
        row["disposition"] = reason
        row["reasons"].append(REPEAT_REASONS[reason])
    else:
        row["disposition"] = "below_threshold"
        row["ordinary_novelty"] = log.novelty(row)
        if not row["exceptional"]:
            row["novelty"] = row["ordinary_novelty"]
        row["score"] = _score(row)
        return True
    return False


def _select_day(available, day_held, log, routine, selected):
    """Pick one UTC date's stories from its available rows, in rank order."""
    topics = Counter(_penalty_key(entry) for entry in day_held)
    aircraft = {address for entry in day_held for address in entry["addresses"]}
    exceptions = Counter(
        address for entry in day_held if entry.get("exceptional") for address in entry["addresses"]
    )
    slots = len(day_held)
    while available:
        for row in available:
            if (
                row["exceptional"]
                and not row["pinned"]
                and any(exceptions[address] >= EXCEPTIONAL_PER_AIRCRAFT for address in row["addresses"])
            ):
                row["exceptional"] = False
                row["novelty"] = row["ordinary_novelty"]
                row["score"] = _score(row)
                row["reasons"].append("Two exceptional stories already cover this aircraft and date")
            _adjust(row, topics)
        choices = [
            row
            for row in available
            if row["pinned"]
            or row["exceptional"]
            or (
                not aircraft.intersection(row["addresses"])
                and row["adjusted_score"] >= MIN_SCORE
                and slots < MAX_PER_DAY
                and not log.repeat(row, routine)
            )
        ]
        if not choices:
            break
        winner = min(choices, key=_order)
        available.remove(winner)
        winner.update(disposition="selected", rank=1 + slots)
        selected.append(winner)
        remaining = []
        for other in available:
            if other["episode_key"] == winner["episode_key"] or other["story_key"] == winner["story_key"]:
                other["disposition"] = "same_episode"
            else:
                remaining.append(other)
        available = remaining
        topics[_penalty_key(winner)] += 1
        slots += 1
        aircraft.update(winner["addresses"])
        if winner["exceptional"]:
            exceptions.update(winner["addresses"])
        log.add(story_entry(winner))
    for row in available:
        _adjust(row, topics)
        reason = None if row["exceptional"] or row["pinned"] else log.repeat(row, routine)
        if reason:
            row["disposition"] = reason
            row["reasons"].append(REPEAT_REASONS[reason])
        elif aircraft.intersection(row["addresses"]):
            row["disposition"] = "same_aircraft_day"
        else:
            row["disposition"] = "daily_limit" if row["adjusted_score"] >= MIN_SCORE else "below_threshold"


def choose(groups, *, audit=True, history=(), novelty=None, descents=None):
    """Rank complete observation days, with explanatory decisions for each finding.

    Callers supply existing per-finding selection decisions. No input is mutated.
    `history` holds prior stories from the publication ledger (see autopilot), so
    a held repeat never takes a slot; a live story's own finding keeps its slot.
    `novelty` is a function of the UTC date returning its type-day counts.
    Exceptional findings bypass repetition/diversity penalties, never the evidence
    checks, and at most two per address and UTC date stay exceptional. The daily
    target limits ordinary selections; exceptional episodes can take it above nine.
    A shape row a circle or survey grid absorbed never competes.
    """
    days = defaultdict(list)
    priority_review = []
    seen = set()
    for group in groups:
        events = [event for event in group["events"] if not absorbed(event)]
        if not events:
            continue
        first = number(group["first_seen"])
        if first is None:
            raise ValueError("An observation start is required")
        key = episode_key(group)
        if key in seen:
            raise ValueError("Duplicate observation episode")
        seen.add(key)
        days[utc_day(first)].append((key, first, {**group, "events": events}))
    descents = descent_starts(groups) if descents is None else descents
    group_days = {key: day for day, items in days.items() for key, _, _ in items}
    log, held, pinned = StoryLog(), defaultdict(list), defaultdict(dict)
    pinned_entries = defaultdict(list)
    # The owner removed or withdrew these stories; their own findings never return as stories.
    removed = {
        tuple(entry["source"])
        for entry in history
        if (entry.get("removed") or entry.get("withdrawn")) and entry.get("source")
    }
    for entry in history:
        if "routine_type" not in entry and entry.get("kind") in TYPE_IDENTITY_KINDS and entry.get("type"):
            # A prior story without its saved flag gets the routine status of its own date.
            entry = {**entry, "routine_type": entry["type"] in routine_types(_novelty(novelty, entry["day"]))}
        # Every prior story constrains repeats on earlier and later days alike.
        log.add(entry)
        if not entry.get("live"):
            continue
        if entry.get("group_key") in group_days and entry.get("source"):
            # A live story's own finding keeps its slot and primary reason; only an
            # exceptional finding in the same encounter can replace it.
            pinned[entry["group_key"]][tuple(entry["source"])] = bool(entry.get("exceptional"))
            pinned_entries[group_days[entry["group_key"]]].append(entry)
        else:
            held[entry["day"]].append(entry)

    decisions, selected = [], []
    for day, episode_groups in sorted(days.items()):
        counts = _novelty(novelty, day)
        routine = routine_types(counts)
        candidates = []
        for key, first, group in episode_groups:
            rated = _rate_group(
                key,
                day,
                first,
                group,
                counts=counts,
                routine=routine,
                descents=descents,
                removed=removed,
                pinned=pinned,
            )
            if audit:
                review = [row for row in rated if row["priority_review"]]
                if review:
                    priority_review.append(min(review, key=_order))
            candidates.extend(rated)
        available = []
        for row in candidates:
            if _available(row, log, routine):
                available.append(row)
            if audit:
                decisions.append(row)
        # A live story whose own finding is unavailable here still holds its slot.
        live_rows = {row["episode_key"] for row in available if row["pinned"]}
        day_held = held[day] + [entry for entry in pinned_entries[day] if entry["group_key"] not in live_rows]
        _select_day(available, day_held, log, routine, selected)
    return {
        "version": VERSION,
        "max_per_day": MAX_PER_DAY,
        "minimum_score": MIN_SCORE,
        "weights": {"interest": 45, "evidence": 30, "novelty": 15, "depth": 10},
        "selected": selected,
        "decisions": sorted(
            decisions, key=lambda row: (row["day"], row["episode_key"], str(row["finding_id"]))
        ),
        "priority_review": sorted(priority_review, key=_order),
    }


class DailyLimitError(ValueError):
    """A proposed publication exceeds the user's daily event limit."""


def validate_daily_limit(events):
    """At most nine ordinary events per UTC date; supported exceptions survive."""
    if any(
        count > MAX_PER_DAY
        for count in Counter(
            event["observed_start"][:10]
            for event in events
            if not exceptional(
                event.get("kind"),
                event.get("priority_evidence", {}).get("value"),
                event.get("priority_evidence", {}),
            )
        ).values()
    ):
        raise DailyLimitError("Publication exceeds nine ordinary events on one UTC observation date")
