"""Day-level ground-speed asymmetry between eastbound and westbound jets at or above FL280.

Each direct ADS-B report from a level jet at or above the floor, flying roughly
east or west, adds its ground speed to that address's row for the UTC day
(``day_speed_samples``), at most one every SAMPLE_SECONDS for an address and
direction. A row keeps up to MAX_VALUES speeds and their median, its kept
report count, and its first and latest report times; it holds no position. Once both directions have MIN_ADDRESSES addresses, a
median eastbound speed at least MIN_ASYMMETRY_KN above the westbound one makes
the day's ``winds_aloft_day`` finding. The wind itself is never measured: when
the wind worker has cached an Open-Meteo forecast, its 250 hPa speed and
direction are attached, without the cache cell's coordinates.
"""

import json
import math
import statistics
from datetime import datetime, timedelta, timezone

from . import kinds, maneuvers, wind
from .curation import utc_day
from .numeric import number
from .telemetry import direct_value

KIND = "winds_aloft_day"
DDL = """
CREATE TABLE IF NOT EXISTS day_speed_samples (
  day TEXT NOT NULL, hex TEXT NOT NULL, direction TEXT NOT NULL,
  gs REAL NOT NULL, first_ts REAL NOT NULL, ts REAL NOT NULL, samples INTEGER NOT NULL,
  values_json TEXT NOT NULL,
  PRIMARY KEY(day, hex, direction)
);
CREATE INDEX IF NOT EXISTS day_speed_samples_day ON day_speed_samples(day, direction);
CREATE INDEX IF NOT EXISTS day_speed_samples_time ON day_speed_samples(ts);
"""
FLOOR_FT = 28000
MIN_ADDRESSES = 30
MIN_ASYMMETRY_KN = 150
LEVEL_RATE_FPM = 500
SPEED_RANGE_KN = (300, 700)
INTERVAL = 600
RETENTION = 3 * 86400
# One kept speed per address and direction this often, so MAX_VALUES spans about an hour.
SAMPLE_SECONDS = 60
MAX_VALUES = 64
MAX_PARTICIPANTS = 12
EAST = (45, 135)
WEST = (225, 315)
WIND_LEVEL_HPA = 250


def initialize(db):
    db.executescript(DDL)


def qualifies(report):
    """ "east", "west", or None for a report that does not qualify.

    The report's own ADS-B source (adsb_icao) must have supplied its ground speed
    (telemetry.direct_value): an MLAT or TIS-B speed listed in it does not count.
    """
    if report.get("type") != "adsb_icao":
        return None
    gs = direct_value(report, "gs")
    alt, track, rate = (number(report.get(key)) for key in ("alt_baro", "track", "baro_rate"))
    if None in (alt, gs, track, rate) or alt < FLOOR_FT or abs(rate) > LEVEL_RATE_FPM:
        return None
    if not SPEED_RANGE_KN[0] <= gs <= SPEED_RANGE_KN[1]:
        return None
    if EAST[0] <= track <= EAST[1]:
        return "east"
    if WEST[0] <= track <= WEST[1]:
        return "west"
    return None


def remember(db, report, stamp, taken=None):
    """Add a qualifying report's ground speed to its address's row for the UTC day.

    ``taken`` maps (address, direction) to the newest kept report's time; with it,
    a report within SAMPLE_SECONDS of that one is skipped.
    """
    direction = qualifies(report)
    if direction is None:
        return
    day, address = utc_day(stamp), str(report["hex"]).lower()
    if taken is not None:
        if stamp - taken.get((address, direction), -math.inf) < SAMPLE_SECONDS:
            return
        taken[(address, direction)] = stamp
    row = db.execute(
        "SELECT samples, values_json FROM day_speed_samples WHERE day=? AND hex=? AND direction=?",
        (day, address, direction),
    ).fetchone()
    # The median reads the latest MAX_VALUES speeds; samples counts every kept report.
    values = (json.loads(row[1]) if row else [])[-(MAX_VALUES - 1) :] + [number(report["gs"])]
    db.execute(
        "INSERT INTO day_speed_samples (day,hex,direction,gs,first_ts,ts,samples,values_json) "
        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(day,hex,direction) DO UPDATE SET "
        "gs=excluded.gs, ts=excluded.ts, samples=excluded.samples, values_json=excluded.values_json",
        (
            day,
            address,
            direction,
            statistics.median(values),
            stamp,
            stamp,
            (row[0] if row else 0) + 1,
            json.dumps(values),
        ),
    )


def _quartiles(values):
    ordered = sorted(values)
    return [round(ordered[len(ordered) // 4], 1), round(ordered[(3 * len(ordered)) // 4], 1)]


def evaluate(db, now, *, prefer=None, present=None):
    """(primary_hex, finding, participants) for the current UTC day, or None.

    The finding is saved on one eastbound aircraft of the day: ``prefer`` when it
    is one, else the fastest that ``present`` holds (default: the fastest of all).
    None when there is no such aircraft. Participants are the other fastest
    eastbound addresses. The cached Open-Meteo wind is attached when the wind
    worker has one (cached_wind).
    """
    day = utc_day(now)
    rows = db.execute(
        "SELECT hex, direction, gs, first_ts, ts, samples FROM day_speed_samples WHERE day=? "
        "ORDER BY direction, gs DESC, hex",
        (day,),
    ).fetchall()
    east = [(h, gs) for h, d, gs, *_ in rows if d == "east"]
    west = [(h, gs) for h, d, gs, *_ in rows if d == "west"]
    if len(east) < MIN_ADDRESSES or len(west) < MIN_ADDRESSES:
        return None
    east_median = statistics.median(gs for _, gs in east)
    west_median = statistics.median(gs for _, gs in west)
    if east_median - west_median < MIN_ASYMMETRY_KN:
        return None
    asymmetry = round(east_median - west_median)
    addresses = [h for h, _ in east]
    if prefer in addresses:
        primary = prefer
    else:
        primary = next((h for h in addresses if present is None or h in present), None)
    if primary is None:
        return None
    block = {
        "day": day,
        "east_addresses": len(east),
        "west_addresses": len(west),
        "east_median_kn": round(east_median, 1),
        "west_median_kn": round(west_median, 1),
        "east_quartiles_kn": _quartiles([gs for _, gs in east]),
        "west_quartiles_kn": _quartiles([gs for _, gs in west]),
        "asymmetry_kn": asymmetry,
        "altitude_floor_ft": FLOOR_FT,
        "evaluated_at": now,
    }
    if reading := cached_wind(db, now):
        block["wind_250_hpa"] = reading
    # The day's first and latest qualifying reports. Timestamps only: the rows keep no
    # position, and build() checks no field.
    started, ended = min(row[3] for row in rows), max(row[4] for row in rows)
    points = [{"ts": started, "source": "adsb_icao"}, {"ts": ended, "source": "adsb_icao"}]
    finding = maneuvers.build(
        KIND,
        points,
        "Eastbound jets far faster than westbound",
        asymmetry,
        "kn",
        evaluated_at=now,
        sample_count=sum(row[5] for row in rows),
        episode_start=started,
    )
    finding.why = (
        f"Across {len(east)} eastbound and {len(west)} westbound aircraft at or above {FLOOR_FT:,} ft, "
        f"the median ground speed differed by {asymmetry:,} kn on {day}."
    )
    finding.explanation = (
        "A ground-speed difference by direction is consistent with strong winds aloft. The wind itself "
        "was not measured; Open-Meteo values, when shown, are a model forecast for one grid cell."
    )
    finding.score = kinds.KINDS[KIND].fixed_score[0]
    finding.confidence = "Many direct ADS-B reports"
    finding.evidence["winds_aloft"] = block
    # The row stays open through its UTC day and ends at the next midnight (the
    # collector's stale sweep).
    finding.evidence["maneuver"]["closes_at"] = day_end(now)
    participants = [{"hex": h, "median_gs_kn": gs} for h, gs in east if h != primary][:MAX_PARTICIPANTS]
    return primary, finding, participants


def day_end(stamp):
    """The UTC midnight that ends the day of ``stamp``."""
    day = datetime.fromtimestamp(stamp, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return (day + timedelta(days=1)).timestamp()


def cached_wind(db, now):
    """The cached Open-Meteo wind at 250 hPa for the hour nearest ``now``, or None.

    The wind worker caches forecasts for aircraft grid cells; the newest cell
    cached for the UTC day stands in for the area. Only the speed, direction, source, and hour are
    returned, never the cell's coordinates. None without the wind worker's table.
    """
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='wind_cache'").fetchone() is None:
        return None
    day = utc_day(now)
    cell = db.execute(
        "SELECT lat, lon FROM wind_cache WHERE day=? ORDER BY fetched_at DESC LIMIT 1", (day,)
    ).fetchone()
    if cell is None:
        return None
    found = wind.cached(db, cell[0], cell[1], day, now)
    if found is None:
        return None
    hour = round(now / 3600) * 3600
    try:
        reading = wind.wind_from_payload(found[0], WIND_LEVEL_HPA, hour)
    except AttributeError, KeyError, TypeError, ValueError, IndexError:
        return None
    if reading is None:
        return None
    return {
        "speed_kn": reading["speed_kt"],
        "direction_deg": reading["direction_deg"],
        "source": wind.SOURCE,
        "hour": datetime.fromtimestamp(hour, timezone.utc).strftime("%Y-%m-%dT%H:00"),
    }


def prune(db, now):
    db.execute("DELETE FROM day_speed_samples WHERE ts<?", (now - RETENTION,))
