"""Bounded historical pattern analysis and an explicit, idempotent journal import.

Analysis is read-only. Applying a completed report changes only pattern events
and its fingerprint ledger; it does not replay aircraft counts or coverage.
"""

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import airport_context, episode_replay, maneuvers, rules
from .episode_replay import CARRIED_LABEL, add_context_tags, carried_label, same_position
from .history import ARCHIVE_ERRORS, error_type
from .rules import PATTERN_MAX_GAP_SECONDS, Finding, movement_patterns, number
from .telemetry import LIMIT, bounded_append

# v5/v4: episode rules take the pattern replay's circling verdict, held as the collector
# holds it; archive advisories check their response against the altitude change.
# v5/v3: stale endings, survey legs, multi-aircraft and TCAS episodes, identity and
# watchlist evidence. v4/v2: trace gap flags, new legs, merged MLAT repeats and label times.
DETECTOR_VERSION = "position-patterns-v5-episodes-v4"
TRACK_LIMIT = LIMIT
REPLAY_LIMITATIONS = [
    "Replay timestamps identify archive slices, not exact position fix times or message freshness.",
    "Archive altitude may be barometric or geometric; its reference is unspecified and it is not terrain clearance.",
    "Registration, type and description are current metadata lookups and may differ from the historical identity.",
    "A movement pattern does not establish the aircraft's operator, mission or activity on the ground.",
]


def validate_addresses(addresses):
    result = {str(address).lower() for address in addresses}
    if not result or len(result) > 64 or any(not re.fullmatch(r"[0-9a-f]{6}", address) for address in result):
        raise ValueError("Select 1–64 ordinary six-digit aircraft addresses with --hex")
    return result


def select_files(root, since, until, max_files=2000):
    if not isinstance(since, date) or not isinstance(until, date) or not 1 <= (until - since).days <= 366:
        raise ValueError("Select an explicit UTC interval of 1–366 days")
    if not 1 <= max_files <= 6000:
        raise ValueError("max_files must be in 1–6000")
    root = Path(root)
    if not root.is_dir():
        raise ValueError("Replay root does not exist or is not a directory")
    paths = []
    day = since
    while day < until:
        paths.extend(sorted((root / day.strftime("%Y/%m/%d") / "heatmap").glob("*.bin.ttf")))
        if len(paths) > max_files:
            raise ValueError("Selected files exceed max_files; narrow the date interval")
        day += timedelta(days=1)
    return paths


def collect_tracks(files, root, addresses, since, until, decoder, max_points=250000):
    """Reuse the audit's bounded decoder; retain only explicitly selected IDs.

    `decoder(path)` yields (hex, point). The existing decoder limits every
    decompressed gzip file to 64 MiB before making any NumPy arrays.
    """
    addresses = validate_addresses(addresses)
    if not 1 <= max_points <= 1000000:
        raise ValueError("max_points must be in 1–1000000")
    start = datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc).timestamp()
    end = datetime.combine(until, datetime.min.time(), tzinfo=timezone.utc).timestamp()
    tracks, errors, count = defaultdict(list), [], 0
    for path in files:
        relative = str(Path(path).relative_to(root))
        try:
            for address, raw in decoder(path):
                if address not in addresses:
                    continue
                stamp = number(raw.get("ts"))
                if stamp is None or not start <= stamp < end:
                    continue
                count += 1
                if count > max_points:
                    raise OverflowError("Selected positions exceed max_points; narrow the filters")
                # The reader's generic `alt` is not a barometric-altitude claim.
                resolution = number(raw.get("timestamp_resolution_seconds"))
                point = {
                    **raw,
                    "archive_alt": "ground" if raw.get("ground") else raw.get("alt"),
                    "timestamp_resolution_seconds": resolution if resolution and resolution > 0 else None,
                    "archive": relative,
                }
                tracks[address].append(point)
        except ARCHIVE_ERRORS as error:
            errors.append({"archive": relative, "error_type": error_type(error), "error": str(error)})
    return dict(tracks), {
        "files": len(files),
        "selected_positions": count,
        "aircraft_with_positions": len(tracks),
        "unreadable_files": errors,
    }


@dataclass
class ReplayEpisode:
    address: str
    finding: Finding
    aircraft: dict
    first: float
    last: float
    endpoints: set = field(default_factory=set)
    archives: set = field(default_factory=set)
    # The newest window fix whose archive is already in ``archives``.
    archived_through: float = field(default=-math.inf, repr=False, compare=False)

    def add_archives(self, window):
        """Add the archives of a chronological window's fixes not already added.

        Successive windows of one episode overlap, so only fixes newer than the
        last window's newest one can add an archive.
        """
        for point in reversed(window):
            if point["ts"] <= self.archived_through:
                break
            if point.get("archive"):
                self.archives.add(point["archive"])
        if window:
            self.archived_through = max(self.archived_through, window[-1]["ts"])

    def to_dict(self):
        return {
            "hex": self.address,
            "finding": self.finding.to_dict(),
            "aircraft": self.aircraft,
            "first_seen": self.first,
            "last_seen": self.last,
            "confirming_endpoints": sorted(self.endpoints),
            "archives": sorted(self.archives),
        }


def _aircraft(address, point, metadata):
    lookup = metadata.get(address, {})
    return {
        "hex": address,
        "r": lookup.get("registration", lookup.get("r", "")),
        "t": lookup.get("typecode", lookup.get("t", "")),
        "desc": lookup.get("description", lookup.get("desc", "")),
        "lat": point["lat"],
        "lon": point["lon"],
        "gs": point.get("gs"),
        "archive_alt": point.get("archive_alt", point.get("alt")),
        "type": point.get("source", "unknown"),
        **{key: point[key] for key in ("flight", "squawk") if key in point},
        **(
            {"label_at": point["label_at"]}
            if "label_at" in point and ("flight" in point or "squawk" in point)
            else {}
        ),
    }


def analyze_tracks(
    tracks,
    metadata=None,
    metadata_as_of=None,
    metadata_source="current journal aircraft table",
    max_episodes=2000,
    detector=movement_patterns,
    include_episodes=False,
    episode_tracks=None,
    episode_horizon=None,
    archive=None,
):
    """Evaluate chronological positions with a fixed-size 20-minute buffer.

    Sorting is O(N log N); each evaluation sees at most 2,401 points. A whole
    flight is never rescanned for every new point. Repeated timestamps cannot
    manufacture the two distinct qualifying endpoints required for confirmation.
    A trace gap flag is not a break: the 90-s movement gap decides continuity.
    A new leg breaks before its first fix. Same-time fixes more than 0.01 NM
    apart reset the window at that time; reading disagreements at one position
    are left to the scalar and episode rules. ``episode_horizon`` is where the
    analyzed data stops (see episode_replay.analyze). As in the collector, the
    episode rules take each evaluation's circling verdict instead of finding
    circles again. Unlike the collector, the pattern rules here get no held
    verdict, so a held circle does not suppress a loitering pattern.
    ``archive`` names the tracks' archive format ("traces" or "heatmap") for the
    episode rules' freshness rule (see episode_replay.analyze).
    """
    if not 1 <= max_episodes <= 10000:
        raise ValueError("max_episodes must be in 1–10000")
    metadata = metadata or {}
    metadata_as_of = metadata_as_of or datetime.now(timezone.utc).isoformat()
    episodes, verdicts = [], {}
    for address, input_points in tracks.items():
        validate_addresses({address})
        evaluations = _evaluations(
            address, input_points, metadata, detector, episodes, max_episodes, verdicts=include_episodes
        )
        if include_episodes:
            # The episode replay advances this aircraft's evaluations alongside its reports.
            verdicts[address] = evaluations
        else:
            deque(evaluations, maxlen=0)
    if include_episodes:
        episodes.extend(
            episode_replay.analyze(
                tracks if episode_tracks is None else episode_tracks,
                metadata,
                max_episodes=max_episodes,
                horizon=episode_horizon,
                orbits=verdicts,
                archive=archive,
            )
        )
        if len(episodes) > max_episodes:
            raise OverflowError("Candidate episodes exceed max_episodes")
    for episode in episodes:
        annotate_episode(episode, metadata_as_of, metadata_source)
    return episodes


def _verdict(findings):
    """One evaluation's circling verdict (rules.orbit_verdict), or False, as the collector takes it."""
    try:
        return rules.orbit_verdict(findings)
    except KeyError, TypeError, AttributeError:
        # A pattern without its saved samples cannot drive episode precedence.
        return False


def _evaluations(address, input_points, metadata, detector, episodes, max_episodes, *, verdicts=False):
    """Evaluate one aircraft's positions, adding its confirmed pattern episodes to ``episodes``.

    Yields (time, circling verdict) once per position time, in time order. The
    verdict is False where nothing was evaluated (a reset, or fewer than ten
    positions) and is only computed when ``verdicts`` is set.
    """
    window, pending, active = deque(maxlen=TRACK_LIMIT), {}, {}
    previous = None
    # Reject conflicting same-time positions rather than choosing one;
    # near-identical MLAT repeats of one fix are a single position.
    by_stamp = {}
    for point in input_points:
        stamp = number(point.get("ts"))
        if stamp is None:
            continue
        existing = by_stamp.get(stamp, point)
        if point.get("conflicting_position") or existing is None or not same_position(existing, point):
            by_stamp[stamp] = None
        elif point.get("new_leg") and not existing.get("new_leg"):
            by_stamp[stamp] = {**existing, "new_leg": True}
        else:
            by_stamp[stamp] = existing
    for stamp in sorted(by_stamp):
        point = by_stamp[stamp]
        if (
            point is None
            or point.get("ground")
            or point.get("archive_alt", point.get("alt")) == "ground"
            or number(point.get("lat")) is None
            or number(point.get("lon")) is None
        ):
            window.clear()
            pending.clear()
            active.clear()
            previous = None
            yield stamp, False
            continue
        if point.get("new_leg") or (previous is not None and stamp - previous > PATTERN_MAX_GAP_SECONDS):
            window.clear()
            pending.clear()
            active.clear()
        previous = stamp
        sample = {**point, "alt": point.get("archive_alt", point.get("alt"))}
        bounded_append(window, sample)
        findings = detector(list(window)) if len(window) >= 10 else []
        # Taken before the episode bookkeeping below annotates the findings.
        verdict = _verdict(findings) if verdicts and findings else False
        found = {finding.kind for finding in findings}
        pending = {kind: value for kind, value in pending.items() if kind in found}
        for finding in findings:
            pattern = (finding.evidence or {}).get("pattern", {})
            endpoint = number(pattern.get("ended_at"))
            first = number(pattern.get("started_at"))
            if endpoint is None or first is None or endpoint > stamp or endpoint < first:
                continue
            confirmation = pending.get(finding.kind)
            if confirmation is None or endpoint - confirmation["last"] > 120:
                confirmation = {"first": endpoint, "last": endpoint, "endpoints": set()}
            if endpoint in confirmation["endpoints"]:
                continue
            confirmation["endpoints"].add(endpoint)
            confirmation["last"] = endpoint
            pending[finding.kind] = confirmation
            if len(confirmation["endpoints"]) < 2 or endpoint - confirmation["first"] < 8:
                continue
            episode = active.get(finding.kind)
            if episode is None or endpoint - episode.last > 120:
                if len(episodes) >= max_episodes:
                    raise OverflowError("Candidate episodes exceed max_episodes; narrow the filters")
                episode = ReplayEpisode(
                    address, finding, _aircraft(address, point, metadata), first, endpoint
                )
                episodes.append(episode)
                active[finding.kind] = episode
            episode.first, episode.last = min(episode.first, first), max(episode.last, endpoint)
            episode.endpoints.update(confirmation["endpoints"])
            episode.add_archives(window)
            if (
                finding.value is None
                or episode.finding.value is None
                or abs(finding.value) >= abs(episode.finding.value)
            ):
                episode.finding, episode.aircraft = finding, _aircraft(address, point, metadata)
                if context := airport_context.pattern_context(finding, episode.aircraft):
                    finding.evidence["airport_context"] = context
                    # As in the collector, a runway circuit keeps its holding, oscillation
                    # and descent properties as attributes of the circling finding.
                    if finding.kind == "circling" and (
                        circuit := maneuvers.circuit_attributes(list(window), episode.aircraft, finding)
                    ):
                        add_context_tags(finding.evidence, circuit["context_tags"])
                        finding.evidence["circuit"] = circuit["attributes"]
            # Only the first qualifying endpoint is needed after confirmation;
            # avoid repeatedly copying an ever-growing set into the episode.
            confirmation["endpoints"] = {confirmation["first"], endpoint}
        yield stamp, verdict


def annotate_episode(episode, metadata_as_of, metadata_source="current journal aircraft table"):
    """Add the replay provenance, archive limitations and any carried-label tag to one episode."""
    history = {
        "detector_version": DETECTOR_VERSION,
        "timestamp_resolution_seconds": (episode.finding.evidence or {})
        .get("pattern", {})
        .get("timestamp_resolution_seconds"),
        "altitude_reference": "archive_unspecified",
        "archives": sorted(episode.archives),
        "confirming_positions": len(episode.endpoints),
        "first_confirmation_endpoint": min(episode.endpoints),
        "last_confirmation_endpoint": max(episode.endpoints),
        "metadata_lookup": {
            "as_of": metadata_as_of,
            "source": metadata_source,
            "scope": "Current lookup; not historical identity verification",
        },
        "limitations": REPLAY_LIMITATIONS,
    }
    episode.finding = replace(
        episode.finding,
        confidence="Archived positions; " + episode.finding.confidence,
        explanation=episode.finding.explanation
        + " Archive timestamps mark slices rather than exact position times, and altitude has an unspecified reference. Aircraft metadata is a current lookup, not historical verification.",
        evidence={**(episode.finding.evidence or {}), "replay_pattern_import": history},
    )
    measurement = episode.finding.evidence.get("maneuver") or episode.finding.evidence.get("pattern", {})
    if carried_label(episode.aircraft, measurement.get("peak_at", measurement.get("ended_at"))):
        add_context_tags(episode.finding.evidence, [CARRIED_LABEL])
    return episode


def endpoint_fingerprint(address, kind, endpoint):
    # Independent of filenames/date filters, so overlapping reruns stay idempotent.
    identity = json.dumps([address, kind, format(endpoint, ".6f")], separators=(",", ":"))
    return hashlib.sha256(identity.encode()).hexdigest()


def prune_fingerprints(db):
    """Discard ledger entries whose event expired, including reused row IDs."""
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='replay_pattern_fingerprints'"
    ).fetchone():
        db.execute("""DELETE FROM replay_pattern_fingerprints WHERE NOT EXISTS (
            SELECT 1 FROM events e WHERE e.id=replay_pattern_fingerprints.event_id
            AND e.hex=replay_pattern_fingerprints.hex AND e.kind=replay_pattern_fingerprints.kind
            AND e.first_seen<=replay_pattern_fingerprints.endpoint
            AND MAX(e.last_seen, COALESCE(json_extract(e.evidence,'$.maneuver.evaluated_at'), e.last_seen))>=replay_pattern_fingerprints.endpoint)""")


ARCHIVE_ORIGINS = ("Saved compact replay", "Saved compact replay patterns")


def live_row(db, address, finding, first, last):
    """A non-archive journal row that app.record() could merge this archive finding into.

    Such a merge would relabel the live row, replace its evidence and double
    count its hits, so archive imports refuse the finding instead.
    """
    from . import selection
    from .app import PATTERN_GAP

    gap = PATTERN_GAP if finding.category == "patterns" else selection.ENCOUNTER_GAP
    return db.execute(
        "SELECT id FROM events WHERE hex=? AND kind=? AND last_seen>=? AND first_seen<=? AND origin NOT IN (?,?) LIMIT 1",
        (address, finding.kind, first - gap, last + gap, *ARCHIVE_ORIGINS),
    ).fetchone()


def apply_episodes(database, episodes, *, allow_live_overlap=False):
    """Insert new endpoints or refresh evidence revalidated by a newer detector.

    An episode reaching the journal's live installation (less the receiver-age
    margin) is refused unless explicitly allowed, and an episode that could
    merge into a live row is always refused: either would relabel live rows
    and double count them.
    """
    from .app import MAX_RECEIVER_AGE, connect, meta, record

    database = Path(database)
    if not database.is_file():
        raise ValueError("The initialized journal database must already exist")
    imported, skipped, refused, live_merges, event_ids, refreshed = 0, 0, 0, 0, set(), set()
    with connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        installed_at = number(meta(db, "installed_at"))
        if installed_at is None and not allow_live_overlap:
            raise ValueError("Journal installation time is unavailable; the live boundary cannot be applied")
        boundary = None if allow_live_overlap else installed_at - MAX_RECEIVER_AGE
        db.execute("""CREATE TABLE IF NOT EXISTS replay_pattern_fingerprints (
            fingerprint TEXT PRIMARY KEY, hex TEXT NOT NULL, kind TEXT NOT NULL,
            endpoint REAL NOT NULL, event_id INTEGER NOT NULL,
            imported_at REAL NOT NULL, detector_version TEXT NOT NULL)""")
        prune_fingerprints(db)
        for episode in episodes:
            if boundary is not None and max(episode.last, *episode.endpoints) >= boundary:
                refused += 1
                continue
            if live_row(db, episode.address, episode.finding, episode.first, episode.last):
                live_merges += 1
                continue
            measurement = episode.finding.evidence.get("maneuver") or episode.finding.evidence["pattern"]
            observed_at = measurement.get("peak_at", measurement["ended_at"])
            fresh = []
            upgrades = defaultdict(list)
            for endpoint in sorted(episode.endpoints):
                fingerprint = endpoint_fingerprint(episode.address, episode.finding.kind, endpoint)
                existing = db.execute(
                    "SELECT event_id,detector_version FROM replay_pattern_fingerprints WHERE fingerprint=?",
                    (fingerprint,),
                ).fetchone()
                if existing:
                    skipped += 1
                    if existing["detector_version"] != DETECTOR_VERSION:
                        upgrades[existing["event_id"]].append(fingerprint)
                else:
                    fresh.append((fingerprint, endpoint))
            for old_id, fingerprints in upgrades.items():
                old = db.execute(
                    "SELECT evidence,value FROM events WHERE id=? AND origin IN (?,?)",
                    (old_id, *ARCHIVE_ORIGINS),
                ).fetchone()
                if not old:
                    continue
                evidence = json.loads(old["evidence"])
                current_pattern = evidence.get("pattern", {})
                if not current_pattern.get("shape_verified") or (episode.finding.value or 0) >= (
                    old["value"] or 0
                ):
                    evidence.update(episode.finding.evidence or {})
                    evidence.update(
                        aircraft=episode.aircraft,
                        observed_at=observed_at,
                        replay_refresh={
                            "detector_version": DETECTOR_VERSION,
                            "retained_legacy_hit_count": True,
                        },
                    )
                    finding = episode.finding
                    db.execute(
                        "UPDATE events SET title=?,value=?,unit=?,why=?,explanation=?,score=?,confidence=?,evidence=? WHERE id=?",
                        (
                            finding.title,
                            finding.value,
                            finding.unit,
                            finding.why,
                            finding.explanation,
                            finding.score,
                            finding.confidence,
                            json.dumps(evidence),
                            old_id,
                        ),
                    )
                    refreshed.add(old_id)
                    event_ids.add(old_id)
                db.executemany(
                    "UPDATE replay_pattern_fingerprints SET detector_version=? WHERE fingerprint=?",
                    [(DETECTOR_VERSION, fingerprint) for fingerprint in fingerprints],
                )
            if not fresh:
                if episode.finding.evidence.get("context_samples"):
                    record(
                        db,
                        episode.aircraft,
                        episode.finding,
                        episode.first,
                        episode.last,
                        hits=0,
                        origin="Saved compact replay patterns",
                        extra={"observed_at": observed_at},
                    )
                continue
            event_id = record(
                db,
                episode.aircraft,
                episode.finding,
                episode.first,
                episode.last,
                hits=len(fresh),
                origin="Saved compact replay patterns",
                extra={"observed_at": observed_at},
            )
            db.executemany(
                "INSERT INTO replay_pattern_fingerprints VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        fingerprint,
                        episode.address,
                        episode.finding.kind,
                        endpoint,
                        event_id,
                        time.time(),
                        DETECTOR_VERSION,
                    )
                    for fingerprint, endpoint in fresh
                ],
            )
            imported += len(fresh)
            event_ids.add(event_id)
    return {
        "imported_endpoints": imported,
        "skipped_existing_endpoints": skipped,
        "refused_live_overlap_episodes": refused,
        "refused_live_merge_episodes": live_merges,
        "events_written": len(event_ids),
        "events_refreshed": len(refreshed),
        "event_ids": sorted(event_ids),
    }


def private_report_directory(directory):
    """Create or validate a report directory before reading private evidence."""
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError("Use a real private directory")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = directory.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Report directory must be owned by the current user with mode 0700")
    return directory


def write_private_report(directory, value, filename="report.json"):
    """Write atomically with mode 0600 into an owned, mode-0700 directory."""
    if Path(filename).name != filename:
        raise ValueError("Use a plain report filename")
    directory = private_report_directory(directory)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=directory, prefix=".report-", delete=False) as output:
            temporary = Path(output.name)
            os.chmod(temporary, 0o600)
            json.dump(value, output, indent=2, allow_nan=False)
            output.write("\n")
        os.replace(temporary, directory / filename)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return directory / filename


def report_summary(episodes):
    return {
        "candidate_episodes": len(episodes),
        "by_kind": dict(Counter(item.finding.kind for item in episodes)),
        "aircraft_with_candidates": len({item.address for item in episodes}),
        "qualifying_endpoints": sum(len(item.endpoints) for item in episodes),
    }
