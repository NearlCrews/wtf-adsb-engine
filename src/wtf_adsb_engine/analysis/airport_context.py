"""Conservative presentation hints for repeated circuits aligned with a runway.

These hints are not detections of training or landings. Unknown aircraft types,
helicopters, incomplete analysis windows and weak geometry are left visible.
All geometry comes from aircraft positions; no receiver information is used.
"""

import json
import logging
import math
from datetime import datetime, timezone
from functools import lru_cache
from itertools import pairwise
from pathlib import Path

from . import reference
from .rules import distance_nm, finite, number

RULE = json.loads((Path(__file__).parent / "data/airport-pattern-rule-v1.json").read_text())
DATASET_VERSION = RULE["dataset_version"]
DATASET_HASH = RULE["dataset_sha256"]
CLASSIFIER = RULE["classifier"]
# Positive, deliberately limited fixed-wing identification. Adding types requires
# review; an unrecognized code must not silently become a fixed-wing aircraft.
FIXED_WING_TYPES = frozenset(RULE["fixed_wing_types"])
SOURCES = frozenset({"adsb_icao", "adsr_icao"})
ALIGNMENT = math.cos(math.radians(25))
# Saved tracks are drawn and classified in the same parts (see event-media.py).
MAX_GAP = 45
MAX_PART_SPEED = 1200
# Same-time copies of one source this close together are one position.
DUPLICATE_DISTANCE_NM = 0.01
# Fields cleared together when same-time copies disagree.
MERGE_GROUPS = (("speed",), ("altitude", "altitude_reference"))
LOG = logging.getLogger("discoveries")
# reference.type_class names grouped for these rules; other classes keep their name.
CLASS_NAMES = {"helicopter": "rotorcraft", "gyroplane": "rotorcraft", "airplane": "fixed_wing"}


@lru_cache(maxsize=1)
def dataset():
    return reference.pinned_json(
        Path(__file__).parent / "data" / f"{DATASET_VERSION}.json",
        file_sha256=RULE["dataset_file_sha256"],
        content_sha256=DATASET_HASH,
    )


def runway_data():
    return dataset()["runways"]


def coverage():
    """The dataset's own bounds as (west, south, east, north)."""
    west, south, east, north = dataset()["coverage"]
    return west, south, east, north


def type_class(typecode):
    """ "rotorcraft", "fixed_wing" or another reference class ("glider", "balloon",
    "tiltrotor", ...) for a type designator; None when the type is unknown."""
    value = reference.type_class(typecode) if typecode else None
    return None if value in (None, "unknown") else CLASS_NAMES.get(value, value)


def fixed_wing(typecode):
    known = type_class(typecode)
    if known is not None:
        return known == "fixed_wing"
    return typecode in FIXED_WING_TYPES


def analysis_loops(event, points):
    rows = [m for m in event.get("measurements", []) if m.get("key") == "closed_loops"]
    if len(rows) != 1:
        return None
    row = rows[0]
    loops = row.get("value")
    if (
        row.get("scope") != "analysis-window"
        or not finite(loops)
        or not 2 <= loops <= 100
        or int(loops) != loops
    ):
        return None
    try:
        start = datetime.fromisoformat(row["start_utc"]).timestamp()
        end = datetime.fromisoformat(row["end_utc"]).timestamp()
    except KeyError, TypeError, ValueError, AttributeError:
        return None
    # A short archived excerpt must never inherit loops from a larger episode.
    if points[0]["ts"] != start or points[-1]["ts"] != end or not 300 <= end - start <= 3600:
        return None
    return int(loops)


def ground_fix(raw):
    return bool(raw.get("ground")) or any(
        raw.get(key) == "ground" for key in ("archive_alt", "alt", "alt_baro", "alt_geom")
    )


def merged_fix(copies):
    """One fix from same-time copies, or None for a conflict.

    readsb repeats some MLAT fixes at one timestamp a few metres apart. Copies
    from one source within 0.01 NM are one position; anything else at that time
    is a conflict. The first copy in coordinate order keeps its position, so
    input order never changes the result. Other fields that disagree are cleared
    (an altitude together with its reference), never chosen.
    """
    if any(copy is None for copy in copies):
        return None
    if len(copies) == 1:
        # Nearly every fix: a lone copy cannot conflict or disagree with itself.
        return {**copies[0], "break_before": bool(copies[0]["break_before"])}
    ordered = sorted(copies, key=lambda copy: (copy["lat"], copy["lon"]))
    first = ordered[0]
    if any(
        copy["source"] != first["source"]
        or distance_nm((first["lat"], first["lon"]), (copy["lat"], copy["lon"])) > DUPLICATE_DISTANCE_NM
        for copy in ordered
    ):
        return None
    point = {**first, "break_before": any(copy["break_before"] for copy in ordered)}
    extra = sorted(first.keys() - {"ts", "lat", "lon", "source", "break_before"})
    groups = [group for group in MERGE_GROUPS if group[0] in first]
    groups += [(key,) for key in extra if not any(key in group for group in MERGE_GROUPS)]
    for group in groups:
        if len({tuple(copy.get(key) for key in group) for copy in ordered}) > 1:
            point.update(dict.fromkeys(group))
    return point


def parts(raw_points, describe=None):
    """Split saved positions where a drawn track breaks: the one splitter for the
    runway classifier and the drawn maps (event-media.py).

    Invalid, stale, conflicting or ground fixes, conflicting same-time reports,
    gaps over 45 s, source changes, implied speeds over 1,200 kn and explicit
    new-leg markers all end a part. Each point keeps ts, lat, lon and source,
    plus the fields ``describe(raw)`` returns (see merged_fix for same-time copies).
    """
    copies = {}
    for raw in raw_points:
        stamp = number(raw.get("ts"))
        if stamp is None:
            continue
        lat, lon = number(raw.get("lat")), number(raw.get("lon"))
        valid = (
            lat is not None
            and lon is not None
            and -90 <= lat <= 90
            and -180 <= lon <= 180
            and not raw.get("stale")
            and not raw.get("conflicting_position")
            and not ground_fix(raw)
        )
        point = None
        if valid:
            point = {
                "ts": stamp,
                "lat": lat,
                "lon": lon,
                "source": raw.get("source", "unknown"),
                "break_before": bool(raw.get("new_leg") or raw.get("break_before")),
                **(describe(raw) if describe else {}),
            }
        copies.setdefault(stamp, []).append(point)
    by_time = {stamp: merged_fix(items) for stamp, items in copies.items()}
    result, current, previous = [], [], None
    for stamp in sorted(by_time):
        point = by_time[stamp]
        broken = point is None
        if point is not None and previous is not None:
            elapsed = stamp - previous["ts"]
            speed = (
                distance_nm((previous["lat"], previous["lon"]), (point["lat"], point["lon"])) * 3600 / elapsed
            )
            broken = (
                elapsed > MAX_GAP
                or point["source"] != previous["source"]
                or speed > MAX_PART_SPEED
                or point["break_before"]
            )
        if broken and current:
            result.append(current)
            current = []
        if point is not None:
            current.append({key: value for key, value in point.items() if key != "break_before"})
        previous = point
    if current:
        result.append(current)
    return result


def runway_passes(parts, runway):
    """Count distinct same-direction passes, with an observed return between them."""
    a, b = runway["ends"]
    lon, lat = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    longitude_scale = math.cos(math.radians(lat)) * 60
    dx, dy = (b[0] - a[0]) * longitude_scale, (b[1] - a[1]) * 60
    length = math.hypot(dx, dy)
    if not 0.1 <= length <= 4:
        return None

    def project(point):
        x, y = (point["lon"] - lon) * longitude_scale, (point["lat"] - lat) * 60
        return point["ts"], (x * dx + y * dy) / length, (-x * dy + y * dx) / length

    projected = [[project(point) for point in part] for part in parts]
    radii = [math.hypot(x, y) for part in projected for _, x, y in part]
    if max(radii) > 5 or sum(r <= 3 for r in radii) / len(radii) < 0.8:
        return None
    candidates = []
    for part in projected:
        # A missing turn must not connect an otherwise unrelated pass and return.
        # Evaluate repeats independently inside each accepted continuous segment.
        crossings = {1: [], -1: []}
        returns = {1: [], -1: []}
        run = None
        for (t0, x0, y0), (t1, x1, y1) in pairwise(part):
            along, across = x1 - x0, y1 - y0
            distance = math.hypot(along, across)
            if distance < 0.05:
                continue
            if abs(along) / distance < ALIGNMENT:
                run = None
                continue
            direction = 1 if along > 0 else -1
            # A parallel return offset from the runway supports a circuit, not
            # merely repeated straight crossings of a nearby airport. Consecutive
            # aligned steps form one leg, so slower aircraft and denser sampling
            # measure the same 0.3 NM return as one long step does.
            if 0.4 <= abs((y0 + y1) / 2) <= 1.8:
                if run is None or run[0] != direction:
                    run = [direction, t0, t1, 0.0]
                run[2], run[3] = t1, run[3] + abs(along)
                if run[3] >= 0.3:
                    returns[direction].append((run[1], run[2]))
            else:
                run = None
            # The midpoint or a gate 0.5 nmi before the runway can establish a
            # pass. Both gates on one approach count only once below.
            for gate in (0, -direction * (length / 2 + 0.5)):
                if min(x0, x1) <= gate < max(x0, x1):
                    fraction = (gate - x0) / along
                    offset = y0 + fraction * across
                    if abs(offset) <= 0.2:
                        crossings[direction].append((t0 + fraction * (t1 - t0), abs(offset)))
        for direction, hits in crossings.items():
            accepted = []
            for instant, offset in sorted(hits):
                if not accepted or (
                    instant - accepted[-1][0] >= 90
                    and any(accepted[-1][0] < start < end < instant for start, end in returns[-direction])
                ):
                    accepted.append((instant, offset))
            if len(accepted) >= 2:
                candidates.append((len(accepted), sum(offset for _, offset in accepted) / len(accepted)))
    return min(candidates, key=lambda match: (-match[0], match[1])) if candidates else None


def classify(parts, event, *, analysis_window, runways=None):
    """Return a versioned, receiver-free candidate annotation, or no classification."""
    if (
        not analysis_window
        or event.get("synthetic")
        or event.get("kind") != "circling"
        or not fixed_wing(event.get("aircraft", {}).get("type_code"))
    ):
        return None
    points = [point for part in parts for point in part]
    if not 20 <= len(points) <= 256 or not available():
        return None
    west, south, east, north = coverage()
    if any(
        not all(finite(point.get(key)) for key in ("ts", "lat", "lon"))
        or not south <= point["lat"] <= north
        or not west <= point["lon"] <= east
        or point.get("source") not in SOURCES
        for point in points
    ):
        return None
    if len({point["source"] for point in points}) != 1:
        return None
    if any(b["ts"] <= a["ts"] for a, b in pairwise(points)) or any(
        b["ts"] - a["ts"] > MAX_GAP for part in parts for a, b in pairwise(part)
    ):
        return None
    loops = analysis_loops(event, points)
    if loops is None:
        return None
    candidates = []
    for runway in runway_data() if runways is None else runways:
        match = runway_passes(parts, runway)
        if match and match[0] <= loops + 1:
            candidates.append((match, runway))
    if not candidates:
        return None
    (passes, _), runway = min(
        candidates,
        key=lambda item: (-item[0][0], item[0][1], item[1]["airport_ident"], item[1]["runway"]),
    )
    return {
        "version": 1,
        "classifier": CLASSIFIER,
        "dataset_version": DATASET_VERSION,
        "dataset_sha256": DATASET_HASH,
        "airport_ident": runway["airport_ident"],
        "airport_name": runway["airport_name"],
        "runway": runway["runway"],
        "aligned_passes": passes,
        "closed_loops": loops,
    }


@lru_cache(maxsize=1)
def runway_index():
    cells = {}
    for runway in runway_data():
        a, b = runway["ends"]
        cell = (math.floor((a[1] + b[1]) / 2), math.floor((a[0] + b[0]) / 2))
        cells.setdefault(cell, []).append(runway)
    return cells


@lru_cache(maxsize=1)
def available():
    """Whether the pinned runway data loaded; a failure never stops collection."""
    try:
        runway_index()
        coverage()
    except (OSError, ValueError, KeyError, TypeError) as error:
        LOG.warning("Runway reference data unavailable: %s", type(error).__name__)
        return False
    return True


def runways_near(lat, lon, radius):
    """The runways filed in the index cells a circle of ``radius`` NM around (lat, lon)
    can reach; [] when the runway table is unavailable.

    Runways are filed by the whole degree of their center (runway_index). A degree
    of latitude is 60 NM, and a degree of longitude 60 NM times the cosine of the
    latitude, so enough cells each way cover the radius.
    """
    if not available():
        return []
    rows = max(1, math.ceil(radius / 60))
    columns = min(180, max(1, math.ceil(radius / max(60 * math.cos(math.radians(lat)), 1.0))))
    index = runway_index()
    return [
        runway
        for dy in range(-rows, rows + 1)
        for dx in range(-columns, columns + 1)
        for runway in index.get((math.floor(lat) + dy, math.floor(lon) + dx), ())
    ]


def nearby(point, radius=3):
    """Runways whose center lies within ``radius`` NM of a report."""
    lat, lon = number(point.get("lat")), number(point.get("lon"))
    if lat is None or lon is None:
        return []
    return [runway for runway in runways_near(lat, lon, radius) if runway_distance(point, runway) <= radius]


def runway_distance(point, runway):
    a, b = runway["ends"]
    return distance_nm((point["lat"], point["lon"]), ((a[1] + b[1]) / 2, (a[0] + b[0]) / 2))


def aligned_approach(points):
    if len(points) < 3 or points[-1]["ts"] - points[0]["ts"] < 20:
        return None
    first, last = points[0], points[-1]
    for runway in nearby(last, 2):
        a, b = runway["ends"]
        scale = math.cos(math.radians(last["lat"])) * 60
        axis = ((b[0] - a[0]) * scale, (b[1] - a[1]) * 60)
        length = math.hypot(*axis)
        vx, vy = (last["lon"] - first["lon"]) * scale, (last["lat"] - first["lat"]) * 60
        travel = math.hypot(vx, vy)
        if not length or travel < 0.3 or abs(vx * axis[0] + vy * axis[1]) / travel / length < ALIGNMENT:
            continue
        offsets = [
            abs((p["lon"] - a[0]) * scale * axis[1] - (p["lat"] - a[1]) * 60 * axis[0]) / length
            for p in points
        ]
        if max(offsets) <= 0.3 and runway_distance(last, runway) < runway_distance(first, runway):
            return runway
    return None


def annotation(runway):
    return {key: runway[key] for key in ("airport_ident", "runway")} | {"dataset_version": DATASET_VERSION}


def window_context(pattern, points, aircraft):
    """Classify a circling window's saved samples, split as event-media.py draws them."""
    if not points or not isinstance(pattern, dict):
        return None
    try:
        start = datetime.fromtimestamp(pattern["started_at"], timezone.utc).isoformat()
        end = datetime.fromtimestamp(pattern["ended_at"], timezone.utc).isoformat()
    except KeyError, TypeError, ValueError, OverflowError, OSError:
        return None
    event = {
        "kind": "circling",
        "aircraft": {"type_code": aircraft.get("t")},
        "measurements": [
            {
                "key": "closed_loops",
                "scope": "analysis-window",
                "value": pattern.get("closed_loops"),
                "start_utc": start,
                "end_utc": end,
            }
        ],
    }
    split = parts(points)
    if not split:
        return None
    return classify(split, event, analysis_window=True, runways=nearby(split[-1][-1], 5))


def pattern_context(finding, aircraft):
    evidence = finding.evidence or {}
    if finding.kind != "circling":
        return None
    return window_context(evidence.get("pattern", {}), evidence.get("pattern_samples", []), aircraft)
