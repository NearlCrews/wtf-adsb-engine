"""Deterministic editorial selection. Saved observations are never deleted here."""

import json
import math
import statistics
import threading
from collections import Counter, defaultdict
from datetime import datetime, timezone

from . import curation, kinds, typecheck
from .curation import ENVELOPE_KINDS, IDENTITY_COOLDOWN, IDENTITY_KINDS, RECORD_KINDS, _mapping
from .rules import (
    ARCHIVE_SQUAWK_PREFIX,
    COVERAGE_SECONDS,
    LIVE_SQUAWK_PREFIX,
    MAX_PLAUSIBLE_ALT_FT,
    RATED_KINDS,
    SEASON_SUSTAINED_SECONDS,
    SQUAWK_PREFIXES,
    WATCH_TYPES,
    number,
    performance_context,
    performance_thresholds,
)
from .telemetry import DIRECT, quality

VERSION = 3
# The collector's own rows; every other origin is an archive import.
LIVE_ORIGIN = "Live receiver"
PROMOTION_SCORE = 65
STORY_SCORE = 75
ENCOUNTER_GAP = 600
EVENT_LIMIT = 50000
# The publisher waits this long without reports before an encounter can be a story.
QUIET_SECONDS = 1800
# Spotting readings the collector confirms over sustained reports. Readings above a
# type envelope (curation.ENVELOPE_KINDS) or a type's certified limits
# (rules.RATED_KINDS), and season records (curation.RECORD_KINDS), are assessed
# from their own saved windows instead.
NUMERIC_KINDS = set(kinds.where(numeric=True))
NOVELTY_KINDS = set(kinds.where(novelty=True))
VERDICTS = {"keep", "routine", "bad_data"}
# rules.performance_thresholds without a learned type profile.
DEFAULT_SPEED = 600
# A context baseline may fall below the type-level threshold only after this many days.
CONTEXT_DAYS = 14
# A context's spotting threshold (performance_profiles) needs this many addresses in its phase
# and band, and a type's speed norm (type_norm) as many besides the story's own.
CONTEXT_MIN_ADDRESSES = 20
NORM_MIN_ADDRESSES = CONTEXT_MIN_ADDRESSES
# Samples older than this, before the newest sample, are left out of profiles and norms.
NORM_WINDOW_DAYS = 30
# The lowest altitude a performance sample is taken at.
SAMPLE_FLOOR_FT = 5000
_SELECTION_LOCK = threading.RLock()


def _weighted_percentile(entries, share):
    entries = sorted(entries)
    total = sum(weight for _, weight in entries)
    running = 0
    for value, weight in entries:
        running += weight
        if running >= share * total - 1e-9:
            return value
    return entries[-1][0]


def _rounded(value, margin, low, high, step):
    """value plus margin, rounded up to the step and clamped to [low, high]."""
    return min(high, max(low, math.ceil((value + margin) / step) * step))


def _threshold(values, margin, low, high, step):
    return _rounded(values[math.ceil(len(values) * 0.9) - 1], margin, low, high, step)


def sample_rows(db):
    """(typecode, phase, band, address, encounter, median) for every usable sample in the
    NORM_WINDOW_DAYS window, or None without a sample table."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='performance_samples'").fetchone():
        return None
    rows = []
    for typecode, phase, band, address, encounter, values in db.execute(
        "SELECT typecode,phase,altitude_band,hex,encounter,values_json FROM performance_samples "
        "WHERE updated_at >= (SELECT MAX(updated_at)-? FROM performance_samples)",
        (NORM_WINDOW_DAYS * 86400,),
    ):
        values = json.loads(values)
        if len(values) < 4:
            continue
        rows.append((typecode, phase, band, address, encounter, statistics.median(values)))
    return rows


def _contexts(rows):
    contexts = defaultdict(list)
    for typecode, phase, band, address, encounter, median in rows:
        contexts[(typecode, phase, band)].append((address, encounter, median))
    return dict(contexts)


def norm_contexts(db, rows=None):
    """{(typecode, phase, band): [(address, encounter, median), ...]} from the sample rows, read
    here unless a caller passes the ones it read."""
    return _contexts((sample_rows(db) if rows is None else rows) or ())


def _context_stats(entries):
    """(encounters per address, (median, weight) pairs with each address weighted once, UTC days)
    for one context's (address, encounter, median) entries."""
    by_address = Counter(address for address, _, _ in entries)
    weighted = [(median, 1 / by_address[address]) for address, _, median in entries]
    days = {
        datetime.fromtimestamp(encounter, timezone.utc).date()
        for _, encounter, _ in entries
        if number(encounter) is not None
    }
    return by_address, weighted, days


def type_norm(contexts, typecode, phase_band, exclude_address):
    """The usual ground speed of a type in one phase and altitude band, as a block, or None.

    Tenth and ninetieth percentiles of per-address encounter medians, each address
    weighted once, rounded to five knots. It needs NORM_MIN_ADDRESSES addresses besides
    the story's own over CONTEXT_DAYS days. A population figure from these records,
    never a limit or a claim about one aircraft.
    """
    if not typecode or not phase_band:
        return None
    phase, band = phase_band
    entries = [item for item in contexts.get((typecode, phase, band), ()) if item[0] != exclude_address]
    by_address, weighted, days = _context_stats(entries)
    if len(by_address) < NORM_MIN_ADDRESSES or len(days) < CONTEXT_DAYS:
        return None
    return {
        "low": round(_weighted_percentile(weighted, 0.1) / 5) * 5,
        "high": round(_weighted_percentile(weighted, 0.9) / 5) * 5,
        "phase": phase,
        # Samples start at SAMPLE_FLOOR_FT, so the lowest band does too.
        "band_floor_ft": max(SAMPLE_FLOOR_FT, int(band) * 10000),
        "band_ceiling_ft": (int(band) + 1) * 10000,
        "addresses": len(by_address),
        "days": len(days),
        "window_days": NORM_WINDOW_DAYS,
    }


def performance_profiles(db, rows=None):
    """Learn from quality-filtered direct samples, using robust per-address values.

    These are local spotting thresholds, never published aircraft limits. Each
    sample row holds one encounter's direct, quality-checked speeds; its median
    resists single spikes. A type's speed uses each address's highest encounter
    median. Its altitude uses each sampled address's maximum, capped by the highest
    altitude band of its direct samples, so a solver or decoder spike cannot set
    it. The 90th percentile avoids letting one extreme address set a threshold.
    `rows` are sample_rows(db) when a caller has read them.
    """
    profiles = {}
    rows = sample_rows(db) if rows is None else rows
    if rows is None:
        return profiles
    speeds, bands = defaultdict(dict), defaultdict(dict)
    for typecode, _phase, band, address, _encounter, median in rows:
        speeds[typecode][address] = max(median, speeds[typecode].get(address, median))
        bands[typecode][address] = max(band, bands[typecode].get(address, band))
    contexts = _contexts(rows)
    altitudes = defaultdict(list)
    for typecode, address, altitude in db.execute(
        "SELECT typecode,hex,max_alt FROM aircraft WHERE typecode<>'' AND hex NOT LIKE '~%'"
    ):
        band = bands.get(typecode, {}).get(address)
        if band is not None and number(altitude) is not None and 5000 <= altitude <= MAX_PLAUSIBLE_ALT_FT:
            altitudes[typecode].append(min(altitude, (band + 1) * 10000))
    for typecode in speeds.keys() | altitudes.keys():
        profile = {}
        for field, values, limits in (
            ("gs", speeds.get(typecode, {}).values(), (50, 200, 700, 25)),
            ("alt", altitudes.get(typecode, []), (5000, 18000, 55000, 1000)),
        ):
            values = sorted(values)
            if len(values) >= 20:
                profile[field] = _threshold(values, *limits)
                profile[field + "_addresses"] = len(values)
        if profile:
            profiles[typecode] = profile
    for (typecode, phase, band), entries in contexts.items():
        encounters_by_address, weighted, days = _context_stats(entries)
        if len(encounters_by_address) < CONTEXT_MIN_ADDRESSES:
            continue
        # Each address has the same total weight, however many encounters it has.
        threshold = _rounded(_weighted_percentile(weighted, 0.95), 50, 200, 700, 25)
        if len(days) < CONTEXT_DAYS:
            # A few days of one wind pattern cannot lower the type-level threshold.
            threshold = max(threshold, profiles.get(typecode, {}).get("gs", DEFAULT_SPEED))
        profiles.setdefault(typecode, {}).setdefault("contexts", {})[f"{phase}:{band}"] = {
            "gs": threshold,
            "addresses": len(encounters_by_address),
            "days": len(days),
            "basis": "Per-encounter medians, each address weighted equally",
        }
    return profiles


def observe_performance(db, aircraft, stamp, encounter):
    context = performance_context(aircraft)
    speed = number(aircraft.get("gs"))
    if (
        not context
        or not aircraft.get("t")
        or aircraft["hex"].startswith("~")
        or speed is None
        or not 100 <= speed <= 750
        or not SAMPLE_FLOOR_FT <= aircraft["alt_baro"] <= MAX_PLAUSIBLE_ALT_FT
    ):
        return
    if quality(
        [{**aircraft, "source": aircraft.get("type", "unknown")}],
        ("gs", "alt_baro", "baro_rate", "lat", "lon"),
    ):
        return
    key = (aircraft["hex"], encounter, aircraft["t"], *context)
    old = db.execute(
        "SELECT updated_at,values_json FROM performance_samples WHERE hex=? AND encounter=? AND typecode=? AND phase=? AND altitude_band=?",
        key,
    ).fetchone()
    if old and stamp - old[0] < 60:
        return
    values = (json.loads(old[1]) if old else []) + [speed]
    db.execute(
        "INSERT OR REPLACE INTO performance_samples VALUES (?,?,?,?,?,?,?)",
        (*key, stamp, json.dumps(values[-9:])),
    )


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS event_reviews (
          event_id INTEGER PRIMARY KEY, hex TEXT NOT NULL, kind TEXT NOT NULL,
          event_first REAL NOT NULL, verdict TEXT NOT NULL, reviewed_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS encounter_state (
          hex TEXT PRIMARY KEY, started REAL NOT NULL, last_seen REAL NOT NULL,
          ground INTEGER NOT NULL, registration TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS performance_samples (
          hex TEXT, encounter REAL, typecode TEXT, phase TEXT, altitude_band INTEGER,
          updated_at REAL, values_json TEXT,
          PRIMARY KEY(hex,encounter,typecode,phase,altitude_band));
    """)


def encounter_start(db, aircraft, stamp):
    address = aircraft["hex"]
    ground = aircraft.get("alt_baro") == "ground"
    registration = aircraft.get("r", "")
    old = db.execute("SELECT * FROM encounter_state WHERE hex=?", (address,)).fetchone()
    new = (
        not old
        or stamp - old["last_seen"] > ENCOUNTER_GAP
        or ground != bool(old["ground"])
        or (registration and old["registration"] and registration != old["registration"])
    )
    started = stamp if new else old["started"]
    db.execute(
        "INSERT OR REPLACE INTO encounter_state VALUES (?,?,?,?,?)",
        (address, started, stamp, int(ground), registration or (old["registration"] if old else "")),
    )
    return started, bool(new)


def save_review(db, event_id, verdict, now):
    if type(event_id) is not int or not 1 <= event_id <= 9223372036854775807:
        raise ValueError("Invalid sighting ID")
    if verdict not in VERDICTS | {"clear"}:
        raise ValueError("Choose keep, routine, bad_data or clear")
    event = db.execute("SELECT id,hex,kind,first_seen FROM events WHERE id=?", (event_id,)).fetchone()
    if event is None:
        raise ValueError("Sighting not found")
    if verdict == "clear":
        db.execute("DELETE FROM event_reviews WHERE event_id=?", (event_id,))
    else:
        db.execute("INSERT OR REPLACE INTO event_reviews VALUES (?,?,?,?,?,?)", (*event, verdict, now))


def review_verdict(db, event):
    # Feedback is bound to the event identity, so a reused row ID inherits nothing.
    row = db.execute(
        "SELECT verdict FROM event_reviews WHERE event_id=? AND hex=? AND kind=? AND event_first=?",
        (event["id"], event["hex"], event["kind"], event["first_seen"]),
    ).fetchone()
    return row[0] if row else None


def _contextual_spike(event):
    """A documented isolated reading: neighbouring samples bracket one direct peak.

    It ranks a private review lead; it never publishes, because the owner
    publishes no one-report stories (the single_report gate, curation.gates).
    """
    evidence = event["evidence"]
    fields = {
        "outlier_gs": "gs",
        "type_speed_mismatch": "gs",
        "type_altitude_mismatch": "alt_baro",
        "outlier_alt_baro": "archive_alt",
        "outlier_archive_alt": "archive_alt",
    }
    field = fields.get(event["kind"])
    if field is None:
        return False
    peak = number(evidence.get("observed_at"))
    samples = evidence.get("context_samples", [])
    if peak is None or not isinstance(samples, list):
        return False
    before, after, at_peak = [], [], []
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        stamp, value = number(sample.get("timestamp")), number(sample.get(field))
        if stamp is None or value is None or abs(stamp - peak) > 90:
            continue
        source = sample.get("source")
        if not isinstance(source, str) or source in {"", "unknown"}:
            continue
        reading = (stamp, value, source)
        if stamp < peak:
            before.append(reading)
        elif stamp > peak:
            after.append(reading)
        else:
            at_peak.append(reading)
    value = number(event.get("value"))
    if value is None or not before or not after or len(at_peak) != 1:
        return False
    previous, following, peak_reading = max(before), min(after), at_peak[0]
    # A multilateration value is the solver's estimate, not a transmitted field.
    # The value keeps its sign, so a negative spike compares by magnitude too.
    return (
        previous[2] == following[2] == peak_reading[2]
        and peak_reading[2] in DIRECT
        and abs(peak_reading[1] - value) <= 1
        and abs(value) > 2 * max(abs(previous[1]), abs(following[1]), 1)
    )


# Kinds scored by kind alone, with the reason they carry, in their recorded order.
FIXED_SCORES = kinds.table(
    "fixed_score",
    order=(
        "identity_conflict",
        "military",
        "database_watch",
        "unusual_emitter",
        "vertical",
        "wind_boost",
        "altitude_gap",
        "distant",
    ),
)
# A novelty claim (first sighting, notable variant) needs two reports at least 8 s apart.
NOVELTY_MIN_HITS = 2
NOVELTY_MIN_SECONDS = 8


def _brief(event):
    """Too few reports, or too short a span, to support a novelty claim."""
    first, last = number(event.get("first_seen")), number(event.get("last_seen"))
    return (number(event.get("hits")) or 0) < NOVELTY_MIN_HITS or (
        first is not None and last is not None and last - first < NOVELTY_MIN_SECONDS
    )


def assess(event, profiles=None, *, independent=False):
    """Score one saved finding for private views and story eligibility.

    Context-only kinds stay below promotion unless `independent` says their
    encounter also holds an independently promoted finding.
    """
    evidence = _mapping(event.get("evidence"))
    aircraft = _mapping(evidence.get("aircraft"))
    kind, value = event["kind"], number(event.get("value"))
    reasons, review, private = [], False, False
    score = 40
    speed, altitude = performance_thresholds(aircraft, profiles)
    if kind in {"fast", "high"}:
        threshold = speed if kind == "fast" else altitude
        step = 10 if kind == "fast" else 1000
        score = (
            70 + min(15, int((value - threshold) / step)) if value is not None and value >= threshold else 30
        )
        reasons.append(f"Local promotion threshold: {threshold:g} {'kn' if kind == 'fast' else 'ft'}")
    elif kind == "vintage":
        story = curation.VINTAGE_YEARS
        score = (
            80 if value is not None and value >= story else 65 if value is not None and value >= 65 else 30
        )
        reasons.append(f"Ages below {story} stay private; below 65 they are searchable metadata")
    elif kind == "watch_type":
        score = 82 if isinstance(aircraft.get("t"), str) and aircraft["t"] in WATCH_TYPES else 45
    elif kind in NOVELTY_KINDS:
        score = 80
        if _brief(event):
            score, review = 50, True
            reasons.append("A novelty claim needs two reports at least 8 seconds apart")
    elif kind in FIXED_SCORES:
        score, reason = FIXED_SCORES[kind]
        if reason:
            reasons.append(reason)
    elif kind == "watchlist_match":
        match = _mapping(evidence.get("watchlist"))
        specific = isinstance(match.get("neutral_category"), str) and match.get("neutral_category") in {
            "Public figure association",
            "Historic aircraft",
            "Distinctive aircraft",
            "Royal aviation association",
            "Aerobatic display aircraft",
            "Specialized aircraft",
        }
        score = 80 if specific and match.get("reference_url") else 65 if specific else 50
        if (
            match.get("public_claim_allowed") is not True
            or match.get("match_status") != "icao_and_registration"
        ):
            score, review = 20, True
        source = _mapping(match.get("source"))
        try:
            source_date = datetime.fromisoformat(source["source_date"])
            stale = source_date.tzinfo is None or event["last_seen"] - source_date.timestamp() > 180 * 86400
        except KeyError, TypeError, ValueError, AttributeError:
            stale = True
        if stale:
            score, review = min(score, 60), True
            reasons.append("Watchlist source age needs review; source dates do not verify associations")
    elif kind == "rare_here":
        rarity = _mapping(evidence.get("rarity"))
        aircraft_days, type_days = number(rarity.get("aircraft_days")), number(rarity.get("type_days"))
        review = aircraft_days is None or type_days is None
        resident = (aircraft_days or 0) > 2 or (type_days or 0) > 3
        score = 35 if resident else 70
        reasons.append("Repeat days matter as well as the number of aircraft addresses")
    elif kind.startswith(SQUAWK_PREFIXES):
        # Only a confirmed live episode below can make a status story-eligible.
        score, review, private = 60, True, True
        reasons.append(
            "A transponder status needs a confirmed live episode; archived labels never confirm one"
        )
    elif kind in {"circling", "repeated_passes", "loitering", "confined_activity"}:
        pattern = _mapping(evidence.get("pattern"))
        review = pattern.get("quality_review_required") is not False
        score = {"circling": 80, "repeated_passes": 78, "loitering": 75, "confined_activity": 45}[kind]
        if review:
            score = min(score, 60)
            reasons.append("Source or geometry needs review before promotion")
        if pattern.get("shape_verified") is not True:
            score, review = min(score, 60), True
            reasons.append("Saved pattern predates the closure/corridor checks")
    elif (
        kind in RECORD_KINDS
        or kind in RATED_KINDS
        or (kind in ENVELOPE_KINDS and _mapping(evidence.get("confirmation")).get("sustained") is True)
    ):
        confirmation = _mapping(evidence.get("confirmation"))
        window = _mapping(evidence.get("season_record"))
        if kind in RECORD_KINDS and not confirmation and window:
            # A seasonal record carries its own sustained direct-report window.
            confirmation = {"sustained": True, "duration_seconds": window.get("sustained_seconds")}
            source, reports = window.get("source"), number(window.get("samples")) or 0
        else:
            source, reports = aircraft.get("type"), event.get("hits", 0)
        score = 75
        if confirmation.get("sustained") is not True or (
            kind in RECORD_KINDS
            and (number(confirmation.get("duration_seconds")) or 0) < SEASON_SUSTAINED_SECONDS
        ):
            score, review = 50, True
            reasons.append("Sustained direct reports were not preserved")
        if confirmation.get("quality_flags") or reports < 2 or source not in DIRECT:
            score, review = min(score, 50), True
            reasons.append("Measurement provenance needs review; only sustained direct reports qualify")
        if kind in ENVELOPE_KINDS:
            reasons.append("Sustained readings beyond the listed type's envelope suggest a metadata mismatch")
    elif kind.startswith("outlier_") or kind in {"position_jump", "space_category"} | ENVELOPE_KINDS:
        score, review = 50, True
        if _contextual_spike(event):
            # A bracketed spike is a better private lead than a bare extreme, never a story.
            score = 60
            reasons.append("Neighboring samples document an isolated reading, not a flight capability")
        else:
            reasons.append("Extreme magnitude alone does not establish reliable evidence")
    if kind in NUMERIC_KINDS:
        confirmation = _mapping(evidence.get("confirmation"))
        if confirmation.get("quality_flags"):
            review = True
            reasons.append("Measurement or position provenance needs review")
        if confirmation.get("sustained") is not True:
            review = True
            reasons.append("Sustained field-coherent observations were not preserved")
        if event.get("hits", 0) < 2:
            score = min(score, 40)
        if not isinstance(aircraft.get("type"), str) or aircraft["type"] in {"mlat", "unknown"}:
            review = True
            reasons.append("Source reliability needs review; correlated fields are not independent evidence")
    verdict = event.get("verdict")
    episode = _mapping(evidence.get("maneuver"))
    if episode:
        review = episode.get("quality_review_required") is not False or episode.get("confirmed") is not True
        if kind in {"rapid_climb", "rapid_descent"} and (
            (number(episode.get("qualifying_duration_seconds")) or 0) < 30
            or (number(episode.get("qualifying_sample_count")) or 0) < 4
        ):
            review = True
            reasons.append("Consecutive above-threshold reports have not been established")
        score = 95 if kind.startswith(LIVE_SQUAWK_PREFIX) else 80
        private = private and review
        if review:
            score = min(score, 60)
            reasons.extend(episode.get("quality_flags", []))
            if episode.get("confirmed") is not True:
                reasons.append("Episode has not met its confirmation window")
    if kind.startswith(ARCHIVE_SQUAWK_PREFIX):
        score, review, private = min(score, 60), True, True
    tags = curation.context_tags(evidence)
    if kind in curation.CIRCUIT_KINDS and (evidence.get("airport_context") or tags & curation.AIRPORT_TAGS):
        score = min(score, 50)
        reasons.append("Observed near a runway's circuit or approach; retained as routine-context evidence")
    if evidence.get("quality_flags"):
        score, review = min(score, 60), True
        reasons.append("Blocking quality flags require review")
    if kind in IDENTITY_KINDS and _mapping(evidence.get("identity_checks")).get("conflict") is True:
        score, review = min(score, 60), True
        reasons.append("Identity checks disagree; the database identity cannot support this story")
    blocking = curation.gates(event)
    if blocking:
        # A second source's disagreement, too few reports, or an unsettled rated
        # verdict hold the finding for review whatever its score.
        review = True
        reasons.extend(reason for _, reason in blocking)
    hold = curation.type_hold(event)
    if hold:
        # Not a review hold: selection waits for the type check and resumes unattended.
        reasons.append(hold[1])
    if not independent and curation.context_only(kind, evidence):
        score = min(score, PROMOTION_SCORE - 1)
        reasons.append("Supporting context; promoted only beside an independent finding in its encounter")
    if verdict == "keep":
        score = max(80, score)
        reasons.append("Marked worth keeping; this is editorial feedback, not factual verification")
    elif verdict in {"routine", "bad_data"}:
        score = 0
        reasons.append("Marked " + verdict.replace("_", " "))
    return {
        "version": VERSION,
        "score": score,
        "promoted": score >= PROMOTION_SCORE and (not review or verdict == "keep"),
        "story_eligible": score >= STORY_SCORE
        and (not review or verdict == "keep")
        and kind != "distant"
        and not private,
        "review_required": review,
        "reasons": reasons,
        "blocking_rules": [rule for rule, _ in blocking],
        "publication_hold": hold[0] if hold else None,
        "verdict": verdict,
    }


def publishable(source):
    """Whether one saved finding may become an unattended story: the one eligibility check.

    A Keep vote never certifies uncertain telemetry, so a rejected verdict or any
    quality flag refuses it. Archived labels never confirm a status, and a live
    status needs its confirmed episode (8 s and two reports). A second source
    that disagrees about the type (unless a mismatch story names it), or none
    for a performance kind, refuses it through the review gates; a check still
    pending is a selection hold
    (curation.type_hold), not a refusal, so a released story is never
    unpublished for a check that has not run. The rest is assess() without the
    vote: story eligible, no review, and not context only.
    """
    if source.get("verdict") in {"routine", "bad_data"}:
        return False
    raw = source.get("evidence", {})
    metrics = raw.get("maneuver", {})
    if (
        metrics.get("quality_flags")
        or raw.get("confirmation", {}).get("quality_flags")
        or raw.get("quality_flags")
    ):
        return False
    if source["kind"].startswith(ARCHIVE_SQUAWK_PREFIX):
        return False
    # A kind the story engine cannot render has no unattended story. Imported
    # here so the collector container never loads the story engine.
    from .stories import STORY_KINDS

    if source["kind"] not in STORY_KINDS:
        return False
    if source["kind"].startswith(LIVE_SQUAWK_PREFIX) and not (
        metrics.get("confirmed") is True
        and metrics.get("quality_review_required") is False
        and (number(metrics.get("duration_seconds")) or 0) >= 8
        and (number(metrics.get("sample_count")) or 0) >= 2
    ):
        return False
    decision = assess({**source, "verdict": None}, {})
    return bool(
        decision["story_eligible"]
        and not decision["review_required"]
        and not curation.context_only(source["kind"], raw)
    )


def _rows(db, now, limit=None):
    # Project only fields used by selection. Never load all raw track evidence.
    # Each altitude field, since the record plausibility gate picks its wind band from any.
    fields = (
        "hex",
        "r",
        "t",
        "flight",
        "desc",
        "type",
        "category",
        "year",
        "alt_baro",
        "alt_geom",
        "archive_alt",
        "baro_rate",
    )
    aircraft = ",".join(f"'{key}',json_extract(e.evidence,'$.aircraft.{key}')" for key in fields)
    extras = ",".join(
        f"'{key}',json_extract(e.evidence,'$.{key}')"
        for key in (
            "pattern",
            "confirmation",
            "rarity",
            "watchlist",
            "context_samples",
            "observed_at",
            "encounter_start",
            "maneuver",
            "airport_context",
            "quality_flags",
            "context_tags",
            "identity_checks",
            "participants",
            "variant",
            "country",
            "season_record",
            "first_sighting",
            "absorbed_by",
            "type_check",
            "rated_limit",
        )
    )
    has_reviews = db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='event_reviews' AND type='table'"
    ).fetchone()
    review_join = (
        "LEFT JOIN event_reviews r ON r.event_id=e.id AND r.hex=e.hex AND r.kind=e.kind AND r.event_first=e.first_seen"
        if has_reviews
        else ""
    )
    review_value = "r.verdict" if has_reviews else "NULL AS verdict"
    query = f"""WITH retained AS (
        SELECT id FROM events WHERE first_seen<=? ORDER BY last_seen DESC,id DESC LIMIT ?
        ) SELECT e.id,e.hex,e.kind,e.category,e.title,e.value,e.unit,e.first_seen,e.last_seen,e.hits,e.origin,
        json_object('aircraft',json_object({aircraft}),{extras}) evidence,{review_value}
        FROM retained JOIN events e ON e.id=retained.id {review_join}
        WHERE json_valid(e.evidence)
          AND json_type(e.evidence)='object' AND json_type(e.evidence,'$.aircraft')='object'
        ORDER BY e.first_seen,e.id"""
    # A check the worker has saved but not yet written into the row, or a frozen
    # corpus's check, counts for selection as it will once written: read them once.
    checks = typecheck.saved_all(db)
    for row in db.execute(query, (now, EVENT_LIMIT if limit is None else limit)):
        event = dict(row)
        event["evidence"] = json.loads(event["evidence"])
        event["evidence"] = {k: v for k, v in event["evidence"].items() if v is not None}
        aircraft = event["evidence"]["aircraft"]
        for key in ("alt_geom", "archive_alt"):
            # A key's presence marks an archived or geometric reading elsewhere
            # (stories.archive_origin), so an unsaved one is left out, not null.
            if aircraft.get(key) is None:
                aircraft.pop(key, None)
        if any(
            value is not None and not isinstance(value, str)
            for key, value in event["evidence"]["aircraft"].items()
            if key not in {"year", "alt_baro", "alt_geom", "archive_alt", "baro_rate"}
        ):
            continue
        address = str(event.get("hex") or "").lower()
        typecheck.fill(event, checks.get(address), now if event["origin"] == LIVE_ORIGIN else None)
        yield event


def encounters(
    db, now, profiles=None, ignore_reviews=False, *, identity_cooldown=True, include_absorbed=False
):
    profiles = performance_profiles(db) if profiles is None else profiles
    groups, latest, started, promoted = [], {}, {}, {}
    for event in _rows(db, now):
        if curation.absorbed(event) and not include_absorbed:
            # A shape a later circle or survey grid covers is never a separate reason.
            continue
        event["selection"] = decision = assess(
            {**event, "verdict": None} if ignore_reviews else event, profiles
        )
        aircraft = event["evidence"]["aircraft"]
        # Any identity reason repeats the same airframe's identity story.
        key = (event["hex"], aircraft.get("r"), aircraft.get("t"))
        if identity_cooldown and event["kind"] in IDENTITY_KINDS and decision["promoted"]:
            earlier = promoted.get(key)
            if (
                earlier is not None
                and event["first_seen"] - earlier < IDENTITY_COOLDOWN
                and (ignore_reviews or event.get("verdict") != "keep")
            ):
                decision.update(score=30, promoted=False, story_eligible=False)
                decision["reasons"].append("This aircraft had an identity story promoted within 30 days")
            else:
                promoted[key] = event["first_seen"]
        start = number(event["evidence"].get("encounter_start"))
        registration = aircraft.get("r")
        # The collector's encounter start wins over intervening rows without one.
        group = started.get((event["hex"], start)) if start is not None else None
        if (
            group is not None
            and registration
            and group["registration"]
            and registration != group["registration"]
        ):
            group = None
        if group is None:
            group = latest.get(event["hex"])
            if (
                group is None
                or (
                    event["first_seen"] > group["last_seen"] + ENCOUNTER_GAP
                    and not (start is not None and start == group["start"])
                )
                or (start is not None and group["start"] is not None and start != group["start"])
                or (registration and group["registration"] and registration != group["registration"])
            ):
                group = {
                    "events": [],
                    "first_seen": event["first_seen"],
                    "last_seen": event["last_seen"],
                    "start": None,
                    "registration": registration,
                }
                groups.append(group)
        latest[event["hex"]] = group
        group["events"].append(event)
        group["last_seen"] = max(group["last_seen"], event["last_seen"])
        group["registration"] = registration or group["registration"]
        if group["start"] is None and start is not None and (event["hex"], start) not in started:
            group["start"] = start
            started[(event["hex"], start)] = group
    keys = set()
    for group in groups:
        # Context-only shapes stay supporting evidence unless another finding qualifies.
        if any(
            event["selection"]["promoted"] and not curation.context_only(event["kind"], event["evidence"])
            for event in group["events"]
        ):
            for event in group["events"]:
                if curation.context_only(event["kind"], event["evidence"]):
                    event["selection"] = assess(
                        {**event, "verdict": None} if ignore_reviews else event, profiles, independent=True
                    )
        key = curation.episode_key(group)
        if key in keys:
            # A stable key for a split encounter, independent of other aircraft.
            group["id"] = key = f"{key}#{group['events'][0]['id']}"
        keys.add(key)
    return groups


def independent_finding(db, event, profiles):
    """Whether a context-only finding's encounter also holds an independently
    promoted finding: the rows encounters() groups with it by address and saved
    encounter start (within a day), assessed the same way."""
    from .app import event_dict

    start = event["evidence"].get("encounter_start") if isinstance(event.get("evidence"), dict) else None
    if start is None or not curation.context_only(event["kind"], event["evidence"]):
        return False
    rows = db.execute(
        """SELECT * FROM events WHERE hex=? AND id<>? AND last_seen>=? AND first_seen<=?
        AND json_extract(evidence,'$.encounter_start')=?""",
        (event["hex"], event["id"], event["first_seen"] - 86400, event["last_seen"] + 86400, start),
    )
    for row in rows:
        try:
            other = event_dict(row)
        except ValueError, TypeError, KeyError:
            continue
        if curation.context_only(other["kind"], other["evidence"]) or curation.absorbed(other):
            continue
        other["verdict"] = review_verdict(db, other)
        if assess(other, profiles)["promoted"]:
            return True
    return False


def type_presence(db):
    """Coverage days and the days each aircraft type was seen, from the daily table."""
    names = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('daily','coverage')"
        )
    }
    if names != {"daily", "coverage"}:
        return {"coverage": [], "types": {}}
    coverage = [
        row[0]
        for row in db.execute("SELECT day FROM coverage WHERE seconds>=? ORDER BY day", (COVERAGE_SECONDS,))
    ]
    days, types = {}, defaultdict(set)
    for typecode, day in db.execute(
        "SELECT typecode,day FROM daily WHERE typecode<>'' AND hex NOT LIKE '~%' GROUP BY typecode,day"
    ):
        # One shared string per date keeps this small in the collector container.
        types[typecode].add(days.setdefault(day, day))
    return {"coverage": coverage, "types": dict(types)}


def novelty(db):
    """Per-date type-day counts for curation.choose."""
    presence = type_presence(db)
    cache = {}

    def counts(day):
        if day not in cache:
            cache[day] = curation.type_day_counts(presence, day)
        return cache[day]

    return counts


def select(db, params, now, limit=120, stream=False):
    with _SELECTION_LOCK:
        return _select(db, params, now, limit, stream)


def _select(db, params, now, limit=120, stream=False):
    days = params.get("days", ["1"])[0]
    cutoff = now - (int(days) if days in {"1", "7", "30", "365"} else 1) * 86400
    view = params.get("view", ["promoted"])[0]
    category, query = params.get("category", [""])[0], params.get("q", [""])[0][:80].casefold()
    choices = []
    for group in encounters(db, now):
        if group["last_seen"] < cutoff:
            continue
        members = [
            e
            for e in group["events"]
            if (not category or e["category"] == category)
            and (not query or query in (e["title"] + json.dumps(e["evidence"]["aircraft"])).casefold())
        ]
        if view == "promoted":
            members = [e for e in members if e["selection"]["promoted"]]
        elif view == "review":
            members = [e for e in members if e["selection"]["review_required"] and not e["verdict"]]
        if not members:
            continue
        best = max(members, key=lambda e: (e["selection"]["score"], e["last_seen"], -e["id"]))
        choices.append((best, group))
    choices.sort(key=lambda item: (-item[0]["selection"]["score"], -item[1]["last_seen"]))
    if params.get("sort", ["wow"])[0] == "wow":
        ranks, decorated = Counter(), []
        for best, group in choices:
            key = (best["kind"], best["evidence"]["aircraft"].get("t") or best["hex"])
            decorated.append((ranks[key], best, group))
            ranks[key] += 1
        decorated.sort(key=lambda item: (item[0], -item[1]["selection"]["score"], -item[2]["last_seen"]))
        choices = [(best, group) for _, best, group in decorated]
    else:
        choices.sort(key=lambda item: -item[1]["last_seen"])
    from .app import event_dict

    # Detach compact IDs and reason summaries before returning a stream. Raw
    # evidence and the full projected graph must not live as long as a download.
    total = len(choices)
    summaries = [
        (
            best["id"],
            best["selection"],
            {
                "first_seen": group["first_seen"],
                "last_seen": group["last_seen"],
                "reasons": [
                    {"id": e["id"], "kind": e["kind"], "title": e["title"], "selection": e["selection"]}
                    for e in group["events"]
                ],
            },
        )
        for best, group in choices[:limit]
    ]
    choices.clear()

    def results():
        for event_id, decision, encounter in summaries:
            row = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if row is None:
                continue
            event = event_dict(row)
            event["selection"] = decision
            event["encounter"] = encounter
            yield event

    return (results() if stream else list(results())), total
