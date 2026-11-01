"""Allowlisted public episode measurements; no private diagnostic prose."""

import re
from datetime import datetime, timezone

from . import kinds, maneuvers, telemetry
from .rules import number

# Shared metric contract. The detector's own table wins once it defines a key.
CONTRACT_METRICS = {
    "start_altitude_ft": ("Episode start altitude", "ft"),
    "end_altitude_ft": ("Episode end altitude", "ft"),
    "level_off_altitude_ft": ("Level-off altitude", "ft"),
    "sustained_derived_rate_fpm": ("Altitude-derived rate in the high-rate interval", "ft/min"),
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
# Public wording keeps every label distinct, writes nautical miles as NM and knots as kn.
PUBLIC_LABELS = {
    "qualifying_duration_seconds": ("Duration of consecutive reports above the detection threshold", "s"),
    "qualifying_sample_count": ("Consecutive reports above the detection threshold", "samples"),
    "threshold_fpm": ("Detection threshold (magnitude)", "ft/min"),
}
UNIT_NAMES = {"nm": "NM", "kt": "kn"}
METRICS = {
    key: PUBLIC_LABELS.get(key, (label, UNIT_NAMES.get(unit, unit)))
    for key, (label, unit) in {**CONTRACT_METRICS, **maneuvers.METRICS}.items()
}
# Episode metrics shown with their own timestamp or as a rule, not over the window.
PEAK_METRICS = {"peak_rate_fpm": "peak_at_utc"}
RULE_METRICS = {"threshold_fpm"}
# Words used by the decoder's resolution-advisory text; any other text is dropped.
ADVISORY_WORDS = {
    "climb",
    "climbing",
    "descend",
    "descending",
    "descent",
    "don't",
    "do",
    "not",
    "level",
    "off",
    "increase",
    "reversal",
    "reverse",
    "crossing",
    "maintain",
    "adjust",
    "vertical",
    "speed",
    "rate",
    "monitor",
    "clear",
    "of",
    "conflict",
    "multiple",
    "threats",
    "threat",
    "ra",
    "terminated",
    "corrective",
    "preventive",
    "positive",
    "negative",
    "limit",
    "more",
    "than",
    "faster",
    "fpm",
    "ft/min",
    "and",
    "or",
}
# Every episode's standing limitation, and the one an unconfirmed episode adds.
FIELD_AGE_LIMITATION = (
    "Individual field ages are unknown; correlated measurements are not independent verification."
)
UNCONFIRMED_LIMITATION = "The episode has not met its confirmation requirements."
LIMITATIONS = {
    "position_speed_disagreement": "Position-implied movement disagrees with reported ground speed.",
    "conflicting_same_time_reports": "Different saved reports share one timestamp; their ordering is unknown.",
    "source_transition": "Measurement sources changed within the selected window.",
    "uncertain_source": "Some measurements use an uncertain or indirect source.",
    "low_position_quality": "Reported position integrity or accuracy was low.",
    "archive_field_freshness_unknown": "Archive timestamps do not establish individual message freshness.",
    "matching_altitude_unavailable": "Matching altitude reports were unavailable to check the vertical rate.",
    "rate_altitude_disagreement": "Reported vertical rates disagree with the retained altitude trend.",
    "extreme_rate": "The rate exceeds the normal spotting band and requires review.",
    "rate_estimated_from_altitude": "Vertical rate was estimated from altitude changes; it was not a transmitted rate.",
    "uncertain_geometry": "Position quality makes the track shape uncertain.",
    "airport_maneuver_hypothesis": "Runway alignment suggests an airport maneuver; landing, clearance, and intent are unconfirmed. Altitudes are not terrain clearance.",
    "shared_source_cause_unknown": "Several addresses show related telemetry anomalies. Their common cause has not been established.",
    # Sentences for the episode detector's flags follow its own descriptions.
    "rate_field_conflict": "Two directly reported vertical rates disagreed by more than 1,500 ft/min.",
    "uncorroborated_rate": "No high vertical-rate report was matched by a neighbouring report within 25%.",
    "airport_context_unavailable": "Runway reference data could not be loaded, so the track was not compared with airports.",
    "ground_status_unverified": "The aircraft did not clearly move before and after the stationary period, so it may have been on the ground.",
    "acas_decoder_experimental": "The advisory came from an experimental decode of a Mode S reply, not an ADS-B broadcast.",
    "threat_track_missing": "The other aircraft in the advisory was not identified, or its track was not received.",
    "no_advised_response": "The reported vertical rate did not show the advised response within 20 seconds, or the advisory text was not recognised.",
    "participant_uncertain_source": "At least one other aircraft was tracked by multilateration, TIS-B, or ADS-R rather than its own ADS-B broadcast.",
    "possible_duplicate_target": "The two tracks may be one aircraft reported under two addresses.",
    "sensitive_code_block": "This code block can be assigned to special flights; the assignment for this flight is not known.",
    "selected_altitude_not_clearance": "A selected altitude is an autopilot setting, not an air traffic control clearance.",
    "extreme_turn_review": "A turn this tight is implausible for this aircraft class and may come from position errors.",
    "identity_unverified": "The aircraft's identity could not be checked against independent identifiers.",
    "altitude_reference_conflict": "Pressure and geometric altitude reports disagreed by more than 3,000 ft, so the reported height is uncertain.",
    "balloon_class_conflict": "The aircraft's declared category or listed type is not a balloon, so these reports may come from a data error.",
    "altitude_coverage_incomplete": "Altitude reports did not cover every leg, so the altitude spread the survey check needs was not established.",
    "altitude_reference_mixed": "The circling aircraft reported their altitudes on different references, or on an unknown one, so their vertical separation was not established.",
}
# Non-blocking context tags. Each explains an ordinary alternative or a scope.
CONTEXT_NOTES = {
    "runway_circuit_context": "The track repeatedly lined up with a runway, as in circuit training; training and intent are unconfirmed.",
    "airport_descent_context": "The descent was centred within 3 NM of a mapped runway, or ended lined up with one, where steep descents such as parachute-drop flights are routine; the purpose is unconfirmed.",
    "terminal_area_context": "This happened below 5,000 ft within 5 NM of a mapped runway, or ended lined up with one, where such turns are routine.",
    "transient_code_selection": "The code lasted under a minute or only a few reports before a normal code returned; codes like this are sometimes passed through while dialling.",
    "status_field_disagreement": "The aircraft's own emergency field did not match the code it was sending.",
    "ground_only_status": "Every report with this status came while the aircraft reported being on the ground.",
    "status_from_mode_s_reply": "The code came from the transponder's replies to interrogation; the aircraft was not broadcasting it over ADS-B.",
    "emergency_descent_profile": "The descent began above 25,000 ft and levelled off between 8,000 and 15,000 ft, a profile used after cabin-pressure problems. The data cannot show the reason.",
    "isolated_rate_outlier": "One vertical-rate report above 12,000 ft/min had no matching neighbour and was left out of the measurements.",
    "ground_referenced_orbit": "The loops stayed centred on one ground position.",
    "drifting_orbit": "The loop centre drifted between loops, as wind drift can produce; the loop count includes loops closed after allowing for that drift.",
    "metadata_identity_conflict": "The aircraft database identity conflicts with transmitted identifiers, so type-specific comparisons were not used.",
    "mlat_solver_estimate": "The value comes from a multilateration position solution, not a field the aircraft transmitted.",
    "archive_carried_label": "The callsign or transponder code is an archived label carried between saved positions, not a reading at the position time.",
    # Robust archive shapes publish with this note instead of a review hold (owner decision, 2026-10-06).
    "archive_field_freshness_unknown": "These measurements come from saved archive tracks, which do not record how fresh each reported field was.",
    "refuelling_like": "One aircraft is a tanker type. The data cannot show whether fuel was transferred.",
    "aerotow_like": "A glider and another aircraft climbed together within 0.2 NM, as in an aerotow; the data cannot confirm a tow.",
}
# Kinds whose headline speed or position MLAT would estimate rather than receive.
MLAT_ESTIMATE_KINDS = set(kinds.where(mlat_estimate=True))
STATE_MEANINGS = {
    "active": "The condition was still present in the last retained report; this does not mean it continues now.",
    "recovered": "Later reports returned below the detection threshold.",
    "ended": "The condition ended within the retained reports.",
}


def octal_code(value):
    """A four-digit transponder code; 0000 is an absent-code placeholder, never a reading."""
    return maneuvers.octal_code(value, padded=False)


CANONICAL_ADVISORIES = frozenset(canonical for canonical, _sense in telemetry.ACAS_ADVISORIES.values())
COMPLEMENTS = frozenset(telemetry.ACAS_COMPLEMENTS.values())


def advisory_text(value):
    """The decoder's canonical advisory wording, or text made only of advisory words.

    The decoder canonicalizes new advisories; the word allowlist keeps saved ones valid.
    """
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 80:
        return None
    if value in CANONICAL_ADVISORIES:
        return value
    words = re.findall(r"[A-Za-z/']+|\d+", value)
    allowed = all(word.lower() in ADVISORY_WORDS or (word.isdigit() and len(word) <= 4) for word in words)
    return (
        " ".join(value.split()) if words and allowed and re.fullmatch(r"[A-Za-z0-9 ,/'-]+", value) else None
    )


def advisory_complement(value):
    """One of the decoder's fixed complement phrases, such as "Do not pass below"."""
    return value if isinstance(value, str) and value in COMPLEMENTS else None


def code_sequence(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        return None
    sequence = []
    for item in value:
        if not isinstance(item, dict):
            return None
        code = item.get("code")
        code = octal_code(code) or (
            code if isinstance(code, str) and code in maneuvers.STATUS_NAMES else None
        )
        seconds = number(item.get("seconds"))
        if code is None or seconds is None or not 0 <= seconds <= 86400:
            return None
        sequence.append({"code": code, "seconds": seconds})
    return sequence


def tags(raw):
    """Sorted allowlisted context tags from a finding or episode."""
    value = raw.get("context_tags", []) if isinstance(raw, dict) else []
    if not isinstance(value, list):
        return []
    return sorted({tag for tag in value if isinstance(tag, str) and tag in CONTEXT_NOTES})


def flags(raw):
    """Known blocking quality flags; unknown names stay private."""
    value = raw.get("quality_flags", []) if isinstance(raw, dict) else []
    if not isinstance(value, list):
        return []
    return sorted({flag for flag in value if isinstance(flag, str) and flag in LIMITATIONS})


def summary(raw):
    if not isinstance(raw, dict) or not raw:
        return {}
    result = {key: value for key in METRICS if (value := number(raw.get(key))) is not None}
    for key, allowed in {
        "altitude_reference": {"alt_baro", "alt_geom", "archive_alt"},
        "rate_field": {"baro_rate", "geom_rate", "derived_baro_rate", "derived_geom_rate"},
        "state": set(STATE_MEANINGS),
        "anomaly_type": {"position_jump", "position_quality_drop"},
    }.items():
        if isinstance(raw.get(key), str) and raw[key] in allowed:
            result[key] = raw[key]
    if "state" in result:
        result["state_meaning"] = STATE_MEANINGS[result["state"]]
    if code := octal_code(raw.get("code")):
        result["code"] = code
    if sequence := code_sequence(raw.get("code_sequence")):
        result["code_sequence"] = sequence
    if advisory := advisory_text(raw.get("advisory")):
        result["advisory"] = advisory
    if complement := advisory_complement(raw.get("advisory_complement")):
        result["advisory_complement"] = complement
    share = number(raw.get("ground_report_share"))
    if share is not None and 0 <= share <= 1:
        result["ground_report_share"] = share
    result["confirmed"] = raw.get("confirmed") is True
    result["quality_review_required"] = raw.get("quality_review_required") is not False or bool(
        raw.get("quality_flags")
    )
    result["limitations"] = [FIELD_AGE_LIMITATION]
    result["limitations"].extend(LIMITATIONS[flag] for flag in flags(raw))
    if not result["confirmed"]:
        result["limitations"].append(UNCONFIRMED_LIMITATION)
    result["context_tags"] = tags(raw)
    result["context_notes"] = [CONTEXT_NOTES[tag] for tag in result["context_tags"]]
    times = [("started_at", "start_utc"), ("ended_at", "end_utc"), ("peak_at", "peak_at_utc")]
    for prefix in ("qualifying", "sustained"):
        times.extend(
            ((f"{prefix}_started_at", f"{prefix}_start_utc"), (f"{prefix}_ended_at", f"{prefix}_end_utc"))
        )
    for key, target in times:
        stamp = number(raw.get(key))
        if stamp is not None:
            try:
                result[target] = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
            except ValueError, OverflowError, OSError:
                pass
    return result
