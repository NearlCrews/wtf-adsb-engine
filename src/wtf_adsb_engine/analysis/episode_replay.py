"""Bounded offline adapter for the same episode rules used by the collector."""

import bisect
import itertools
import math
import statistics
from collections import defaultdict, deque

from . import maneuvers
from .rules import PATTERN_WINDOW_SECONDS, distance_nm, number
from .selection import ENCOUNTER_GAP
from .telemetry import DIRECT, LIMIT, WINDOW, advisory_sense, bounded_append, positions, recent

# Non-blocking context tag: the callsign or squawk is an archive label saved
# before the position it accompanies, not a reading at that position's time.
CARRIED_LABEL = "archive_carried_label"
# readsb traces repeat some MLAT fixes at one timestamp a few metres apart.
DUPLICATE_DISTANCE_NM = 0.01
# Fields two same-time versions must agree on, or their time becomes a conflict that
# clears an episode's window. Ground speed is not one: hover, movement and formation
# checks read it, but a disagreement about it alone should not clear every other
# rule's window, so merged_reading() leaves a disputed speed unknown instead.
# history_backfill.DUPLICATE_FIELDS adds it for sustained-speed windows.
READING_FIELDS = ("alt_baro", "alt_geom", "archive_alt", "baro_rate", "geom_rate")
STATUS_FIELDS = ("squawk", "emergency")
LABEL_FIELDS = ("flight", "squawk", "label_at")
# A break marked on any copy of one fix applies to that fix.
BREAK_FLAGS = ("gap_before", "new_leg")
# Like the collector's buffer, the replay buffer survives source changes and short gaps;
# the episode rules split their own segments. A minute of silence ends it.
SILENT_SECONDS = maneuvers.STATUS_GAP
# telemetry.positions() starts a new position segment after a longer gap.
POSITION_GAP_SECONDS = 45
# Rules over fields that readsb saves only with a trace row's detail object.
DETAIL_FIELDS = ("nic", "nac_p", "gpsOkBefore", "nav_altitude_mcp")
DETAIL_RULES = (maneuvers.gnss_integrity_loss, maneuvers.selected_altitude_overshoot)
DETAIL_KINDS = ("gnss_integrity_loss", "selected_altitude_overshoot")
# Readings copied into a snapshot taken from a track fix rather than an episode sample.
SNAPSHOT_READINGS = (
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
    "nic",
    "nac_p",
)
# The collector ends an open episode once its aircraft has been silent this long.
CLOSE_AFTER_SECONDS = SILENT_SECONDS + 1
# Straight-leg summaries kept per aircraft for survey grids longer than the buffer,
# bounded like the collector's per-encounter list.
LEG_REFRESH_SECONDS = 300
LEG_HORIZON_SECONDS = 7200
LEG_LIMIT = 64
LEG_RESET_SECONDS = 1800
# Multi-aircraft detectors run over a day's saved tracks at the collector's cadence.
FORMATION_CADENCE_SECONDS = 60
FORMATION_CLOSE_NM = 0.5
FORMATION_CLOSE_SECONDS = 5
FORMATION_FLOOR_FT = 1500
GNSS_MEMORY_SECONDS = 900
# Results of successive evaluations up to this far apart describe one phenomenon.
GROUP_MERGE_SECONDS = 120
ORBIT_KINDS = ("circling", "prolonged_holding")
# TCAS evidence uses the richest retained track around the advisory.
TCAS_MARGIN_SECONDS = 120
TCAS_SNAPSHOT_SECONDS = 30
# The response rule reads the latest rate in the 30 s before the first active message and
# the latest in the 20 s after it (maneuvers.tcas_episode). An archived rate's freshness is
# unknown, so each one used must agree, within 500 ft/min or half the rate, with the
# altitude change between saved fixes 6-30 s apart that end at, start at or span it (a
# rate can change sharply at the response, and traces can be sparse).
RESPONSE_WINDOW = (30, 20)
RATE_SPAN_SECONDS = (6, 30)
RATE_AGREEMENT_FPM = 500
SEPARATION_KEYS = ("min_separation_nm", "vertical_separation_ft")


def same_position(left, right):
    """Two fixes within 0.01 NM: one position as far as movement is concerned."""
    first, second = ((number(p.get("lat")), number(p.get("lon"))) for p in (left, right))
    if None in first or None in second:
        return first == second
    return distance_nm(first, second) <= DUPLICATE_DISTANCE_NM


def _label_time(point):
    value = number(point.get("label_at"))
    return -math.inf if value is None else value


def _agree(left, right, key):
    first, second = left.get(key), right.get(key)
    if first is None or second is None or first == second:
        return True
    # A squawk saved at another time is the newer label state, not a disagreement;
    # without both label times the codes simply disagree.
    times = (number(left.get("label_at")), number(right.get("label_at")))
    return key == "squawk" and None not in times and times[0] != times[1]


def duplicate_reading(left, right, fields=READING_FIELDS):
    """Same-time, same-source rows within 0.01 NM whose `fields` agree are one reading.

    A field reported by only one row is not a disagreement: readsb attaches a
    detail object to one row of a repeated MLAT fix. Anything else at one
    timestamp is a material disagreement, i.e. a conflict.
    """
    return (
        left.get("source") == right.get("source")
        and all(_agree(left, right, key) for key in fields)
        and same_position(left, right)
    )


def merged_reading(kept, other):
    """The union of two agreeing readings: break flags combine and the newer label wins.

    Two different ground speeds leave none: the input order must not choose one.
    """
    merged = {**other, **{key: value for key, value in kept.items() if value is not None}}
    speeds = (number(kept.get("gs")), number(other.get("gs")))
    if None not in speeds and speeds[0] != speeds[1]:
        merged["gs"] = None
    for flag in BREAK_FLAGS:
        if kept.get(flag) or other.get(flag):
            merged[flag] = True
    newer, older = (other, kept) if _label_time(other) > _label_time(kept) else (kept, other)
    labels = {key: newer[key] for key in LABEL_FIELDS if key in newer}
    if _label_time(older) == _label_time(newer):
        # One saved label state: the copies agree, so their fields combine.
        labels = {**{key: older[key] for key in LABEL_FIELDS if key in older}, **labels}
    for key in LABEL_FIELDS:
        merged.pop(key, None)
    return {**merged, **labels}


def carried_label(aircraft, observed_at):
    label_at, observed = number(aircraft.get("label_at")), number(observed_at)
    return (
        ("flight" in aircraft or "squawk" in aircraft)
        and label_at is not None
        and observed is not None
        and label_at < observed
    )


def add_context_tags(evidence, tags):
    """Maneuver findings keep tags in the maneuver; other findings at top level."""
    target = evidence["maneuver"] if isinstance(evidence.get("maneuver"), dict) else evidence
    target["context_tags"] = sorted(set(target.get("context_tags", [])) | set(tags))


def snapshot(aircraft, finding):
    """The saved report snapshot (maneuvers.saved_snapshot), which keeps the
    database flags and the archive label time.

    The carried-label tag is added once, to the finished episode
    (replay_patterns.annotate_episode).
    """
    return maneuvers.saved_snapshot(aircraft, finding)[0]


def _provenance(finding, archive):
    """Apply the archive format's freshness rule (maneuvers.archived) when one is named."""
    return maneuvers.archived(finding, archive) if archive else finding


def identity(address, lookup):
    """Current metadata for one address; a lookup, not historical verification."""
    result = {
        "hex": address,
        "r": lookup.get("registration", lookup.get("r", "")),
        "t": lookup.get("typecode", lookup.get("t", "")),
        "desc": lookup.get("description", lookup.get("desc", "")),
    }
    for key in ("dbFlags", "year"):
        if key in lookup:
            result[key] = lookup[key]
    return result


def track_snapshot(aircraft, finding, track, stamp, limit=60):
    """An episode snapshot; without readings in its samples, the nearest saved fix supplies them."""
    result = snapshot(aircraft, finding)
    if "lat" in result or not track:
        return result
    nearest = min(track, key=lambda p: (abs(p["ts"] - stamp), p["ts"]))
    if abs(nearest["ts"] - stamp) > limit:
        return result
    readings = {key: nearest[key] for key in SNAPSHOT_READINGS if key in nearest}
    if "label_at" in nearest and ("flight" in readings or "squawk" in readings):
        readings["label_at"] = nearest["label_at"]
    result.update(readings, type=nearest.get("source", result.get("type", "unknown")))
    return result


def readings_by_time(raw_points):
    """One reading per timestamp; material same-time disagreement becomes a conflict."""
    by_stamp = {}
    for point in raw_points:
        stamp = number(point.get("ts"))
        if stamp is None:
            continue
        prior = by_stamp.get(stamp)
        if prior is None:
            by_stamp[stamp] = point
        elif "same_time_conflict" in prior:
            if not any(
                duplicate_reading(v, point, READING_FIELDS + STATUS_FIELDS) for v in prior["conflicts"]
            ):
                prior["conflicts"].append(point)
        elif duplicate_reading(prior, point, READING_FIELDS + STATUS_FIELDS):
            by_stamp[stamp] = merged_reading(prior, point)
        else:
            by_stamp[stamp] = {"ts": stamp, "same_time_conflict": True, "conflicts": [prior, point]}
    return by_stamp


def conflict_leads(variants, stamp, archive=None):
    leads = []
    for variant in variants:
        report = {**variant, "archive": variant.get("archive") or "retained archive"}
        found = [_provenance(lead, archive) for lead in maneuvers.signals([report])]
        for field in ("baro_rate", "geom_rate"):
            rate = number(report.get(field))
            if rate is not None and abs(rate) >= 4500:
                found.append(
                    maneuvers.build(
                        "rapid_climb" if rate > 0 else "rapid_descent",
                        [report],
                        "Conflicting vertical-rate report",
                        rate,
                        "ft/min",
                        confirmed=False,
                        flags=("conflicting_same_time_reports",),
                        archive=archive,
                        peak_rate_fpm=rate,
                        altitude_reference="alt_baro" if field == "baro_rate" else "alt_geom",
                    )
                )
        for lead in found:
            info = lead.evidence["maneuver"]
            info["quality_flags"] = sorted(set(info["quality_flags"]) | {"conflicting_same_time_reports"})
            info["quality_review_required"] = True
            leads.append(lead)
    return leads


class _Legs:
    """Straight legs of one aircraft, for survey grids longer than the buffer.

    A summary covers the buffer's latest position segment: every five minutes,
    and whenever that segment ends. A leg cut off at the buffer's start, once
    the buffer has dropped earlier reports, is skipped: an earlier summary saw
    it whole. A later summary of the same flown line replaces overlapping
    copies. Only legs that ended before the current position segment are
    passed on, since the episode rules measure that segment themselves.
    """

    def __init__(self):
        self.clear()

    def clear(self):
        self.legs, self.ends, self.refreshed = [], [], None

    def refresh(self, window, stamp, buffer_start, *, force=False):
        if not window or (
            not force and self.refreshed is not None and stamp - self.refreshed < LEG_REFRESH_SECONDS
        ):
            return
        self.refreshed = stamp
        track = positions(list(window))
        cut = (
            track[0]["ts"]
            if track and window[0]["ts"] > buffer_start and track[0]["ts"] <= window[0]["ts"]
            else None
        )
        for leg in maneuvers.straight_legs(track) if track else ():
            if cut is not None and leg["started_at"] <= cut:
                continue
            # Consecutive legs share their boundary fix; only a real overlap is another copy.
            self.legs = [
                old
                for old in self.legs
                if old["ended_at"] <= leg["started_at"] or old["started_at"] >= leg["ended_at"]
            ]
            self.legs.append(leg)
        self.legs = sorted(
            (leg for leg in self.legs if leg["ended_at"] >= stamp - LEG_HORIZON_SECONDS),
            key=lambda leg: (leg["ended_at"], leg["started_at"]),
        )[-LEG_LIMIT:]
        self.ends = [leg["ended_at"] for leg in self.legs]

    def before(self, stamp):
        """Saved legs that ended before ``stamp``, the current position segment's first fix."""
        index = bisect.bisect_left(self.ends, stamp)
        return sorted(self.legs[:index], key=lambda leg: leg["started_at"]) or None


def _matching(active, finding, info):
    """The replay episode a finding continues, if any."""
    key = (finding.kind, info.get("episode_start"))
    episode = active.get(key)
    if episode is None and info.get("episode_start") is not None:
        episode = next(
            (
                old
                for old in active.values()
                if old.finding.kind == finding.kind
                and old.last >= info["started_at"]
                and (
                    old.finding.evidence["maneuver"]["state"] == "active"
                    or all(
                        old.finding.evidence["maneuver"][field] == info[field]
                        for field in ("ended_at", "evaluated_at")
                    )
                )
            ),
            None,
        )
    return key, episode


def _record(add, active, address, window, aircraft, finding):
    """Fold one evaluation's finding into its replay episode, as the collector merges rows."""
    from .replay_patterns import ReplayEpisode

    info = finding.evidence["maneuver"]
    endpoint = info["evaluated_at"]
    key, episode = _matching(active, finding, info)
    if episode is None or info["started_at"] - episode.last > 120:
        episode = add(ReplayEpisode(address, finding, aircraft, info["started_at"], info["ended_at"]))
        active[key] = episode
    if finding.evidence.get("context_samples"):
        finding.evidence["context_samples"] = maneuvers.merge_context(
            episode.finding.evidence.get("context_samples", []), finding.evidence["context_samples"]
        )
    if endpoint in episode.endpoints:
        if finding.evidence.get("context_samples"):
            episode.finding.evidence["context_samples"] = finding.evidence["context_samples"]
        return
    if info.get("episode_start") is not None:
        info["episode_start"] = episode.finding.evidence["maneuver"].get(
            "episode_start", info["episode_start"]
        )
    episode.first = min(episode.first, info["started_at"])
    episode.last = max(episode.last, info["ended_at"])
    episode.finding, episode.aircraft = finding, snapshot(aircraft, finding)
    episode.endpoints.add(endpoint)
    episode.add_archives(window)


def _close(window, active, aircraft, legs, segment_start, orbit=None, archive=None):
    """End open episodes as the collector does when reports stop: state "ended", reason "stale".

    The closing evaluation only updates episodes already found; its evaluation
    time is a minute after the data and never counts as a confirming endpoint.
    Episodes from detail rows end the same way when the full buffer's rules
    no longer see them. ``orbit`` is the circling verdict of the last evaluation;
    ``archive`` is the archive format (see analyze).
    """
    if not window or aircraft is None:
        return
    if not any(old.finding.evidence["maneuver"].get("state") == "active" for old in active.values()):
        return
    newest = window[-1]["ts"]
    current = max(segment_start, window[0]["ts"])
    closing = maneuvers.analyze(
        window,
        aircraft,
        orbit=orbit,
        now=newest + CLOSE_AFTER_SECONDS,
        legs=legs.before(current),
        archive=archive,
    )
    for finding in closing:
        info = finding.evidence["maneuver"]
        _, episode = _matching(active, finding, info)
        if episode is None or info["started_at"] - episode.last > 120:
            continue
        if finding.evidence.get("context_samples"):
            finding.evidence["context_samples"] = maneuvers.merge_context(
                episode.finding.evidence.get("context_samples", []), finding.evidence["context_samples"]
            )
        if info.get("episode_start") is not None:
            info["episode_start"] = episode.finding.evidence["maneuver"].get(
                "episode_start", info["episode_start"]
            )
        episode.first = min(episode.first, info["started_at"])
        episode.last = max(episode.last, info["ended_at"])
        episode.finding, episode.aircraft = finding, snapshot(aircraft, finding)
    for old in active.values():
        info = old.finding.evidence["maneuver"]
        if old.finding.kind in DETAIL_KINDS and info.get("state") == "active":
            info.update(
                state="ended",
                end_reason="stale",
                evaluated_at=max(info["evaluated_at"], newest + maneuvers.STATUS_GAP),
            )


def _ground(reading):
    """Whether a reading is on the ground; None for a same-time conflict, which cannot say."""
    if reading.get("same_time_conflict"):
        return None
    return reading.get("ground") is True or reading.get("archive_alt", reading.get("alt")) == "ground"


class _OrbitFeed:
    """One aircraft's circling verdicts from the pattern replay, held as the collector holds them.

    ``evaluations`` yields (time, verdict) in time order (replay_patterns
    analyze_tracks). An evaluation uses the latest verdict at or before its
    time; a circling verdict keeps its precedence until its window leaves the
    20-minute buffer or the encounter ends.
    """

    def __init__(self, evaluations):
        self.evaluations, self.pending = iter(evaluations), None
        self.current, self.held = False, None

    def _advance(self, stamp, *, inclusive=True):
        """Apply every evaluation before ``stamp`` (and at it when ``inclusive``)."""
        while True:
            if self.pending is None:
                self.pending = next(self.evaluations, None)
            if (
                self.pending is None
                or self.pending[0] > stamp
                or (self.pending[0] == stamp and not inclusive)
            ):
                return
            self.current, self.pending = self.pending[1], None
            if self.current:
                self.held = self.current

    def reset(self, stamp):
        """A new encounter from ``stamp``, as the collector starts one: nothing earlier holds."""
        self._advance(stamp, inclusive=False)
        self.current, self.held = False, None

    def at(self, stamp):
        """The verdict for an evaluation at ``stamp``: the latest one, else the held one, else False."""
        self._advance(stamp)
        # The collector holds a verdict while its window is in the track buffer.
        if self.held and (number(self.held.get("ended_at")) or 0) < stamp - PATTERN_WINDOW_SECONDS:
            self.held = None
        return self.current or self.held or False

    def finish(self):
        """Run the remaining evaluations; the pattern replay records its own episodes as they run."""
        self.pending = None
        for _ in self.evaluations:
            pass


def analyze(tracks, metadata, max_episodes=10000, *, horizon=None, orbits=None, archive=None):
    """Per-aircraft episodes from archived reports, evaluated as the collector does.

    Each report is evaluated with the bounded 20-minute buffer, which, as in the
    collector, spans source changes and short gaps: the rules segment it
    themselves. Earlier straight legs are passed on for survey grids. readsb
    saves integrity and selected-altitude fields only with a trace row's detail
    object, so the rules that need them also run over those rows alone. A
    minute without reports, a new leg or a same-time conflict ends the buffer,
    and open episodes then end as stale, as when a live aircraft goes silent;
    an episode found again with the same start continues, as a collector row
    does. ``horizon`` is where the analyzed data stops: a track ending within a
    minute of it may continue, so its open episodes stay open.

    ``orbits`` maps an address to the pattern replay's (time, circling verdict)
    evaluations. As in the collector, the rules then take that verdict, held
    through the encounter, instead of finding circles again; an address
    without them is evaluated as before.

    ``archive`` names the archive format ("traces" or "heatmap"). Every rule
    then applies its freshness rule (maneuvers.archived): a blocking flag, or a
    limitation for shapes the freshness cannot change. Without one, the only
    freshness flag is the one telemetry.quality() adds to findings built from
    archived points.
    """
    from .replay_patterns import ReplayEpisode

    episodes, anomalies, last_aircraft = [], [], {}
    pending = dict(orbits or {})

    def add(episode):
        if len(episodes) >= max_episodes:
            raise OverflowError("Candidate episodes exceed max_episodes")
        episodes.append(episode)
        return episode

    for address, raw_points in tracks.items():
        known = identity(address, metadata.get(address, {}))
        feed = _OrbitFeed(pending.pop(address)) if address in pending else None
        window, details, active, legs = deque(maxlen=LIMIT), deque(maxlen=LIMIT), {}, _Legs()
        aircraft, category, previous, buffer_start, segment_start = None, None, None, None, None
        orbit, last_seen, last_ground = None, None, None
        for stamp, raw in sorted(readings_by_time(raw_points).items()):
            ground = _ground(raw)
            # The collector's encounter ends here (selection.encounter_start); episodes the
            # previous one leaves open close first, with its last circling verdict.
            encounter = feed is not None and bool(
                raw.get("new_leg")
                or (last_seen is not None and stamp - last_seen > ENCOUNTER_GAP)
                or (None not in (ground, last_ground) and ground != last_ground)
            )
            last_seen, last_ground = stamp, last_ground if ground is None else ground
            if raw.get("same_time_conflict"):
                legs.refresh(window, stamp, buffer_start, force=True)
                _close(window, active, aircraft, legs, segment_start, orbit, archive)
                if encounter:
                    feed.reset(stamp)
                if feed is not None:
                    orbit = feed.at(stamp)
                window.clear()
                details.clear()
                active.clear()
                for lead in conflict_leads(raw["conflicts"], stamp, archive):
                    add(ReplayEpisode(address, lead, snapshot(known, lead), stamp, stamp, {stamp}))
                continue
            point = {
                # A new leg is a hard break before this fix; the fix itself is kept, as
                # telemetry.recent() and positions() keep it.
                **raw,
                "archive": raw.get("archive") or "retained archive",
                "source": raw.get("source", "unknown"),
                "ground": ground,
            }
            gap = stamp - window[-1]["ts"] if window else None
            split = bool(window) and bool(
                raw.get("new_leg")
                or gap > POSITION_GAP_SECONDS
                or point["source"] != window[-1]["source"]
                or window[-1]["ground"]
            )
            if split:
                # The buffer's latest position segment ends here; keep its straight legs.
                legs.refresh(window, stamp, buffer_start, force=True)
            if raw.get("new_leg") or (window and gap > SILENT_SECONDS):
                _close(window, active, aircraft, legs, segment_start, orbit, archive)
                window.clear()
                details.clear()
                if raw.get("new_leg"):
                    active.clear()
            if raw.get("new_leg") or (previous is not None and stamp - previous > LEG_RESET_SECONDS):
                legs.clear()
            if encounter:
                feed.reset(stamp)
            previous = stamp
            active = {key: old for key, old in active.items() if old.last >= stamp - LEG_RESET_SECONDS}
            if window:
                for tag in maneuvers.anomaly_tags(window[-1], point):
                    anomalies.append((address, stamp, point["source"], tag))
            else:
                buffer_start = stamp
            if not window or split:
                segment_start = stamp
            bounded_append(window, point)
            legs.refresh(window, stamp, buffer_start)
            if isinstance(point.get("category"), str):
                # An emitter category is installed configuration, not a timed reading.
                category = point["category"]
            aircraft = {
                **known,
                **({"category": category} if category else {}),
                **{
                    k: point[k]
                    for k in (
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
                        "category",
                        "flight",
                        "label_at",
                    )
                    if k in point
                },
                "type": point["source"],
            }
            last_aircraft[address] = {**known, "type": point["source"]}
            # The rules measure the latest position segment, which starts here at the earliest.
            current = max(segment_start, window[0]["ts"])
            orbit = feed.at(stamp) if feed is not None else None
            findings = maneuvers.analyze(
                window, aircraft, orbit=orbit, legs=legs.before(current), archive=archive
            )
            if any(key in point for key in DETAIL_FIELDS):
                bounded_append(details, point)
                rows = recent(list(details), max_gap=WINDOW)
                findings += [_provenance(found, archive) for rule in DETAIL_RULES if (found := rule(rows))]
            for finding in findings:
                _record(add, active, address, window, aircraft, finding)
        if window and (horizon is None or window[-1]["ts"] + CLOSE_AFTER_SECONDS <= horizon):
            _close(window, active, aircraft, legs, segment_start, orbit, archive)
        if feed is not None:
            feed.finish()
    for evaluations in pending.values():
        # Aircraft without reports here still finish their pattern evaluations.
        _OrbitFeed(evaluations).finish()
    # Correlate distinct addresses within one source and sixty seconds, keeping
    # isolated anomalies and overlapping archive copies from creating clusters.
    # Like the collector's poll, each moment is evaluated once, after all its rows.
    recent_rows, emitted = deque(), set()
    ordered = sorted(set(anomalies), key=lambda row: (row[1], row))
    for now, rows in itertools.groupby(ordered, key=lambda row: row[1]):
        recent_rows.extend(rows)
        while recent_rows and recent_rows[0][1] < now - 60:
            recent_rows.popleft()
        for source, tag, addresses in maneuvers.correlated(recent_rows, now):
            for address in addresses:
                key = (address, source, tag, int(now // 60))
                if key in emitted:
                    continue
                emitted.add(key)
                # Replayed reports are archived even when no format is named.
                finding = maneuvers.correlation_finding(source, tag, addresses, archive=archive or True)
                add(
                    ReplayEpisode(
                        address,
                        finding,
                        snapshot(last_aircraft[address], finding),
                        min(addresses.values()),
                        now,
                        {now},
                    )
                )
    return episodes


# Multi-aircraft detectors over one day's saved tracks. The collector runs the same
# pure helpers on its live buffers; here they run over saved fixes.


def formation_track(points, compact=False):
    """Movement fixes as the pairing rule reads them; conflicting positions are left out.

    A compact archive altitude has no retained reference. Pairing compares it
    between the two aircraft, so it is passed as their common altitude, and the
    evidence keeps its archive name.
    """
    track = []
    for point in points:
        if point.get("conflicting_position"):
            continue
        fix = {key: value for key, value in point.items() if key != "variants"}
        if compact:
            altitude = point.get("archive_alt")
            if altitude == "ground":
                fix["ground"] = True
            elif number(altitude) is not None:
                fix["alt_baro"] = altitude
        track.append(fix)
    return track


def _pairable(point, compact=False):
    """A fix that could take part in a formation pair: direct, airborne and high enough."""
    if point.get("conflicting_position") or point.get("source") not in DIRECT or point.get("ground"):
        return False
    lat, lon = number(point.get("lat")), number(point.get("lon"))
    keys = ("archive_alt",) if compact else ("alt_baro", "alt_geom")
    heights = [v for k in keys if (v := number(point.get(k))) is not None]
    return lat is not None and lon is not None and bool(heights) and max(heights) >= FORMATION_FLOOR_FT


def close_pairs(tracks, compact=False):
    """Times at which two pairable fixes came within 0.5 NM and 5 s of each other.

    Every pair the formation rule can accept has such fixes inside its window, so
    the rule only needs evaluating where they occur. Fixes are bucketed by 5 s
    and swept by latitude.
    """
    buckets = defaultdict(list)
    for address, points in tracks.items():
        for point in points:
            if _pairable(point, compact):
                buckets[math.floor(point["ts"] / FORMATION_CLOSE_SECONDS)].append(
                    (number(point["lat"]), number(point["lon"]), point["ts"], address)
                )
    close = defaultdict(set)
    reach = FORMATION_CLOSE_NM / 60 + 1e-9
    for bucket in sorted(buckets):
        items = sorted(buckets[bucket] + buckets.get(bucket + 1, []))
        for index, (lat, lon, stamp, address) in enumerate(items):
            for other_lat, other_lon, other_stamp, other in items[index + 1 :]:
                if other_lat - lat > reach:
                    break
                if (
                    other == address
                    or abs(other_stamp - stamp) > FORMATION_CLOSE_SECONDS
                    or distance_nm((lat, lon), (other_lat, other_lon)) > FORMATION_CLOSE_NM
                ):
                    continue
                pair = (address, other) if address < other else (other, address)
                close[pair].update((stamp, other_stamp))
    return close


def _window(track, times, stamp):
    """The saved fixes of the collector's 20-minute buffer ending at ``stamp``."""
    return track[bisect.bisect_left(times, stamp - WINDOW) : bisect.bisect_right(times, stamp)]


def formation_results(tracks, metadata, start, end, *, compact=False, archive=None):
    """(evaluation time, primary, finding) for formation pairing every 60 s of the day.

    Only aircraft with a close fix inside a window are passed to the rule. A
    pair needs such fixes, and pairs never depend on other aircraft, so the
    results equal an evaluation of every aircraft at each time. ``archive`` is
    the archive format (see analyze).
    """
    close = close_pairs(tracks, compact)
    # Only aircraft with a close fix are prepared for the rule.
    usable = {
        address: formation_track(tracks[address], compact)
        for address in sorted({a for p in close for a in p})
    }
    times = {address: [p["ts"] for p in track] for address, track in usable.items()}
    due = defaultdict(set)
    first = math.ceil(start / FORMATION_CADENCE_SECONDS) * FORMATION_CADENCE_SECONDS
    for pair, stamps in close.items():
        for stamp in stamps:
            # Evaluations whose buffer holds this fix, before the end of the day.
            low = max(first, math.ceil(stamp / FORMATION_CADENCE_SECONDS) * FORMATION_CADENCE_SECONDS)
            high = min(math.ceil(end), math.floor(stamp + WINDOW) + 1)
            for evaluated in range(int(low), int(high), FORMATION_CADENCE_SECONDS):
                due[evaluated].update(pair)
    meta = {address: {"hex": address, **metadata.get(address, {})} for address in usable}
    results = []
    for evaluated in sorted(due):
        history = {
            address: window
            for address in sorted(due[evaluated])
            if (window := _window(usable[address], times[address], evaluated))
        }
        pairs = maneuvers.formation_pairs(history, meta)
        for primary, finding in maneuvers.formation_findings(pairs, meta, archive=archive):
            if compact:
                for sample in finding.evidence.get("maneuver_samples", []):
                    # The pairing altitude was the archive altitude, whose reference is unknown.
                    if "archive_alt" in sample:
                        sample.pop("alt_baro", None)
            results.append((evaluated, primary, finding))
    return results


# An orbit's height: a reported altitude, else the pattern sample's or archive's altitude.
HEIGHT_FIELDS = ("alt_baro", "alt_geom", "alt", "archive_alt")


def _orbit_input(episode, metadata):
    """A circling or holding episode as the holding-stack helper reads it, or None.

    The centre is the mean of the episode's saved positions; it is an input to
    grouping only and is never stored. An orbit whose geometry or sources need
    review joins no stack, and one seen by several sources is "mixed".
    """
    evidence = episode.finding.evidence or {}
    pattern, maneuver = evidence.get("pattern") or {}, evidence.get("maneuver") or {}
    if pattern.get("quality_review_required") or set(maneuver.get("quality_flags", ())) - {
        maneuvers.ARCHIVE_FRESHNESS
    }:
        return None
    samples = evidence.get("pattern_samples") or evidence.get("maneuver_samples") or []
    located = [
        (number(s.get("lat")), number(s.get("lon")))
        for s in samples
        if number(s.get("lat")) is not None and number(s.get("lon")) is not None
    ]
    heights = [height for s in samples if (height := maneuvers.altitude(s, HEIGHT_FIELDS)) is not None]
    if not located or not heights:
        return None
    sources = set(pattern.get("observed_sources") or (s.get("source", "unknown") for s in samples))
    lookup = metadata.get(episode.address, {})
    return {
        "hex": episode.address,
        "started_at": episode.first,
        "ended_at": episode.last,
        "lat": statistics.fmean(lat for lat, _ in located),
        "lon": statistics.fmean(lon for _, lon in located),
        "altitude_ft": statistics.median(heights),
        # As the collector's holding inputs: a member seen by several sources is "mixed".
        "source": next(iter(sources)) if len(sources) == 1 else "mixed",
        **{key: lookup[key] for key in ("r", "t") if isinstance(lookup.get(key), str)},
        **({"flight": episode.aircraft["flight"]} if isinstance(episode.aircraft.get("flight"), str) else {}),
    }


def stack_results(episodes, metadata, archive=None):
    """(evaluation time, primary, finding) for stacked orbits, evaluated as each orbit starts."""
    inputs = [
        item
        for episode in episodes
        if episode.finding.kind in ORBIT_KINDS and (item := _orbit_input(episode, metadata))
    ]
    results = []
    for evaluated in sorted({item["started_at"] for item in inputs}):
        current = [item for item in inputs if item["started_at"] <= evaluated <= item["ended_at"]]
        if len({item["hex"] for item in current}) < 3:
            continue
        results.extend(
            (evaluated, primary, finding)
            for primary, finding in maneuvers.holding_stacks(current, archive=archive)
        )
    return results


def cluster_results(episodes, metadata, archive=None):
    """(evaluation time, primary, finding) over 15 minutes of integrity losses, as the collector keeps."""
    members = sorted(
        (
            maneuvers.gnss_member(
                episode.address,
                episode.finding,
                {
                    **metadata.get(episode.address, {}),
                    **({"flight": episode.aircraft["flight"]} if "flight" in episode.aircraft else {}),
                },
            )
            for episode in episodes
            if episode.finding.kind == "gnss_integrity_loss"
        ),
        key=lambda member: (member["onset_at"], member["hex"]),
    )
    results = []
    for member in members:
        evaluated = member["onset_at"]
        current = [m for m in members if evaluated - GNSS_MEMORY_SECONDS <= m["onset_at"] <= evaluated]
        if len({m["hex"] for m in current}) < 3:
            continue
        results.extend(
            (evaluated, primary, finding)
            for primary, finding in maneuvers.gnss_clusters(current, archive=archive)
        )
    return results


def merge_groups(results):
    """One phenomenon per kind and aircraft group, across successive evaluations.

    Evaluations merge while one aircraft set contains the other, as when a
    formation gains or loses a member; a different membership is a new
    phenomenon, so no member drops out unrecorded. The finding with the most
    aircraft (then the latest) is kept; the interval spans every evaluation, and
    ``episode_start`` is the earliest start.
    """
    merged = []
    for evaluated, primary, finding in sorted(results, key=lambda r: (r[0], r[2].kind, r[1])):
        info = finding.evidence["maneuver"]
        hexes = {primary, *(p["hex"] for p in finding.evidence.get("participants", []))}
        group = next(
            (
                g
                for g in reversed(merged)
                if g["kind"] == finding.kind
                and (g["hexes"] <= hexes or hexes <= g["hexes"])
                and info["started_at"] - g["last"] <= GROUP_MERGE_SECONDS
                and g["first"] - info["ended_at"] <= GROUP_MERGE_SECONDS
            ),
            None,
        )
        if group is None:
            merged.append(
                {
                    "kind": finding.kind,
                    "hexes": hexes,
                    "first": info["started_at"],
                    "last": info["ended_at"],
                    "start": info.get("episode_start", info["started_at"]),
                    "endpoints": {evaluated},
                    "primary": primary,
                    "finding": finding,
                }
            )
            continue
        group["hexes"] = group["hexes"] | hexes
        group["first"] = min(group["first"], info["started_at"])
        group["last"] = max(group["last"], info["ended_at"])
        group["start"] = min(group["start"], info.get("episode_start", info["started_at"]))
        group["endpoints"].add(evaluated)
        kept = group["finding"].evidence["maneuver"]
        if (info.get("participant_count", 0), info["ended_at"]) >= (
            kept.get("participant_count", 0),
            kept["ended_at"],
        ):
            group.update(primary=primary, finding=finding)
    for group in merged:
        group["finding"].evidence["maneuver"]["episode_start"] = group["start"]
    return merged


def group_episodes(tracks, episodes, metadata, start, end, *, archive=None):
    """Formation, holding-stack and GNSS-cluster episodes evaluated within [start, end).

    ``tracks`` are one format's movement fixes; ``episodes`` are that format's
    per-aircraft replay episodes, which feed the stack and cluster helpers.
    ``archive`` is that format, "traces" or "heatmap" (compact).
    """
    from .replay_patterns import ReplayEpisode

    results = [
        *formation_results(tracks, metadata, start, end, compact=archive == "heatmap", archive=archive),
        *stack_results(episodes, metadata, archive),
        *cluster_results(episodes, metadata, archive),
    ]
    found = []
    for group in merge_groups([r for r in results if start <= r[0] < end]):
        finding, primary = group["finding"], group["primary"]
        known = identity(primary, metadata.get(primary, {}))
        info = finding.evidence["maneuver"]
        aircraft = track_snapshot(
            known, finding, tracks.get(primary, []), info.get("peak_at", info["ended_at"])
        )
        found.append(
            ReplayEpisode(primary, finding, aircraft, group["first"], group["last"], group["endpoints"])
        )
    return found


# TCAS resolution advisories from readsb's ACAS exports, joined to both tracks.


def advisory_episodes(reports):
    """(own address, episode) for each aircraft's advisory messages, grouped as
    tcas_episode groups them (maneuvers.advisory_runs)."""
    by_own = defaultdict(list)
    for own, report in reports:
        by_own[own].append(report)
    for own in sorted(by_own):
        for episode in maneuvers.advisory_runs(by_own[own]):
            yield own, episode


def best_track(tracks, address, first, last):
    """Trace fixes when retained (they carry reported rates), otherwise compact fixes."""
    for source in ("traces", "heatmap"):
        points = [
            {key: value for key, value in p.items() if key != "variants"}
            for p in tracks.get(source, {}).get(address, [])
            if first <= p["ts"] <= last and not p.get("conflicting_position")
        ]
        if points:
            return source, points
    return None, []


def _altitude_rates(points, field, stamp):
    """ft/min from ``field`` altitudes 6-30 s apart that end at, start at or span ``stamp``."""
    low, high = RATE_SPAN_SECONDS
    heights = sorted(
        (p["ts"], value)
        for p in points
        if abs(p["ts"] - stamp) <= high and (value := number(p.get(field))) is not None
    )
    before = [h for h in heights if h[0] <= stamp]
    after = [h for h in heights if h[0] >= stamp]
    pairs = [
        # The nearest fixes at least 3 s either side of the rate.
        (
            next((h for h in reversed(before) if h[0] <= stamp - low / 2), None),
            next((h for h in after if h[0] >= stamp + low / 2), None),
        )
    ]
    if before:
        pairs.append((next((h for h in reversed(before) if h[0] <= before[-1][0] - low), None), before[-1]))
    if after:
        pairs.append((after[0], next((h for h in after if h[0] >= after[0][0] + low), None)))
    return [
        (late[1] - early[1]) * 60 / (late[0] - early[0])
        for early, late in pairs
        if early is not None and late is not None and low <= late[0] - early[0] <= high
    ]


def _rates_agree(points, first):
    """Whether both rates the response rule read agree with the altitude change around them.

    Those are the latest saved rate in the 30 s before the first active message
    and the latest in the 20 s after it. A stale archived rate can look like
    the advised response; one the saved altitudes cannot check is unverified.
    """
    rated = [
        p for p in points if any(number(p.get(field)) is not None for field in ("baro_rate", "geom_rate"))
    ]
    samples = [
        side[-1]
        for side in (
            [p for p in rated if first - RESPONSE_WINDOW[0] <= p["ts"] <= first],
            [p for p in rated if first < p["ts"] <= first + RESPONSE_WINDOW[1]],
        )
        if side
    ]
    if len(samples) < 2:
        return False
    for point in samples:
        for field, altitudes in (
            ("baro_rate", ("alt_baro", "alt_geom")),
            ("geom_rate", ("alt_geom", "alt_baro")),
        ):
            if (rate := number(point.get(field))) is None:
                continue
            derived = next(
                (rates for name in altitudes if (rates := _altitude_rates(points, name, point["ts"]))), []
            )
            if not any(abs(rate - value) <= max(RATE_AGREEMENT_FPM, abs(rate) / 2) for value in derived):
                return False
    return True


def _archived_response(finding, own_points, first):
    """An archived advisory keeps its measured response only when the altitude change agrees.

    Otherwise the rate change is not stated and, unless the advisory asked for
    no change, no_advised_response blocks the finding as a missing response does.
    """
    info = finding.evidence["maneuver"]
    if "response_rate_change_fpm" not in info or _rates_agree(own_points, first):
        return
    info.pop("response_rate_change_fpm")
    if advisory_sense(info.get("advisory")) not in {"maintain", "preventive"}:
        maneuvers._flag(finding, "no_advised_response")


def _without_separation(finding):
    """Compact fixes are interpolated across up to 30 s: too coarse for a stated separation."""
    for measurements in (
        finding.evidence["maneuver"],
        *(item.get("measurements", {}) for item in finding.evidence.get("participants", [])),
    ):
        for key in SEPARATION_KEYS:
            measurements.pop(key, None)


def tcas_episodes(reports, tracks, metadata, start, end):
    """(source, episode) for advisories whose first active message is in [start, end).

    ``reports`` are (own address, allowlisted acas_ra) pairs; ``tracks`` maps each
    archive format to its prepared tracks. The episode's source is the format of
    the own aircraft's track, or "acas" when none was retained. A response is
    kept only where the saved rates agree with the altitude change, and no
    separation is stated from compact fixes.
    """
    from .replay_patterns import ReplayEpisode

    found = []
    for own, messages in advisory_episodes(reports):
        active = [m for m in messages if m.get("RAT") == 0]
        if not active or not start <= active[0]["unix_timestamp"] < end:
            continue
        first, last = active[0]["unix_timestamp"], messages[-1]["unix_timestamp"]
        window = (first - TCAS_MARGIN_SECONDS, last + TCAS_MARGIN_SECONDS)
        source, own_points = best_track(tracks, own, *window)
        threat_hex = maneuvers.advisory_threat(messages, own)
        threat_source, threat_points = best_track(tracks, threat_hex, *window) if threat_hex else (None, [])
        known = identity(own, metadata.get(own, {}))
        threat = None
        if threat_hex:
            threat = {**identity(threat_hex, metadata.get(threat_hex, {}))}
            if threat_points:
                threat["type"] = threat_points[-1].get("source", "unknown")
                labelled = [p for p in threat_points if isinstance(p.get("flight"), str)]
                if labelled:
                    threat["flight"] = labelled[-1]["flight"]
        finding = maneuvers.tcas_episode(
            messages,
            own_points,
            threat_points,
            own={**known, "type": own_points[-1].get("source", "unknown") if own_points else "unknown"},
            threat=threat,
            # The own aircraft's saved track format; without one nothing archived was measured.
            archive=source,
        )
        if finding is None:
            continue
        _archived_response(finding, own_points, first)
        if "heatmap" in (source, threat_source):
            _without_separation(finding)
        info = finding.evidence["maneuver"]
        # Readings at the time the evidence is saved for, as for every other episode.
        aircraft = track_snapshot(known, finding, own_points, info["ended_at"], TCAS_SNAPSHOT_SECONDS)
        confirming = {m["unix_timestamp"] for m in active}
        found.append(
            (
                source or "acas",
                ReplayEpisode(own, finding, aircraft, info["started_at"], info["ended_at"], confirming),
            )
        )
    return found
