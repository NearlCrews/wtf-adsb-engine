#!/usr/bin/env python3
"""ADS-B discovery journal: receiver -> explainable rules -> saved evidence."""

import copy
import itertools
import json
import logging
import math
import os
import re
import sqlite3
import statistics
import time
import traceback
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen

from . import (
    airport_context,
    curation,
    flightaware,
    kinds,
    maneuvers,
    reference,
    rules,
    selection,
    telemetry,
    typecheck,
    watchlists,
    wind,
)
from .replay_patterns import ARCHIVE_ORIGINS, prune_fingerprints
from .rules import (
    MAX_PLAUSIBLE_ALT_FT,
    MAX_PLAUSIBLE_GS_KT,
    PATTERN_WINDOW_SECONDS,
    RANGE_BAND_NM,
    RARE_TYPE_ADDRESSES,
    WATCH_TYPES,
    Finding,
    candidates,
    jump,
    movement_patterns,
    number,
    position,
    range_band,
)
from .telemetry import RECEIVER_FIELDS

ROOT = Path(__file__).resolve().parent
DATABASE = Path(os.getenv("WTF_ADSB_JOURNAL", "journal.sqlite3"))
RECEIVER = os.getenv("WTF_ADSB_RECEIVER_URL", "http://127.0.0.1/data/aircraft.json")
POLL = 10
MAX_RECEIVER_AGE = 45
RARITY_REFRESH = 60
PROFILE_REFRESH = 600
# Movement windows up to this far apart belong to one episode.
PATTERN_GAP = 120
EXPORT_STALL_SECONDS = 300
TRACK_SECONDS = PATTERN_WINDOW_SECONDS
TRACK_LIMIT = 181
CATEGORIES = {"airframes", "performance", "patterns", "oddities", "signals"}
LIVE_ORIGIN = selection.LIVE_ORIGIN
ADDRESS = re.compile(r"~?[0-9a-f]{6}")
ICAO_ADDRESS = re.compile(r"[0-9a-f]{6}")
KEEP = {
    "hex",
    "r",
    "t",
    "desc",
    "year",
    "flight",
    "type",
    "dbFlags",
    "category",
    "gs",
    "tas",
    "mach",
    "alt_baro",
    "alt_geom",
    "baro_rate",
    "geom_rate",
    "lat",
    "lon",
    "seen",
    "seen_pos",
    "squawk",
    "emergency",
    "nic",
    "nac_p",
    "messages",
    "mlat",
    "tisb",
    "track",
    *telemetry.OPTIONAL_NUMBERS,
    "nav_modes",
    "acas_ra",
}
# Plausible numeric bounds: a value outside them is not kept. They lie far beyond
# the outlier findings' bands, so they only stop overflow and non-JSON values.
NUMBER_BOUNDS = {
    "lat": (-90, 90),
    "lon": (-180, 180),
    "seen": (0, 86400),
    "seen_pos": (0, 86400),
    "gs": (0, 5000),
    "tas": (0, 5000),
    "mach": (0, 10),
    "baro_rate": (-100000, 100000),
    "geom_rate": (-100000, 100000),
    "track": (-360, 720),
    "dbFlags": (0, 2**31),
    "messages": (0, 2**53),
    "nic": (0, 15),
    "nac_p": (0, 15),
    "alt_geom": (-10000, 200000),
    "alt_baro": (-10000, 200000),
}
# The private "distant" record keeps a range band computed from the raw report
# (RULES-12); the exact range never enters the sanitized report.
RANGE_BOUND_NM = 1000
# A "ground" report interrupting airborne flight must persist before it splits
# an encounter: a speed-based air/ground switch can flip in slow flight.
GROUND_SPEED_KT = 80
GROUND_CONFIRM_SECONDS = 20
GROUND_LOOKBACK_SECONDS = 60
GROUND_AIRBORNE_FLOOR_FT = 2000
# Identity kinds, an identity conflict, and an emitter mismatch are saved once per aircraft per UTC day.
DAILY_KINDS = kinds.where(daily_row=True)
# Window-based shapes of one aircraft: a later evaluation can see less of the same
# shape, so the saved row keeps its largest measurement (state changes and new
# review flags are still recorded). A multi-aircraft group always takes the latest.
BEST_WINDOW_KINDS = kinds.where(best_window=True)
# Rows saved before a circle or survey grid verified that it then covers.
ABSORBED_KINDS = kinds.table("absorbs")
# Type-envelope and rated-limit readings need rules.MIN_REPORTS coherent reports
# over rules.MIN_REPORT_SECONDS, so no finding rests on a single report; the
# numeric spotting kinds need four over 30 s.
NUMERIC_REPORTS = 4
NUMERIC_SECONDS = 30
# Multi-aircraft detectors run on this cadence over every address's buffer.
MULTI_INTERVAL = 30
GNSS_MEMBER_SECONDS = 900
LEG_SECONDS = 7200
LEG_LIMIT = 64
LEG_REFRESH = 60
# Formation candidates: another aircraft near the latest fix.
FORMATION_NEAR_NM = 4
FORMATION_NEAR_FT = 3000
# An address silent this long has its open episodes ended once ("stale").
STALE_SECONDS = maneuvers.STATUS_GAP
ANOMALY_RETENTION = 7 * 86400
# When the season_speeds table began, the earliest report a season record compares.
SEASON_SINCE = "season_speeds_since"
# The first-sighting baseline of the latest UTC day, so a restart reads one day.
FIRST_BASELINE = "first_sighting_baseline"
FIRST_BASELINE_KEYS = ("day", "coverage_days", "first_day", "types", "countries", "operators")
# A failed first-sighting baseline is retried after this long.
FIRST_RETRY_SECONDS = 3600
# Balloons and high-flying types keep altitude records up to rules.HIGH_FLYING_ALT_FT.
HIGH_FLYING_SQL = (
    f"(COALESCE(typecode,'') IN ({','.join('?' * len(rules.HIGH_FLYING_TYPES))}) OR "
    "COALESCE(CASE WHEN json_valid(last_json) THEN json_extract(last_json,'$.category') END,'')='B2')"
)
HIGH_FLYING_ARGS = tuple(sorted(rules.HIGH_FLYING_TYPES))
LOG = logging.getLogger("discoveries")


class CollectorFault(RuntimeError):
    """Most aircraft reports of one poll failed: a fault of the collector, not the receiver."""

    def __init__(self, errors):
        super().__init__("Most aircraft reports failed")
        self.errors = errors


class JournalConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect(path=DATABASE):
    db = sqlite3.connect(path, timeout=15, factory=JournalConnection)
    try:
        db.row_factory = sqlite3.Row
        # The service root filesystem is read-only. Keep bounded query sorts in memory.
        db.execute("PRAGMA temp_store=MEMORY")
    except BaseException:
        db.close()
        raise
    return db


def meta(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def put_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))


def initialize(path=DATABASE):
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS aircraft (
          hex TEXT PRIMARY KEY, registration TEXT, typecode TEXT, description TEXT, year TEXT,
          first_seen REAL NOT NULL, last_seen REAL NOT NULL, observations INTEGER NOT NULL,
          last_json TEXT NOT NULL, max_gs REAL DEFAULT 0, max_alt REAL DEFAULT 0, max_range REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS daily (day TEXT, hex TEXT, typecode TEXT, PRIMARY KEY(day,hex));
        CREATE INDEX IF NOT EXISTS daily_type ON daily(typecode,day);
        CREATE TABLE IF NOT EXISTS coverage (day TEXT PRIMARY KEY, seconds REAL, polls INTEGER);
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY, hex TEXT NOT NULL, kind TEXT NOT NULL, category TEXT NOT NULL,
          title TEXT NOT NULL, value REAL, unit TEXT, why TEXT, explanation TEXT, score INTEGER,
          confidence TEXT, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
          hits INTEGER NOT NULL, evidence TEXT NOT NULL, origin TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS events_time ON events(last_seen);
        CREATE INDEX IF NOT EXISTS events_aircraft_kind ON events(hex,kind,last_seen);
        CREATE TABLE IF NOT EXISTS track_samples (
          hex TEXT NOT NULL, ts REAL NOT NULL, sample TEXT NOT NULL, PRIMARY KEY(hex,ts));
        CREATE INDEX IF NOT EXISTS track_samples_time ON track_samples(ts);
        CREATE TABLE IF NOT EXISTS telemetry_samples (
          hex TEXT NOT NULL, ts REAL NOT NULL, sample TEXT NOT NULL, PRIMARY KEY(hex,ts));
        CREATE INDEX IF NOT EXISTS telemetry_samples_time ON telemetry_samples(ts);
        CREATE TABLE IF NOT EXISTS anomaly_observations (
          hex TEXT NOT NULL, ts REAL NOT NULL, source TEXT NOT NULL, tag TEXT NOT NULL,
          PRIMARY KEY(hex,ts,tag));
        CREATE INDEX IF NOT EXISTS anomaly_observations_time ON anomaly_observations(ts);
        CREATE TABLE IF NOT EXISTS season_speeds (
          season TEXT NOT NULL, typecode TEXT NOT NULL, hex TEXT NOT NULL,
          max_gs REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(season,typecode,hex));
        """)
        # Journals created before the operator designator was recorded gain the column here.
        if "designator" not in {row[1] for row in db.execute("PRAGMA table_info(daily)")}:
            db.execute("ALTER TABLE daily ADD COLUMN designator TEXT NOT NULL DEFAULT ''")
        db.execute("CREATE INDEX IF NOT EXISTS daily_designator ON daily(designator,day)")
        if meta(db, "installed_at") is None:
            put_meta(db, "installed_at", time.time())
        if meta(db, SEASON_SINCE) is None:
            # Season maxima compare only reports saved since the table began.
            first = db.execute("SELECT MIN(updated_at) FROM season_speeds").fetchone()[0]
            put_meta(db, SEASON_SINCE, time.time() if first is None else first)
        # Idempotent, and run at every start: a rolled-back version may have
        # saved receiver-relative values again.
        remove_receiver_fields(db)
        selection.initialize(db)
        flightaware.initialize(db)
        typecheck.initialize(db)
        wind.initialize(db)


def _without_receiver_fields(value):
    if isinstance(value, dict):
        return {k: _without_receiver_fields(v) for k, v in value.items() if k not in RECEIVER_FIELDS}
    if isinstance(value, list):
        return [_without_receiver_fields(item) for item in value]
    return value


def remove_receiver_fields(db):
    """Remove receiver-relative values saved by earlier versions; idempotent.

    Range and bearing beside an aircraft position locate the receiver, so saved
    reports, evidence and AI requests keep neither, and the range record keeps
    only its band. Freed space is overwritten. Backups made before this keep
    the old values. Records beyond the outlier bounds are reset.
    """
    db.execute("PRAGMA secure_delete=ON")
    for table, column, prefix in (("aircraft", "last_json", "$"), ("events", "evidence", "$.aircraft")):
        paths = ",".join(f"'{prefix}.{key}'" for key in sorted(RECEIVER_FIELDS))
        tests = " OR ".join(
            f"json_type({column},'{prefix}.{key}') IS NOT NULL" for key in sorted(RECEIVER_FIELDS)
        )
        db.execute(
            f"UPDATE {table} SET {column}=json_remove({column},{paths}) WHERE json_valid({column}) AND ({tests})"
        )
    db.execute(
        "UPDATE aircraft SET max_range=CAST(max_range/? AS INTEGER)*? "
        "WHERE max_range>0 AND max_range<>CAST(max_range/? AS INTEGER)*?",
        (RANGE_BAND_NM, RANGE_BAND_NM) * 2,
    )
    # Earlier records beyond the outlier bounds were decoding or solver spikes;
    # balloons and the few high-flying types keep theirs (rules.altitude_bound).
    db.execute("UPDATE aircraft SET max_gs=0 WHERE max_gs>?", (MAX_PLAUSIBLE_GS_KT,))
    db.execute(
        f"UPDATE aircraft SET max_alt=0 WHERE max_alt>? AND NOT (max_alt<=? AND {HIGH_FLYING_SQL})",
        (MAX_PLAUSIBLE_ALT_FT, rules.HIGH_FLYING_ALT_FT, *HIGH_FLYING_ARGS),
    )
    for row in db.execute("SELECT * FROM events WHERE kind='distant' AND json_valid(evidence)").fetchall():
        evidence = json.loads(row["evidence"])
        for sample in (evidence.get("confirmation") or {}).get("samples") or []:
            if isinstance(sample, dict) and number(sample.get("value")) is not None:
                sample["value"] = range_band(sample["value"])
        finding = _coarse_range(
            Finding("distant", row["category"], row["title"], row["value"], "", "", "", 0)
        )
        values = (finding.value, finding.unit, finding.why, json.dumps(evidence))
        if values != (row["value"], row["unit"], row["why"], row["evidence"]):
            db.execute("UPDATE events SET value=?,unit=?,why=?,evidence=? WHERE id=?", (*values, row["id"]))
    # The viewer's cached records may hold values computed before the cleanup.
    db.execute("DELETE FROM meta WHERE key='baseline'")
    if meta(db, "receiver_fields_removed") is None:
        put_meta(db, "receiver_fields_removed", {"at": time.time(), "range_band_nm": RANGE_BAND_NM})


def fetch_json(url, timeout=15):
    with urlopen(url, timeout=timeout) as response:
        return json.load(response)


def sanitize(a):
    """The allowlisted, type-checked and bounded fields of one receiver report."""
    if not isinstance(a, dict):
        return {"hex": ""}
    result = {k: v for k, v in a.items() if k in KEEP}
    for key, value in list(result.items()):
        if isinstance(value, str):
            result[key] = value.strip()[:160]
        elif isinstance(value, float) and not math.isfinite(value):
            result.pop(key)
    for key in {"hex", "r", "t", "desc", "flight", "type", "category", "emergency"}:
        if key in result and not isinstance(result[key], str):
            result.pop(key)
    for key in {"year", "squawk"}:
        if key in result and (isinstance(result[key], bool) or not isinstance(result[key], (str, int))):
            result.pop(key)
    for key in {"mlat", "tisb"}:
        if key in result:
            if isinstance(result[key], list):
                result[key] = [value[:160] for value in result[key] if isinstance(value, str)][:32]
            else:
                result.pop(key)
    result["hex"] = str(result.get("hex", "")).lower()
    for key, (low, high) in NUMBER_BOUNDS.items():
        if key in result and not (key == "alt_baro" and result[key] == "ground"):
            value = number(result[key])
            if value is None or not low <= value <= high:
                result.pop(key)
            else:
                result[key] = value
    # Optional readsb fields within telemetry's bounds, allowlisted navigation
    # modes and advisory fields; free text and raw message bytes never pass.
    optional = telemetry.optional(result)
    for key in (*telemetry.OPTIONAL_NUMBERS, "nav_modes", "acas_ra"):
        result.pop(key, None)
    result.update(optional)
    return result


def stored(a):
    """A report as saved: receiver-relative values stay in memory only."""
    return {key: value for key, value in a.items() if key not in RECEIVER_FIELDS}


def _sources(a):
    """The provenance fields telemetry.source_for reads, for a sanitized report."""
    return {"source": a.get("type", "unknown"), "mlat": a.get("mlat"), "tisb": a.get("tisb")}


def _direct(a, field, sources=None):
    """The field's value when the aircraft's own ADS-B transmission supplied it."""
    value = number(a.get(field))
    direct = telemetry.source_for(sources or _sources(a), field) in telemetry.DIRECT
    return value if value is not None and direct else None


def remember(db, a, first, last, observations=1, *, performance=True, band=0):
    """Save the latest report. Records come only from direct reports, and an
    identity conflict (``performance=False``) adds nothing to them. ``band`` is
    the report's range band (rules.range_band); the exact range is never saved."""
    address = a["hex"]
    sources = _sources(a)
    gs = _direct(a, "gs", sources) if performance else None
    altitude = _direct(a, "alt_baro", sources) if performance else None
    # Values beyond the outlier bounds are reported as outliers, never as records.
    gs = gs if gs is not None and gs <= MAX_PLAUSIBLE_GS_KT else None
    altitude = altitude if altitude is not None and altitude <= rules.altitude_bound(a) else None
    db.execute(
        """INSERT INTO aircraft VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(hex) DO UPDATE SET
        registration=CASE WHEN excluded.last_seen>=aircraft.last_seen
          THEN COALESCE(NULLIF(excluded.registration,''),aircraft.registration)
          ELSE COALESCE(NULLIF(aircraft.registration,''),excluded.registration) END,
        typecode=CASE WHEN excluded.last_seen>=aircraft.last_seen
          THEN COALESCE(NULLIF(excluded.typecode,''),aircraft.typecode)
          ELSE COALESCE(NULLIF(aircraft.typecode,''),excluded.typecode) END,
        description=CASE WHEN excluded.last_seen>=aircraft.last_seen
          THEN COALESCE(NULLIF(excluded.description,''),aircraft.description)
          ELSE COALESCE(NULLIF(aircraft.description,''),excluded.description) END,
        year=CASE WHEN excluded.last_seen>=aircraft.last_seen
          THEN COALESCE(NULLIF(excluded.year,''),aircraft.year)
          ELSE COALESCE(NULLIF(aircraft.year,''),excluded.year) END,
        first_seen=MIN(aircraft.first_seen,excluded.first_seen),
        last_json=CASE WHEN excluded.last_seen>=aircraft.last_seen THEN excluded.last_json ELSE aircraft.last_json END,
        last_seen=MAX(aircraft.last_seen,excluded.last_seen),
        observations=aircraft.observations+excluded.observations,
        max_gs=MAX(aircraft.max_gs,excluded.max_gs), max_alt=MAX(aircraft.max_alt,excluded.max_alt),
        max_range=MAX(aircraft.max_range,excluded.max_range)
      """,
        (
            address,
            a.get("r", ""),
            a.get("t", ""),
            a.get("desc", ""),
            str(a.get("year", "")),
            first,
            last,
            observations,
            json.dumps(stored(a)),
            gs or 0,
            altitude or 0,
            band,
        ),
    )
    designator = rules.callsign_designator(a)
    for stamp in {first, last}:
        day = datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()
        db.execute(
            """INSERT INTO daily VALUES (?,?,?,?) ON CONFLICT(day,hex) DO UPDATE SET typecode=
            CASE WHEN ?>=(SELECT last_seen FROM aircraft WHERE hex=excluded.hex)
              THEN COALESCE(NULLIF(excluded.typecode,''),daily.typecode)
              ELSE COALESCE(NULLIF(daily.typecode,''),excluded.typecode) END,
            designator=CASE WHEN daily.designator='' THEN excluded.designator ELSE daily.designator END""",
            (day, address, a.get("t", ""), designator, last),
        )


STATUS_PREFIX = "special_squawk_"


def _status_continuation(db, address, kind, episode):
    """An earlier row of this status episode that the 20-minute buffer no longer shows whole.

    A long sequence loses its first reports: its start moves later, and the
    remaining codes can name it differently (7700 then 7600 reads as 7600).
    A row that began earlier continues when this episode starts inside its
    span, which includes a brief reverted code the episode later rejoined, or
    within 60 s after it while it is still open. Priority declarations stay
    separate.
    """
    priority = kind[len(STATUS_PREFIX) :] in maneuvers.PRIORITY_STATUSES
    started = episode["started_at"]
    rows = db.execute(
        r"""SELECT * FROM events WHERE hex=? AND kind LIKE 'special\_squawk\_%' ESCAPE '\'
        AND origin=? AND first_seen<=? AND last_seen>=?
        AND json_extract(evidence,'$.maneuver.episode_start')<?
        ORDER BY last_seen DESC""",
        (address, LIVE_ORIGIN, started, started - maneuvers.STATUS_GAP, episode["episode_start"]),
    )
    for row in rows:
        if (row["kind"][len(STATUS_PREFIX) :] in maneuvers.PRIORITY_STATUSES) != priority:
            continue
        state = (json.loads(row["evidence"]).get("maneuver") or {}).get("state")
        if row["last_seen"] >= started or state == "active":
            return row
    return None


# Status tags that hold only when every report qualifies.
WHOLE_STATUS_TAGS = frozenset({"ground_only_status", "status_from_mode_s_reply"})


def _continued_status(old, new):
    """Keep a long status episode's earlier codes, start, tags and flags beyond the buffer.

    Report counts and the ground share still describe the retained reports.
    """
    start, old_start = number(new.get("started_at")), number(old.get("started_at"))
    if start is None or old_start is None or old_start >= start:
        return new
    sequence, at = [], old_start
    for item in old.get("code_sequence") or []:
        seconds = number(item.get("seconds")) if isinstance(item, dict) else None
        if seconds is None or at >= start:
            break
        sequence.append({"code": item.get("code"), "seconds": round(min(seconds, start - at), 1)})
        at += seconds
    for item in new.get("code_sequence") or []:
        if sequence and isinstance(item, dict) and sequence[-1]["code"] == item.get("code"):
            sequence[-1] = {
                "code": item["code"],
                "seconds": round(sequence[-1]["seconds"] + item["seconds"], 1),
            }
        else:
            sequence.append(dict(item))
    merged = {**new, "started_at": old_start, "code_sequence": sequence}
    if number(new.get("ended_at")) is not None:
        merged["duration_seconds"] = new["ended_at"] - old_start
    # An all-reports tag needs both parts; a disagreement before the buffer still
    # counts, and review flags only accumulate.
    old_tags, new_tags = set(old.get("context_tags") or []), set(new.get("context_tags") or [])
    merged["context_tags"] = sorted(
        (new_tags - WHOLE_STATUS_TAGS)
        | (old_tags & new_tags & WHOLE_STATUS_TAGS)
        | (old_tags & {"status_field_disagreement"})
    )
    flags = sorted(set(old.get("quality_flags") or []) | set(new.get("quality_flags") or []))
    merged["confirmed"] = old.get("confirmed") is True or new.get("confirmed") is True
    merged.update(quality_flags=flags, quality_review_required=bool(flags) or not merged["confirmed"])
    return merged


def _keeps_best(previous, finding, old_episode, episode):
    """A smaller later window of the same shape keeps the saved measurement,
    unless the new evaluation cleared a review flag or changed confirmation."""
    return (
        finding.kind in BEST_WINDOW_KINDS
        and bool(old_episode)
        and previous["value"] is not None
        and finding.value is not None
        and abs(finding.value) < abs(previous["value"])
        and set(episode.get("quality_flags", [])) >= set(old_episode.get("quality_flags", []))
        and episode.get("confirmed") == old_episode.get("confirmed")
    )


def _implausible_speed(db, address, session, previous):
    """Whether an encounter's ground speed stands on a rated-speed window the wind worker
    found implausible (wind.flag_ground_speed).

    The flag stays on the row once set, and a new or rewritten ``fast`` or season
    record row of an encounter with such a window takes it, so a later report never
    clears it.
    """
    if previous is not None:
        flags = json.loads(previous["evidence"]).get("quality_flags") or []
        if rules.RATED_SPEED_IMPLAUSIBLE in flags:
            return True
    if session is None:
        return False
    return (
        db.execute(
            """SELECT 1 FROM events WHERE hex=? AND kind='above_rated_speed' AND origin=?
            AND json_valid(evidence) AND json_extract(evidence,'$.encounter_start')=?
            AND json_extract(evidence,'$.rated_limit.verdict')='implausible' LIMIT 1""",
            (address, LIVE_ORIGIN, session),
        ).fetchone()
        is not None
    )


def record(db, a, finding, first, last, hits=1, origin=LIVE_ORIGIN, extra=None):
    a = stored(a)
    live = origin == LIVE_ORIGIN
    # Pattern episodes should not collapse separate flights six hours apart.
    merge_gap = PATTERN_GAP if finding.category == "patterns" else selection.ENCOUNTER_GAP
    session = (extra or {}).get("encounter_start")
    episode = (finding.evidence or {}).get("maneuver", {})
    episode_start = episode.get("episode_start")
    # A reconstructed rate/status episode can predate the current ground or
    # airborne encounter. Its own start identifies it across that transition.
    if episode_start is not None:
        session = None
    window, window_args = "last_seen>=? AND first_seen<=?", [first - merge_gap, last + merge_gap]
    if session is not None:
        window += " AND json_extract(evidence,'$.encounter_start')=?"
        window_args.append(session)
    if episode_start is not None:
        window += """ AND (json_extract(evidence,'$.maneuver.episode_start')=? OR
            (last_seen>=? AND (json_extract(evidence,'$.maneuver.state')='active' OR
             (json_extract(evidence,'$.maneuver.ended_at')=? AND
              json_extract(evidence,'$.maneuver.evaluated_at')=?))))"""
        window_args += [episode_start, first, episode["ended_at"], episode["evaluated_at"]]
    # An archive import never merges into a live row (nor live reports into an
    # archive row), so live evidence, origin and hit counts stay intact. The
    # origins are those replay_patterns.live_row() treats as archive.
    marks = ",".join("?" * len(ARCHIVE_ORIGINS))
    where, args = (
        ["hex=?", "kind=?", f"origin {'IN' if origin in ARCHIVE_ORIGINS else 'NOT IN'} ({marks})"],
        [a["hex"], finding.kind, *ARCHIVE_ORIGINS],
    )
    daily = live and finding.kind in DAILY_KINDS and episode_start is None
    if daily:
        # Identity findings: one row per aircraft and UTC day, across encounters.
        day = first - first % 86400
        where.append(
            f"(({window}) OR (first_seen>=? AND first_seen<? AND "
            "COALESCE(json_extract(evidence,'$.aircraft.r'),'')=?))"
        )
        args += [*window_args, day, day + 86400, a.get("r") or ""]
    else:
        where.append(window)
        args += window_args
    previous = db.execute(
        "SELECT * FROM events WHERE " + " AND ".join(where) + " ORDER BY last_seen DESC LIMIT 1", args
    ).fetchone()
    if previous is None and live and episode_start is not None and finding.kind.startswith(STATUS_PREFIX):
        previous = _status_continuation(db, a["hex"], finding.kind, episode)
    evidence = {"aircraft": a, "observed_at": last, **(finding.evidence or {}), **(extra or {})}
    if (
        live
        and finding.kind in rules.GROUND_SPEED_KINDS
        and _implausible_speed(db, a["hex"], session, previous)
    ):
        evidence["quality_flags"] = sorted(
            set(evidence.get("quality_flags") or []) | {rules.RATED_SPEED_IMPLAUSIBLE}
        )
    if previous and daily:
        old_evidence = json.loads(previous["evidence"])
        if not (
            old_evidence.get("encounter_start") == session
            and previous["last_seen"] >= first - merge_gap
            and previous["first_seen"] <= last + merge_gap
        ):
            flags = set((finding.evidence or {}).get("quality_flags") or [])
            if not flags < set(old_evidence.get("quality_flags") or []):
                # A later encounter the same day only adds its reports. The row keeps
                # the first encounter's interval and evidence, so it stays in that
                # encounter's group and never holds it open.
                db.execute("UPDATE events SET hits=hits+? WHERE id=?", (hits, previous["id"]))
                return previous["id"]
            # A later encounter clears a review flag the day's row carries: it gets
            # its own row, which the rest of the day then joins.
            previous = None
    if previous:
        old_evidence = json.loads(previous["evidence"])
        old_episode = old_evidence.get("maneuver", {})
        if episode and evidence.get("context_samples"):
            evidence["context_samples"] = maneuvers.merge_context(
                old_evidence.get("context_samples", []), evidence["context_samples"]
            )
        ending = old_episode.get("state") == "active" and episode.get("state") not in (None, "active")
        if episode and (
            old_episode.get("evaluated_at", -1) > episode.get("evaluated_at", 0)
            or (old_episode.get("evaluated_at", -1) == episode.get("evaluated_at", 0) and not ending)
        ):
            context = evidence.get("context_samples", [])
            old_context = old_evidence.get("context_samples", [])
            if (
                old_episode.get("evaluated_at") == episode.get("evaluated_at")
                and context
                and (not old_context or context[-1]["ts"] > old_context[-1]["ts"])
            ):
                old_evidence["context_samples"] = context
                db.execute(
                    "UPDATE events SET evidence=? WHERE id=?", (json.dumps(old_evidence), previous["id"])
                )
            return previous["id"]
        if episode_start is not None and old_episode.get("episode_start") is not None:
            evidence["maneuver"] = {**episode, "episode_start": old_episode["episode_start"]}
            if "encounter_start" in old_evidence:
                evidence["encounter_start"] = old_evidence["encounter_start"]
            if previous["kind"].startswith(STATUS_PREFIX):
                # The row keeps its name; its sequence keeps the codes before the buffer.
                evidence["maneuver"] = _continued_status(
                    old_episode, {**evidence["maneuver"], "kind": previous["kind"]}
                )
        refresh = old_evidence.get("replay_refresh")
        if isinstance(refresh, dict) and refresh.get("retained_legacy_hit_count") is True:
            evidence["replay_refresh"] = refresh
        if episode and _keeps_best(previous, finding, old_episode, episode):
            # Keep the larger measurement; record the latest state, ending and
            # review flags (the later window's flags include the saved ones).
            kept = {
                **old_episode,
                **{key: episode[key] for key in ("state", "evaluated_at") if key in episode},
            }
            kept.pop("end_reason", None)
            if "end_reason" in episode:
                kept["end_reason"] = episode["end_reason"]
            kept["quality_flags"] = sorted(episode.get("quality_flags", []))
            kept["quality_review_required"] = bool(kept["quality_flags"]) or kept.get("confirmed") is not True
            old_evidence["maneuver"] = kept
            db.execute(
                "UPDATE events SET first_seen=MIN(first_seen,?),last_seen=MAX(last_seen,?),hits=hits+?,evidence=? WHERE id=?",
                (first, last, hits, json.dumps(old_evidence), previous["id"]),
            )
            return previous["id"]
    values = (
        finding.title,
        finding.value,
        finding.unit,
        finding.why,
        finding.explanation,
        finding.score,
        finding.confidence,
        json.dumps(evidence),
        origin,
    )
    if previous:
        if first < previous["first_seen"]:
            db.execute(
                "UPDATE event_reviews SET event_first=? WHERE event_id=? AND hex=? AND kind=? AND event_first=?",
                (first, previous["id"], previous["hex"], previous["kind"], previous["first_seen"]),
            )
        db.execute(
            "UPDATE events SET first_seen=MIN(first_seen,?),last_seen=MAX(last_seen,?),hits=hits+? WHERE id=?",
            (first, last, hits, previous["id"]),
        )
        if (
            episode
            or finding.value is None
            or previous["value"] is None
            or abs(finding.value) >= abs(previous["value"])
        ):
            db.execute(
                "UPDATE events SET title=?,value=?,unit=?,why=?,explanation=?,score=?,confidence=?,evidence=?,origin=? WHERE id=?",
                (*values, previous["id"]),
            )
        return previous["id"]
    else:
        return db.execute(
            """INSERT INTO events
        (hex,kind,category,title,value,unit,why,explanation,score,confidence,evidence,origin,first_seen,last_seen,hits)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (a["hex"], finding.kind, finding.category, *values, first, last, hits),
        ).lastrowid


def fresh_track_sample(a, now):
    """A fresh position can be useful even without transmitted speed or heading."""
    if not position(a) or a.get("alt_baro") == "ground":
        return None
    altitude = number(a.get("alt_baro"))
    if altitude is None:
        altitude = number(a.get("alt_geom"))
    return {
        "ts": now - a["seen_pos"],
        "lat": a["lat"],
        "lon": a["lon"],
        "gs": number(a.get("gs")),
        "alt": altitude,
        "track": number(a.get("track")),
        "source": a.get("type", "unknown"),
        **{key: a[key] for key in ("mlat", "tisb", "nic", "nac_p", "alt_baro", "alt_geom") if key in a},
    }


def _extra(session, checks):
    """Evidence every finding of one report or encounter carries."""
    return {"encounter_start": session, "identity_checks": checks}


def watchlist_finding(match):
    if not match or match.get("public_claim_allowed") is not True:
        return None
    return Finding(
        "watchlist_match",
        "airframes",
        "A name on the enthusiast watchlist",
        None,
        "",
        "Matching address and registration appear in Plane Alert DB's "
        + match["neutral_category"]
        + " category.",
        "This is an attributed community watchlist match, not verified current ownership, a mission, or evidence that any associated person is aboard.",
        75,
        "Community metadata; association needs verification",
        {"watchlist": match},
    )


def rarity_context(db, now):
    # Thirty UTC days including today.
    cutoff = datetime.fromtimestamp(now - 29 * 86400, timezone.utc).date().isoformat()
    # daily's key is (day, hex): each row is one distinct day for that address.
    aircraft_days, known_total = {}, 0
    for address, days, known in db.execute(
        "SELECT hex,COUNT(*),COALESCE(MAX(typecode<>''),0) FROM daily WHERE day>=? AND hex NOT LIKE '~%' GROUP BY hex",
        (cutoff,),
    ):
        aircraft_days[address] = days
        known_total += known
    counts, type_days = {}, {}
    for typecode, addresses, days in db.execute(
        "SELECT typecode,COUNT(DISTINCT hex),COUNT(DISTINCT day) FROM daily WHERE day>=? AND typecode<>'' AND hex NOT LIKE '~%' GROUP BY typecode",
        (cutoff,),
    ):
        counts[typecode], type_days[typecode] = addresses, days
    coverage = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(seconds),0) FROM coverage WHERE day>=? AND seconds>=?",
        (cutoff, rules.COVERAGE_SECONDS),
    ).fetchone()
    total = len(aircraft_days)
    return {
        "counts": counts,
        "total": total,
        "known_total": known_total,
        "type_days": type_days,
        "aircraft_days": aircraft_days,
        "days": coverage[0],
        "hours": coverage[1] / 3600,
        "ready": coverage[0] >= 7 and coverage[1] >= 86400 and total >= 250,
    }


def _verdict(patterns):
    """This evaluation's circling verdict (see rules.orbit_verdict), or False.

    The collector classifies every pattern's airport context first, so the
    verdict records that classification even when it found no runway.
    """
    try:
        verdict = rules.orbit_verdict(patterns)
    except KeyError, TypeError, AttributeError:
        # A pattern without its saved samples cannot drive episode precedence.
        return False
    if verdict:
        verdict.setdefault("airport_context", None)
    return verdict


def _orbit_onset(verdict, straight=120):
    """When the orbit itself began in a circling verdict's window (rules.orbit_verdict).

    The window can open on straight flight before the turning starts. The onset
    follows the last straight run (headings within 15° for at least two
    minutes) that turning follows; without one it is the window start.
    Racetrack and circuit legs are shorter, so they stay inside the orbit.
    """
    start = number(verdict.get("started_at"))
    points = [
        p
        for p in verdict.get("pattern_samples") or []
        if None not in (number(p.get("ts")), number(p.get("lat")), number(p.get("lon")))
    ]
    run = []
    for leg in maneuvers.track_legs(points, 0.02):
        if run and abs(maneuvers.wrap_angle(leg["heading"] - run[0]["heading"])) > 15:
            # Only a straight run that turning follows precedes the orbit.
            if run[-1]["t1"] - run[0]["t0"] >= straight:
                start = max(start or run[-1]["t1"], run[-1]["t1"])
            run = []
        run.append(leg)
    return start


def _coarse_range(finding):
    """A "distant" row saved with an exact range keeps only its band (RULES-12);
    for the cleanup of rows from earlier versions."""
    if finding.kind != "distant" or number(finding.value) is None:
        return finding
    low = range_band(finding.value)
    finding.value, finding.unit = low, "nm band"
    finding.why = (
        f"A position was reported between {low:,.0f} and {low + RANGE_BAND_NM:,.0f} nautical miles "
        f"from the receiver. Only this {RANGE_BAND_NM}-nm band is saved."
    )
    return finding


def _sustained_speed(history):
    """The lowest ground speed over the latest minute of one direct ADS-B track.

    Every report is airborne, from one direct source with a directly reported
    ground speed, at most 45 s apart and without position-quality flags.
    Returns (window summary or None, the reports examined).
    """
    window = []
    for point in reversed(history):
        if (
            not telemetry.speed_point_usable(point)
            or (window and point.get("source") != window[-1].get("source"))
            or (window and window[-1]["ts"] - point["ts"] > rules.SEASON_SAMPLE_GAP_SECONDS)
        ):
            break
        window.append(point)
        if window[0]["ts"] - point["ts"] >= rules.SEASON_SUSTAINED_SECONDS:
            break
    window.reverse()
    if (
        len(window) < rules.SEASON_MIN_SAMPLES
        or window[-1]["ts"] - window[0]["ts"] < rules.SEASON_SUSTAINED_SECONDS
        or telemetry.window_quality(window)
    ):
        return None, window
    summary = {
        "gs": min(number(point["gs"]) for point in window),
        "duration_seconds": window[-1]["ts"] - window[0]["ts"],
        "samples": len(window),
        "source": window[-1]["source"],
    }
    return summary, window


class Detector:
    def __init__(self, path=DATABASE, *, require_complete=False):
        self.path = path
        self.require_complete = bool(require_complete)
        self.pending, self.previous, self.tracks = {}, {}, defaultdict(lambda: deque(maxlen=TRACK_LIMIT))
        self.telemetry = defaultdict(lambda: deque(maxlen=telemetry.LIMIT))
        self.last_poll = 0
        self.next_cleanup = 0
        self.next_track_cleanup = 0
        self.tracks_restored = False
        self.profiles, self.next_profiles = {}, 0
        self.rarity, self.next_rarity = None, 0
        # First-sighting baselines by UTC day; each reads only earlier days. Reports
        # around midnight can belong to either day, so two are kept. A failed
        # build waits FIRST_RETRY_SECONDS before the next attempt.
        self.first_sightings, self.first_retry = {}, {}
        self.failed = set()
        # Reference data loads once; the status meta rows are written when they change.
        self.reference_status, self.saved_status = None, {}
        self.aircraft_failures = 0
        self._forget()

    def _forget(self):
        """Derived per-aircraft state, rebuilt from telemetry as reports arrive.

        Circling verdicts per encounter, straight legs, TCAS threat addresses,
        GNSS cluster members, open multi-aircraft episodes, seasonal-speed
        caches and the newest report already ended as stale. Each is bounded by
        time, encounter or address and pruned with them.
        """
        self.orbits, self.legs, self.acas, self.gnss, self.multi = {}, {}, {}, {}, {}
        self.season_stats, self.season_own, self.stale_ended = {}, {}, {}
        # Each buffered report's normalized position and track sample, so
        # analysis and movement patterns reuse them.
        self.position_caches = defaultdict(telemetry.PositionCache)
        self.segment_caches = defaultdict(lambda: rules.IdentityCache(rules.segment_point))
        self.season_since = None
        self.next_multi = 0

    def _forget_aircraft(self, address):
        """Drop one aircraft's in-memory state after its report failed and its writes were undone."""
        self.previous.pop(address, None)
        self.pending = {key: value for key, value in self.pending.items() if key[0] != address}
        for state in (
            self.tracks,
            self.telemetry,
            self.orbits,
            self.legs,
            self.acas,
            self.season_own,
            self.position_caches,
            self.segment_caches,
        ):
            state.pop(address, None)
        self.gnss = {key: member for key, member in self.gnss.items() if key[0] != address}
        # Season counts may include a row that was rolled back; they are re-read.
        self.season_stats.clear()

    def restore_tracks(self, db, now):
        # The short rolling buffer survives a routine container upgrade. Event
        # evidence is retained separately; this is not an indefinite position log.
        rows = db.execute(
            "SELECT hex,sample FROM track_samples WHERE ts>=? AND ts<=? ORDER BY ts",
            (now - TRACK_SECONDS, now),
        )
        for row in rows:
            self.tracks[row["hex"]].append(json.loads(row["sample"]))
        for row in db.execute(
            "SELECT hex,sample FROM telemetry_samples WHERE ts>=? AND ts<=? ORDER BY ts",
            (now - telemetry.WINDOW, now),
        ):
            self.telemetry[row["hex"]].append(json.loads(row["sample"]))
        self.tracks_restored = True

    def process(self, snapshot, wall_now=None):
        wall_now = time.time() if wall_now is None else wall_now
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("aircraft"), list):
            raise ValueError("Invalid receiver snapshot")
        now = number(snapshot.get("now"))
        if now is None or not -5 <= wall_now - now <= MAX_RECEIVER_AGE:
            raise ValueError("Receiver snapshot is stale")
        if now <= self.last_poll:
            return
        try:
            self._process_snapshot(snapshot, now, wall_now)
        except Exception:
            # SQLite rolls back the batch on failure. Rebuild the matching
            # in-memory state from committed positions before processing again.
            self.pending.clear()
            self.previous.clear()
            self.tracks.clear()
            self.telemetry.clear()
            self._forget()
            self.tracks_restored = False
            self.next_cleanup = self.next_track_cleanup = 0
            # A prune may have reduced the in-memory first-sighting baselines in the
            # batch that rolled back; the next lookup reads the saved one again.
            self.first_sightings.clear()
            # The caller may record its own status; write every status row again.
            self.saved_status = {}
            raise
        self.last_poll = now

    def _process_snapshot(self, snapshot, now, wall_now):
        with connect(self.path) as db:
            if not self.tracks_restored:
                self.restore_tracks(db, now)
            # Both baselines change slowly. The 30-day rarity window moves at UTC
            # midnight, and _observe asks for an early read when a report could
            # change a rare type's counts. A baseline that is not ready, or small
            # enough for one address to move the 1% share, is read every poll.
            if now >= self.next_rarity:
                self.rarity = rarity_context(db, now)
                settled = self.rarity["ready"] and self.rarity["known_total"] >= 100 * RARE_TYPE_ADDRESSES
                self.next_rarity = min(now + RARITY_REFRESH, (now // 86400 + 1) * 86400) if settled else 0
                put_meta(db, "baseline", {"at": now, **baseline(db, self.rarity)})
            if now >= self.next_profiles:
                self.profiles = selection.performance_profiles(db)
                self.next_profiles = now + PROFILE_REFRESH
            if self.reference_status is None:
                self.reference_status = reference.status()
            status = {
                "last_success": wall_now,
                "last_receiver_timestamp": now,
                "last_error": None,
                "live_aircraft": 0,
                "metadata_gaps": 0,
                "watchlist_matches": 0,
                "watchlist_last_error": None,
                "detector_errors": {},
                "reference_data": self.reference_status,
                "pattern_detector": {
                    "version": 4,
                    "episodes": maneuvers.VERSION,
                    "rules": rules.VERSION,
                    "last_run": now,
                    "fresh_positions": 0,
                    "evaluated_windows": 0,
                    "candidate_patterns": 0,
                    "retention_seconds": TRACK_SECONDS,
                },
            }
            # One aircraft's failure undoes only its own writes; a database error
            # still fails the whole batch. A savepoint needs an open transaction.
            # The write lock is taken first: a deferred transaction that reads and then
            # writes cannot wait for another writer, so SQLite would fail the poll at
            # once instead of waiting within the busy timeout.
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            self.aircraft_failures = 0
            for raw in snapshot["aircraft"]:
                address = str(raw.get("hex", "")).strip().lower() if isinstance(raw, dict) else ""
                self._isolated(db, status, address, self._observe, db, raw, now, status)
            if self.aircraft_failures >= max(3, len(snapshot["aircraft"]) / 2):
                # Most reports failing is a fault of the collector, not of one aircraft:
                # fail the batch so health reports it.
                raise CollectorFault(dict(status["detector_errors"]))
            self._guarded(status, "correlations", self._correlations, db, now)
            if now >= self.next_multi:
                self._guarded(status, "multi_aircraft", self._multi, db, now)
                self.next_multi = now + MULTI_INTERVAL
            day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
            elapsed = max(0, min(30, now - self.last_poll)) if self.last_poll else 0
            db.execute(
                "INSERT INTO coverage VALUES (?,?,1) ON CONFLICT(day) DO UPDATE SET seconds=seconds+excluded.seconds,polls=polls+1",
                (day, elapsed),
            )
            if now >= self.next_track_cleanup:
                # Open episodes of addresses silent for a minute end before the buffers go.
                self._guarded(status, "stale_endings", self._stale, db, now, status)
                db.execute("DELETE FROM track_samples WHERE ts<? OR ts>?", (now - TRACK_SECONDS, now + 5))
                db.execute(
                    "DELETE FROM telemetry_samples WHERE ts<? OR ts>?", (now - telemetry.WINDOW, now + 5)
                )
                # Private diagnostics; the tags carry no position or receiver value.
                db.execute("DELETE FROM anomaly_observations WHERE ts<?", (now - ANOMALY_RETENTION,))
                # An episode not found again while its window was in the buffer has ended.
                db.execute(
                    """UPDATE events SET evidence=json_set(evidence,'$.maneuver.state','ended',
                      '$.maneuver.end_reason','stale','$.maneuver.evaluated_at',MAX(last_seen+?,
                      COALESCE(json_extract(evidence,'$.maneuver.evaluated_at'),0)))
                    WHERE last_seen<? AND last_seen>=? AND origin=?
                      AND json_extract(evidence,'$.maneuver.state')='active'""",
                    (
                        STALE_SECONDS,
                        now - TRACK_SECONDS - STALE_SECONDS,
                        now - TRACK_SECONDS - 3600,
                        LIVE_ORIGIN,
                    ),
                )
                self.telemetry = defaultdict(
                    lambda: deque(maxlen=telemetry.LIMIT),
                    {k: v for k, v in self.telemetry.items() if v and v[-1]["ts"] >= now - telemetry.WINDOW},
                )
                self.tracks = defaultdict(
                    lambda: deque(maxlen=TRACK_LIMIT),
                    {k: v for k, v in self.tracks.items() if v and v[-1]["ts"] >= now - TRACK_SECONDS},
                )
                for state in (self.orbits, self.legs, self.acas, self.stale_ended, self.position_caches):
                    for address in [address for address in state if address not in self.telemetry]:
                        del state[address]
                for address in [address for address in self.segment_caches if address not in self.tracks]:
                    del self.segment_caches[address]
                self.next_track_cleanup = now + 60
            if self.require_complete and status["detector_errors"]:
                # The native adapter cannot acknowledge a partially evaluated
                # replay snapshot: its core checkpoint belongs to another DB.
                # Roll back before marking this source timestamp committed so a
                # retry after the failing phase recovers evaluates it again.
                raise CollectorFault(dict(status["detector_errors"]))
            for key, value in status.items():
                if self.saved_status.get(key, self) != value:
                    put_meta(db, key, value)
                    self.saved_status[key] = value
            if now >= self.next_cleanup:
                self._prune(db, now)
                self.next_cleanup = now + 3600

    def _isolated(self, db, status, address, function, *args):
        """Run one aircraft's work in a savepoint: a failure undoes only its writes.

        The failure is counted in status, and its first traceback is logged
        without the exception text, which can hold report values.
        """
        db.execute("SAVEPOINT aircraft")
        try:
            function(*args)
        except sqlite3.Error:
            raise
        except Exception as error:  # noqa: BLE001 - one aircraft must not stop live collection
            db.execute("ROLLBACK TO aircraft")
            self._forget_aircraft(address)
            self.aircraft_failures += 1
            status["detector_errors"]["aircraft"] = (
                f"{type(error).__name__} ({self.aircraft_failures} reports)"
            )
            if ("aircraft", type(error).__name__) not in self.failed:
                self.failed.add(("aircraft", type(error).__name__))
                LOG.warning(
                    "One aircraft's report failed (%s); collection continues\n%s",
                    type(error).__name__,
                    "".join(traceback.format_tb(error.__traceback__)),
                )
        db.execute("RELEASE aircraft")

    def _guarded(self, status, name, function, *args, default=None):
        """An optional detector's failure is reported in status; collection continues.

        Database errors still fail the batch, so SQLite rolls it back whole.
        """
        try:
            return function(*args)
        except sqlite3.Error:
            raise
        except Exception as error:  # noqa: BLE001 - one detector must not stop live collection
            status["detector_errors"][name] = type(error).__name__
            if name not in self.failed:
                self.failed.add(name)
                LOG.warning("Detector %s failed (%s); collection continues", name, type(error).__name__)
            return default

    def _drop_track(self, db, address):
        self.tracks.pop(address, None)
        self.segment_caches.pop(address, None)
        db.execute("DELETE FROM track_samples WHERE hex=?", (address,))
        if address in self.orbits:
            # Without its track this evaluation has no circle; the encounter's
            # earlier verdict still holds its precedence.
            self.orbits[address]["current"] = False

    def _ground(self, a, known, stamp):
        """Debounce a brief "ground" report inside airborne flight (RULES-11).

        Ground is accepted at once without recent airborne reports. After them
        it needs a low ground speed, no report above 2,000 ft in the previous
        minute, and two ground reports at least 20 s apart; it then holds until
        an airborne report. Otherwise the report is airborne with unknown altitude.
        """
        state = dict((known or {}).get("ground") or {})
        if a.get("alt_baro") != "ground":
            altitude = number(a.get("alt_baro"))
            state.update(since=None, reports=0, accepted=False)
            if altitude is not None:
                state["airborne_at"] = stamp
                if altitude > GROUND_AIRBORNE_FLOOR_FT:
                    state["high_at"] = stamp
            return state
        since = stamp if state.get("since") is None else state["since"]
        reports = state.get("reports", 0) + 1
        speed = number(a.get("gs"))
        airborne = stamp - state.get("airborne_at", -math.inf) <= GROUND_LOOKBACK_SECONDS
        high = stamp - state.get("high_at", -math.inf) <= GROUND_LOOKBACK_SECONDS
        accepted = state.get("accepted") or (
            (speed is None or speed <= GROUND_SPEED_KT)
            and not high
            and (not airborne or (reports >= 2 and stamp - since >= GROUND_CONFIRM_SECONDS))
        )
        state.update(since=since, reports=reports, accepted=bool(accepted))
        if not accepted:
            a.pop("alt_baro")
        return state

    def _observe(self, db, raw, now, status):
        """Record one aircraft report and advance its pending confirmations."""
        a = sanitize(raw)
        address = a["hex"]
        if not ADDRESS.fullmatch(address):
            return
        seen = number(a.get("seen"))
        if seen is None or not 0 <= seen <= 30:
            return
        status["live_aircraft"] += 1
        status["metadata_gaps"] += int(not a.get("r") or not a.get("t"))
        old = self.previous.get(address)
        # Different JSON timestamps do not make a repeated aircraft message new.
        if old:
            before_count, after_count = number(old["a"].get("messages")), number(a.get("messages"))
            if before_count is not None and after_count is not None:
                if before_count == after_count:
                    return
            elif now - seen <= old["now"] - old["a"]["seen"]:
                return
        # The rarity baseline compares with the last report even across an encounter boundary.
        known = old
        stamp = now - seen
        ground = self._ground(a, known, stamp)
        session, session_changed = selection.encounter_start(db, a, stamp)
        if session_changed:
            old = None
            self.pending = {k: v for k, v in self.pending.items() if k[0] != address}
            self._drop_track(db, address)
        checks = rules.identity_checks(a)
        # When the identity checks disagree the listed type may be wrong: the
        # report adds nothing to records, performance samples or season speeds.
        conflict = checks.get("conflict") is True
        # Only a coarse band of the receiver range is used (RULES-12).
        distance = number(raw.get("r_dst"))
        band = range_band(distance) if distance is not None and distance <= RANGE_BOUND_NM else 0
        remember(db, a, stamp, stamp, performance=not conflict, band=band)
        findings = candidates(a, now, self.rarity, self.profiles, identity=checks, band=band)
        findings += rules.supplementary_candidates(a, now, identity=checks)
        findings += self._guarded(
            status, "first_sighting", self._first_sightings, db, a, stamp, checks, default=[]
        )
        findings = [f for f in findings if f.kind != "vertical" and not f.kind.startswith(STATUS_PREFIX)]
        history = self.telemetry[address]
        if known and a.get("r") and known["a"].get("r") and a["r"] != known["a"]["r"]:
            history.clear()
            db.execute("DELETE FROM telemetry_samples WHERE hex=?", (address,))
        report = telemetry.sample(a, now)
        fresh = not history or report["ts"] > history[-1]["ts"]
        if fresh:
            # anomaly_tags compares only consecutive reports of one direct source.
            for tag in maneuvers.anomaly_tags(history[-1] if history else None, report):
                db.execute(
                    "INSERT OR IGNORE INTO anomaly_observations VALUES (?,?,?,?)",
                    (address, report["ts"], report["source"], tag),
                )
            telemetry.bounded_append(history, report)
            self.position_caches[address].prune(history)
            db.execute(
                "INSERT OR IGNORE INTO telemetry_samples VALUES (?,?,?)",
                (address, report["ts"], json.dumps(report, allow_nan=False)),
            )
            self._note_acas(address, report)
        if not conflict:
            selection.observe_performance(db, a, stamp, session)
        typecode = a.get("t", "")
        # A changed type also takes an address away from its old type, and the
        # stored type can settle one report later.
        retyped = known is not None and known["a"].get("t", "") != typecode
        if not address.startswith("~") and (
            retyped
            or (known is not None and known["retyped"])
            or (
                typecode
                and self.rarity["counts"].get(typecode, 0) <= RARE_TYPE_ADDRESSES
                and (known is None or (known["now"] - known["a"]["seen"]) // 86400 != (now - seen) // 86400)
            )
        ):
            # This report changes a baseline that a rarity verdict may depend on.
            # Later polls must count it, as they would without the cache.
            self.next_rarity = 0
        try:
            enrichment = watchlists.lookup(a, identity=checks)
        except (OSError, ValueError, TypeError, KeyError) as error:
            enrichment = None
            status["watchlist_last_error"] = type(error).__name__
        if watch := watchlist_finding(enrichment):
            findings.append(watch)
            status["watchlist_matches"] += 1
        extra = _extra(session, checks)
        if enrichment:
            extra["watchlist"] = enrichment
        unusual_jump = jump(old, a, now)
        if unusual_jump:
            record(
                db,
                a,
                unusual_jump,
                now,
                now,
                extra={
                    **_extra(session, checks),
                    "previous_position": {k: old["a"].get(k) for k in ("lat", "lon", "seen_pos", "type")},
                    "previous_snapshot_at": old["now"],
                },
            )
        pattern_evaluated = bool(unusual_jump) or a.get("alt_baro") == "ground"
        if pattern_evaluated:
            self._drop_track(db, address)
        orbit = self._orbit(address, session, now)
        position_sample = fresh_track_sample(a, now)
        if position_sample and not unusual_jump:
            track = self.tracks[address]
            position_at = position_sample["ts"]
            new_position = not track or position_at > track[-1]["ts"]
            if new_position:
                track.append(position_sample)
                db.execute(
                    "INSERT OR IGNORE INTO track_samples VALUES (?,?,?)",
                    (address, position_at, json.dumps(position_sample, allow_nan=False)),
                )
            while track and track[0]["ts"] < now - TRACK_SECONDS:
                track.popleft()
            self.segment_caches[address].prune(track)
            if new_position:
                pattern_evaluated = True
                # One circling verdict per evaluation drives loitering, holding,
                # spiral and reversal precedence (GEOMETRY-4).
                patterns = movement_patterns(track, orbit=orbit["held"], cache=self.segment_caches[address])
                for pattern in patterns:
                    if context := airport_context.pattern_context(pattern, a):
                        pattern.evidence["airport_context"] = context
                        self._circuit(pattern, history, a)
                orbit["current"] = _verdict(patterns)
                if orbit["current"]:
                    orbit["held"] = orbit["current"]
                    # Where this continuous orbit's turning began, whichever window verified it.
                    started = _orbit_onset(orbit["current"])
                    if started is not None and (orbit["since"] is None or started < orbit["since"]):
                        orbit["since"] = started
                findings.extend(patterns)
                windows = status["pattern_detector"]
                windows["fresh_positions"] += 1
                windows["evaluated_windows"] += 1
                windows["candidate_patterns"] += len(patterns)
        if fresh:
            episodes = self._analyze(
                address, history, a, session, legs=self._legs(address, session, history, now)
            )
            for finding in episodes:
                if finding.kind == "gnss_integrity_loss":
                    member = maneuvers.gnss_member(address, finding, a)
                    self.gnss[(address, member["onset_at"])] = member
            findings.extend(episodes)
            if not conflict and (
                season := self._guarded(
                    status, "seasonal_speed_record", self._season_record, db, a, history, session, checks
                )
            ):
                finding, first, last = season
                record(db, a, finding, first, last, extra=extra)
        matches = {f.kind for f in findings}
        for key in [k for k in self.pending if k[0] == address and k[1] not in matches]:
            # Message updates without a new position do not invalidate
            # a partially confirmed movement episode.
            if pattern_evaluated or self.pending[key].get("category") != "patterns":
                self.pending.pop(key)
        for finding in findings:
            event_id = self._confirm(db, a, finding, now, extra)
            if event_id is not None and finding.kind in ABSORBED_KINDS:
                self._absorb(db, address, session, finding, event_id, now)
        self.previous[address] = {
            "now": now,
            "a": a,
            "session": session,
            "retyped": retyped,
            "checks": checks,
            "ground": ground,
        }

    def _analyze(self, address, history, aircraft, session, *, legs, now=None):
        """maneuvers.analyze() with this encounter's circling verdict, threats and legs."""
        orbit = self.orbits.get(address)
        verdict = (
            (orbit["current"] or orbit["held"] or False) if orbit and orbit["session"] == session else False
        )
        return maneuvers.analyze(
            history,
            aircraft,
            orbit=verdict,
            now=now,
            threats=self._threats(address),
            legs=legs,
            positions_cache=self.position_caches[address],
        )

    def _orbit(self, address, session, now):
        """This encounter's latest circling verdict and the one it holds.

        A verdict holds precedence for the rest of the encounter until its
        window leaves the track buffer.
        """
        state = self.orbits.get(address)
        if state is None or state["session"] != session:
            state = self.orbits[address] = {"session": session, "current": False, "held": None, "since": None}
        if state["held"] and (number(state["held"].get("ended_at")) or 0) < now - TRACK_SECONDS:
            state["held"] = state["since"] = None
        return state

    @staticmethod
    def _circuit(pattern, history, a):
        """Runway-circuit circling keeps its holding, oscillation and descent as attributes."""
        if pattern.kind != "circling":
            return
        found = maneuvers.circuit_attributes(list(history), a, pattern)
        if found:
            evidence = pattern.evidence
            evidence["context_tags"] = sorted(
                set(evidence.get("context_tags", [])) | set(found["context_tags"])
            )
            evidence["circuit"] = found["attributes"]

    def _legs(self, address, session, history, now):
        """This encounter's straight legs, for shapes longer than the buffer.

        Once a minute, legs that began before the current position segment are
        kept (two hours, at most 64) and the segment's own legs are recomputed.
        analyze() merges these with the buffer's legs, so overlapping copies of
        one flown line become one leg.
        """
        state = self.legs.get(address)
        if state is None or state["session"] != session:
            state = self.legs[address] = {"session": session, "legs": [], "at": None}
        if state["at"] is None or now - state["at"] >= LEG_REFRESH:
            state["at"] = now
            pos = telemetry.positions(history, cache=self.position_caches[address])
            if len(pos) >= 2:
                start = pos[0]["ts"]
                kept = [
                    leg
                    for leg in state["legs"]
                    if leg["started_at"] < start and leg["ended_at"] >= now - LEG_SECONDS
                ]
                state["legs"] = maneuvers.merge_legs([*kept, *maneuvers.straight_legs(pos)])[-LEG_LIMIT:]
        return state["legs"] or None

    def _note_acas(self, address, report):
        advisory = report.get("acas_ra")
        if isinstance(advisory, dict):
            state = self.acas.setdefault(address, {"threats": set()})
            state["at"] = report["ts"]
            threat = advisory.get("threat_id_hex")
            if threat and threat != address and len(state["threats"]) < 8:
                state["threats"].add(threat)

    def _identity(self, address):
        previous = self.previous.get(address)
        a = previous["a"] if previous else {"hex": address}
        return {k: a[k] for k in ("hex", "r", "t", "desc", "flight", "type", "category", "dbFlags") if k in a}

    def _threats(self, address):
        """Retained reports of each aircraft this one's advisories named as a threat.

        Threats are kept while this aircraft's buffer still holds an advisory report.
        """
        state = self.acas.get(address)
        history = self.telemetry.get(address)
        if not state or not history or history[-1]["ts"] - state["at"] > telemetry.WINDOW:
            self.acas.pop(address, None)
            return None
        threats = {}
        for threat in state["threats"]:
            samples = self.telemetry.get(threat)
            if samples:
                threats[threat] = {"samples": list(samples), "aircraft": self._identity(threat)}
        return threats or None

    def _first_sightings(self, db, a, stamp, checks):
        """first_type_here / first_country_here / first_operator_here against days before
        the current UTC day.

        Another address with the same type, allocation State, or operator designator
        earlier the same day already had the first sighting, so a second one is not first.
        """
        address = a["hex"]
        if not ICAO_ADDRESS.fullmatch(address):
            return []
        day = datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()
        cache = self._first_cache(db, day, address, stamp)
        typecode = a.get("t", "")
        designator = rules.callsign_designator(a)
        history = cache["aircraft"].get((address, typecode, designator))
        if history is None:
            country = checks.get("allocation_country")
            today = cache["today"]
            type_days = None
            if typecode:
                type_days = cache["types"].get(typecode, 0) + (
                    today["types"].setdefault(typecode, address) != address
                )
            other = None
            if type_days == 0:
                other = db.execute(
                    "SELECT COUNT(DISTINCT day) FROM daily WHERE hex=? AND day<? AND typecode NOT IN ('',?)",
                    (address, day, typecode),
                ).fetchone()[0]
            country_days = None
            if country and cache["countries"] is not None:
                earlier_today = today["countries"].setdefault(country, address) != address
                country_days = cache["countries"].get(country, 0) + earlier_today
            operator_days = None
            if designator and cache["operators"] is not None:
                earlier_today = today["operators"].setdefault(designator, address) != address
                operator_days = cache["operators"].get(designator, 0) + earlier_today
            history = cache["aircraft"][(address, typecode, designator)] = {
                "coverage_days": cache["coverage_days"],
                "first_day": cache["first_day"],
                "type_days": type_days,
                "other_type_days": other,
                "country_days": country_days,
                "operator_days": operator_days,
            }
        return rules.first_sighting(a, history)

    def _first_cache(self, db, day, address, stamp):
        """The first-sighting baseline for ``day``, built once (see _first_history).

        A failed build is retried an hour later; until then each lookup raises,
        so the detector's status keeps reporting the failure.
        """
        cache = self.first_sightings.get(day, False)
        if cache is None and stamp < self.first_retry.get(day, 0):
            raise RuntimeError("First-sighting baseline unavailable until its retry")
        if not cache:
            before = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
            earlier = self.first_sightings.get(before)
            if not earlier:
                saved = meta(db, FIRST_BASELINE)
                if isinstance(saved, dict) and saved.get("day") in (day, before):
                    earlier = saved
            try:
                # A baseline saved before operators were counted is built again.
                if isinstance(earlier, dict) and earlier.get("day") == day and "operators" in earlier:
                    cache = {**earlier, "aircraft": {}}
                else:
                    cache = self._first_history(db, day, earlier)
                    put_meta(db, FIRST_BASELINE, {key: cache[key] for key in FIRST_BASELINE_KEYS})
                cache["today"] = self._first_today(
                    db, day, address, cache["countries"] is not None, cache["operators"] is not None
                )
                self.first_sightings[day] = cache
            except sqlite3.Error:
                raise
            except Exception:
                self.first_sightings[day] = None
                self.first_retry[day] = stamp + FIRST_RETRY_SECONDS
                raise
            finally:
                while len(self.first_sightings) > 2:
                    oldest = min(self.first_sightings)
                    del self.first_sightings[oldest]
                    self.first_retry.pop(oldest, None)
        return cache

    @staticmethod
    def _first_today(db, day, address, countries, operators):
        """Types, allocation States, and operator designators other addresses reported earlier on ``day``.

        Usually empty, as the baseline is built at the day's first report. After
        a restart the day's earlier reports count against every address (None
        stands for "an address before the restart"), since their order is not saved.
        """
        today = {"types": {}, "countries": {}, "operators": {}}
        for other, typecode, designator in db.execute(
            "SELECT hex,typecode,designator FROM daily WHERE day=? AND hex<>? AND hex NOT LIKE '~%'",
            (day, address),
        ):
            if typecode:
                today["types"][typecode] = None
            if countries and (found := reference.allocation(other)):
                today["countries"][found["country"]] = None
            if operators and designator:
                today["operators"][designator] = None
        return today

    @staticmethod
    def _first_history(db, day, earlier=None):
        """Coverage, type days, allocation-country days, and operator days before ``day``.

        With the previous day's baseline only that day's rows are read; otherwise
        every retained day is read once. Countries and operators are each counted
        once there are enough coverage days for their first-sighting finding.
        """
        before = (date.fromisoformat(day) - timedelta(days=1)).isoformat()

        def country(address):
            found = reference.allocation(address)
            return found["country"] if found else None

        if (
            isinstance(earlier, dict)
            and earlier["day"] == before
            and (
                earlier["countries"] is not None
                or earlier["coverage_days"] + 1 < rules.FIRST_COUNTRY_COVERAGE_DAYS
            )
            # A baseline saved before operators were counted is read again in full.
            and "operators" in earlier
            and (
                earlier["operators"] is not None
                or earlier["coverage_days"] + 1 < rules.FIRST_OPERATOR_COVERAGE_DAYS
            )
        ):
            covered = (
                db.execute(
                    "SELECT 1 FROM coverage WHERE day=? AND seconds>=?", (before, rules.COVERAGE_SECONDS)
                ).fetchone()
                is not None
            )
            types = dict(earlier["types"])
            for (typecode,) in db.execute(
                "SELECT DISTINCT typecode FROM daily WHERE day=? AND typecode<>'' AND hex NOT LIKE '~%'",
                (before,),
            ):
                types[typecode] = types.get(typecode, 0) + 1
            countries = None
            if earlier["countries"] is not None:
                countries = dict(earlier["countries"])
                seen = {
                    country(address)
                    for (address,) in db.execute(
                        "SELECT DISTINCT hex FROM daily WHERE day=? AND hex NOT LIKE '~%'", (before,)
                    )
                }
                for code in seen - {None}:
                    countries[code] = countries.get(code, 0) + 1
            operators = None
            if earlier["operators"] is not None:
                operators = dict(earlier["operators"])
                for (designator,) in db.execute(
                    "SELECT DISTINCT designator FROM daily WHERE day=? AND designator<>'' AND hex NOT LIKE '~%'",
                    (before,),
                ):
                    operators[designator] = operators.get(designator, 0) + 1
            return {
                "day": day,
                "coverage_days": earlier["coverage_days"] + covered,
                "first_day": earlier["first_day"] or (before if covered else None),
                "types": types,
                "countries": countries,
                "operators": operators,
                "aircraft": {},
            }
        coverage_days, first_day = db.execute(
            "SELECT COUNT(*),MIN(day) FROM coverage WHERE seconds>=? AND day<?",
            (rules.COVERAGE_SECONDS, day),
        ).fetchone()
        types = dict(
            db.execute(
                "SELECT typecode,COUNT(DISTINCT day) FROM daily WHERE day<? AND typecode<>'' AND hex NOT LIKE '~%' GROUP BY typecode",
                (day,),
            )
        )
        countries = None
        if coverage_days >= rules.FIRST_COUNTRY_COVERAGE_DAYS:
            allocated, days = {}, defaultdict(set)
            for address, seen in db.execute(
                "SELECT hex,day FROM daily WHERE day<? AND hex NOT LIKE '~%'", (day,)
            ):
                if address not in allocated:
                    allocated[address] = country(address)
                if allocated[address]:
                    days[allocated[address]].add(seen)
            countries = {code: len(seen) for code, seen in days.items()}
        operators = None
        if coverage_days >= rules.FIRST_OPERATOR_COVERAGE_DAYS:
            operators = dict(
                db.execute(
                    "SELECT designator,COUNT(DISTINCT day) FROM daily WHERE day<? AND designator<>'' AND hex NOT LIKE '~%' GROUP BY designator",
                    (day,),
                )
            )
        return {
            "day": day,
            "coverage_days": coverage_days,
            "first_day": first_day,
            "types": types,
            "countries": countries,
            "operators": operators,
            "aircraft": {},
        }

    @staticmethod
    def _first_expiring(db, cutoff_day):
        """What the days before ``cutoff_day`` add to a first-sighting baseline, counted as
        _first_history counts them: covered days, and the days each type, allocation
        State, and operator designator was seen. Read before the prune deletes them;
        usually one day's rows."""
        covered = db.execute(
            "SELECT COUNT(*) FROM coverage WHERE day<? AND seconds>=?", (cutoff_day, rules.COVERAGE_SECONDS)
        ).fetchone()[0]
        types = dict(
            db.execute(
                "SELECT typecode,COUNT(DISTINCT day) FROM daily WHERE day<? AND typecode<>'' AND hex NOT LIKE '~%' GROUP BY typecode",
                (cutoff_day,),
            )
        )
        days = defaultdict(set)
        for address, seen in db.execute(
            "SELECT hex,day FROM daily WHERE day<? AND hex NOT LIKE '~%'", (cutoff_day,)
        ):
            if found := reference.allocation(address):
                days[found["country"]].add(seen)
        operators = dict(
            db.execute(
                "SELECT designator,COUNT(DISTINCT day) FROM daily WHERE day<? AND designator<>'' AND hex NOT LIKE '~%' GROUP BY designator",
                (cutoff_day,),
            )
        )
        return {
            "coverage_days": covered,
            "types": types,
            "countries": {code: len(seen) for code, seen in days.items()},
            "operators": operators,
        }

    def _reduce_first_baselines(self, db, expiring):
        """Take expired days out of the kept first-sighting baselines (in memory and saved).

        Each baseline counted every retained day before its own, so subtracting the
        expired days leaves what a full read of the retained rows gives. A count that
        would fall below zero means a baseline did not hold those days: every baseline
        is then dropped, and the next lookup rebuilds one (_first_history).
        """
        saved = meta(db, FIRST_BASELINE)
        held = [cache for cache in self.first_sightings.values() if cache]
        reduced = [self._without_days(db, baseline, expiring) for baseline in held]
        kept = self._without_days(db, saved, expiring) if isinstance(saved, dict) else None
        if None in reduced or (isinstance(saved, dict) and kept is None):
            self.first_sightings.clear()
            self.first_retry.clear()
            db.execute("DELETE FROM meta WHERE key=?", (FIRST_BASELINE,))
            return
        for cache, counts in zip(held, reduced, strict=True):
            # Per-aircraft histories were computed from the old counts.
            cache.update(counts, aircraft={})
        if kept is not None:
            put_meta(db, FIRST_BASELINE, {**saved, **kept})

    @staticmethod
    def _without_days(db, baseline, expiring):
        """A baseline's counts without the expiring days ({coverage_days, first_day, types,
        countries, operators}), or None when they cannot be subtracted."""
        try:
            day = baseline["day"]
            coverage_days = baseline["coverage_days"] - expiring["coverage_days"]
            types = dict(baseline["types"])
            for typecode, days in expiring["types"].items():
                types[typecode] = types.get(typecode, 0) - days
            countries = None
            if baseline["countries"] is not None:
                countries = dict(baseline["countries"])
                for code, days in expiring["countries"].items():
                    countries[code] = countries.get(code, 0) - days
            operators = None
            if baseline["operators"] is not None:
                operators = dict(baseline["operators"])
                for designator, days in expiring["operators"].items():
                    operators[designator] = operators.get(designator, 0) - days
        except KeyError, TypeError:
            return None
        counts = [coverage_days, *types.values(), *(countries or {}).values(), *(operators or {}).values()]
        if not isinstance(day, str) or any(not isinstance(value, int) or value < 0 for value in counts):
            return None
        first_day = db.execute(
            "SELECT MIN(day) FROM coverage WHERE seconds>=? AND day<?", (rules.COVERAGE_SECONDS, day)
        ).fetchone()[0]
        # As a full read: States and operators are counted only once their finding can fire.
        if coverage_days < rules.FIRST_COUNTRY_COVERAGE_DAYS:
            countries = None
        if coverage_days < rules.FIRST_OPERATOR_COVERAGE_DAYS:
            operators = None
        return {
            "coverage_days": coverage_days,
            "first_day": first_day,
            "types": {typecode: days for typecode, days in types.items() if days},
            "countries": None
            if countries is None
            else {code: days for code, days in countries.items() if days},
            "operators": None
            if operators is None
            else {designator: days for designator, days in operators.items() if days},
        }

    def _season_record(self, db, a, history, session, checks):
        """The fastest sustained direct ground speed of a type this season, or None.

        season_speeds keeps each address's first sustained speed (it counts
        toward the minimum number of addresses) and every new type maximum. A
        record compares only reports saved since the later of the season start
        and the table's start, and needs rules.SEASON_MIN_BASELINE_DAYS of them.
        Returns (finding, window start, window end).
        """
        typecode = a.get("t")
        if not typecode or a["hex"].startswith("~") or not history:
            return None
        season, _ = rules.season_start(history[-1]["ts"])
        key = (season, typecode)
        stats, own = self.season_stats.get(key), self.season_own.get(a["hex"])
        current = number(history[-1].get("gs"))
        if (
            stats is not None
            and stats["max"] is not None
            and own is not None
            and (own["key"], own["session"], own["saved"]) == (key, session, True)
            and (current is None or current <= stats["max"])
        ):
            # The window's lowest speed is at most the current one: no new maximum.
            return None
        sustained, window = _sustained_speed(history)
        if sustained is None:
            return None
        # A speed beyond the listed type's envelope or rated limit suggests a wrong
        # type or bad data; it must not become that type's season maximum.
        if not rules.speed_plausible(a, sustained["gs"]):
            return None
        if stats is None:
            if len(self.season_stats) >= 8192:
                self.season_stats.clear()
            best, count = db.execute(
                "SELECT MAX(max_gs),COUNT(*) FROM season_speeds WHERE season=? AND typecode=?", key
            ).fetchone()
            stats = self.season_stats[key] = {"max": best, "addresses": count}
        if own is None or own["key"] != key or own["session"] != session:
            row = db.execute(
                "SELECT max_gs FROM season_speeds WHERE season=? AND typecode=? AND hex=?", (*key, a["hex"])
            ).fetchone()
            own = self.season_own[a["hex"]] = {
                "key": key,
                "session": session,
                "before": row[0] if row else None,
                "saved": row is not None,
            }
        speed = sustained["gs"]
        finding = None
        if stats["max"] is not None and speed > stats["max"]:
            # Earlier maxima: other addresses, and this one before this encounter.
            others, count = db.execute(
                "SELECT MAX(max_gs),COUNT(*) FROM season_speeds WHERE season=? AND typecode=? AND hex<>?",
                (*key, a["hex"]),
            ).fetchone()
            earlier = [value for value in (others, own["before"]) if value is not None]
            baseline, days = self._season_baseline(db, season, window[-1]["ts"])
            finding = rules.seasonal_speed_record(
                a,
                {
                    "type": typecode,
                    "season_start": season,
                    "baseline_start": baseline,
                    "baseline_days": days,
                    "max_gs": max(earlier) if earlier else None,
                    "addresses": count + (own["before"] is not None),
                },
                sustained=sustained,
                identity=checks,
            )
        if not own["saved"] or stats["max"] is None or speed > stats["max"]:
            db.execute(
                """INSERT INTO season_speeds VALUES (?,?,?,?,?) ON CONFLICT(season,typecode,hex)
                DO UPDATE SET max_gs=MAX(max_gs,excluded.max_gs),updated_at=excluded.updated_at""",
                (*key, a["hex"], speed, window[-1]["ts"]),
            )
            stats["addresses"] += not own["saved"]
            stats["max"] = speed if stats["max"] is None else max(stats["max"], speed)
            own["saved"] = True
        return (finding, window[0]["ts"], window[-1]["ts"]) if finding else None

    def _season_baseline(self, db, season, stamp):
        """(ISO date, days) of the reports a season record compares.

        They begin at the later of the season start and the season_speeds
        table's start, on the first whole UTC day; the days run to ``stamp``.
        """
        if self.season_since is None:
            self.season_since = number(meta(db, SEASON_SINCE)) or 0
        start = datetime.fromisoformat(season).replace(tzinfo=timezone.utc)
        since = datetime.fromtimestamp(max(start.timestamp(), self.season_since), timezone.utc)
        midnight = datetime.combine(since.date(), datetime.min.time(), timezone.utc)
        if since > midnight:
            # A partial first day is not claimed.
            midnight += timedelta(days=1)
        return midnight.date().isoformat(), (stamp - midnight.timestamp()) / 86400

    def _absorb(self, db, address, session, shape, shape_id, now):
        """Orbit and survey-grid precedence over the encounter's earlier rows (GEOMETRY-4).

        A course reversal, loitering or parallel-passes row saved before this
        circle or grid verified, whose turn or window overlaps the orbit itself
        or the grid, is superseded. It is removed, unless it has notes, a draft
        or a review, or is old enough to have been published (the publisher
        waits for selection.QUIET_SECONDS without reports): then it is marked
        "absorbed_by", and selection and curation leave it out.
        """
        if shape.kind == "circling":
            evidence = shape.evidence or {}
            pattern = evidence.get("pattern") or {}
            start = _orbit_onset({**pattern, "pattern_samples": evidence.get("pattern_samples")})
            end = number(pattern.get("ended_at"))
        else:
            info = (shape.evidence or {}).get("maneuver") or {}
            start, end = number(info.get("started_at")), number(info.get("ended_at"))
        absorbed = ABSORBED_KINDS[shape.kind]
        if start is None or end is None or session is None:
            return
        # The native journal keeps reviews only; a journal from the original
        # application also keeps notes and drafts. Ask only the tables present.
        review_tables = [
            name
            for (name,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('notes','story_drafts','event_reviews')"
            )
        ]
        attached_query = " UNION ALL ".join(
            f"SELECT 1 FROM {name} WHERE event_id=?" for name in sorted(review_tables)
        )
        rows = db.execute(
            f"""SELECT id,kind,evidence,last_seen FROM events WHERE hex=? AND kind IN ({",".join("?" * len(absorbed))})
            AND origin=? AND last_seen>=? AND first_seen<=? AND json_extract(evidence,'$.encounter_start')=?""",
            (address, *absorbed, LIVE_ORIGIN, start - PATTERN_GAP, end + PATTERN_GAP, session),
        ).fetchall()
        for row in rows:
            evidence = json.loads(row["evidence"])
            if evidence.get("absorbed_by"):
                continue
            block = evidence.get("maneuver") or evidence.get("pattern") or {}
            first = number(block.get("turn_started_at", block.get("started_at")))
            last = number(block.get("turn_ended_at", block.get("ended_at")))
            if first is None or last is None or last <= start or first >= end:
                continue
            attached = (
                attached_query
                and db.execute(f"{attached_query} LIMIT 1", (row["id"],) * len(review_tables)).fetchone()
            )
            if attached or row["last_seen"] < now - selection.QUIET_SECONDS:
                evidence["absorbed_by"] = {"kind": shape.kind, "event_id": shape_id}
                db.execute("UPDATE events SET evidence=? WHERE id=?", (json.dumps(evidence), row["id"]))
            else:
                db.execute("DELETE FROM events WHERE id=?", (row["id"],))

    def _holding_inputs(self, now):
        """Circling verdicts still current, as holding_stacks() episodes.

        Each starts at its orbit's onset. The orbit centre is the mean of the
        verdict's samples; it is used only in memory to compare aircraft and is
        never saved. An orbit whose shape needs review joins no stack, as in
        the archive replay.
        """
        episodes = []
        for address, state in self.orbits.items():
            verdict = state.get("held") or {}
            ended = number(verdict.get("ended_at"))
            if (
                ended is None
                or ended < now - PATTERN_GAP
                or verdict.get("quality_review_required") is not False
            ):
                continue
            samples = [
                s
                for s in verdict.get("pattern_samples") or []
                if number(s.get("lat")) is not None and number(s.get("lon")) is not None
            ]
            if not samples:
                continue
            started = state.get("since") or verdict.get("started_at")
            altitude = self._stack_altitude(address, started, ended, samples)
            if altitude is None:
                continue
            sources = verdict.get("observed_sources") or []
            source = sources[0] if len(sources) == 1 else "mixed"
            if altitude["source"] not in telemetry.DIRECT:
                # An indirect altitude names its source, so the stack carries the flag.
                source = altitude["source"]
            identity = self._identity(address)
            episodes.append(
                {
                    **{key: identity[key] for key in ("r", "t", "flight") if key in identity},
                    "hex": address,
                    # The orbit's first onset keeps the stack's anchor and episode stable.
                    "started_at": started,
                    "ended_at": ended,
                    "lat": statistics.fmean(number(s["lat"]) for s in samples),
                    "lon": statistics.fmean(number(s["lon"]) for s in samples),
                    "altitude_ft": altitude["altitude_ft"],
                    "altitude_reference": altitude["reference"],
                    "altitude_source": altitude["source"],
                    "altitude_reports": altitude["reports"],
                    "source": source,
                }
            )
        return episodes

    # A stack level needs this many altitude reports of one reference inside the orbit.
    STACK_ALTITUDE_REPORTS = 3

    def _stack_altitude(self, address, started, ended, samples):
        """The orbit's level from retained telemetry, with its reference and provenance.

        Vertical separation between circling aircraft needs comparable altitude
        measurements. Direct barometric reports inside the orbit window decide
        the level; failing those, multilaterated or TIS-B barometric reports (an
        uncertain source), then geometric reports (another reference), then the
        track samples' unlabelled altitude. Returns {altitude_ft, reference,
        source, reports} or None.
        """
        window = [p for p in self.telemetry.get(address, ()) if started <= p["ts"] <= ended + 1]
        for field, reference_name in (("alt_baro", "barometric"), ("alt_geom", "geometric")):
            by_source = defaultdict(list)
            for point in window:
                value = number(point.get(field))
                if value is not None and not point.get("ground"):
                    by_source[telemetry.source_for(point, field)].append(value)
            # Direct sources first, then the indirect ones by name.
            for origin in sorted(by_source, key=lambda name: (name not in telemetry.DIRECT, name)):
                values = by_source[origin]
                if len(values) >= self.STACK_ALTITUDE_REPORTS:
                    return {
                        "altitude_ft": statistics.median(values),
                        "reference": reference_name,
                        "source": origin,
                        "reports": len(values),
                    }
        heights = [number(s.get("alt")) for s in samples if number(s.get("alt")) is not None]
        if not heights:
            return None
        # Track samples do not say which reference their altitude used.
        return {
            "altitude_ft": statistics.median(heights),
            "reference": "unknown",
            "source": "unknown",
            "reports": 0,
        }

    @staticmethod
    def _flag_stack(primary, finding, inputs):
        """Hold a stack whose levels are not comparable: mixed or unknown altitude references.

        A member with an indirect altitude already carries
        participant_uncertain_source through its source; references that
        differ between members, or an unlabelled one, add altitude_reference_mixed.
        """
        info = finding.evidence["maneuver"]
        addresses = [primary, *(item["hex"] for item in finding.evidence.get("participants", []))]
        info["altitude_references"] = sorted(
            {inputs[a]["altitude_reference"] for a in addresses if a in inputs}
        )
        Detector._flag_references(info)
        return finding

    @staticmethod
    def _flag_references(info):
        """Add altitude_reference_mixed when a stack's levels use more than one reference or
        an unlabelled one; any review flag holds the stack for review."""
        references = set(info.get("altitude_references") or [])
        if "unknown" in references or len(references) > 1:
            info["quality_flags"] = sorted(
                set(info.get("quality_flags") or []) | {"altitude_reference_mixed"}
            )
        if info.get("quality_flags"):
            info["quality_review_required"] = True

    def _continued_stack(self, primary, finding, inputs):
        """One holding stack stays one row while its membership changes (A#6).

        A stack that shares at least two aircraft with an open one continues it:
        the open stack's primary and episode start remain, every aircraft seen in
        it stays a participant, and the count is the most circling at once. The
        open stack's review flags and altitude references carry forward, since
        _flag_stack sees only the current members: a member whose indirect or
        incomparable altitude flagged the stack still stands in it after leaving.
        Returns (primary, finding).
        """
        members = {primary, *(item["hex"] for item in finding.evidence["participants"])}
        for (kind, held), entry in self.multi.items():
            old = entry["finding"]
            listed = {item["hex"]: item for item in old.evidence["participants"]}
            if kind != finding.kind or held not in self.previous or len(members & {held, *listed}) < 2:
                continue
            listed.update({item["hex"]: item for item in finding.evidence["participants"]})
            if primary != held and primary in inputs:
                current = inputs[primary]
                listed[primary] = maneuvers.participant(
                    primary, "member", aircraft=current, measurements={"altitude_ft": current["altitude_ft"]}
                )
            listed.pop(held, None)
            info, before = finding.evidence["maneuver"], old.evidence["maneuver"]
            info["episode_start"] = before["episode_start"]
            info["started_at"] = min(info["started_at"], before["started_at"])
            info["duration_seconds"] = info["ended_at"] - info["started_at"]
            info["participant_count"] = max(info["participant_count"], before["participant_count"])
            for key in ("quality_flags", "altitude_references"):
                info[key] = sorted(set(info.get(key) or []) | set(before.get(key) or []))
            # Levels kept from earlier members are compared with the current members'
            # levels, so the references of both count.
            self._flag_references(info)
            finding.value = info["participant_count"]
            finding.why = maneuvers.why(finding.title, finding.value, finding.unit, info["duration_seconds"])
            finding.evidence["participants"] = [listed[address] for address in sorted(listed)][
                : maneuvers.MAX_PARTICIPANTS
            ]
            return held, finding
        return primary, finding

    def _formation_candidates(self, now):
        """Buffers of current direct ADS-B aircraft with another one nearby.

        formation_pairs() needs two aircraft within 0.5 NM at a matched fix in
        the last minute; this coarse check on each latest fix (4 NM, 3,000 ft)
        spares rebuilding every other aircraft's positions.
        """
        latest = {}
        for address, samples in self.telemetry.items():
            if (
                address.startswith("~")
                or address not in self.previous
                or not samples
                or samples[-1]["ts"] < now - STALE_SECONDS
            ):
                continue
            for point in itertools.islice(reversed(samples), 6):
                lat, lon, height = (
                    number(point.get("lat")),
                    number(point.get("lon")),
                    maneuvers.altitude(point),
                )
                if point.get("source") in telemetry.DIRECT and None not in (lat, lon, height):
                    latest[address] = (lat, lon, height)
                    break
        if len(latest) < 2:
            return {}
        scale = maneuvers.longitude_scale(statistics.fmean(fix[0] for fix in latest.values()))
        cells = maneuvers.near_pairs(
            {address: fix[:2] for address, fix in latest.items()},
            lambda lat, lon: (
                math.floor(lat * 60 / FORMATION_NEAR_NM),
                math.floor(lon * scale / FORMATION_NEAR_NM),
            ),
        )
        chosen = set()
        for a, b in cells:
            if (
                abs(latest[a][2] - latest[b][2]) <= FORMATION_NEAR_FT
                and rules.distance_nm(latest[a][:2], latest[b][:2]) <= FORMATION_NEAR_NM
            ):
                chosen.update((a, b))
        return {address: list(self.telemetry[address]) for address in sorted(chosen)}

    def _multi(self, db, now):
        """Formation pairs, holding stacks and GNSS clusters across addresses.

        Each finding is saved on its primary aircraft; the others are its
        participants. A group no longer found for a minute ends its episode once.
        """
        active = self._formation_candidates(now)
        meta = {address: self._identity(address) for address in active}
        found = maneuvers.formation_findings(maneuvers.formation_pairs(active, meta), meta)
        inputs = {episode["hex"]: episode for episode in self._holding_inputs(now)}
        found += [
            self._continued_stack(primary, self._flag_stack(primary, finding, inputs), inputs)
            for primary, finding in maneuvers.holding_stacks(inputs.values())
        ]
        # A member stays 15 min after its onset, or after the latest onset of an
        # open cluster it belongs to, so one interference event stays one row.
        kept = {}
        for (kind, primary), entry in self.multi.items():
            if kind == "gnss_integrity_cluster":
                latest = number(entry["finding"].evidence["maneuver"].get("ended_at")) or 0
                for address in (
                    primary,
                    *(p["hex"] for p in entry["finding"].evidence.get("participants", [])),
                ):
                    kept[address] = max(kept.get(address, 0), latest)
        self.gnss = {
            key: member
            for key, member in self.gnss.items()
            if max(number(member.get("onset_at")) or 0, kept.get(key[0], 0)) >= now - GNSS_MEMBER_SECONDS
        }
        found += maneuvers.gnss_clusters(list(self.gnss.values()))
        current = set()
        for primary, finding in found:
            previous = self.previous.get(primary)
            if previous is None:
                continue
            key = (finding.kind, primary)
            current.add(key)
            extra = _extra(previous["session"], previous["checks"])
            self.multi[key] = {"finding": finding, "a": previous["a"], "extra": extra, "seen": now}
            self._confirm(db, previous["a"], finding, now, extra)
        for key, entry in list(self.multi.items()):
            if key in current or now - entry["seen"] < 2 * MULTI_INTERVAL:
                continue
            finding = copy.deepcopy(entry["finding"])
            info = finding.evidence["maneuver"]
            if info.get("state") == "active":
                info.update(
                    state="ended",
                    end_reason="stale",
                    evaluated_at=max(info["evaluated_at"], info["ended_at"] + STALE_SECONDS),
                )
                self._confirm(db, entry["a"], finding, now, entry["extra"])
            del self.multi[key]

    def _stale(self, db, now, status):
        """End once the open episodes of addresses whose reports stopped 1–3 minutes ago."""
        for address, history in list(self.telemetry.items()):
            previous = self.previous.get(address)
            if (
                not history
                or previous is None
                or not STALE_SECONDS < now - history[-1]["ts"] <= 3 * STALE_SECONDS
                # Already ended for this newest report: a second pass changes nothing.
                or self.stale_ended.get(address) == history[-1]["ts"]
            ):
                continue
            self.stale_ended[address] = history[-1]["ts"]
            self._isolated(db, status, address, self._end_stale, db, address, history, previous, now)

    def _end_stale(self, db, address, history, previous, now):
        legs = self.legs.get(address)
        findings = self._analyze(
            address,
            history,
            previous["a"],
            previous["session"],
            legs=legs["legs"] or None if legs and legs["session"] == previous["session"] else None,
            now=now,
        )
        extra = _extra(previous["session"], previous["checks"])
        for finding in findings:
            self._confirm(db, previous["a"], finding, now, extra)

    def _confirm(self, db, a, finding, now, extra):
        """Count one qualifying report; save the finding once it is confirmed.

        Returns the saved row's id, or None while the finding is unconfirmed.
        """
        episode = (finding.evidence or {}).get("maneuver")
        if episode:
            # Episode detectors already evaluate duration and report endpoints.
            # Unconfirmed leads remain available privately with an explicit review flag.
            a, observed_at = maneuvers.saved_snapshot(a, finding)
            evidence = {**extra, "observed_at": observed_at}
            return record(db, a, finding, episode["started_at"], episode["ended_at"], hits=1, extra=evidence)
        key = (a["hex"], finding.kind)
        pending = self.pending.get(key)
        pattern = (finding.evidence or {}).get("pattern")
        endpoint = pattern["ended_at"] if pattern else None
        if not pending or now - pending["last"] > (PATTERN_GAP if pattern else 45):
            pending = {
                "first": now,
                "last": now,
                "hits": 0,
                "recorded": False,
                "category": finding.category,
                "endpoint": None,
                "first_endpoint": endpoint,
                "samples": [],
            }
        # A repeated position endpoint is not another movement observation.
        if endpoint is not None and pending["endpoint"] is not None and endpoint <= pending["endpoint"]:
            return None
        self.pending[key] = pending
        pending.update(last=now, hits=pending["hits"] + 1, endpoint=endpoint)
        envelope = finding.kind in curation.ENVELOPE_KINDS or finding.kind in rules.RATED_KINDS
        if finding.kind in selection.NUMERIC_KINDS or envelope:
            recent = self._numeric_samples(pending, a, finding, now)
            if recent is None:
                return None
            elapsed = min(
                recent[-1]["ts"] - recent[0]["ts"],
                recent[-1]["position_at"] - recent[0]["position_at"],
            )
            reports, seconds = (
                (rules.MIN_REPORTS, rules.MIN_REPORT_SECONDS)
                if envelope
                else (NUMERIC_REPORTS, NUMERIC_SECONDS)
            )
            if pending["hits"] < reports or elapsed < seconds:
                return None
            first, last = recent[0]["ts"], recent[-1]["ts"]
            peak = pending["peak"]
            a, finding = peak["aircraft"], Finding(**peak["finding"])
            finding.confidence = "Sustained coherent reports; individual field age unknown"
            extra = {
                **extra,
                "confirmation": {
                    "sustained": True,
                    "samples": recent,
                    # The headline peak can predate the eight retained samples.
                    "peak_sample": peak["sample"],
                    "quality_flags": sorted(
                        {flag for sample in (*recent, peak["sample"]) for flag in sample["quality_flags"]}
                    ),
                    "duration_seconds": elapsed,
                    "field_freshness_known": False,
                    "limitation": "Fresh aircraft and position updates do not establish each field's age or independence",
                },
                "observed_at": peak["at"],
            }
        else:
            elapsed = endpoint - pending["first_endpoint"] if pattern else now - pending["first"]
            if pending["hits"] < 2 or elapsed < 8:
                return None
            first, last = (pattern["started_at"], endpoint) if pattern else (pending["first"], now)
        event_id = record(
            db, a, finding, first, last, hits=1 if pending["recorded"] else pending["hits"], extra=extra
        )
        pending["recorded"] = True
        return event_id

    # The report fields each numeric or envelope finding measures (kinds names why a rated
    # speed lists four).
    NUMERIC_FIELDS = kinds.table("numeric_fields")

    # Kinds whose readings may spread up to 2,000 ft, and whose sign must hold.
    WIDE_SPREAD_KINDS = kinds.where(wide_spread=True)
    SIGNED_KINDS = kinds.where(signed=True)

    @classmethod
    def _numeric_samples(cls, pending, a, finding, now):
        """Return the coherent recent readings, or None without a newer position."""
        fields = cls.NUMERIC_FIELDS[finding.kind]
        report = {**a, "source": a.get("type", "unknown")}
        sample = {
            "ts": now - a["seen"],
            "position_at": now - a["seen_pos"],
            "value": finding.value,
            "source": a.get("type", "unknown"),
            "baro_rate": number(a.get("baro_rate")),
            # The measured fields' values, so an enrichment can recompute every sample.
            "inputs": {field: number(a.get(field)) for field in fields},
            "field_sources": {field: telemetry.source_for(report, field) for field in fields},
            "quality_flags": telemetry.quality([report], (*fields, "lat", "lon")),
        }
        recent = pending["samples"]
        if recent and (
            sample["position_at"] <= recent[-1]["position_at"] or sample["ts"] <= recent[-1]["ts"]
        ):
            pending["hits"] -= 1
            return None
        recent.append(sample)
        del recent[:-8]
        values = [p["value"] for p in recent]
        tolerance = 2000 if finding.kind in cls.WIDE_SPREAD_KINDS else max(50, abs(finding.value) * 0.2)
        coherent = max(values) - min(values) <= tolerance and len({p["source"] for p in recent}) == 1
        coherent = coherent and all(p["field_sources"] == recent[0]["field_sources"] for p in recent)
        if finding.kind in cls.SIGNED_KINDS:
            coherent = coherent and len({p["baro_rate"] > 0 for p in recent}) == 1
        if not coherent:
            # A source change, wide spread or rate reversal restarts confirmation.
            recent = [sample]
            pending.update(first=now, hits=1, samples=recent, recorded=False)
            pending.pop("peak", None)
        if "peak" not in pending or abs(finding.value) >= abs(pending["peak"]["finding"]["value"]):
            pending["peak"] = {
                "aircraft": dict(a),
                "finding": finding.to_dict(),
                "at": sample["ts"],
                "sample": sample,
            }
        return recent

    def _correlations(self, db, now):
        rows = db.execute(
            "SELECT hex,ts,source,tag FROM anomaly_observations WHERE ts>=?", (now - 60,)
        ).fetchall()
        for source, tag, addresses in maneuvers.correlated(rows, now):
            for address in addresses:
                old = self.previous.get(address)
                if not old:
                    continue
                finding = maneuvers.correlation_finding(source, tag, addresses)
                snapshot, observed_at = maneuvers.saved_snapshot(old["a"], finding)
                record(
                    db,
                    snapshot,
                    finding,
                    min(addresses.values()),
                    max(addresses.values()),
                    extra={**_extra(old["session"], old.get("checks")), "observed_at": observed_at},
                )

    def _prune(self, db, now):
        """Hourly retention: 365 days and 50,000 events, plus dependent rows."""
        db.execute("DELETE FROM flightaware_cache WHERE fetched_at<?", (now - flightaware.RETENTION,))
        db.execute("DELETE FROM type_checks WHERE fetched_at<?", (now - typecheck.RETENTION,))
        db.execute("DELETE FROM wind_cache WHERE fetched_at<?", (now - wind.RETENTION,))
        cutoff = now - 365 * 86400
        db.execute("DELETE FROM events WHERE last_seen<?", (cutoff,))
        db.execute(
            "DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY last_seen DESC LIMIT -1 OFFSET ?)",
            (selection.EVENT_LIMIT,),
        )
        for table in ("event_reviews", "wind_retries"):
            db.execute(f"DELETE FROM {table} WHERE event_id NOT IN (SELECT id FROM events)")
        # The wind worker never asks about an event older than its lookback again.
        db.execute("DELETE FROM wind_retries WHERE retry_at<?", (now - wind.LOOKBACK,))
        db.execute("DELETE FROM encounter_state WHERE last_seen<?", (now - 86400,))
        db.execute("DELETE FROM performance_samples WHERE updated_at<?", (now - 30 * 86400,))
        # Seasonal maxima are compared within one season; a year covers every season.
        db.execute("DELETE FROM season_speeds WHERE updated_at<?", (cutoff,))
        prune_fingerprints(db)
        db.execute("DELETE FROM aircraft WHERE last_seen<?", (cutoff,))
        cutoff_day = datetime.fromtimestamp(cutoff, timezone.utc).date().isoformat()
        expiring = self._first_expiring(db, cutoff_day)
        removed = db.execute("DELETE FROM daily WHERE day<?", (cutoff_day,)).rowcount
        removed += db.execute("DELETE FROM coverage WHERE day<?", (cutoff_day,)).rowcount
        if removed:
            # The first-sighting baselines counted these days, so a type or State seen only
            # on them would still suppress a first sighting. The days are taken out of every
            # kept baseline. A full rebuild reads the year of retained rows on this collector
            # thread (about 1-2 s for a busy year), so it is only the fallback. The cutoff
            # moves once a day.
            self._reduce_first_baselines(db, expiring)
        self.previous = {k: v for k, v in self.previous.items() if now - v["now"] < 900}
        self.pending = {
            k: v
            for k, v in self.pending.items()
            if now - v["last"] < (PATTERN_GAP if v.get("category") == "patterns" else 60)
        }
        self.season_own = {k: v for k, v in self.season_own.items() if k in self.previous}
        season, _ = rules.season_start(now)
        self.season_stats = {k: v for k, v in self.season_stats.items() if k[0] == season}


# Every numeric, envelope or rated kind that selection confirms has a measured field.
if set(Detector.NUMERIC_FIELDS) != selection.NUMERIC_KINDS | curation.ENVELOPE_KINDS | rules.RATED_KINDS:
    raise RuntimeError("Detector.NUMERIC_FIELDS must cover selection's numeric, envelope and rated kinds")


def report_unavailable(detector, error):
    """Record a failed poll in status; the detector then writes every status row again.

    A collector fault keeps its own message and the failing detectors' errors.
    """
    fault = isinstance(error, CollectorFault)
    LOG.warning(
        "%s (%s)", "Collector fault" if fault else "Receiver observation unavailable", type(error).__name__
    )
    detector.saved_status.clear()
    try:
        with connect(detector.path) as db:
            put_meta(
                db,
                "last_error",
                "Most aircraft reports failed in the collector; saved sightings are still available."
                if fault
                else "Receiver data temporarily unavailable; saved sightings are still available.",
            )
            if fault:
                put_meta(db, "detector_errors", error.errors)
    except Exception as status_error:
        # A locked or unavailable journal must not end collection.
        LOG.warning("Collector status unavailable (%s)", type(status_error).__name__)


def collector():
    detector = Detector()
    while True:
        started = time.monotonic()
        try:
            detector.process(fetch_json(RECEIVER))
        except Exception as error:
            report_unavailable(detector, error)
        time.sleep(max(1, POLL - (time.monotonic() - started)))


def event_dict(row):
    value = dict(row)
    value["evidence"] = json.loads(value["evidence"])
    aircraft = value["evidence"]["aircraft"]
    value["aircraft"] = {k: aircraft.get(k) for k in ("hex", "r", "t", "desc", "flight", "type", "year")}
    return value


def baseline(db, rarity):
    """Slow-changing viewer facts: rarity readiness, the rarest types and record reports."""
    types = []
    for typecode, count in sorted(rarity["counts"].items(), key=lambda x: (x[1], x[0]))[:8]:
        row = db.execute(
            "SELECT description FROM aircraft WHERE typecode=? AND description<>'' LIMIT 1", (typecode,)
        ).fetchone()
        types.append(
            {
                "type": typecode,
                "count": count,
                "description": row[0] if row else WATCH_TYPES.get(typecode, ""),
                "share": round(100 * count / max(1, rarity["total"]), 2),
            }
        )
    records = {}
    # Archive imports can still write maxima beyond the outlier bounds; balloons
    # and high-flying types keep altitude records up to their own bound.
    for name, field, bound, args in (
        ("speed", "max_gs", "max_gs<=?", (MAX_PLAUSIBLE_GS_KT,)),
        (
            "altitude",
            "max_alt",
            f"(max_alt<=? OR (max_alt<=? AND {HIGH_FLYING_SQL}))",
            (MAX_PLAUSIBLE_ALT_FT, rules.HIGH_FLYING_ALT_FT, *HIGH_FLYING_ARGS),
        ),
        ("range", "max_range", "max_range<=?", (1e9,)),
    ):
        row = db.execute(
            f"SELECT hex,registration,typecode,{field} AS value FROM aircraft WHERE {field}>0 AND {bound} ORDER BY {field} DESC LIMIT 1",
            args,
        ).fetchone()
        records[name] = dict(row) if row else None
    return {
        "rarity": {k: v for k, v in rarity.items() if k not in {"counts", "aircraft_days", "type_days"}},
        "types": types,
        "records": records,
    }
