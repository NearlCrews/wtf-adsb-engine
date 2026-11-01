"""Upper winds from Open-Meteo, for turning a ground speed into an airspeed estimate.

Weather data by Open-Meteo.com (CC BY 4.0). The API is free for
non-commercial use; this client is off unless WIND_SOURCE=open-meteo is set.

An ``above_rated_speed`` candidate records a ground speed above a type's rated
airspeed limit with no wind assumed. This module fetches the hourly wind at the
pressure level nearest each report's altitude, subtracts it from the ground
velocity (limits.tas_from_ground_speed) and settles the verdict. Positions are
the aircraft's own, rounded to 0.25 degrees before any request; the receiver's
position is never involved. Responses are kept 24 hours in ``wind_cache``, at
most one request is made every five seconds, and failures back off like the
type check worker (registry_http). Public evidence records the wind and names
its source; ATTRIBUTION is the one attribution text.

Only a completed calculation settles a verdict. With the source off, a window
that needs the wind keeps "pending" (one whose reports all state their own true
airspeed is still settled); a window whose wind or altitude is missing waits
for a later attempt (``wind_retries``), with a per-event backoff.
"""

import json
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from . import limits, reference, registry_http
from .curation import utc_day
from .enrichment_rules import WIND_ATTRIBUTION as ATTRIBUTION
from .enrichment_rules import WIND_SOURCE as SOURCE
from .rules import (
    GROUND_SPEED_KINDS,
    MIN_REPORT_SECONDS,
    MIN_REPORTS,
    RATED_SPEED_IMPLAUSIBLE,
    REPORTED_TAS_KT,
    number,
)

NAME = "wind"
BASE = "https://api.open-meteo.com/v1/forecast"
MAX_BYTES = 512 * 1024
LEVELS = (1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100)
GRID = 0.25
PAST_DAYS = 2
FORECAST_DAYS = 1
RETENTION = 86400
INTERVAL = 5
IDLE = 30
# A reading is matched to the nearest hour; beyond this the wind is unknown.
MAX_HOUR_GAP = 5400
LOOKBACK = 7 * 86400
# A window the worker could not settle is tried again after a per-event delay that
# doubles from 30 minutes to at most six hours, while the event is within LOOKBACK.
RETRY_FIRST = registry_http.NETWORK_BACKOFF
RETRY_MAX = registry_http.REFUSAL_BACKOFF
# The most waiting events one pass reads: with the source off, windows that need the
# wind are passed over, and one that does not can still be settled.
PENDING_LIMIT = 50


def enabled():
    return os.getenv("WIND_SOURCE", "").strip().lower() == SOURCE


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS wind_cache (
          lat REAL NOT NULL, lon REAL NOT NULL, day TEXT NOT NULL, fetched_at REAL NOT NULL,
          payload TEXT NOT NULL, PRIMARY KEY(lat,lon,day));
        CREATE TABLE IF NOT EXISTS wind_retries (
          event_id INTEGER PRIMARY KEY, attempts INTEGER NOT NULL, retry_at REAL NOT NULL,
          observed_at REAL);
    """)


class RetryLater(Exception):
    """The service cannot be asked now: it is backing off or rate limited, or the request
    just failed. The verdict stays pending for a later pass."""


def level_for(alt_baro_ft):
    """The listed pressure level nearest the ISA pressure of a pressure altitude."""
    pressure = limits.isa_pressure_hpa(alt_baro_ft)
    return min(LEVELS, key=lambda level: abs(level - pressure))


def grid(lat, lon):
    """The aircraft position rounded to the 0.25-degree grid sent to the service."""
    lat, lon = number(lat), number(lon)
    if lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return None
    return round(lat / GRID) * GRID, round(lon / GRID) * GRID


def askable(stamp, now):
    """Whether the service's past-days and forecast window covers a reading's time."""
    return now - PAST_DAYS * 86400 - 86400 <= stamp <= now + FORECAST_DAYS * 86400


def request_url(lat, lon):
    hourly = ",".join(f"wind_speed_{level}hPa,wind_direction_{level}hPa" for level in LEVELS)
    return (
        BASE
        + "?"
        + urlencode(
            {
                "latitude": f"{lat:.2f}",
                "longitude": f"{lon:.2f}",
                "hourly": hourly,
                "wind_speed_unit": "kn",
                "past_days": PAST_DAYS,
                "forecast_days": FORECAST_DAYS,
                "timezone": "UTC",
            }
        )
    )


def _hour(text):
    try:
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()
    except TypeError, ValueError:
        return None


def compact(raw):
    """The hourly times (epoch seconds) and per-level speeds and directions, or raise."""
    hourly = raw.get("hourly") if isinstance(raw, dict) else None
    if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
        raise ValueError("Invalid Open-Meteo response")
    times = [_hour(text) for text in hourly["time"]]
    if (
        not times
        or any(stamp is None for stamp in times)
        or len(times) > 24 * (PAST_DAYS + FORECAST_DAYS + 1)
    ):
        raise ValueError("Invalid Open-Meteo hours")
    levels = {}
    for level in LEVELS:
        speeds, directions = hourly.get(f"wind_speed_{level}hPa"), hourly.get(f"wind_direction_{level}hPa")
        if not isinstance(speeds, list) or not isinstance(directions, list):
            continue
        if len(speeds) != len(times) or len(directions) != len(times):
            raise ValueError("Invalid Open-Meteo series")
        levels[str(level)] = {
            "speed": [number(value) for value in speeds],
            "direction": [number(value) for value in directions],
        }
    if not levels:
        raise ValueError("Open-Meteo response has no pressure levels")
    return {"time": times, "levels": levels}


def fetch(lat, lon, opener=None):
    """One grid cell's compact hourly winds; raises registry_http.Unavailable."""
    try:
        return compact(registry_http.get_json(request_url(lat, lon), opener, max_bytes=MAX_BYTES))
    except (ValueError, TypeError) as error:
        raise registry_http.Unavailable() from error


def wind_from_payload(payload, level, stamp):
    """The wind at a level for the hour nearest a timestamp, or None."""
    series = (payload.get("levels") or {}).get(str(level))
    times = payload.get("time") or []
    if not series or not times:
        return None
    index = min(range(len(times)), key=lambda i: abs(times[i] - stamp))
    if abs(times[index] - stamp) > MAX_HOUR_GAP:
        return None
    speed, direction = series["speed"][index], series["direction"][index]
    if speed is None or direction is None or speed < 0 or not 0 <= direction <= 360:
        return None
    return {"speed_kt": speed, "direction_deg": direction % 360, "level_hpa": level}


def cached(db, lat, lon, day_key, now):
    """(payload, fetched_at) kept for a grid cell and day, or None when missing or expired."""
    row = db.execute(
        "SELECT fetched_at,payload FROM wind_cache WHERE lat=? AND lon=? AND day=?", (lat, lon, day_key)
    ).fetchone()
    if row is None or not 0 <= now - row["fetched_at"] < RETENTION:
        return None
    try:
        return json.loads(row["payload"]), row["fetched_at"]
    except ValueError:
        return None


def payloads(path, cell, days, now, opener=None):
    """{day: (payload, fetched_at)} for a grid cell's UTC days, from the cache or one request.

    One response covers the service's whole past-days and forecast window, so a
    single request serves every missing day and is kept under each. Raises
    RetryLater when a day is missing and the service is backing off or rate
    limited, or when the request fails (the failure sets the backoff).
    """
    from .app import connect

    with connect(path) as db:
        found = {key: cached(db, *cell, key, now) for key in days}
        missing = [key for key, value in found.items() if value is None]
        if missing:
            if registry_http.waiting(db, NAME, now, INTERVAL):
                raise RetryLater()
            registry_http.requested(db, NAME, now)
    if not missing:
        return found
    try:
        payload = fetch(*cell, opener)
    except registry_http.Unavailable as error:
        with connect(path) as db:
            registry_http.failed(db, NAME, now, error.status, "Open-Meteo")
        raise RetryLater() from None
    text = json.dumps(payload, allow_nan=False)
    with connect(path) as db:
        db.executemany(
            "INSERT OR REPLACE INTO wind_cache VALUES (?,?,?,?,?)",
            [(*cell, key, now, text) for key in missing],
        )
        registry_http.succeeded(db, NAME, now, "Open-Meteo lookup complete")
    return {**found, **dict.fromkeys(missing, (payload, now))}


def window_samples(evidence):
    """The confirmation window's reports with their inputs, oldest first; the peak
    report stands in for an older row that kept no inputs."""
    confirmation = evidence.get("confirmation") if isinstance(evidence.get("confirmation"), dict) else {}
    samples = [
        sample
        for sample in confirmation.get("samples") or []
        if isinstance(sample, dict)
        and isinstance(sample.get("inputs"), dict)
        and number(sample.get("ts")) is not None
    ]
    if samples:
        return sorted(samples, key=lambda sample: sample["ts"])
    aircraft = evidence.get("aircraft") or {}
    observed = number(evidence.get("observed_at"))
    if observed is None:
        return []
    return [
        {
            "ts": observed,
            "inputs": {field: number(aircraft.get(field)) for field in ("gs", "track", "alt_baro", "tas")},
            "quality_flags": [],
        }
    ]


def _reported_tas(sample):
    tas = number((sample.get("inputs") or {}).get("tas"))
    return tas if tas is not None and REPORTED_TAS_KT[0] <= tas <= REPORTED_TAS_KT[1] else None


def correct_sample(entry, sample, wind):
    """One report's airspeed and verdict: from its reported true airspeed, else
    from ground speed, track, and the wind. Returns the corrected sample, whose
    airspeed_basis says which ("reported" or "modelled_wind"), or None without one."""
    inputs = sample.get("inputs") or {}
    gs, track, altitude = (number(inputs.get(key)) for key in ("gs", "track", "alt_baro"))
    result = {
        "ts": sample["ts"],
        "gs": gs,
        "track": track,
        "alt_baro": altitude,
        "wind": wind,
        "airspeed_kt": None,
        "airspeed_basis": None,
        "verdict": "unknown",
    }
    tas, basis = _reported_tas(sample), "reported"
    if tas is None and wind is not None:
        estimate = limits.tas_from_ground_speed(gs, track, wind.get("speed_kt"), wind.get("direction_deg"))
        tas, basis = (estimate[0], "modelled_wind") if estimate is not None else (None, None)
    if tas is None or altitude is None:
        return result
    result.update(
        airspeed_kt=round(tas, 1),
        airspeed_basis=basis,
        verdict=limits.speed_verdict(entry, tas, altitude) or "unknown",
    )
    return result


def resolve_rated_speed(entry, block, corrected):
    """Settle an above_rated_speed verdict over the corrected window.

    "exceeded" needs a run of at least rules.MIN_REPORTS consecutive corrected
    "exceeded" reports spanning rules.MIN_REPORT_SECONDS (limits.exceeded_run): a
    report within the limit between them breaks the run. Any "implausible" report
    makes the window implausible (bad data); a report without an airspeed makes
    the calculation incomplete, "unknown", which run_once never saves; otherwise it
    is "within". The block keeps every corrected report, which
    limits.speed_comparison summarizes. `entry` is the type's limits, which
    correct_sample already applied.
    """
    verdicts = [sample["verdict"] for sample in corrected]
    if not corrected or "unknown" in verdicts:
        verdict = "unknown"
    elif "implausible" in verdicts:
        verdict = "implausible"
    elif limits.exceeded_run(corrected, MIN_REPORTS, MIN_REPORT_SECONDS):
        verdict = "exceeded"
    else:
        verdict = "within"
    return {**block, "verdict": verdict, "samples": corrected}


def pending(db, now, limit=PENDING_LIMIT, *, reported_only=False):
    """Live above_rated_speed events whose verdict still waits for the wind, newest first.

    An event waiting out its retry delay is left out until the delay ends or the
    collector saves newer reports for it. "unknown" was saved by earlier versions for
    a window they could not settle; such a window is tried again like a pending one.
    ``reported_only`` leaves out a window with a report that states no usable true
    airspeed of its own, so windows that need the wind never hide one that does not.
    """
    from .app import LIVE_ORIGIN

    rows = db.execute(
        """SELECT e.id,e.hex,e.evidence FROM events e LEFT JOIN wind_retries r ON r.event_id=e.id
        WHERE e.origin=? AND e.kind='above_rated_speed' AND e.last_seen>=? AND json_valid(e.evidence)
        AND json_extract(e.evidence,'$.rated_limit.verdict') IN ('pending','unknown')
        AND (r.retry_at IS NULL OR r.retry_at<=?
             OR r.observed_at IS NOT json_extract(e.evidence,'$.observed_at'))
        AND (NOT ? OR NOT EXISTS (SELECT 1 FROM json_each(e.evidence,'$.confirmation.samples') s
             WHERE COALESCE(json_extract(s.value,'$.inputs.tas'),-1) NOT BETWEEN ? AND ?))
        ORDER BY e.last_seen DESC,e.id DESC LIMIT ?""",
        (LIVE_ORIGIN, now - LOOKBACK, now, reported_only, *REPORTED_TAS_KT, limit),
    ).fetchall()
    events = []
    for row in rows:
        event = dict(row)
        event["raw"] = event["evidence"]
        event["evidence"] = json.loads(event["evidence"])
        events.append(event)
    return events


def _wind_key(sample, cell, now):
    """(UTC day, pressure level) of the wind a report needs, or None: the source is off or
    has no grid cell, the report states its own usable true airspeed or no altitude, or its
    time is outside the service's window."""
    altitude = number((sample.get("inputs") or {}).get("alt_baro"))
    if (
        cell is None
        or _reported_tas(sample) is not None
        or altitude is None
        or not askable(sample["ts"], now)
    ):
        return None
    return utc_day(sample["ts"]), level_for(altitude)


def _sample_wind(found, key, stamp):
    """The wind a report takes from its day's payload, naming its source; None without one."""
    if key is None:
        return None
    payload, fetched_at = found[key[0]]
    reading = wind_from_payload(payload, key[1], stamp)
    return None if reading is None else {**reading, "source": SOURCE, "fetched_at": fetched_at}


def _needs_wind(samples):
    return any(_reported_tas(sample) is None for sample in samples)


def run_once(path, now=None, opener=None, *, enabled_override=None):
    """Settle one waiting above_rated_speed verdict over its whole confirmation window.

    Each report without its own true airspeed takes the wind at its level and
    hour from one payload per grid cell and day. With the source off, a window
    that needs the wind is passed over and stays "pending". A calculation that
    misses a report's airspeed (no wind for its level or hour, no altitude, or
    no limits entry) saves nothing: the event is tried again after its retry delay
    (_defer). The row is updated only if its evidence is still the text that was
    read, so a collector update during the weather request never leaves a
    calculation made from superseded inputs; the next pass recomputes it.
    """
    from .app import connect

    now = time.time() if now is None else now
    on = enabled() if enabled_override is None else enabled_override
    with connect(path) as db:
        candidates = pending(db, now, reported_only=not on)
    waiting = None
    for event in candidates:
        evidence = event["evidence"]
        aircraft = evidence.get("aircraft") or {}
        entry = reference.limits(aircraft.get("t"))
        samples = window_samples(evidence) if entry is not None else []
        needs = not samples or _needs_wind(samples)
        # With the source off, or while it cannot be asked, only a window whose reports
        # state their own true airspeed can be settled.
        if needs and (not on or waiting):
            continue
        result = _settle(path, event, entry, samples, now, opener, on)
        if result["state"] != "waiting":
            return result
        waiting = result
    return waiting or {"state": "idle" if on else "disabled"}


def _settle(path, event, entry, samples, now, opener, on):
    """run_once() for one event: resolve, defer, or wait for the service."""
    from .app import connect

    evidence = event["evidence"]
    aircraft, block = evidence.get("aircraft") or {}, evidence.get("rated_limit") or {}
    # The window is a minute or two long: one aircraft position names its grid cell.
    cell = grid(aircraft.get("lat"), aircraft.get("lon")) if on else None
    wanted = [_wind_key(sample, cell, now) for sample in samples]
    try:
        found = payloads(path, cell, {key[0] for key in wanted if key}, now, opener) if any(wanted) else {}
    except RetryLater:
        return {"state": "waiting", "id": event["id"]}
    corrected = [
        correct_sample(entry, sample, _sample_wind(found, key, sample["ts"]))
        for sample, key in zip(samples, wanted, strict=True)
    ]
    updated = resolve_rated_speed(entry, block, corrected)
    with connect(path) as db:
        if updated["verdict"] == "unknown":
            # A missing wind leaves the verdict pending. A cached day that lacked a
            # report's level or hour is fetched again at the retry.
            stale = {
                key[0] for key, sample in zip(wanted, corrected, strict=True) if key and not sample["wind"]
            }
            # An empty executemany would still open a deferred transaction, and the
            # read in _defer would then fail its write instead of waiting for the lock.
            if stale:
                db.executemany(
                    "DELETE FROM wind_cache WHERE lat=? AND lon=? AND day=?", [(*cell, day) for day in stale]
                )
            attempts = _defer(db, event, now)
            return {"state": "deferred", "id": event["id"], "attempts": attempts}
        changed = db.execute(
            "UPDATE events SET evidence=json_set(evidence,'$.rated_limit',json(?)) WHERE id=? AND evidence=?",
            (json.dumps(updated, allow_nan=False), event["id"], event["raw"]),
        ).rowcount
        if changed:
            db.execute("DELETE FROM wind_retries WHERE event_id=?", (event["id"],))
            if updated["verdict"] == "implausible":
                flag_ground_speed(db, event)
    if not changed:
        # The collector updated the row meanwhile: its new inputs are recomputed next pass.
        return {"state": "superseded", "id": event["id"]}
    return {"state": "resolved", "id": event["id"], "verdict": updated["verdict"]}


def _defer(db, event, now):
    """Schedule the event's next attempt; the delay doubles with each one. Returns the count.

    The delay belongs to the reports it was computed from: once the collector saves
    newer ones (a new observed_at), pending() offers the event again at once.
    """
    row = db.execute("SELECT attempts FROM wind_retries WHERE event_id=?", (event["id"],)).fetchone()
    attempts = (row["attempts"] if row else 0) + 1
    delay = min(RETRY_FIRST * 2 ** (attempts - 1), RETRY_MAX)
    # SQLite reads observed_at from the evidence text here as pending() does, so an
    # unchanged row compares equal to the last bit.
    db.execute(
        "INSERT OR REPLACE INTO wind_retries VALUES (?,?,?,json_extract(?,'$.observed_at'))",
        (event["id"], attempts, now + delay, event["raw"]),
    )
    return attempts


def flag_ground_speed(db, event):
    """Hold the encounter's ground-speed findings once its rated-speed window is implausible.

    A window whose airspeed works out at 1.5 times the certified limit or more is
    bad data, so the ground speed of the same encounter is no performance story
    (``fast``) or season record either; the collector routes a report whose own
    airspeed is that far over to the data-quality path the same way
    (rules._performance_candidates). The flag (rules.RATED_SPEED_IMPLAUSIBLE) is
    blocking, and app.record() keeps it on the encounter's later rows of those kinds.
    """
    from .app import LIVE_ORIGIN

    session = event["evidence"].get("encounter_start")
    if session is None:
        return
    speed_kinds = sorted(GROUND_SPEED_KINDS)
    rows = db.execute(
        f"""SELECT id,evidence FROM events WHERE hex=? AND kind IN ({",".join("?" * len(speed_kinds))})
        AND origin=? AND json_valid(evidence) AND json_extract(evidence,'$.encounter_start')=?""",
        (event["hex"], *speed_kinds, LIVE_ORIGIN, session),
    ).fetchall()
    for row in rows:
        evidence = json.loads(row["evidence"])
        flags = set(evidence.get("quality_flags") or [])
        if RATED_SPEED_IMPLAUSIBLE not in flags:
            evidence["quality_flags"] = sorted(flags | {RATED_SPEED_IMPLAUSIBLE})
            db.execute("UPDATE events SET evidence=? WHERE id=?", (json.dumps(evidence), row["id"]))


def status(db):
    on = enabled()
    return {
        **registry_http.status(db, NAME, on, "Waiting for rated-speed candidates"),
        "source": SOURCE if on else None,
        "attribution": ATTRIBUTION if on else None,
    }


def enrichment_loop(path):
    registry_http.loop(
        NAME,
        "Wind worker",
        run_once,
        path,
        lambda result: (
            INTERVAL if result.get("state") in {"resolved", "waiting", "superseded", "deferred"} else IDLE
        ),
    )
