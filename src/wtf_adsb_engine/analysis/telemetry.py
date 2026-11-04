"""Bounded telemetry with explicit altitude references and field provenance."""

import re
import statistics
from functools import partial

from .rules import IdentityCache, distance_nm, null_island, number, position

WINDOW = 1200
LIMIT = 2401
DIRECT = {"adsb_icao", "adsr_icao", "adsb_other", "adsr_other"}
# Mode S replies and MLAT aircraft: the transponder itself sent squawk and altitude.
MODE_S_SOURCES = {"mode_s", "mlat"}
FIELDS = ("gs", "alt_baro", "alt_geom", "baro_rate", "geom_rate", "nic", "nac_p", "track")
# readsb lists some MLAT/TIS-B fields under another name.
ALIASES = {"alt_baro": ("altitude",)}
# Optional readsb fields, kept only when the collector retains them. Values outside
# these bounds are dropped; none of them is receiver-relative.
OPTIONAL_NUMBERS = {
    "nav_altitude_mcp": (-2000, 65000),
    "nav_qnh": (800, 1100),
    "nic_baro": (0, 1),
    "sil": (0, 3),
    "gva": (0, 3),
    "sda": (0, 3),
    "version": (0, 7),
    "gpsOkBefore": (0, 1e10),
    "gpsOkLat": (-90, 90),
    "gpsOkLon": (-180, 180),
}
NAV_MODES = frozenset({"autopilot", "vnav", "althold", "approach", "lnav", "tcas"})
# Receiver-relative values (range, bearing, signal level, receiver position):
# never part of a saved report, evidence or archive corpus.
RECEIVER_FIELDS = frozenset({"r_dst", "r_dir", "rssi", "rr_lat", "rr_lon"})


def _words(value):
    return re.sub(r"[^a-z0-9]+", " ", re.sub(r"['’]", "", value.lower())).strip()


# readsb's TCAS advisory text, mapped through an allowlist to a fixed wording and
# advised sense. Unknown text is dropped; free text never passes through.
ACAS_ADVISORIES = {
    _words(text): (canonical, sense)
    for canonical, sense, variants in (
        ("Clear of Conflict", "clear", ("Clear of Conflict",)),
        ("Climb", "up", ("Climb", "Climb Climb")),
        ("Descend", "down", ("Descend", "Descend Descend")),
        ("Crossing Climb", "up", ("Crossing Climb", "Climb Crossing Climb")),
        ("Crossing Descend", "down", ("Crossing Descend", "Crossing Descent", "Descend Crossing Descend")),
        ("Increase Climb", "up", ("Increase Climb",)),
        ("Increase Descent", "down", ("Increase Descent", "Increase Descend")),
        ("Reversal Climb", "up", ("Reversal Climb", "Reverse Climb", "Climb, Climb Now", "Climb Now")),
        (
            "Reversal Descend",
            "down",
            ("Reversal Descend", "Reverse Descend", "Descend, Descend Now", "Descend Now"),
        ),
        ("Maintain Climb", "maintain", ("Maintain Climb",)),
        ("Maintain Descent", "maintain", ("Maintain Descent", "Maintain Descend")),
        (
            "Maintain Vertical Speed",
            "maintain",
            ("Maintain Vertical Speed", "Maintain Vertical Speed Maintain"),
        ),
        ("Level Off", "level", ("Level Off", "Level Off Level Off")),
        ("Adjust Vertical Speed", "level", ("Adjust Vertical Speed", "Adjust Vertical Speed Adjust")),
        ("Reduce Climb", "level", ("Reduce Climb",)),
        ("Reduce Descent", "level", ("Reduce Descent", "Reduce Descend")),
        ("Monitor Vertical Speed", "preventive", ("Monitor Vertical Speed",)),
        ("Don't Climb", "preventive", ("Don't Climb", "Do Not Climb")),
        ("Don't Descend", "preventive", ("Don't Descend", "Do Not Descend")),
    )
    for text in variants
}
ACAS_COMPLEMENTS = {
    _words(text): text
    for text in ("Do not pass below", "Do not pass above", "Do not turn left", "Do not turn right")
}


def advisory_sense(text):
    """The advised sense for allowlisted advisory text, or None."""
    match = ACAS_ADVISORIES.get(_words(text[:80])) if isinstance(text, str) else None
    return match[1] if match else None


def _flag(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, str) and value.strip() in {"0", "1"}:
        return int(value.strip())
    return int(value) if isinstance(value, int) and value in (0, 1) else None


def _bits(value, width):
    if isinstance(value, str) and re.fullmatch(rf"[01]{{1,{width}}}", value.strip()):
        return value.strip()
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**width:
        return format(value, "b")
    return None


def acas_report(raw):
    """Allowlisted readsb ``acas_ra`` fields; raw message bytes are never kept."""
    if not isinstance(raw, dict):
        return None
    result = {}
    stamp = number(raw.get("unix_timestamp"))
    if stamp is not None and stamp > 0:
        result["unix_timestamp"] = stamp
    df = raw.get("df_type")
    if isinstance(df, int) and not isinstance(df, bool) and 0 <= df <= 31:
        result["df_type"] = df
    for key, table in (("advisory", ACAS_ADVISORIES), ("advisory_complement", ACAS_COMPLEMENTS)):
        if isinstance(raw.get(key), str) and (match := table.get(_words(raw[key][:80]))):
            result[key] = match[0] if key == "advisory" else match
    for key in ("RAT", "MTE"):
        if (value := _flag(raw.get(key))) is not None:
            result[key] = value
    tti = raw.get("TTI")
    if isinstance(tti, str) and re.fullmatch(r"[01]{1,2}", tti.strip()):
        result["TTI"] = int(tti.strip(), 2)
    elif isinstance(tti, int) and not isinstance(tti, bool) and 0 <= tti <= 3:
        result["TTI"] = tti
    for key, width in (("ARA", 14), ("RAC", 4)):
        if (value := _bits(raw.get(key), width)) is not None:
            result[key] = value
    threat = raw.get("threat_id_hex")
    if isinstance(threat, str) and re.fullmatch(r"~?[0-9a-f]{6}", threat.strip().lower()):
        result["threat_id_hex"] = threat.strip().lower()
    return result if "unix_timestamp" in result else None


def optional(aircraft):
    """The optional readsb fields within their bounds; free text never passes."""
    result = {}
    for key, (low, high) in OPTIONAL_NUMBERS.items():
        value = number(aircraft.get(key))
        if value is not None and low <= value <= high:
            result[key] = value
    modes = aircraft.get("nav_modes")
    if isinstance(modes, list):
        result["nav_modes"] = sorted({m for m in modes if isinstance(m, str) and m in NAV_MODES})
    if (acas := acas_report(aircraft.get("acas_ra"))) is not None:
        result["acas_ra"] = acas
    return result


def sample(aircraft, now):
    result = {
        "ts": now - aircraft["seen"],
        "source": aircraft.get("type", "unknown"),
        "ground": aircraft.get("alt_baro") == "ground",
        "airborne_known": any(number(aircraft.get(k)) is not None for k in ("alt_baro", "alt_geom")),
        **{
            key: aircraft[key]
            for key in (*FIELDS, "squawk", "emergency", "mlat", "tisb", "flight", "category")
            if key in aircraft
        },
        **optional(aircraft),
    }
    if position(aircraft):
        result.update(lat=aircraft["lat"], lon=aircraft["lon"], position_at=now - aircraft["seen_pos"])
    return result


def source_for(point, field):
    names = (field, *ALIASES.get(field, ()))
    for source in ("mlat", "tisb"):
        listed = point.get(source)
        if isinstance(listed, (list, tuple)) and any(name in listed for name in names):
            return source
    source = point.get("source", "unknown")
    return source if isinstance(source, str) else "unknown"


def report_sources(report):
    """The provenance fields source_for reads, for a sanitized report or saved aircraft
    (its own source is in "type")."""
    return {"source": report.get("type", "unknown"), "mlat": report.get("mlat"), "tisb": report.get("tisb")}


def direct_value(report, field, sources=None):
    """The field's value when the aircraft's own ADS-B transmission supplied it, else None."""
    value = number(report.get(field))
    direct = source_for(sources or report_sources(report), field) in DIRECT
    return value if value is not None and direct else None


def low_position_quality(point):
    """A reported navigation integrity or accuracy category of 1 or less."""
    return any(number(point.get(key)) is not None and number(point[key]) <= 1 for key in ("nic", "nac_p"))


def speed_point_usable(point):
    """Whether one report can join a sustained ground-speed window (live and archive).

    An airborne report from one of the aircraft's own ADS-B sources, whose
    ground speed that source supplied, with a usable position quality. The
    caller still keeps a window to one source, bounds its gaps and checks the
    window as a whole with quality().
    """
    return (
        not point.get("ground")
        and point.get("archive_alt") != "ground"
        and number(point.get("gs")) is not None
        and point.get("source") in DIRECT
        and source_for(point, "gs") in DIRECT
        and not low_position_quality(point)
    )


def window_quality(points, fields=("gs", "lat", "lon")):
    """quality() for a sustained window: archive freshness is a limitation the
    archive path states on its own, never a reason to drop a window."""
    return [flag for flag in quality(points, fields) if flag != "archive_field_freshness_unknown"]


def quality(points, fields):
    flags = set()
    for field in fields:
        sources = {source_for(p, field) for p in points}
        if len(sources) != 1:
            flags.add("source_transition")
        if sources - DIRECT:
            flags.add("uncertain_source")
    if {"lat", "lon"} & set(fields):
        if any(low_position_quality(p) for p in points):
            flags.add("low_position_quality")
        ratios = []
        for before, after in zip(points, points[1:], strict=False):
            dt = after["ts"] - before["ts"]
            if dt <= 0 or not all(
                number(p.get(k)) is not None for p in (before, after) for k in ("lat", "lon", "gs")
            ):
                continue
            speed = distance_nm((before["lat"], before["lon"]), (after["lat"], after["lon"])) * 3600 / dt
            reported = (before["gs"] + after["gs"]) / 2
            if speed > 12:
                ratios.append(speed / max(1, reported))
        if len(ratios) >= 3 and not 0.45 <= statistics.median(ratios) <= 1.8:
            flags.add("position_speed_disagreement")
    if any(p.get("archive") for p in points):
        flags.add("archive_field_freshness_unknown")
    return sorted(flags)


def bounded_append(buffer, point):
    """Keep twenty minutes at up to 2 Hz, retaining the newest subsecond report.

    Signals and rates are still evaluated on every incoming report. Replacing
    the last sample cannot manufacture independent confirmation timestamps.
    """
    if (
        buffer
        and 0 < point["ts"] - buffer[-1]["ts"] < 1
        and len(buffer) > 1
        and point["ts"] - buffer[-2]["ts"] < 1
    ):
        buffer.pop()
    buffer.append(point)
    while buffer and buffer[0]["ts"] < point["ts"] - WINDOW:
        buffer.popleft()


def recent(points, max_gap=45, split_sources=True):
    """A changed source, invalid timestamp, stale fix or gap breaks an episode.

    Transponder statuses pass ``split_sources=False``: an ADS-B aircraft whose
    best source briefly becomes a Mode S reply still sends the same code. A
    new-leg marker starts a new segment at its report, as in positions().
    """
    segment = []
    for raw in points[-LIMIT:]:
        # Later arithmetic uses the raw timestamp, so it must already be a number.
        stamp = raw.get("ts") if isinstance(raw.get("ts"), (int, float)) else None
        if number(stamp) is None or raw.get("stale"):
            segment = []
            continue
        if raw.get("new_leg"):
            segment = []
        if segment:
            dt = stamp - segment[-1]["ts"]
            if dt == 0 and raw == segment[-1]:
                continue
            if dt <= 0:
                segment = []
                continue
            if dt > max_gap or (split_sources and raw.get("source") != segment[-1].get("source")):
                segment = []
        segment.append(raw)
    return [p for p in segment if p["ts"] >= segment[-1]["ts"] - WINDOW] if segment else []


def _position(raw):
    """A report's normalized position copy, or None without a usable position."""
    lat, lon = number(raw.get("lat")), number(raw.get("lon"))
    stamp = number(raw.get("position_at", raw.get("ts")))
    if (
        lat is None
        or lon is None
        or stamp is None
        or not (-90 <= lat <= 90 and -180 <= lon <= 180)
        or null_island(lat, lon)
    ):
        return None
    return {**raw, "ts": stamp, "lat": lat, "lon": lon}


def positions(points, max_gap=45, split_sources=True, *, cache=None):
    """Deduplicate position endpoints; no geometry across missing fixes or jumps.

    A report without a usable position, or with a (0, 0) decoder default, is
    skipped, not treated as a break, so a Mode S reply between ADS-B positions
    cannot fragment an orbit. Ground status, stale fixes, a new leg, a gap over
    ``max_gap`` between positions, a changed position source (unless
    ``split_sources`` is False) and an implausible jump start a new segment.
    Only the latest segment is returned. ``cache`` (a dict the caller keeps per
    buffer, see PositionCache) holds each report's normalized copy, so repeated
    calls over a growing buffer normalize only new reports; callers must treat
    the returned positions as read-only.
    """
    segment = []
    for raw in points:
        if raw.get("ground") or raw.get("stale"):
            segment = []
            continue
        if cache is None:
            p = _position(raw)
        else:
            p = cache.get(raw)
        if p is None:
            continue
        if raw.get("new_leg"):
            segment = []
        if segment:
            previous = segment[-1]
            dt = p["ts"] - previous["ts"]
            if dt == 0 and (p["lat"], p["lon"]) == (previous["lat"], previous["lon"]):
                continue
            if dt <= 0:
                segment = []
                continue
            speed = distance_nm((p["lat"], p["lon"]), (previous["lat"], previous["lon"])) * 3600 / dt
            reported = [number(p.get("gs")), number(previous.get("gs"))]
            reported = [v for v in reported if v is not None and 0 <= v <= 650]
            limit = min(700, max(250, 3 * max(reported) + 60)) if reported else 700
            if dt > max_gap or speed > limit or (split_sources and p.get("source") != previous.get("source")):
                segment = []
        segment.append(p)
    return segment


# Each buffered report's normalized position (see positions), by identity.
PositionCache = partial(IdentityCache, _position)


def altitude_series(points):
    """Only compare an uninterrupted, explicitly retained altitude reference."""
    for field in ("alt_baro", "alt_geom"):
        values = [number(p.get(field)) for p in points]
        if values and all(v is not None for v in values):
            if len({source_for(p, field) for p in points}) == 1:
                return field, values
    return None, []


def spread_series(points):
    """Like altitude_series, but a spread-only check may use an archive altitude.

    An archive altitude has no known reference, so only its range is meaningful.
    Archive points already carry the archive freshness limitation.
    """
    field, values = altitude_series(points)
    if field:
        return field, values
    values = [number(p.get("archive_alt")) for p in points]
    if values and all(v is not None for v in values):
        return "archive_alt", values
    return None, []
