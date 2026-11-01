"""Pure allowlisted evidence projection for the legacy analysis adapter.

Build a fresh allowlisted packet instead of recycling private journal prose.
Aircraft tracks are permitted; receiver coordinates and relative metrics are not.
"""

import math
import re
from copy import deepcopy
from datetime import date, datetime, timezone

from . import enrichment_rules, episode_evidence, kinds, limits, maneuvers, watchlists
from .rules import IDENTIFIERS, IDENTITY_CHECK_KEYS, RATED_KINDS, distance_nm, identifier, number

ICAO_TYPES = "https://www.icao.int/operational-safety/doc-8643-aircraft-type-designators/search"
TYPE_REFERENCES = {
    "B407": ("Bell 407", "https://www.bellflight.com/products/bell-407"),
    "R44": (
        "R44 Raven II / Clipper II variant reference",
        "https://www.robinsonheli.com/helicopters/r44-raven-ii-clipper-ii",
    ),
}
PATTERN_FIELDS = {
    "kind",
    "duration_seconds",
    "sample_count",
    "movement_sample_count",
    "evidence_sample_count",
    "evidence_samples_downsampled",
    "largest_gap_seconds",
    "timestamp_resolution_seconds",
    "path_distance_nm",
    "net_displacement_nm",
    "footprint_span_nm",
    "median_position_speed_kn",
    "median_reported_speed_kn",
    "position_reported_speed_ratio",
    "sharp_turn_fraction",
    "quality_review_required",
    "observed_sources",
    "limitations",
    "accumulated_turns",
    "direction_consistency",
    "turn_direction",
    "complete_passes",
    "corridor_length_nm",
    "corridor_width_nm",
    "smooth_turn_fraction",
    "closed_loops",
    "ground_closed_loops",
    "drift_corrected_loops",
    "orbit_center_drift_nm",
    "corridor_center_spread_nm",
    "shape_verified",
}
SOURCES = {
    "adsb_icao",
    "adsb_icao_nt",
    "adsr_icao",
    "tisb_icao",
    "adsc",
    "mlat",
    "other",
    "mode_s",
    "adsb_other",
    "adsr_other",
    "tisb_trackfile",
    "tisb_other",
    "mode_ac",
    "unknown",
}
# The detector's episode kinds (maneuvers.KINDS), and every movement-pattern kind: the
# story-side shapes and the episode kinds outside a journal category.
EPISODE_KINDS = set(kinds.where(origin="episode"))
PATTERN_KINDS = set(kinds.where(pattern=True))
READINGS = {
    "gs": ("Ground speed", "kn"),
    "tas": ("True airspeed", "kn"),
    "mach": ("Mach report", "Mach"),
    "alt_baro": ("Pressure altitude", "ft"),
    "alt_geom": ("Geometric altitude", "ft"),
    "archive_alt": ("Archived altitude; reference not retained", "ft"),
    "baro_rate": ("Pressure-altitude vertical rate", "ft/min"),
    "geom_rate": ("Geometric-altitude vertical rate", "ft/min"),
    "nic": ("Navigation integrity category", "category"),
    "nac_p": ("Navigation accuracy category", "category"),
}
# Readings that matter only to some kinds (a kind's `reading` names its group); other cards
# would gain clutter.
READING_GROUPS = {
    "selected_altitude": {
        "nav_altitude_mcp": ("Selected altitude (autopilot setting)", "ft"),
        "nav_qnh": ("Altimeter setting", "hPa"),
    },
    "integrity": {
        "nic_baro": ("Barometric altitude integrity", "category"),
        "sil": ("Source integrity level", "category"),
        "gva": ("Geometric vertical accuracy", "category"),
        "sda": ("System design assurance", "category"),
        "version": ("ADS-B version", ""),
    },
}
KIND_READINGS = {
    name: READING_GROUPS[group]
    for name, group in kinds.table(
        "reading",
        order=(
            "selected_altitude_overshoot",
            "gnss_integrity_loss",
            "gnss_integrity_cluster",
            "correlated_data_anomaly",
        ),
    ).items()
}
KINDS = set(kinds.where(origin="story"))
# The transponder-status families: live codes and statuses, archived codes.
SPECIAL_KINDS = set(kinds.where(origin="special"))
STORY_KINDS = KINDS | SPECIAL_KINDS | EPISODE_KINDS
EMERGENCY_STATUSES = maneuvers.STATUS_NAMES | {"none", "reserved"}
DATABASE_FLAGS = {"military": 1, "interesting": 2, "pia": 4, "ladd": 8}
DATABASE_FLAG_CAVEAT = (
    "Database flags are labels in the aircraft database. PIA (Privacy ICAO Address) and LADD (Limiting "
    "Aircraft Data Displayed) describe FAA data-distribution programs; they do not identify the operator, "
    "the occupants, or the purpose of a flight."
)
# How a record's messages were received is not in it; every packet's readings carry this.
SOURCE_CAVEAT = (
    "The collection can include ADS-B, MLAT, and relayed data; a record in it does not show how its messages "
    "were received."
)
# What a movement pattern does not establish; every pattern summary carries it.
PATTERN_LIMITATION = "Movement geometry does not establish the operator, mission, or activity on the ground."
# Named roles first, so the 12-aircraft cap never drops the advisory threat or a lead.
PARTICIPANT_ROLES = ("threat", "lead", "wingman", "member")
# The identity checks rules computes; a check not named here is kept only as a boolean.
IDENTITY_TEXT_CHECKS = {
    "us_registration_from_address": "registration",
    "callsign_registration": "registration",
    "allocation_country": "country",
    "registration_country": "country",
}
IDENTITY_CHECKS = {key: IDENTITY_TEXT_CHECKS.get(key, "bool") for key in IDENTITY_CHECK_KEYS}
# The attributed wording of a community-list association; public_events pins it.
WATCHLIST_SOURCE = "Plane Alert DB"
WATCHLIST_ATTRIBUTION = "Aircraft association/category: Plane Alert DB / SDR-Enthusiasts and contributors."
CLAIM_SCOPE = (
    "Source association only; not verified current ownership, operator, historical association, or "
    "presence of any person aboard."
)
# Evidence markers left by archive importers and the frozen historical corpus. A live
# row keeps replay_refresh after later live updates, so it does not mark archive data.
ARCHIVE_MARKERS = ("historical_corpus", "imported", "archive", "replay_pattern_import")
LABEL_CAVEAT = (
    "Archived callsign and transponder-code labels are carried between saved positions; they are not "
    "readings at the position time."
)
FIRST_SIGHTING_KINDS = set(kinds.where(first_sighting=True))
FIRST_SIGHTING_SCOPES = {"type", "allocation_country"}
# A rated finding's comparison (rules._rated_altitude, rules._rated_speed, and wind.py):
# the field each kind compares and the certified limits a block may name.
RATED_FIELDS = kinds.table("rated_field")
RATED_LIMIT_NAMES = frozenset(limits.LIMITS)
RATED_VERDICTS = {"exceeded", "within", "implausible", "pending", "unknown"}
# The wind sources wind.py names, and the registries typecheck.py asks.
WIND_SOURCES = {enrichment_rules.WIND_SOURCE}
TYPE_CHECK_SOURCES = enrichment_rules.REGISTRIES
# A cited limit's sheet is a public HTTPS page; public_events checks the host again.
HTTPS_URL = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[^\s<>\"'\\]*)?")


def _utc_seconds(stamp):
    """An epoch time as an ISO 8601 UTC timestamp with its offset, as packets record times."""
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def text_value(value, limit):
    """Keep bounded text without turning arbitrary private structures into prose."""
    if not isinstance(value, str):
        return None
    return value.strip()[:limit] or None


def pattern_summary(raw):
    """Typed derived metrics and new limitations, never private free-form prose."""
    if not isinstance(raw, dict) or not raw:
        return {}
    text_fields = {"kind", "turn_direction", "observed_sources", "limitations"}
    bool_fields = {"evidence_samples_downsampled", "quality_review_required", "shape_verified"}
    summary = {
        key: value
        for key in PATTERN_FIELDS - text_fields - bool_fields
        if (value := number(raw.get(key))) is not None
    }
    for key in bool_fields:
        if isinstance(raw.get(key), bool):
            summary[key] = raw[key]
    if isinstance(raw.get("kind"), str) and raw["kind"] in PATTERN_KINDS:
        summary["kind"] = raw["kind"]
    if isinstance(raw.get("turn_direction"), str) and raw["turn_direction"] in {"left", "right"}:
        summary["turn_direction"] = raw["turn_direction"]
    sources = raw.get("observed_sources", [])
    sources = (
        sorted({item for item in sources if isinstance(item, str) and item in SOURCES})
        if isinstance(sources, list)
        else []
    )
    summary["observed_sources"] = sources
    summary["quality_review_required"] = bool(
        summary.get("quality_review_required", True)
        or len(sources) != 1
        or any(source in {"unknown", "mlat"} for source in sources)
    )
    limitations = [PATTERN_LIMITATION]
    if summary["quality_review_required"]:
        limitations.append(
            "Source or geometry quality needs review; the exact path or turn count may be unreliable."
        )
    if "mlat" in sources:
        limitations.append(
            "MLAT estimates position from signal arrival-time differences across receivers; its speed and position estimates may not be independent evidence."
        )
    if len(sources) > 1:
        limitations.append("Multiple position sources are present; source transitions need review.")
    if summary.get("largest_gap_seconds", 0) > 45:
        limitations.append(
            "Some positions are more than 45 seconds apart; missing movement was not observed."
        )
    if summary.get("timestamp_resolution_seconds", 0) > 0:
        limitations.append(
            "Archive slice times are approximate and can affect position-derived speed comparisons."
        )
    summary["limitations"] = limitations
    for source_key, public_key in (("started_at", "start_utc"), ("ended_at", "end_utc")):
        stamp = number(raw.get(source_key))
        if stamp is not None:
            try:
                summary[public_key] = _utc_seconds(stamp)
            except ValueError, OverflowError, OSError:
                pass
    summary["metrics_scope"] = (
        "These metrics and sample counts describe this selected analysis window, which can be shorter than the merged observation interval."
    )
    return summary


def aircraft_links(aircraft):
    """Validated identity links only; no guessed flight-number conversions."""
    links = []
    registration = identifier(aircraft.get("r"), IDENTIFIERS["registration"])
    callsign = identifier(aircraft.get("flight"), IDENTIFIERS["callsign"])
    address = identifier(aircraft.get("hex"), IDENTIFIERS["icao_address"])
    if registration:
        links.extend(
            [
                {
                    "id": "aircraft_history",
                    "label": "Aircraft history on Flightradar24",
                    "url": "https://www.flightradar24.com/data/aircraft/" + registration.lower(),
                },
                {
                    "id": "aircraft_photos",
                    "label": "Photos of this registration on JetPhotos",
                    "url": "https://www.jetphotos.com/registration/" + registration,
                },
            ]
        )
    if callsign or registration:
        links.append(
            {
                "id": "flight_lookup",
                "label": "Callsign / aircraft lookup on FlightAware",
                "url": "https://www.flightaware.com/live/flight/" + (callsign or registration),
            }
        )
    if address:
        links.append(
            {
                "id": "address_lookup",
                "label": "Address lookup on ADS-B Exchange",
                "url": "https://globe.adsbexchange.com/?icao=" + address.lower(),
            }
        )
    typecode = identifier(aircraft.get("t"), IDENTIFIERS["type_code"])
    if typecode:
        links.append(
            {"id": "type_lookup", "label": "Look up the aircraft type designator", "url": ICAO_TYPES}
        )
        if typecode in TYPE_REFERENCES:
            label, url = TYPE_REFERENCES[typecode]
            links.append({"id": "type_reference", "label": label, "url": url})
    return links


def replay_data(evidence):
    """Aircraft positions only. Break the path at gaps and source changes."""
    points = []
    samples = evidence.get("pattern_samples", [])
    if not samples and evidence.get("maneuver_samples"):
        import inspect

        from .telemetry import positions

        # A source change is drawn as a break below, not as a separate segment.
        options = (
            {"split_sources": False} if "split_sources" in inspect.signature(positions).parameters else {}
        )
        samples = positions(evidence["maneuver_samples"], **options)
    # Reject unexpectedly large arrays, rather than silently truncating an episode.
    if not isinstance(samples, list) or len(samples) > 200:
        return None
    previous = None
    for sample in samples:
        if not isinstance(sample, dict):
            return None
        stamp, lat, lon = (number(sample.get(key)) for key in ("ts", "lat", "lon"))
        if stamp is None or lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            return None
        if previous and stamp <= previous["timestamp"]:
            return None
        source = sample.get("source", "unknown")
        source = source if isinstance(source, str) and source in SOURCES else "unknown"
        point = {
            "timestamp": stamp,
            "latitude": lat,
            "longitude": lon,
            "source": source,
            "ground_speed_kn": number(sample.get("gs")),
            "altitude_ft": number(sample.get("alt", sample.get("alt_baro", sample.get("alt_geom")))),
            "break_before": previous is None
            or stamp - previous["timestamp"] > 45
            or source != previous["source"],
        }
        points.append(point)
        previous = point
    if len(points) < 2:
        return None
    for point in points:
        point["elapsed_seconds"] = point["timestamp"] - points[0]["timestamp"]
    return {
        "coordinate_reference": "WGS84 aircraft positions",
        "points": points,
        "altitude_reference": "May be pressure or geometric; not height above ground",
        "rendering": "Frame the selected aircraft episode; no receiver marker, rings, range, or bearing. Do not interpolate across breaks.",
        "quality_review_required": pattern_summary(evidence.get("pattern", {})).get(
            "quality_review_required", True
        ),
        "sampled_evidence": True,
    }


def jump_summary(evidence):
    before, after = evidence.get("previous_position", {}), evidence.get("aircraft", {})
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    snapshot_before, snapshot_after = (
        number(evidence.get("previous_snapshot_at")),
        number(evidence.get("observed_at")),
    )
    age_before, age_after = number(before.get("seen_pos")), number(after.get("seen_pos"))
    coordinates = [number(point.get(key)) for point in (before, after) for key in ("lat", "lon")]
    if any(value is None for value in (snapshot_before, snapshot_after, age_before, age_after, *coordinates)):
        return None
    lat1, lon1, lat2, lon2 = coordinates
    if not (-90 <= lat1 <= 90 and -90 <= lat2 <= 90 and -180 <= lon1 <= 180 and -180 <= lon2 <= 180):
        return None
    elapsed = snapshot_after - age_after - (snapshot_before - age_before)
    if not 2 <= elapsed <= 45 or not 0 <= age_before <= 15 or not 0 <= age_after <= 15:
        return None
    displacement = distance_nm((lat1, lon1), (lat2, lon2))
    return {
        "elapsed_seconds": elapsed,
        "displacement_nm": round(displacement, 3),
        "implied_speed_kn": round(displacement * 3600 / elapsed, 1),
        "source_before": before.get("type")
        if isinstance(before.get("type"), str) and before["type"] in SOURCES
        else "unknown",
        "source_after": after.get("type")
        if isinstance(after.get("type"), str) and after["type"] in SOURCES
        else "unknown",
        "interpretation": "A discontinuity between two positions, not a measured physical speed or an observed connecting flight path.",
    }


def watchlist_summary(match, aircraft):
    if not isinstance(match, dict) or match.get("public_claim_allowed") is not True:
        return None
    source = match.get("source", {})
    if not isinstance(source, dict):
        return None
    registration = watchlists.normalized_registration(aircraft.get("r"))
    if (
        match.get("hex") != str(aircraft.get("hex", "")).lower()
        or not registration
        or registration != watchlists.normalized_registration(match.get("source_registration"))
    ):
        return None
    try:
        commit = watchlists.source_commit(source.get("url"))
    except ValueError, TypeError:
        return None
    if source.get("commit") != commit:
        return None
    association = watchlists.entry_text(match.get("association"), "association")
    if association is None:
        return None
    neutral = match.get("neutral_category")
    if not isinstance(neutral, str) or neutral not in watchlists.NEUTRAL_LABELS:
        neutral = watchlists.GENERIC_CATEGORY
    # The source's own category name and tags say why the list carries the aircraft.
    tags = match.get("tags") if isinstance(match.get("tags"), list) else []
    tags = [
        tag for tag in (watchlists.entry_text(item, "tag") for item in tags[: watchlists.MAX_TAGS]) if tag
    ]
    dates = {}
    for key in ("source_date", "fetched_at"):
        try:
            value = source.get(key)
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
            dates[key] = parsed.isoformat() if parsed and parsed.tzinfo else None
        except ValueError:
            dates[key] = None
    source_row = match.get("source_row")
    source_row = (
        source_row
        if isinstance(source_row, int)
        and not isinstance(source_row, bool)
        and 2 <= source_row <= watchlists.MAX_ROWS + 1
        else None
    )
    return {
        "source_name": WATCHLIST_SOURCE,
        "association_label": association,
        "category": neutral,
        "list_category": watchlists.entry_text(match.get("source_category"), "list_category"),
        "tags": tags,
        "group": watchlists.GROUPS.get(match.get("group")),
        "description": watchlists.entry_text(match.get("aircraft_description"), "description"),
        # Checked as a public page where it is published (public_events.public_url).
        "link": watchlists.entry_text(match.get("reference_url"), "link"),
        "source_url": watchlists.REPOSITORY_URL + "/blob/" + commit + "/plane-alert-db.csv",
        "source_commit": commit,
        "source_snapshot_date": dates["source_date"],
        "source_row": source_row,
        "retrieved_at": dates["fetched_at"],
        "individual_association_date": None,
        "attribution": WATCHLIST_ATTRIBUTION,
        "licenses": deepcopy(watchlists.LICENSES),
        "review_required": True,
        "claim_scope": CLAIM_SCOPE,
    }


def surrounding_readings(evidence):
    """Neighboring numeric readings and transmitted codes, without positions."""
    surrounding = []
    samples = evidence.get("context_samples", [])
    for sample in samples[:40] if isinstance(samples, list) else []:
        if not isinstance(sample, dict):
            continue
        # Field names differ across early imports; keep only typed facts.
        clean = {
            key: value
            for key in (
                "ts",
                "timestamp",
                "gs",
                "archive_alt",
                "alt_baro",
                "alt_geom",
                "baro_rate",
                "geom_rate",
            )
            if (value := number(sample.get(key))) is not None
        }
        for field in ("archive_alt", "alt_baro"):
            if sample.get(field) == "ground":
                clean[field] = "ground"
        if isinstance(sample.get("source"), str) and sample["source"] in SOURCES:
            clean["source"] = sample["source"]
        if squawk := episode_evidence.octal_code(sample.get("squawk")):
            clean["squawk"] = squawk
        # An archived label keeps its own time, which can precede the position's.
        if (label_at := number(sample.get("label_at"))) is not None and (squawk or "flight" in sample):
            clean["label_at"] = label_at
        # "none" is the absence of a status, not a reading.
        if (
            isinstance(sample.get("emergency"), str)
            and sample["emergency"] in EMERGENCY_STATUSES
            and sample["emergency"] != "none"
        ):
            clean["emergency_status"] = sample["emergency"]
        if clean:
            surrounding.append(clean)
    return surrounding


def archive_origin(event, evidence):
    """Saved archive data, whose callsign and code labels are carried between positions."""
    return (
        any(evidence.get(key) for key in ARCHIVE_MARKERS)
        or "archive_alt" in evidence.get("aircraft", {})
        or str(event.get("origin", "")).startswith(("Historical corpus", "Saved compact replay"))
    )


# Archive formats a finding's data can come from; "archive" is an older import of either.
ARCHIVE_FORMATS = {"heatmap": "archive-heatmap", "traces": "archive-traces"}


def provenance(event, evidence):
    """Where a finding's data came from: "live" reception, or "archive-heatmap",
    "archive-traces" or "archive" for an archive whose format was not kept."""
    if not archive_origin(event, evidence):
        return "live"
    corpus = evidence.get("historical_corpus")
    source = corpus.get("source") if isinstance(corpus, dict) else None
    origin = str(event.get("origin", ""))
    if source not in ARCHIVE_FORMATS and origin.startswith("Historical corpus: "):
        source = origin.removeprefix("Historical corpus: ")
    return ARCHIVE_FORMATS.get(source, "archive")


def label_time(evidence):
    """The archive label record's own time, when the importer kept it."""
    for source in (evidence.get("aircraft", {}), evidence):
        stamp = number(source.get("label_at")) if isinstance(source, dict) else None
        if stamp is not None and 1e9 <= stamp <= 1e10:
            return _utc_seconds(stamp)
    return None


def database_flags(aircraft):
    """Named database flags; there is no publication filter on any of them."""
    flags = number(aircraft.get("dbFlags"))
    if flags is None or flags < 0 or flags != int(flags):
        return None
    return {name: bool(int(flags) & bit) for name, bit in DATABASE_FLAGS.items()}


def identity_checks(raw):
    """Typed identity comparisons; each value is None when it could not be computed."""
    if not isinstance(raw, dict):
        return None
    result = {}
    for key, kind in IDENTITY_CHECKS.items():
        value = raw.get(key)
        if kind == "bool":
            result[key] = value if isinstance(value, bool) else None
        elif kind == "registration":
            result[key] = identifier(value, IDENTIFIERS["registration"])
        else:
            result[key] = (
                value.strip()
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z .,'()&-]{1,59}", value.strip())
                else None
            )
    result["conflict"] = result["conflict"] is True
    return result


def _iso_day(value):
    """A YYYY-MM-DD calendar date, or None."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _day_count(value):
    """A whole, non-negative number of days as the detector wrote it, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return int(value) if value >= 0 and value == int(value) else None


def first_sighting(raw):
    """What a first sighting was compared with: its scope, earlier covered days and their start."""
    if not isinstance(raw, dict) or raw.get("scope") not in FIRST_SIGHTING_SCOPES:
        return None
    days = _day_count(raw.get("coverage_days"))
    if days is None:
        return None
    return {"scope": raw["scope"], "coverage_days": days, "since": _iso_day(raw.get("since"))}


def season_record(raw):
    """The previous seasonal maximum and the baseline it covers: the season's start, the first
    day of compared reports, and how many days they span when the detector counted them."""
    if not isinstance(raw, dict):
        return None
    previous = raw.get("previous_max_gs")
    if (
        isinstance(previous, bool)
        or not isinstance(previous, (int, float))
        or not math.isfinite(previous)
        or previous <= 0
    ):
        return None
    block = {"previous_max_gs": previous}
    for key in ("season_start", "baseline_start"):
        if day := _iso_day(raw.get(key)):
            block[key] = day
    if (days := _day_count(raw.get("baseline_days"))) is not None:
        block["baseline_days"] = days
    return block


def _positive(value):
    value = number(value)
    return value if value is not None and value > 0 else None


def _wind(raw):
    """The modelled wind a speed estimate used: speed, direction it blows from, level, and source.

    Never the grid position it was asked for, nor when it was fetched.
    """
    if not isinstance(raw, dict) or raw.get("source") not in WIND_SOURCES:
        return None
    speed, direction, level = (number(raw.get(key)) for key in ("speed_kt", "direction_deg", "level_hpa"))
    if (
        speed is None
        or speed < 0
        or direction is None
        or not 0 <= direction <= 360
        or not level
        or level <= 0
    ):
        return None
    return {"speed_kt": speed, "direction_deg": direction, "level_hpa": level, "source": raw["source"]}


def _sustained(kind, evidence):
    """limits.comparison() with UTC times and an allowlisted wind, or None.

    Curation gates on the same comparison, so a finding is selected only when its
    story can state it. A modelled estimate whose wind cannot be shown has none.
    """
    summary = limits.comparison(kind, evidence)
    if summary is None:
        return None
    first, last = summary.pop("first"), summary.pop("last")
    summary.update(start_utc=_utc_seconds(first), end_utc=_utc_seconds(last))
    if kind == "above_rated_speed":
        summary["wind"] = _wind(summary["wind"])
        if summary["airspeed_basis"] == "modelled_wind" and summary["wind"] is None:
            return None
    return summary


def rated_limit(evidence, kind):
    """The certified-limit comparison behind a rated finding, allowlisted, or None.

    From the detector's block (rules._rated_altitude, rules._rated_speed): the
    compared field, the limit's name and figure, the type and its cited sheet,
    and the verdict; a speed adds the type's Mmo. `sustained` is the comparison
    over the reports that were over the limit (limits.comparison), with the
    decisive report's airspeed basis and modelled wind. The block must name the
    type the aircraft lists, or the limit belongs to another.
    """
    raw = evidence.get("rated_limit")
    if kind not in RATED_FIELDS or not isinstance(raw, dict):
        return None
    speed = kind == "above_rated_speed"
    source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
    title, url = text_value(source.get("title"), 300), source.get("url")
    listed = identifier((evidence.get("aircraft") or {}).get("t"), IDENTIFIERS["type_code"])
    block = {
        "field": RATED_FIELDS[kind],
        "limit_name": raw.get("limit_name"),
        "limit": _positive(raw.get("limit")),
        "verdict": raw.get("verdict"),
        "type": identifier(raw.get("type"), IDENTIFIERS["type_code"]),
        "source": {"title": title, "url": url},
    }
    if (
        raw.get("field") != block["field"]
        or raw.get("unit") != ("KIAS" if speed else "ft")
        or block["limit_name"] not in RATED_LIMIT_NAMES
        or block["verdict"] not in RATED_VERDICTS
        or block["type"] is None
        or block["type"] != listed
        or not title
        or not isinstance(url, str)
        or not HTTPS_URL.fullmatch(url)
    ):
        return None
    if (block["limit_name"] == "max_operating_altitude_ft") == speed:
        return None
    if speed:
        block["mmo"] = _positive(raw.get("mmo"))
    if block["limit"] is None and block.get("mmo") is None:
        return None
    # Only a settled exceedance has reports over the limit to summarise.
    block["sustained"] = _sustained(kind, evidence) if block["verdict"] == "exceeded" else None
    return block


def type_check(evidence, kind=None):
    """The second-source type check, reduced to what the story needs: its state and the
    registry that answered.

    A rated story is told only when the registry's type agrees with the listed
    type. The registry's type code is public for a mismatch story when it
    differs (enrichment_rules.REGISTRY_TYPE_KINDS). Its registration and the listed
    type stay private: the identity already shows the database's, and a
    registry's own spelling may differ.
    """
    raw = evidence.get("type_check")
    if not isinstance(raw, dict):
        return None
    source = raw.get("source")
    found = {
        # A check made against a type the aircraft no longer lists is pending.
        "state": enrichment_rules.type_check_state(evidence),
        "source": source if source in TYPE_CHECK_SOURCES else None,
    }
    if kind in enrichment_rules.REGISTRY_TYPE_KINDS and found["state"] == "disagrees":
        code = identifier(raw.get("type_code"), IDENTIFIERS["type_code"])
        if code:
            found["type_code"] = code
    return found


def participants(evidence, primary=None):
    """Other aircraft in a multi-aircraft episode, using the primary identity allowlist."""
    raw = evidence.get("participants")
    if not isinstance(raw, list) and isinstance(evidence.get("maneuver"), dict):
        raw = evidence["maneuver"].get("participants")
    if not isinstance(raw, list):
        return []
    result, seen = [], {primary} if primary else set()
    for item in raw[:64]:
        if not isinstance(item, dict) or item.get("role") not in PARTICIPANT_ROLES:
            continue
        address = identifier(item.get("hex"), IDENTIFIERS["icao_address"])
        if address and address in seen:
            continue
        if address:
            seen.add(address)
        measurements = item.get("measurements", {})
        values = {}
        if isinstance(measurements, dict):
            for key, value in measurements.items():
                value = number(value)
                if value is not None and (key in READINGS or key in episode_evidence.METRICS):
                    values[key] = value
        source = item.get("source")
        result.append(
            {
                "icao_address": address,
                "role": item["role"],
                "registration": identifier(item.get("registration"), IDENTIFIERS["registration"]),
                "callsign": identifier(item.get("callsign"), IDENTIFIERS["callsign"]),
                "type_code": identifier(item.get("type"), IDENTIFIERS["type_code"]),
                "type_description": text_value(item.get("desc", item.get("type_description")), 160),
                "source": source if isinstance(source, str) and source in SOURCES else "unknown",
                "measurements": values,
            }
        )
    result.sort(key=lambda item: (PARTICIPANT_ROLES.index(item["role"]), item["icao_address"] or "~"))
    return result[:12]


def evidence_packet(event, include_track=True, *, observation_interval=None):
    """Deliberately omit existing title, why, notes, owner and private provenance.

    This packet is a draft input, not authorization to release an event. Range
    records are intentionally unsupported because their subject is the receiver.
    """
    kind = event["kind"]
    if not isinstance(kind, str) or kind not in STORY_KINDS:
        raise ValueError("Event kind has no public evidence policy")
    evidence = event["evidence"]
    aircraft = evidence.get("aircraft", {})
    identity = {
        "registration": identifier(aircraft.get("r"), IDENTIFIERS["registration"]),
        "callsign": identifier(aircraft.get("flight"), IDENTIFIERS["callsign"]),
        "icao_address": identifier(aircraft.get("hex"), IDENTIFIERS["icao_address"]),
        "type_code": identifier(aircraft.get("t"), IDENTIFIERS["type_code"]),
        "type_description": text_value(aircraft.get("desc"), 160),
        "metadata_caveat": "Current database match; historical registration/type assignment is not independently verified.",
    }
    readings = []
    for field, (label, unit) in {**READINGS, **KIND_READINGS.get(kind, {})}.items():
        value = number(aircraft.get(field))
        if value is not None:
            readings.append({"field": field, "label": label, "value": value, "unit": unit})
    archived = archive_origin(event, evidence)
    packet = {
        "schema_version": 1,
        "kind": kind,
        # Readers of the packet, such as the AI writer, take the data's origin from here.
        "provenance": provenance(event, evidence),
        "aircraft": identity,
        "observation": {
            "start_utc": _utc_seconds(event["first_seen"]),
            "end_utc": _utc_seconds(event["last_seen"]),
            "qualifying_count": event["hits"]
            if isinstance(event.get("hits"), int)
            and not isinstance(event["hits"], bool)
            and event["hits"] > 0
            else None,
            "metric_value": number(event.get("value")),
            "metric_unit": text_value(event.get("unit"), 32) or "",
            "count_meaning": "Overlapping qualifying windows, not flights or loops"
            if kind in PATTERN_KINDS
            else "Qualifying reports with possibly different values; not repetitions of the peak",
        },
        "received": {
            "source": aircraft.get("type")
            if isinstance(aircraft.get("type"), str) and aircraft["type"] in SOURCES
            else "unknown",
            "readings": readings,
            "squawk": episode_evidence.octal_code(aircraft.get("squawk")),
            "label_provenance": "archived_label" if archived else "saved_report",
            "emergency_status": aircraft.get("emergency")
            if isinstance(aircraft.get("emergency"), str) and aircraft["emergency"] in EMERGENCY_STATUSES
            else None,
            "emitter_category": identifier(aircraft.get("category"), r"[A-C][0-7]"),
            "source_caveat": SOURCE_CAVEAT,
        },
        "pattern": pattern_summary(evidence.get("pattern", {})),
        "maneuver": episode_evidence.summary(evidence.get("maneuver")),
        "links": aircraft_links(aircraft),
        "link_caveat": "Identity lookups may show another date or assignment. Links are not verification of this flight or permission to copy photos.",
    }
    if archived:
        packet["received"].update(label_at_utc=label_time(evidence), label_caveat=LABEL_CAVEAT)
    packet["metadata"] = {
        "database_flags": database_flags(aircraft),
        "database_flags_caveat": DATABASE_FLAG_CAVEAT,
        "database_year": number(aircraft.get("year")),
        "rarity_scope": "Uncommon in this collection; not a worldwide rarity claim"
        if kind == "rare_here"
        else None,
    }
    packet["surrounding_readings"] = surrounding_readings(evidence)
    # Non-maneuver findings carry their annotations at the top level, patterns in the pattern.
    context = set(episode_evidence.tags(evidence)) | set(episode_evidence.tags(evidence.get("pattern")))
    if archived and (identity["callsign"] or packet["received"]["squawk"]):
        context.add("archive_carried_label")
    if packet["received"]["source"] == "mlat" and kind in episode_evidence.MLAT_ESTIMATE_KINDS:
        context.add("mlat_solver_estimate")
    packet["context_tags"] = sorted(context)
    packet["quality_flags"] = episode_evidence.flags(evidence)
    # A first or a record names what it was compared with. The blocks feed the story
    # facts only, so they stay outside the projected evidence and its version.
    if kind in FIRST_SIGHTING_KINDS and (block := first_sighting(evidence.get("first_sighting"))):
        packet["first_sighting"] = block
    if kind == "seasonal_speed_record" and (block := season_record(evidence.get("season_record"))):
        packet["season_record"] = block
    # A rated finding's comparison and its second-source type check. Unlike the blocks
    # above they project to public measurements, so they join the evidence version. A
    # mismatch story's check feeds the story facts only and stays outside the version.
    if kind in RATED_KINDS and (block := rated_limit(evidence, kind)):
        packet["rated_limit"] = block
    if (kind in RATED_KINDS or kind in enrichment_rules.REGISTRY_TYPE_KINDS) and (
        check := type_check(evidence, kind)
    ):
        packet["type_check"] = check
    if checks := identity_checks(evidence.get("identity_checks")):
        packet["identity_checks"] = checks
    if others := participants(evidence, identity["icao_address"]):
        packet["participants"] = others
    if (
        isinstance(evidence.get("replay_refresh"), dict)
        and evidence["replay_refresh"].get("retained_legacy_hit_count") is True
    ):
        packet["observation"]["count_meaning"] = (
            "Retained qualifying windows from multiple detector versions; not all old windows were revalidated, and counts are not flights or loops"
        )
    match = evidence.get("watchlist")
    if match := watchlist_summary(match, aircraft):
        packet["watchlist"] = match
        packet["links"].append(
            {
                "id": "watchlist_source",
                "label": "Community watchlist association and source data",
                "url": match["source_url"],
            }
        )
    observed_at = number(evidence.get("observed_at"))
    if observed_at is not None:
        packet["received"]["saved_readings_at_utc"] = _utc_seconds(observed_at)
    if kind == "position_jump":
        packet["position_discontinuity"] = jump_summary(evidence)
    if include_track and (replay := replay_data(evidence)):
        packet["replay"] = replay
    if observation_interval is not None:
        first, last = (number(value) for value in observation_interval)
        if first is None or last is None or first > event["first_seen"] or last < event["last_seen"]:
            raise ValueError("The observation interval must contain the selected finding")
        packet["observation"].update(
            start_utc=_utc_seconds(first),
            end_utc=_utc_seconds(last),
            qualifying_count_start_utc=_utc_seconds(event["first_seen"]),
            qualifying_count_end_utc=_utc_seconds(event["last_seen"]),
            count_meaning="Primary finding only: " + packet["observation"]["count_meaning"],
        )
    return packet
