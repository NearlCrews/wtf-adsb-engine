"""Resumable, private historical corpus. This module never writes the live journal.

Host-side analysis reuses the collector's scalar, episode and multi-aircraft
rules. Inputs are frozen before analysis; raw positions and all intermediate
artifacts stay private.
"""

import bisect
import csv
import fcntl
import gzip
import hashlib
import io
import itertools
import json
import math
import os
import re
import shutil
import sqlite3
import statistics
import struct
import subprocess
import sys
import time
from collections import Counter, defaultdict, deque
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import episode_replay, maneuvers, reference, rules, telemetry, typecheck, watchlists
from .episode_evidence import octal_code
from .episode_replay import (
    BREAK_FLAGS,
    CARRIED_LABEL,
    READING_FIELDS,
    STATUS_FIELDS,
    add_context_tags,
    carried_label,
    duplicate_reading,
    same_position,
)
from .history import ARCHIVE_ERRORS, MAGIC, TYPES, error_type, label, read_replay
from .replay_patterns import DETECTOR_VERSION, analyze_tracks, annotate_episode
from .rules import (
    PATTERN_WINDOW_SECONDS,
    candidates,
    identity_checks,
    jump,
    number,
    supplementary_candidates,
)
from .telemetry import NAV_MODES, OPTIONAL_NUMBERS, RECEIVER_FIELDS, acas_report

VERSION = 1
MAX_TRACE_BYTES = 64 * 1024 * 1024
ARCHIVE_PATTERNS = ("20??/*/*/heatmap/*.bin.ttf", "20??/*/*/traces/**/*.json", "20??/*/*/acas/*")
# Every analysis input under the source directory is pinned: all modules, since the
# rules import shared helpers, plus every data table.
CODE_FOLDERS = ("data",)
PINNED_SOURCE = "analysis-source"
SOURCE_PIN = "source-pin.json"
RUNNER = "run_frozen.py"
# Cross-day first sightings and seasonal records (see cross_day_findings). They are saved
# in their own artifact, whose hash completion.json records through the summary.
# v2: the artifact, a first sighting needs two positions 8 s apart, and a seasonal record
# names the season's first covered day as its baseline.
CROSS_DAY_VERSION = "cross-day-v2"
CROSS_DAY_ARTIFACT = "cross-day.json"
CROSS_DAY_KINDS = ("first_type_here", "first_country_here", "seasonal_speed_record")
CROSS_DAY_SCOPE = {
    "version": CROSS_DAY_VERSION,
    "coverage": "Complete analysis days with at least one hour of recorded archive slices; earlier UTC days only.",
    "identity": "Types and allocation States come from frozen current metadata; historical assignments are not retained.",
}
# Like the collector's confirmation, a first sighting needs a position at least 8 s after
# the first, in a run of positions no more than 45 s apart.
FIRST_SIGHTING_SECONDS = 8
SIGHTING_GAP_SECONDS = 45
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
TRACE_EXTRA_FIELDS = (
    "flight",
    "squawk",
    "emergency",
    "category",
    "alt_geom",
    "geom_rate",
    "baro_rate",
    "tas",
    "ias",
    "mach",
    "nic",
    "nac_p",
    "track",
    "roll",
    "type",
)
# Archive labels, never readings at a position time: see label() and trace_label().
LABEL_FIELDS = ("flight", "squawk")
# Fields of a first fix kept per aircraft and day for the cross-day phase.
FIX_FIELDS = (
    "ts",
    "lat",
    "lon",
    "gs",
    "archive_alt",
    "alt_baro",
    "alt_geom",
    "source",
    "flight",
    "squawk",
    "label_at",
    "category",
    # Position quality travels with a fix, so a record's inputs keep it.
    "nic",
    "nac_p",
)
ACAS_REPORTS = (".json", ".json.gz")
ACAS_COMPANIONS = (".csv", ".csv.gz")
# Receiver-relative values (telemetry.RECEIVER_FIELDS) locate the station, so they are
# never indexed; nor is readsb's owner/operator database label. Advisory reports are
# indexed through the allowlists below; this list remains for leak audits.
UNINDEXED_FIELDS = RECEIVER_FIELDS | {"ownOp"}
# The aircraft fields of an indexed advisory report, and readsb's acas_ra fields.
ADVISORY_REPORT_FIELDS = (
    "hex",
    "type",
    "flight",
    "r",
    "t",
    "desc",
    "alt_baro",
    "alt_geom",
    "gs",
    "track",
    "baro_rate",
    "geom_rate",
    "squawk",
    "emergency",
    "category",
    "lat",
    "lon",
    "nic",
    "nac_p",
    "dbFlags",
    "year",
)
ACAS_RAW_FIELDS = (
    "utc",
    "unix_timestamp",
    "df_type",
    "bytes",
    "full_bytes",
    "ARA",
    "RAT",
    "MTE",
    "RAC",
    "advisory",
    "advisory_complement",
    "TTI",
    "threat_id_hex",
)
RA_WINDOW_SECONDS = 60
RA_RESPONSE_FPM = 500
RA_SAMPLE_LIMIT = 200
PRIVATE_LEAD_TABLES = (
    ("advisory_leads", "experimental_advisory_leads"),
    ("recurrence_leads", "private_recurrence_leads"),
)
RECURRENCE_WARMUP_DAYS = 30
RECURRENCE_ABSENCE_DAYS = 30
# A day counts as coverage only with at least an hour of recorded archive slices.
COVERAGE_SECONDS = rules.COVERAGE_SECONDS
# One sighting says nothing about recurrence; type novelty has its own lead.
RECURRENCE_MIN_RETAINED_DAYS = 2
RECURRENCE_LIMITATIONS = [
    "First in the retained archive since its first analyzed day; not a first-ever sighting.",
    "Missing, partially analyzed or briefly recorded archive days are gaps, not observations of absence.",
    "Type and registration are current metadata, not historical identity verification.",
    "Presence and return patterns describe an aircraft's movements over time; they stay private.",
]
# A sustained speed is the slowest of at least four consecutive direct reports over 60 s.
SUSTAINED_SECONDS = rules.SEASON_SUSTAINED_SECONDS
SUSTAINED_SAMPLES = rules.SEASON_MIN_SAMPLES
SUSTAINED_GAP_SECONDS = rules.SEASON_SAMPLE_GAP_SECONDS
# A last frozen day is complete once readsb has written its 48 compact slices,
# compressed its advisory export and stopped rewriting its traces.
THROUGH_SLICES = 48
THROUGH_SETTLED_SECONDS = 1800
THROUGH_REQUIREMENTS = ("capture_after_cutoff", "all_slices_present", "acas_compressed", "traces_settled")
# Without a last day, a compact archive whose newest slice ended within this long before the
# capture began is still being written: analysis stops at that slice (see last_slice_end).
ACTIVE_SLICE_SECONDS = 3600
WATCHLIST_SOURCE = "plane-alert-db.csv"


def encoded(value):
    return json.dumps(value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def utc(stamp=None):
    return datetime.fromtimestamp(time.time() if stamp is None else stamp, timezone.utc).isoformat()


def day_stamp(day):
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()


def private_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("Private output directory cannot be a symbolic link")
    os.chmod(path, 0o700)
    return path


def write_bytes_private(path, data):
    path = Path(path)
    private_directory(path.parent)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        os.chmod(temporary, 0o600)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_private(path, value):
    write_bytes_private(path, (encoded(value) + "\n").encode())


@contextmanager
def connect(path):
    database = sqlite3.connect(path, timeout=30)
    try:
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("PRAGMA synchronous=FULL")
        with database:
            yield database
    finally:
        database.close()


def initialize(destination):
    destination = private_directory(destination)
    path = destination / "corpus.sqlite3"
    with connect(path) as database:
        database.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata(hex TEXT PRIMARY KEY,content TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(day TEXT PRIMARY KEY,status TEXT NOT NULL,completed_at TEXT,content TEXT);
            CREATE TABLE IF NOT EXISTS findings(
              id TEXT PRIMARY KEY,day TEXT NOT NULL,source TEXT NOT NULL,hex TEXT NOT NULL,
              kind TEXT NOT NULL,first_seen REAL NOT NULL,last_seen REAL NOT NULL,
              score INTEGER NOT NULL,content TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS finding_identity_time ON findings(hex,first_seen,last_seen);
            CREATE INDEX IF NOT EXISTS finding_kind ON findings(kind);
            CREATE TABLE IF NOT EXISTS journal_records(
              source_id INTEGER PRIMARY KEY,kind TEXT NOT NULL,first_seen REAL NOT NULL,
              last_seen REAL NOT NULL,content TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS episodes(
              id TEXT PRIMARY KEY,hex TEXT NOT NULL,first_seen REAL NOT NULL,last_seen REAL NOT NULL,
              primary_finding TEXT NOT NULL,score INTEGER NOT NULL,review_status TEXT NOT NULL,
              analyzed_at TEXT NOT NULL,content TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS advisories(id TEXT PRIMARY KEY,day TEXT NOT NULL,content TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS aircraft_days(
              hex TEXT NOT NULL,day TEXT NOT NULL,content TEXT NOT NULL,PRIMARY KEY(hex,day));
            CREATE TABLE IF NOT EXISTS journal_assessments(
              source_id INTEGER PRIMARY KEY,score INTEGER NOT NULL,content TEXT NOT NULL);
        """)
        # Retained second-source type checks share the journal's table schema.
        typecheck.initialize(database)
    os.chmod(path, 0o600)
    return path


@contextmanager
def writer_lock(destination):
    """Only one capture or analysis coordinator may mutate this private corpus."""
    path = private_directory(destination) / "writer.lock"
    with path.open("a") as handle:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Another writer is already using this corpus") from error
        yield


def source_paths(root=None):
    """Files actually used, including an explicitly configured private reference bundle."""
    selected_reference = root is None
    root = Path(root or Path(__file__).parent)
    names = {path.name for path in root.glob("*.py") if path.is_file()}
    for folder in CODE_FOLDERS:
        names.update(
            path.relative_to(root).as_posix()
            for path in (root / folder).rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
    paths = {name: root / name for name in sorted(names)}
    if selected_reference and reference.DATA.resolve() != (root / "data").resolve():
        # Only reference.py's named tables are redirectable. Do not copy other private files.
        for filename, _expected in reference.FILES.values():
            name = "data/" + filename
            paths.pop(name, None)
            candidate = reference.DATA / filename
            if candidate.is_file():
                paths[name] = candidate
    return dict(sorted(paths.items()))


def source_files(root=None):
    """Every analysis file: the modules and selected data/** (never bytecode)."""
    return list(source_paths(root))


def code_hashes(root=None):
    """Analysis code plus selected data files, including private reference inputs."""
    return {name: sha256(path.read_bytes()) for name, path in source_paths(root).items()}


def detector_versions():
    """The detector and threshold versions behind every archive finding."""
    return {
        "replay": DETECTOR_VERSION,
        "maneuvers": maneuvers.VERSION,
        "rules": rules.VERSION,
        "cross_day": CROSS_DAY_VERSION,
    }


def through_checks(root, through, captured):
    """Whether the last frozen day is complete in the active archive.

    readsb writes slice 47 at midnight, rewrites traces after it and compresses
    the advisory export about 15 minutes later.
    """
    cutoff = day_stamp(through) + 86400
    folder = Path(root) / through.replace("-", "/")
    expected = {f"{index:02d}.bin.ttf" for index in range(THROUGH_SLICES)}
    names = {path.name for path in (folder / "heatmap").glob("*.bin.ttf") if path.is_file()}
    acas = sorted(path.name for path in (folder / "acas").glob("*") if path.is_file())
    newest = max(
        (path.stat().st_mtime for path in (folder / "traces").rglob("*.json") if path.is_file()), default=None
    )
    result = {
        "capture_after_cutoff": captured > cutoff,
        "heatmap_slice_files": len(names & expected),
        "all_slices_present": expected <= names,
        "acas_files": acas,
        "acas_compressed": not {"acas.json", "acas.csv"} & set(acas),
        "newest_trace_change": utc(newest) if newest is not None else None,
        "traces_settled": newest is None or captured - newest >= THROUGH_SETTLED_SECONDS,
    }
    result["complete"] = all(result[key] for key in THROUGH_REQUIREMENTS)
    return result


def read_only_uri(database, *, allow_wal=True):
    """A read-only URI that reads a database without WAL content immutably, creating no side file.

    A database with WAL content is still being written, or its writer or a
    reader stopped before a checkpoint. With ``allow_wal`` (journal inputs) it
    is read through its WAL, which can create a -shm file and which root never
    does: it would leave root-owned files beside it. Without (settled corpora)
    it is refused, not read.
    """
    database = Path(database).resolve()
    wal = database.with_name(database.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        if not allow_wal:
            raise ValueError(
                "The corpus database has WAL content; once its writer and readers are done, fold it "
                "in with `wtf-adsb-backfill-history --output CORPUS checkpoint`"
            )
        if os.geteuid() == 0:
            raise ValueError("The database has WAL content; give root a user-made backup copy")
        return database.as_uri() + "?mode=ro"
    return database.as_uri() + "?mode=ro&immutable=1"


def shared_uri(database):
    """A read-only URI for a reader beside a running writer, through SQLite's own locking.

    It may leave an empty -wal and a -shm file (mode 0600) beside the database.
    """
    return Path(database).resolve().as_uri() + "?mode=ro"


def _store_object(objects, sha, data):
    target = objects / sha
    if target.exists() and sha256(target.read_bytes()) == sha:
        return
    write_bytes_private(target, data)


def _version(records, versions, relative, sha, size, source):
    key = (relative, sha)
    if key not in records:
        parts = relative.split("/")
        records[key] = {
            "relative": relative,
            "sha256": sha,
            "bytes": size,
            "sources": [],
            "day": "-".join(parts[:3]),
            "format": parts[3],
            "object": sha,
        }
    if source not in records[key]["sources"]:
        records[key]["sources"].append(source)
    versions[relative].append(sha)


def capture_watchlist(cache, destination):
    """Pin the collector's watchlist: its reviewed CSV, licence and the parsed catalog.

    ``cache`` is a reviewed watchlist cache directory, or its current.json.
    The CSV at the cache's pinned commit is parsed again with
    watchlists.parse_catalog against the recorded SHA-256 and dates.
    """
    cache = Path(cache)
    current = cache if cache.is_file() else cache / "current.json"
    raw = current.read_bytes()
    if len(raw) > watchlists.MAX_CACHE_BYTES:
        raise ValueError("Watchlist cache exceeds its size bound")
    live = json.loads(raw)
    source = live.get("source") if isinstance(live, dict) else None
    if not isinstance(source, dict) or watchlists.source_commit(source.get("url", "")) != source.get(
        "commit"
    ):
        raise ValueError("Watchlist cache provenance does not match its pinned URL")
    folder = current.parent / source["commit"]
    data = (folder / WATCHLIST_SOURCE).read_bytes()
    catalog = watchlists.parse_catalog(
        data,
        source["url"],
        str(source.get("sha256", "")),
        fetched_at=source.get("fetched_at"),
        source_date=source.get("source_date"),
    )
    files = {WATCHLIST_SOURCE: data}
    if source.get("license_sha256"):
        licence = (folder / "LICENSE").read_bytes()
        if sha256(licence) != source["license_sha256"]:
            raise ValueError("The pinned watchlist licence changed")
        catalog["source"]["license_sha256"] = source["license_sha256"]
        files["LICENSE"] = licence
    files["catalog.json"] = (encoded(catalog) + "\n").encode()
    for name, content in files.items():
        write_bytes_private(Path(destination) / "inputs" / "watchlist" / name, content)
    return {
        "source_commit": source["commit"],
        "source_url": source["url"],
        "source_sha256": catalog["source"]["sha256"],
        "source_date": catalog["source"]["source_date"],
        "fetched_at": catalog["source"]["fetched_at"],
        "license_sha256": catalog["source"].get("license_sha256"),
        "accepted_entries": catalog["statistics"]["accepted_entries"],
        "quarantined_rows": catalog["statistics"]["quarantined_rows"],
        "files": {name: sha256(content) for name, content in files.items()},
        "live_cache_sha256": sha256(raw),
        # The collector reads the cache; a re-parse by newer rules may quarantine more rows.
        "live_cache_entries_match": catalog["entries"] == live.get("entries"),
    }


ANALYSIS_ARTIFACTS = ("analysis.json", "days", PINNED_SOURCE, "completion.json", CROSS_DAY_ARTIFACT)


def _fresh_destination(destination):
    """A new corpus directory: no manifest, and no analysis left from another one."""
    destination = Path(destination)
    if (destination / "manifest.json").exists():
        raise ValueError("Input manifest already exists; resume analysis or choose a new run directory")
    if any((destination / name).exists() for name in ANALYSIS_ARTIFACTS):
        raise ValueError("The directory holds analysis results without an input manifest; choose a new one")


def _load_frozen_tables(destination, manifest):
    """Load the metadata and journal tables from the frozen inputs, replacing any partial load."""
    path = initialize(destination)
    inputs = Path(destination) / "inputs"
    with connect(path) as database:
        if database.execute("SELECT 1 FROM jobs UNION ALL SELECT 1 FROM findings LIMIT 1").fetchone():
            raise ValueError("The corpus database already holds analysis results; choose a new run directory")
        for table in ("metadata", "journal_records", "meta"):
            database.execute(f"DELETE FROM {table}")
        database.execute(
            "INSERT OR REPLACE INTO meta VALUES ('manifest',?)",
            (encoded({key: value for key, value in manifest.items() if key != "files"}),),
        )
        with gzip.open(inputs / "aircraft-metadata.csv.gz", "rt") as source:
            for row in csv.reader(source, delimiter=";"):
                if len(row) < 6 or not re.fullmatch(r"[0-9a-fA-F]{6}", row[0]):
                    continue
                item = {
                    "r": row[1],
                    "t": row[2],
                    "dbFlags": sum(1 << index for index, value in enumerate(row[3]) if value == "1"),
                    "desc": row[4],
                    "year": row[5],
                }
                database.execute(
                    "INSERT OR REPLACE INTO metadata VALUES (?,?)", (row[0].lower(), encoded(item))
                )
        with closing(sqlite3.connect(read_only_uri(inputs / "journal.sqlite3"), uri=True)) as saved:
            saved.row_factory = sqlite3.Row
            for row in saved.execute("SELECT * FROM events"):
                event = dict(row)
                event["evidence"] = json.loads(event["evidence"])
                database.execute(
                    "INSERT OR REPLACE INTO journal_records VALUES (?,?,?,?,?)",
                    (event["id"], event["kind"], event["first_seen"], event["last_seen"], encoded(event)),
                )
    return path


def _stable_read(path, attempts=2):
    """A file's bytes, read again when the active writer replaced it during the read."""
    for _ in range(attempts):
        before = path.stat()
        raw = path.read_bytes()
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns):
            return raw
    raise OSError("Archive changed during capture")


def _capture_sources(sources, objects, through, records, versions, failures):
    """Store every archive version under the sources; returns how many later days were left out."""
    excluded = 0
    for source, directory in sources:
        for pattern in ARCHIVE_PATTERNS:
            for path in sorted(directory.glob(pattern)):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = str(path.relative_to(directory))
                if not re.match(r"^20\d\d/\d\d/\d\d/(heatmap|traces|acas)/", relative):
                    continue
                if through is not None and "-".join(relative.split("/")[:3]) > through:
                    excluded += 1
                    continue
                try:
                    raw = _stable_read(path)
                    sha = sha256(raw)
                    if (relative, sha) not in records:
                        _store_object(objects, sha, raw)
                    _version(records, versions, relative, sha, len(raw), source)
                except (OSError, ValueError) as error:
                    failures.append({"relative": relative, "source": source, "error_type": error_type(error)})
    return excluded


def _include_prior(include_prior, labels, objects, through, records, versions, failures):
    """Copy earlier corpora's hash-checked inputs: (prior records, how many later days were left out)."""
    priors, excluded = [], 0
    for prior in include_prior or ():
        prior = Path(prior).resolve()
        name = "prior-capture-" + prior.name
        if name in labels:
            raise ValueError("Prior corpora need distinct directory names")
        labels.add(name)
        raw_manifest = (prior / "manifest.json").read_bytes()
        prior_manifest = json.loads(raw_manifest)
        if prior_manifest.get("schema_version") != VERSION:
            raise ValueError("Unsupported prior manifest version")
        included = added = 0
        for row in prior_manifest["files"]:
            relative, sha = str(row.get("relative", "")), str(row.get("sha256", ""))
            match = re.match(r"^(20\d\d)/(\d\d)/(\d\d)/(heatmap|traces|acas)/", relative)
            if match is None or not re.fullmatch(r"[0-9a-f]{64}", sha) or row.get("object", sha) != sha:
                failures.append({"relative": relative, "source": name, "error_type": "InvalidPriorRecord"})
                continue
            if through is not None and "-".join(match.groups()[:3]) > through:
                excluded += 1
                continue
            try:
                data = (prior / "inputs" / "objects" / sha).read_bytes()
                if sha256(data) != sha:
                    raise ValueError("Prior input hash changed")
            except (OSError, ValueError) as error:
                failures.append({"relative": relative, "source": name, "error_type": error_type(error)})
                continue
            if (relative, sha) not in records:
                added += 1
                _store_object(objects, sha, data)
            _version(records, versions, relative, sha, len(data), name)
            included += 1
        priors.append(
            {
                "label": name,
                "directory": str(prior),
                "manifest_sha256": sha256(raw_manifest),
                "captured_at": prior_manifest.get("captured_at"),
                "analysis_cutoff": prior_manifest.get("analysis_cutoff"),
                "versions_included": included,
                "prior_only_versions_added": added,
            }
        )
    return priors, excluded


def _backup_journal(journal, destination):
    """Copy the journal input through SQLite's backup API; refuse one that changed meanwhile.

    Freeze a user-made backup copy. An immutable read of a live journal takes no
    locks, so a checkpoint during the copy could mix page versions that still
    pass the integrity check. Only the main file is watched: a WAL-mode writer
    that does not checkpoint meanwhile leaves the copy consistent.
    """
    journal = Path(journal)
    journal_copy = destination / "inputs" / "journal.sqlite3"
    before = journal.stat()
    with closing(sqlite3.connect(read_only_uri(journal), uri=True)) as original:
        with closing(sqlite3.connect(journal_copy)) as backup:
            original.backup(backup)
            if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Journal backup failed integrity check")
    after = journal.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("The journal changed while it was copied; freeze a user-made backup copy")
    os.chmod(journal_copy, 0o600)
    return journal_copy


def last_slice_end(records, objects, started, captured):
    """The end of the newest compact slice, when it ended within the hour before the capture began.

    readsb writes each half hour's compact file as it ends, so in an archive
    still being written the frozen inputs are complete only to that slice; a
    capture-time corpus stops there and the live collector covers what follows.
    The compact files are read first, so the check is against the capture's
    start. An older newest slice means the compact archive is no longer being
    written, and the capture time stays the cutoff. Only the newest heatmap
    day is read. Returns None without such a slice.
    """
    heatmaps = [row for row in records if row["format"] == "heatmap"]
    newest = max((row["day"] for row in heatmaps), default=None)
    if newest is None or day_stamp(newest) + 86400 < started - ACTIVE_SLICE_SECONDS:
        return None
    ends = []
    for row in heatmaps:
        if row["day"] != newest:
            continue
        try:
            ends.extend(end for _, end in replay_slices(objects / row["object"]) if end <= captured)
        except ARCHIVE_ERRORS:
            # The analysis reports an unreadable file as an error of its day.
            continue
    end = max(ends, default=None)
    return end if end is not None and end >= started - ACTIVE_SLICE_SECONDS else None


def freeze(
    root,
    journal,
    metadata,
    destination,
    *,
    snapshots=None,
    through=None,
    include_prior=(),
    watchlist=None,
    allow_incomplete=None,
):
    """Freeze unique archive versions and take a SQLite backup through its API.

    through: the last UTC day (YYYY-MM-DD). Later days are left out and analysis
    stops at the next midnight; the day must be complete in the active archive
    unless ``allow_incomplete`` gives a reason, which is recorded. Without it,
    analysis stops at the end of the last recorded compact slice (else the
    capture time).
    include_prior: earlier corpora whose inputs are hash-checked and copied in,
    labelled prior-capture-<directory name>; their manifest hashes are recorded.
    watchlist: the watchlist cache to pin (see capture_watchlist).
    journal: a user-made backup copy of the collector journal (see _backup_journal).
    """
    _fresh_destination(destination)
    if allow_incomplete and through is None:
        raise ValueError("An incomplete-day override needs a last frozen day")
    started = time.time()
    cutoff, completeness = None, None
    if through is not None:
        through = str(through)
        if not re.fullmatch(r"20\d\d-\d\d-\d\d", through):
            raise ValueError("The last frozen day must be YYYY-MM-DD")
        cutoff = day_stamp(through) + 86400
        completeness = through_checks(root, through, started)
        failed = [key for key in THROUGH_REQUIREMENTS if not completeness[key]]
        # A day that has not ended cannot be frozen through, whatever the override.
        if "capture_after_cutoff" in failed or (failed and not allow_incomplete):
            raise ValueError("The last frozen day is not complete: " + ", ".join(failed))
    destination = private_directory(destination)
    manifest_path = destination / "manifest.json"
    pinned_watchlist = capture_watchlist(watchlist, destination) if watchlist is not None else None
    objects = private_directory(destination / "inputs" / "objects")
    sources, skipped = [("active", Path(root))], []
    if snapshots:
        for snapshot in sorted(Path(snapshots).iterdir(), key=lambda p: p.name):
            archive = snapshot / "snapshot" / "ultrafeeder" / "globe_history"
            if archive.is_dir():
                sources.append(("snapshot-" + snapshot.name, archive))
            else:
                # Pruned or never-populated snapshots hold nothing to capture.
                skipped.append(snapshot.name)
    records, versions, failures = {}, defaultdict(list), []
    excluded = _capture_sources(sources, objects, through, records, versions, failures)
    priors, prior_excluded = _include_prior(
        include_prior, {name for name, _ in sources}, objects, through, records, versions, failures
    )
    journal_copy = _backup_journal(journal, destination)
    metadata_copy = destination / "inputs" / "aircraft-metadata.csv.gz"
    shutil.copyfile(metadata, metadata_copy)
    os.chmod(metadata_copy, 0o600)
    captured = time.time()
    basis = "through_day"
    if cutoff is None:
        cutoff = last_slice_end(records.values(), objects, started, captured)
        basis = "capture_time" if cutoff is None else "last_recorded_slice"
        cutoff = captured if cutoff is None else cutoff
    manifest = {
        "schema_version": VERSION,
        "capture_started_at": utc(started),
        "captured_at": utc(captured),
        # Analysis never reads past this instant: the next midnight after the last frozen
        # day, else the end of the last recorded compact slice, else the capture time.
        "analysis_cutoff": utc(cutoff),
        "cutoff_basis": basis,
        "through": {"day": through, "checks": completeness, "override_reason": allow_incomplete or None}
        if through is not None
        else None,
        "sources": [*(name for name, _ in sources), *(item["label"] for item in priors)],
        "skipped_snapshots": skipped,
        "prior_captures": priors,
        "excluded_after_through": excluded + prior_excluded,
        "files": sorted(records.values(), key=lambda row: (row["day"], row["relative"], row["sha256"])),
        "capture_failures": failures,
        "distinct_paths": len(versions),
        "paths_with_multiple_versions": sum(len(set(values)) > 1 for values in versions.values()),
        "metadata_sha256": sha256(metadata_copy.read_bytes()),
        "journal_sha256": sha256(journal_copy.read_bytes()),
        "watchlist": pinned_watchlist,
        "code_sha256": code_hashes(),
        "detector_version": DETECTOR_VERSION,
        "detector_versions": detector_versions(),
    }
    _load_frozen_tables(destination, manifest)
    write_private(manifest_path, manifest)
    return manifest


def clone_paths(source, destination):
    """Resolved (source, destination) for a clone, refused before anything is written."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("Clone into a separate directory")
    if not (source / "manifest.json").is_file():
        raise ValueError("The source has no input manifest")
    _fresh_destination(destination)
    return source, destination


def clone_inputs(source, destination):
    """A new corpus over another corpus's frozen inputs, with the same manifest and cutoff.

    Archive objects are hardlinked (copied across file systems) after a hash
    check; the journal, metadata and watchlist inputs are copied. The source
    corpus is only read.
    """
    source, destination = clone_paths(source, destination)
    raw = (source / "manifest.json").read_bytes()
    manifest = json.loads(raw)
    if manifest.get("schema_version") != VERSION:
        raise ValueError("Unsupported input manifest version")
    destination = private_directory(destination)
    objects = private_directory(destination / "inputs" / "objects")
    linked = copied = 0
    for name in sorted({row["object"] for row in manifest["files"]}):
        if not re.fullmatch(r"[0-9a-f]{64}", name):
            raise ValueError("Invalid frozen object name")
        origin, target = source / "inputs" / "objects" / name, objects / name
        if sha256(origin.read_bytes()) != name:
            raise ValueError("Frozen input hash changed")
        if target.exists():
            if sha256(target.read_bytes()) == name:
                continue
            target.unlink()
        try:
            os.link(origin, target)
            linked += 1
        except OSError:
            write_bytes_private(target, origin.read_bytes())
            copied += 1
    provenance = [
        ("journal.sqlite3", manifest["journal_sha256"]),
        ("aircraft-metadata.csv.gz", manifest["metadata_sha256"]),
        *(
            ("watchlist/" + name, expected)
            for name, expected in (manifest.get("watchlist") or {}).get("files", {}).items()
        ),
    ]
    for name, expected in provenance:
        data = (source / "inputs" / name).read_bytes()
        if sha256(data) != expected:
            raise ValueError("Frozen provenance input hash changed")
        write_bytes_private(destination / "inputs" / name, data)
    _load_frozen_tables(destination, manifest)
    write_bytes_private(destination / "manifest.json", raw)
    record = {
        "cloned_at": utc(),
        "source_directory": str(source),
        "source_manifest_sha256": sha256(raw),
        "objects_linked": linked,
        "objects_copied": copied,
    }
    write_private(destination / "clone.json", record)
    return record


RUN_FROZEN = '''"""Run this corpus's pinned analysis; it never writes the collector journal."""

import argparse
import os
import sys
from pathlib import Path

# No bytecode beside the pinned code, in this process or its workers.
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
ROOT = Path(__file__).resolve().parent
# A frozen run always uses its own captured tables, regardless of the caller's environment.
os.environ["WTF_ADSB_REFERENCE_DIR"] = str(ROOT / "analysis-source" / "data")
import types
package = types.ModuleType("wtf_adsb_frozen_analysis")
package.__path__ = [str(ROOT / "analysis-source")]
sys.modules[package.__name__] = package
from wtf_adsb_frozen_analysis import history_backfill  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-days", type=int)
    args = parser.parse_args()
    os.umask(0o077)
    history_backfill.verify_pinned_source(ROOT)
    with history_backfill.writer_lock(ROOT):
        history_backfill.write_private(
            ROOT / "process.json",
            {
                "pid": os.getpid(),
                "started_at": history_backfill.utc(),
                "workers": args.workers,
                "max_days": args.max_days,
            },
        )
        summary = history_backfill.run(ROOT, workers=args.workers, max_days=args.max_days)
        if history_backfill.all_days_analyzed(summary):
            history_backfill.write_private(
                ROOT / "completion.json", {"completed_at": history_backfill.utc(), "summary": summary}
            )
    print(history_backfill.encoded(summary), flush=True)


if __name__ == "__main__":
    main()
'''


def _git_state(source):
    """The checkout's commit and whether the analysis files differ from it, when known."""
    try:
        commit = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(source), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout.strip()
        changes = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(source), "status", "--porcelain", "--", "."],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout.strip()
    except OSError, subprocess.SubprocessError:
        return None
    return {"commit": commit, "uncommitted_changes": bool(changes)}


def pin_source(destination, source=None):
    """Copy every hashed analysis file into analysis-source/ and write run_frozen.py.

    A pinned corpus is analyzed only by its own runner, so later edits to the
    working tree cannot change a run in progress.
    """
    destination = Path(destination).resolve()
    if not (destination / "manifest.json").is_file():
        raise ValueError("Freeze the corpus before pinning its analysis code")
    if (destination / PINNED_SOURCE).exists():
        raise ValueError("This corpus already pins its analysis code")
    if (destination / "analysis.json").exists():
        raise ValueError("Analysis already started with unpinned code; clone the inputs into a new corpus")
    paths = source_paths(source)
    source = Path(source or Path(__file__).parent).resolve()
    hashes = {name: sha256(path.read_bytes()) for name, path in paths.items()}
    staging = destination / (PINNED_SOURCE + ".partial")
    if staging.exists():
        shutil.rmtree(staging)
    for name in hashes:
        write_bytes_private(staging / name, paths[name].read_bytes())
    if code_hashes(staging) != hashes:
        raise ValueError("The pinned copy differs from its source")
    pin = {
        "pinned_at": utc(),
        "source_directory": str(source),
        "git": _git_state(source),
        "code_sha256": hashes,
        "runner_sha256": sha256(RUN_FROZEN.encode()),
        "detector_version": DETECTOR_VERSION,
        "detector_versions": detector_versions(),
    }
    # The pinned source appears last: until then the corpus is simply unpinned.
    write_private(destination / SOURCE_PIN, pin)
    write_bytes_private(destination / RUNNER, RUN_FROZEN.encode())
    os.replace(staging, destination / PINNED_SOURCE)
    return pin


def verify_pinned_source(destination):
    """Refuse a pinned copy that no longer matches the hashes recorded when it was pinned."""
    destination = Path(destination).resolve()
    pin = json.loads((destination / SOURCE_PIN).read_text())
    if code_hashes(destination / PINNED_SOURCE) != pin["code_sha256"] or sha256(
        (destination / RUNNER).read_bytes()
    ) != pin.get("runner_sha256"):
        raise ValueError("The pinned analysis source changed; never edit analysis-source/ or run_frozen.py")
    return pin


def all_days_analyzed(summary):
    """Every available date has a current receipt, complete or partial."""
    jobs = summary.get("jobs", {})
    return jobs.get("complete", 0) + jobs.get("partial", 0) == summary["available_dates"]


def replay_points(source, slices=None):
    """The existing bounded compact format, including its recorded slice interval.

    ``source`` is a path or the file's raw bytes. Recorded slice intervals, as
    replay_slices() reads them, are appended to ``slices`` when it is given.
    """
    data = read_replay(source)
    stamp, resolution, labels = None, None, {}
    for offset in range(0, len(data), 16):
        address, lat, lon, alt, speed = struct.unpack_from("<Iiihh", data, offset)
        if address == MAGIC:
            stamp = ((lat & 0xFFFFFFFF) * 4294967296 + (lon & 0xFFFFFFFF)) / 1000
            resolution = alt / 1000 if alt > 0 else None
            if slices is not None and resolution:
                slices.append((stamp, stamp + resolution))
            continue
        if stamp is None:
            continue
        identifier = ("~" if address & 0x1000000 else "") + f"{address & 0xFFFFFF:06x}"
        if lat >= 1 << 30:
            labels[identifier] = label(data[offset : offset + 16], stamp)
            continue
        if (
            not -90000000 <= lat <= 90000000
            or not -180000000 <= lon <= 180000000
            or rules.null_island(lat / 1e6, lon / 1e6)
        ):
            continue
        source = (address >> 27) & 31
        yield (
            identifier,
            {
                "ts": stamp,
                "lat": lat / 1e6,
                "lon": lon / 1e6,
                "gs": speed / 10 if speed >= 0 else None,
                "archive_alt": "ground" if alt == -123 else None if alt == -124 else alt * 25,
                "source": TYPES[source] if source < len(TYPES) else "unknown",
                "timestamp_resolution_seconds": resolution,
                **labels.get(identifier, {}),
            },
        )


def replay_slices(source):
    """Recorded archive slice intervals, including slices without aircraft; ``source`` as replay_points."""
    data = read_replay(source)
    for offset in range(0, len(data), 16):
        address, high, low, interval, _ = struct.unpack_from("<Iiihh", data, offset)
        if address == MAGIC and interval > 0:
            start = ((high & 0xFFFFFFFF) * 4294967296 + (low & 0xFFFFFFFF)) / 1000
            yield start, start + interval / 1000


def merged_intervals(intervals, start, end):
    result = []
    for left, right in sorted((max(start, a), min(end, b)) for a, b in intervals if b > start and a < end):
        if result and left <= result[-1][1]:
            result[-1][1] = max(result[-1][1], right)
        else:
            result.append([left, right])
    return result


def read_trace(source):
    """A trace as JSON: a path or its raw bytes, gzip or plain, decompressed within a fixed bound."""
    if isinstance(source, bytes):
        return json.loads(bounded_bytes(source))
    with Path(source).open("rb") as handle:
        raw = handle.read(MAX_TRACE_BYTES + 1)
    if raw[:2] == b"\x1f\x8b":
        with gzip.open(source, "rb") as handle:
            raw = handle.read(MAX_TRACE_BYTES + 1)
    if len(raw) > MAX_TRACE_BYTES:
        raise ValueError("Trace exceeds size bound")
    return json.loads(raw)


def trace_label(extra, stamp):
    """Callsign and squawk from one trace detail object, with that row's time."""
    fields = {}
    if isinstance(extra.get("flight"), str) and extra["flight"].strip():
        fields["flight"] = extra["flight"].strip()
    if squawk := octal_code(extra.get("squawk")):
        fields["squawk"] = squawk
    return {**fields, "label_at": stamp} if fields else {}


def optional_fields(extra):
    """readsb's optional navigation and integrity fields, bounded as the collector keeps them.

    They belong to the detail object's own row; a later fix never inherits them.
    """
    result = {}
    for key, (low, high) in OPTIONAL_NUMBERS.items():
        value = number(extra.get(key))
        if value is not None and low <= value <= high:
            result[key] = value
    modes = extra.get("nav_modes")
    if isinstance(modes, list):
        result["nav_modes"] = sorted({mode for mode in modes if isinstance(mode, str) and mode in NAV_MODES})
    return result


def trace_points(source):
    """(address, fix) rows of one trace, a path or its raw bytes, in file order."""
    data = read_trace(source)
    if not isinstance(data, dict) or not isinstance(data.get("trace"), list):
        raise ValueError("Trace root must contain a trace array")
    identifier = data.get("icao")
    origin = number(data.get("timestamp"))
    if not isinstance(identifier, str) or not re.fullmatch(r"~?[a-fA-F0-9]{6}", identifier) or origin is None:
        raise ValueError("Trace identity or timestamp is invalid")
    labels, leg_pending = {}, False
    for row in data.get("trace", []):
        if not isinstance(row, list) or len(row) < 8 or number(row[0]) is None:
            raise ValueError("Trace row has unsupported structure")
        flags = row[6]
        if type(flags) is not int or flags < 0:
            raise ValueError("Trace flag value is invalid")
        stamp = origin + row[0]
        if flags & 2:
            # A new leg never inherits the previous leg's callsign or squawk.
            labels, leg_pending = {}, True
        extra = row[8] if len(row) > 8 and isinstance(row[8], dict) else {}
        if extra:
            # Each detail object is a full state; a missing code is unknown, not carried.
            labels = trace_label(extra, stamp)
        lat, lon = number(row[1]), number(row[2])
        if (
            lat is None
            or lon is None
            or not -90 <= lat <= 90
            or not -180 <= lon <= 180
            or rules.null_island(lat, lon)
        ):
            # The leg still starts here; its first usable fix carries the break.
            continue
        point = {
            "ts": stamp,
            "lat": lat,
            "lon": lon,
            "gs": row[4],
            "source": row[9] if len(row) > 9 and isinstance(row[9], str) else extra.get("type", "unknown"),
            # readsb timestamps are in seconds. Serialization precision alone
            # does not establish position-fix resolution or message freshness.
            "timestamp_resolution_seconds": None,
            # Flag 1 marks no position for 15 s or more before this fresh fix; it is
            # not a stale or repeated position. Continuity thresholds decide what it breaks.
            "gap_before": bool(flags & 1),
            "new_leg": leg_pending,
            "ground": row[3] == "ground",
            "archive_alt": row[3],
            **labels,
            **{key: extra[key] for key in TRACE_EXTRA_FIELDS if key in extra and key not in LABEL_FIELDS},
            **optional_fields(extra),
        }
        point["alt_geom" if flags & 8 else "alt_baro"] = row[3]
        point["geom_rate" if flags & 4 else "baro_rate"] = row[7]
        for index, field in ((10, "alt_geom"), (11, "geom_rate"), (12, "ias"), (13, "roll")):
            if len(row) > index and row[index] is not None:
                point[field] = row[index]
        leg_pending = False
        yield identifier.lower(), point


def metadata_for(path, addresses):
    with closing(sqlite3.connect(shared_uri(path), uri=True)) as database:
        result = {}
        ordinary = sorted(address for address in addresses if not address.startswith("~"))
        for start in range(0, len(ordinary), 500):
            batch = ordinary[start : start + 500]
            rows = database.execute(
                "SELECT hex,content FROM metadata WHERE hex IN (" + ",".join("?" for _ in batch) + ")", batch
            )
            result.update({address: json.loads(content) for address, content in rows})
        return result


def _aircraft(address, point, metadata):
    return {
        "hex": address,
        **metadata.get(address, {}),
        **{
            key: point[key]
            for key in ("lat", "lon", "gs", "archive_alt", *TRACE_EXTRA_FIELDS, "alt_baro", "label_at")
            if key in point
        },
        "type": point.get("source", "unknown"),
    }


PROVENANCE_FIELDS = {
    "archive",
    "archives",
    "variants",
    "conflicting_position",
    "conflicting_reading",
    "duplicate_count",
}
# Fields two same-time, same-position archive versions must agree on to be one
# reading: the episode rules' fields and ground speed. Versions that disagree about
# ground speed are a conflict, so input order can never decide which speed a
# sustained window holds; episode replay keeps merging them (READING_FIELDS).
DUPLICATE_FIELDS = (*READING_FIELDS, "gs", *STATUS_FIELDS)


def _same_reading(left, right):
    return {key: value for key, value in left.items() if key not in PROVENANCE_FIELDS} == {
        key: value for key, value in right.items() if key not in PROVENANCE_FIELDS
    }


def _adds_reading(kept, variant):
    """A repeat that differs only in position or break flags adds no scalar or status value."""
    ignored = PROVENANCE_FIELDS | {"lat", "lon", *BREAK_FLAGS}
    missing = object()
    return any(key not in ignored and kept.get(key, missing) != value for key, value in variant.items())


def collect_reading(group, point):
    """Add one decoded row to its address group, keyed by (timestamp, source).

    A repeated archive version adds provenance only. Distinct same-time rows
    stay as variants until prepared_tracks() classifies them.
    """
    key = (point["ts"], point.get("source"))
    existing = group.get(key)
    if existing is None:
        group[key] = point
        return
    variants = existing.setdefault("variants", [{**existing, "archives": list(existing["archives"])}])
    same = next((variant for variant in variants if _same_reading(variant, point)), None)
    if same is None:
        variants.append(dict(point))
    elif point["archive"] not in same["archives"]:
        same["archives"].append(point["archive"])
    if point["archive"] not in existing["archives"]:
        existing["archives"].append(point["archive"])


def prepared_tracks(grouped):
    """The one cleaning policy shared by corpus days and the evaluation tool.

    Same-time, same-source rows within 0.01 NM whose altitude, rates, ground
    speed and status agree are one reading with a duplicate count. Positions
    further apart are a conflict: movement resets at that time. Same-position
    rows whose readings disagree are reading conflicts: movement keeps the fix,
    the fix is marked ``conflicting_reading`` so no sustained-speed window
    holds it, and episode replay turns the variants into conflicting leads. A
    break flag on any copy of a fix applies to it. Returns one reading per
    (time, source), the report variants for scalar and episode rules, movement
    tracks and counts.
    """
    names = (
        "merged_duplicate_positions",
        "conflicting_positions",
        "conflicting_readings",
        "gap_flagged_positions",
    )
    counts = Counter(dict.fromkeys(names, 0))
    tracks, reports, movement = {}, {}, {}
    for address, values in grouped.items():
        points = sorted(values.values(), key=lambda p: p["ts"])
        report = []
        for point in points:
            variants = point.get("variants", [point])
            kept, duplicates = variants[0], 0
            for flag in BREAK_FLAGS:
                if any(variant.get(flag) for variant in variants):
                    point[flag] = kept[flag] = True
            report.append(kept)
            for variant in variants[1:]:
                if not same_position(kept, variant):
                    counts["conflicting_positions"] += 1
                    point["conflicting_position"] = True
                    report.append(variant)
                elif duplicate_reading(kept, variant, DUPLICATE_FIELDS):
                    duplicates += 1
                    # A repeat that adds no scalar or status value is not reported twice.
                    if _adds_reading(kept, variant):
                        report.append(variant)
                else:
                    counts["conflicting_readings"] += 1
                    point["conflicting_reading"] = kept["conflicting_reading"] = True
                    report.append(variant)
            if duplicates:
                point["duplicate_count"] = kept["duplicate_count"] = duplicates
                counts["merged_duplicate_positions"] += duplicates
            counts["gap_flagged_positions"] += bool(point.get("gap_before"))
        tracks[address] = points
        reports[address] = sorted(report, key=lambda p: p["ts"])
        if not address.startswith("~"):
            movement[address] = [
                {
                    **{key: value for key, value in point.items() if key != "variants"},
                    "alt": point.get("archive_alt"),
                }
                for point in points
            ]
    return tracks, reports, movement, counts


def _event(address, finding, aircraft, first, last, count, peak_time, source, metadata_as_of, extra=None):
    confidence = (
        "Single archived sample; unconfirmed"
        if count < 2
        else "Archived samples; message freshness unavailable"
    )
    event = {
        "hex": address,
        "kind": finding.kind,
        "category": finding.category,
        "title": finding.title,
        "value": finding.value,
        "unit": finding.unit,
        "why": finding.why,
        "explanation": finding.explanation,
        "score": finding.score,
        "confidence": confidence,
        "first_seen": first,
        "last_seen": last,
        "hits": count,
        "origin": "Historical corpus: " + source,
        "evidence": {
            "aircraft": aircraft,
            "observed_at": peak_time,
            "metadata_as_of": metadata_as_of,
            "historical_corpus": {
                "source": source,
                "altitude_reference": "archive_unspecified"
                if source == "heatmap"
                else "trace_flags"
                if source == "traces"
                else "not_retained",
                "message_freshness": "not_retained",
                "carried_labels": "Callsign and squawk may be carried between saved positions; repeated position samples are not independent retransmissions of those fields.",
            },
            **(finding.evidence or {}),
            **(extra or {}),
        },
    }
    if carried_label(aircraft, peak_time):
        add_context_tags(event["evidence"], [CARRIED_LABEL])
    event["id"] = digest([address, finding.kind, source, first, last, peak_time, finding.value])
    return event


def watchlist_match(aircraft, checks, catalog, cache):
    """The pinned watchlist lookup, with the same identity checks the collector passes."""
    if catalog is None:
        return None
    key = (
        aircraft.get("hex"),
        aircraft.get("r"),
        checks.get("conflict"),
        checks.get("callsign_matches_registration"),
    )
    if key not in cache:
        cache[key] = watchlists.lookup(aircraft, catalog, identity=checks)
    return cache[key]


def enrich(events, metadata, catalog, cache=None):
    """Identity checks and the pinned watchlist lookup on every finding, as the collector adds them.

    Database flags and year come from the frozen metadata, so a privacy-program
    address is never checked as an ordinary one.
    """
    cache = {} if cache is None else cache
    for event in events:
        evidence = event["evidence"]
        aircraft = {**metadata.get(event["hex"], {}), **evidence["aircraft"]}
        checks = evidence.get("identity_checks")
        if not isinstance(checks, dict):
            checks = evidence["identity_checks"] = identity_checks(aircraft)
        if "watchlist" not in evidence and (match := watchlist_match(aircraft, checks, catalog, cache)):
            evidence["watchlist"] = match
    return events


def _archive_wording(finding, source, aircraft):
    """A scalar finding in the archive's terms: saved positions and labels, and a compact
    altitude whose reference was not retained."""
    if finding.kind == "position_jump":
        finding = replace(
            finding,
            why="Two archived positions imply a large displacement over a short saved interval; their message freshness is unavailable.",
            explanation="This is a discontinuity between saved positions, not a measured travel speed. Position errors, source changes, address conflicts and archive timing can explain it.",
        )
    if source == "heatmap" and finding.kind == "high":
        finding = replace(
            finding,
            why=f"The saved archive contains an altitude reading of {finding.value:,.0f} feet.",
            explanation="The archive does not retain whether this altitude was barometric or geometric. It is not terrain clearance or independently verified flight height.",
        )
    if source == "heatmap" and finding.kind == "type_altitude_mismatch":
        envelope = (finding.evidence or {}).get("envelope", {})
        finding = replace(
            finding,
            why=f"The saved archive contains an altitude reading of {finding.value:,.0f} feet, above the conservative {envelope.get('altitude_cap_ft', 0):,.0f}-ft bound for the listed type.",
            evidence={**(finding.evidence or {}), "envelope": {**envelope, "field": "archive_alt"}},
        )
    if source == "heatmap" and finding.kind == "vfr_code_above_fl180":
        finding = replace(
            finding,
            why=f"Squawk 1200, the US VFR code, was the saved label at an archived altitude of {finding.value:,.0f} feet, whose reference was not retained.",
        )
    if source == "heatmap" and finding.kind == "outlier_alt_baro":
        outlier = (finding.evidence or {}).get("outlier", {})
        finding = replace(
            finding,
            kind="outlier_archive_alt",
            why="An archived altitude report falls outside the spotting threshold; its altitude reference was not retained.",
            evidence={**(finding.evidence or {}), "outlier": {**outlier, "field": "archive_alt"}},
        )
    if source == "heatmap" and finding.kind.startswith("special_squawk_"):
        finding = replace(finding, kind="archive_squawk_" + str(aircraft.get("squawk", "unknown")))
    if finding.kind.startswith(("special_squawk_", "archive_squawk_")):
        finding = replace(
            finding,
            explanation=finding.explanation
            + " Saved labels can be carried across positions; position counts do not establish repeated transponder-code messages.",
        )
    return finding


def analyze_scalars(
    tracks,
    metadata,
    start,
    end,
    source,
    metadata_as_of,
    *,
    allow_jumps=True,
    episode_analysis=False,
    watchlist=None,
):
    """Per-report rules over one format's readings; ``watchlist`` is a pinned catalog path.

    A point marked ``stale`` (history_metrics' repeated snapshot, never an archive
    fix) takes part in no position jump.
    """
    from .app import watchlist_finding

    events, lookups = [], {}
    for address, points in tracks.items():
        active, previous, last_point, saved = {}, None, None, []
        for point in points:
            stamp = point["ts"]
            ground = point.get("ground") or point.get("archive_alt") == "ground"
            if point.get("new_leg") or ground or (last_point is not None and stamp - last_point > 600):
                active.clear()
                previous = None
            last_point = stamp
            aircraft = _aircraft(address, point, metadata)
            evaluated = {**aircraft, "seen": 0, "seen_pos": 60 if point.get("stale") else 0}
            if source == "heatmap":
                evaluated["alt_baro"] = point.get("archive_alt")
            else:
                # Trace flags name the altitude reference; only compact data has an unnamed one.
                evaluated.pop("archive_alt", None)
            checks = identity_checks(evaluated)
            findings = candidates(evaluated, stamp, identity=checks)
            findings += supplementary_candidates(evaluated, stamp, identity=checks)
            if watch := watchlist_finding(watchlist_match(evaluated, checks, watchlist, lookups)):
                findings.append(watch)
            if episode_analysis:
                findings = [
                    f for f in findings if f.kind != "vertical" and not f.kind.startswith("special_squawk_")
                ]
            discontinuity = (
                jump(previous, evaluated, stamp)
                if allow_jumps and not ground and not point.get("stale")
                else None
            )
            pair = (
                {"previous_position": previous["a"], "previous_snapshot_at": previous["now"]}
                if discontinuity
                else {}
            )
            if discontinuity:
                findings.append(discontinuity)
            previous = None if ground or point.get("stale") else {"now": stamp, "a": evaluated}
            if not start <= stamp < end:
                continue
            for finding in findings:
                if finding.kind == "distant":
                    continue
                finding = _archive_wording(finding, source, aircraft)
                key = finding.kind
                entry = active.get(key)
                if entry is None or stamp - entry["last"] > 600:
                    entry = {
                        "finding": finding,
                        "aircraft": aircraft,
                        "first": stamp,
                        "last": stamp,
                        "count": 1,
                        "seen_times": {stamp},
                        "peak": stamp,
                        "extra": {
                            "archive_reference": point.get("archive"),
                            "archive_references": point.get("archives", []),
                            **(pair if key == "position_jump" else {}),
                        },
                    }
                    events.append((address, entry))
                    saved.append(entry)
                    active[key] = entry
                else:
                    entry["last"] = stamp
                    entry["seen_times"].add(stamp)
                    entry["count"] = len(entry["seen_times"])
                    if finding.value is not None and (
                        entry["finding"].value is None or abs(finding.value) > abs(entry["finding"].value)
                    ):
                        entry.update(
                            finding=finding,
                            aircraft=aircraft,
                            peak=stamp,
                            extra={
                                "archive_reference": point.get("archive"),
                                "archive_references": point.get("archives", []),
                                **(pair if key == "position_jump" else {}),
                            },
                        )
        # Samples around the saved peak are useful without assuming persistence.
        for entry in saved:
            if entry["finding"].category not in {"oddities", "signals"}:
                continue
            nearest = sorted(
                (point for point in points if abs(point["ts"] - entry["peak"]) <= 120),
                key=lambda point: abs(point["ts"] - entry["peak"]),
            )[:128]
            entry["extra"]["context_samples"] = [
                {
                    "timestamp": p["ts"],
                    **{
                        key: p[key]
                        for key in (
                            "gs",
                            "archive_alt",
                            "alt_baro",
                            "alt_geom",
                            "squawk",
                            "label_at",
                            "source",
                            "archive",
                            "archives",
                        )
                        if key in p
                    },
                }
                for p in sorted(nearest, key=lambda point: point["ts"])
            ]
    return [
        _event(
            address,
            entry["finding"],
            entry["aircraft"],
            entry["first"],
            entry["last"],
            entry["count"],
            entry["peak"],
            source,
            metadata_as_of,
            entry["extra"],
        )
        for address, entry in events
    ]


def day_inputs(manifest, day):
    """The frozen position files one day reads: its own and its continuity day's."""
    previous_day = utc(day_stamp(day) - 86400)[:10]
    return [
        row
        for row in manifest["files"]
        if row["day"] in {day, previous_day} and row["format"] in {"heatmap", "traces"}
    ]


def acas_inputs(manifest, day):
    """The advisory exports a day's TCAS episodes read: its own and both neighbours'.

    An advisory can cross midnight. The day of its first active message owns it
    and reads the reports after midnight; the next day reads the earlier ones
    only to recognize the advisory as already owned.
    """
    neighbours = {utc(day_stamp(day) + offset)[:10] for offset in (-86400, 0, 86400)}
    return [
        row
        for row in manifest["files"]
        if row["format"] == "acas" and row["day"] in neighbours and row["relative"].endswith(ACAS_REPORTS)
    ]


def analysis_end(manifest):
    """The instant analysis never reads past: the frozen cutoff, or the capture time of older corpora."""
    return datetime.fromisoformat(manifest.get("analysis_cutoff") or manifest["captured_at"]).timestamp()


def watchlist_catalog(destination, manifest):
    """The hash-checked path of the pinned watchlist catalog, or None when none was frozen."""
    pinned = manifest.get("watchlist")
    if not pinned:
        return None
    path = Path(destination) / "inputs" / "watchlist" / "catalog.json"
    if sha256(path.read_bytes()) != pinned["files"]["catalog.json"]:
        raise ValueError("Frozen watchlist hash changed")
    return path


INPUT_DEPENDENCIES = ("captured_at", "analysis_cutoff", "metadata_sha256", "watchlist_sha256", "inputs")


def capture_dependencies(manifest):
    """What every analysis result depends on from the capture: its time, cutoff, metadata and watchlist."""
    return {
        "captured_at": manifest["captured_at"],
        "analysis_cutoff": manifest.get("analysis_cutoff"),
        "metadata_sha256": manifest["metadata_sha256"],
        "watchlist_sha256": ((manifest.get("watchlist") or {}).get("files") or {}).get("catalog.json"),
    }


def input_dependencies(manifest, day):
    """The capture-side part of a day's dependencies: cutoff, metadata, watchlist and input versions."""
    return {
        **capture_dependencies(manifest),
        "inputs": sorted(
            [row["relative"], row["sha256"]]
            for row in [*day_inputs(manifest, day), *acas_inputs(manifest, day)]
        ),
    }


def day_dependencies(manifest, day, code_sha256):
    """Everything a day artifact depends on. A resumed run reuses a day only when it matches."""
    return {
        "code_sha256": code_sha256,
        "detector_version": DETECTOR_VERSION,
        "detector_versions": detector_versions(),
        **input_dependencies(manifest, day),
    }


def _fix(point):
    return {key: point[key] for key in FIX_FIELDS if key in point}


def _sighting(points):
    """The day's first sighting, confirmed as the collector confirms one.

    A run of positions no more than 45 s apart needs one at least 8 s after its
    first. Returns that first fix, the confirming time and how many positions
    it took, or None.
    """
    start = 0
    for index, point in enumerate(points):
        if index and point["ts"] - points[index - 1]["ts"] > SIGHTING_GAP_SECONDS:
            start = index
        if point["ts"] - points[start]["ts"] >= FIRST_SIGHTING_SECONDS:
            return {"fix": _fix(points[start]), "at": point["ts"], "positions": index - start + 1}
    return None


def frozen_input(destination, row):
    """A frozen input's bytes, checked against its manifest hash."""
    raw = (Path(destination) / "inputs" / "objects" / row["object"]).read_bytes()
    if sha256(raw) != row["sha256"]:
        raise ValueError("Frozen input hash changed")
    return raw


def sustained_speed(points, *, completed_from=None, completed_before=None):
    """The fastest ground speed held across at least four consecutive direct reports over 60 s.

    Within each continuous run of reports the live collector would also accept
    (telemetry.speed_point_usable: one direct source, airborne, a directly
    reported speed, usable position quality; no gap over 45 s, new leg or
    conflicting position or reading), every shortest qualifying window is
    scored by its slowest report; a window the live quality check would flag
    (telemetry.window_quality) is skipped. The fastest window wins, earliest
    on ties. ``completed_from``/``completed_before`` keep only windows ending
    in [from, before): a day measures with its preceding day's continuity
    reports, and a window belongs to the day its last report falls in, so it
    is counted once. Returns None without a window.
    """
    runs, run = [], []
    for point in points:
        usable = (
            telemetry.speed_point_usable(point)
            and not point.get("conflicting_position")
            and not point.get("conflicting_reading")
        )
        if (
            not usable
            or point.get("new_leg")
            or (
                run
                and (
                    point["ts"] - run[-1]["ts"] > SUSTAINED_GAP_SECONDS
                    or point.get("source") != run[-1].get("source")
                )
            )
        ):
            if run:
                runs.append(run)
            run = [point] if usable else []
            continue
        run.append(point)
    if run:
        runs.append(run)
    best = None
    for run in runs:
        speeds = [number(p["gs"]) for p in run]
        slowest, end = deque(), -1
        for begin in range(len(run)):
            while end + 1 < len(run) and (
                end < begin
                or run[end]["ts"] - run[begin]["ts"] < SUSTAINED_SECONDS
                or end - begin + 1 < SUSTAINED_SAMPLES
            ):
                end += 1
                while slowest and speeds[slowest[-1]] >= speeds[end]:
                    slowest.pop()
                slowest.append(end)
            if run[end]["ts"] - run[begin]["ts"] < SUSTAINED_SECONDS or end - begin + 1 < SUSTAINED_SAMPLES:
                break
            while slowest[0] < begin:
                slowest.popleft()
            completed = run[end]["ts"]
            if completed_from is not None and completed < completed_from:
                continue
            if completed_before is not None and completed >= completed_before:
                break
            value = speeds[slowest[0]]
            if (best is None or value > best[0]) and not telemetry.window_quality(run[begin : end + 1]):
                best = (value, run, begin, end, slowest[0])
    if best is None:
        return None
    value, run, begin, end, index = best
    return {
        "gs": value,
        "duration_seconds": run[end]["ts"] - run[begin]["ts"],
        "samples": end - begin + 1,
        "source": run[begin]["source"],
        "started_at": run[begin]["ts"],
        "ended_at": run[end]["ts"],
        "at": run[index]["ts"],
        "point": _fix(run[index]),
    }


# Current metadata names each archived aircraft; it is not historical identity verification.
METADATA_LIMITATION = "Current registration and type metadata may differ from the historical assignment."
TCAS_EXPLANATION = (
    "readsb decoded these TCAS resolution-advisory reports from the aircraft's own transmissions, "
    "with their message times. Positions and vertical rates come from saved tracks whose field "
    "freshness is unknown. The reports do not establish a near miss, a loss of separation or why "
    "the aircraft maneuvered."
)
TCAS_LIMITATIONS = [
    "Advisory reports are experimental decoder output; they do not establish a real collision threat or incident.",
    "Positions and rates come from saved tracks with unknown field freshness; a rate change can have other causes.",
    "The other aircraft's address comes from the advisory message; its saved track can be missing or incomplete.",
    "Tracks are read up to the end of the advisory's UTC day; a response after midnight is not measured.",
    METADATA_LIMITATION,
]
TRACE_EXPLANATION = (
    "This pattern is derived from saved aircraft positions. Trace flags distinguish altitude references "
    "when retained; saved timestamps do not prove message freshness. Current aircraft metadata does not "
    "establish historical identity or mission."
)
TRACE_LIMITATIONS = [
    "Saved trace timestamps do not establish exact message freshness.",
    "Altitude reference follows the retained trace flags; altitude is not terrain clearance.",
    METADATA_LIMITATION,
    "A movement pattern does not establish the operator, mission or activity on the ground.",
]


def _archive_reading(finding, source):
    """The explanation and limitations of an advisory, or of a trace-derived pattern."""
    if finding.kind == "tcas_ra":
        explanation, limitations = TCAS_EXPLANATION, TCAS_LIMITATIONS
        reference = {"traces": "trace_flags", "heatmap": "archive_unspecified"}.get(source, "not_retained")
    elif source == "traces":
        explanation, limitations, reference = TRACE_EXPLANATION, TRACE_LIMITATIONS, "trace_flags"
    else:
        return finding
    evidence = finding.evidence or {}
    return replace(
        finding,
        explanation=explanation,
        evidence={
            **evidence,
            "replay_pattern_import": {
                **evidence.get("replay_pattern_import", {}),
                "altitude_reference": reference,
                "limitations": limitations,
            },
        },
    )


def _episode_event(episode, source, tracks, start, end, metadata_as_of):
    """The corpus finding for one replay episode with an endpoint in [start, end)."""
    endpoints = {point for point in episode.endpoints if start <= point < end}
    if not endpoints:
        return None
    finding = episode.finding
    evidence = finding.evidence or {}
    hexes = {
        episode.address,
        *(
            item["hex"]
            for item in evidence.get("participants", [])
            if isinstance(item, dict) and "hex" in item
        ),
    }
    supporting_archives = sorted(
        {
            archive
            for address in hexes
            for point in tracks.get(address, [])
            if episode.first <= point["ts"] <= episode.last
            for archive in point["archives"]
        }
    )
    finding = replace(
        finding,
        evidence={
            **evidence,
            "replay_pattern_import": {
                **evidence.get("replay_pattern_import", {}),
                "archives": supporting_archives,
            },
        },
    )
    # Each builder already applied the archive format's freshness rule (R11,
    # maneuvers.archived): a blocking flag, or a limitation for robust shapes.
    finding = _archive_reading(finding, source)
    measurement = finding.evidence.get("maneuver") or finding.evidence["pattern"]
    return _event(
        episode.address,
        finding,
        episode.aircraft,
        episode.first,
        episode.last,
        len(endpoints),
        measurement.get("peak_at", measurement["ended_at"]),
        source,
        metadata_as_of,
    )


def export_cutoffs(manifest):
    """Each source's capture cutoff: a prior capture's own, else this corpus's (key None)."""
    cutoffs = {None: analysis_end(manifest)}
    for prior in manifest.get("prior_captures") or ():
        if stamp := prior.get("analysis_cutoff") or prior.get("captured_at"):
            cutoffs[prior["label"]] = datetime.fromisoformat(stamp).timestamp()
    return cutoffs


def advisory_lines(raw, row, cutoffs):
    """The report lines of one frozen advisory export, and whether its unterminated last line was dropped.

    readsb compresses a day's export once that day has ended. A plain export of
    the day of a capture's cutoff or later could still be written to when that
    capture froze it (``cutoffs``, see export_cutoffs), so an unterminated last
    line there is dropped, not parsed, and the caller records it. Anywhere else
    such a line is an error.
    """
    data = bounded_bytes(raw)
    open_day = min(
        utc(cutoffs.get(source, cutoffs[None]) - 1)[:10] for source in row.get("sources") or [None]
    )
    if raw[:2] != b"\x1f\x8b" and row["day"] >= open_day and data and not data.endswith(b"\n"):
        return data.splitlines()[:-1], True
    return data.splitlines(), False


def advisory_reports(destination, rows, day, manifest):
    """(own address, allowlisted acas_ra) pairs from frozen advisory exports, before the cutoff.

    The allowlist drops receiver-relative fields and raw message bytes. A
    report in several copies counts once; a damaged file of ``day`` is that
    day's error, and one of a neighbouring day only limits advisories that
    cross midnight. Returns the pairs, both error lists and the exports whose
    unterminated last line was dropped (see advisory_lines).
    """
    reports, errors, continuity, dropped = {}, [], [], []
    cutoff, cutoffs = analysis_end(manifest), export_cutoffs(manifest)
    for row in rows:
        try:
            raw = (Path(destination) / "inputs" / "objects" / row["object"]).read_bytes()
            if len(raw) > MAX_TRACE_BYTES or sha256(raw) != row["sha256"]:
                raise ValueError("Advisory input bound or hash check failed")
            lines, unterminated = advisory_lines(raw, row, cutoffs)
            for line in lines:
                if not line.strip():
                    continue
                report = json.loads(line)
                address = report.get("hex")
                advisory = acas_report(report.get("acas_ra"))
                if (
                    not isinstance(address, str)
                    or not re.fullmatch(r"~?[0-9a-fA-F]{6}", address)
                    or advisory is None
                ):
                    raise ValueError("Unsupported advisory record")
                if advisory["unix_timestamp"] < cutoff:
                    reports.setdefault((address.lower(), advisory["unix_timestamp"]), advisory)
            if unterminated:
                dropped.append({"object": row["object"], "relative": row["relative"]})
        except (*ARCHIVE_ERRORS, TypeError, AttributeError) as error:
            (errors if row["day"] == day else continuity).append(
                {"object": row["object"], "error_type": error_type(error)}
            )
    pairs = [(own, advisory) for (own, _), advisory in sorted(reports.items())]
    return pairs, errors, continuity, dropped


def analyze_day(day, destination, *, code_sha256=None):
    destination = Path(destination)
    manifest = json.loads((destination / "manifest.json").read_text())
    start = day_stamp(day)
    end = min(start + 86400, analysis_end(manifest))
    files = day_inputs(manifest, day)
    dependencies = day_dependencies(manifest, day, code_sha256 or code_hashes())
    by_format = {"heatmap": defaultdict(dict), "traces": defaultdict(dict)}
    counts, errors, continuity_errors, slices, cleaning = Counter(), [], [], [], {}
    for row in files:
        try:
            # Read once: the hash check and the decoder share the bytes.
            raw = frozen_input(destination, row)
            decoded = replay_points(raw, slices) if row["format"] == "heatmap" else trace_points(raw)
            reference = row["relative"] + "@" + row["sha256"]
            for address, point in decoded:
                point["archive"] = reference
                point["archives"] = [point["archive"]]
                stamp = point["ts"]
                if not start - PATTERN_WINDOW_SECONDS <= stamp < end:
                    continue
                collect_reading(by_format[row["format"]][address], point)
                if start <= stamp:
                    counts[row["format"] + "_decoded_positions"] += 1
        except (*ARCHIVE_ERRORS, TypeError) as error:
            # An unreadable file of the day, including a damaged deflate stream, leaves it
            # partial. A continuity-day file is that day's error; here it only limits the
            # first minutes of movement continuity.
            (errors if row["day"] == day else continuity_errors).append(
                {"object": row["object"], "error_type": error_type(error)}
            )
    reports, advisory_errors, advisory_continuity, unterminated = advisory_reports(
        destination, acas_inputs(manifest, day), day, manifest
    )
    errors.extend(advisory_errors)
    continuity_errors.extend(advisory_continuity)
    addresses = {address for source in by_format.values() for address in source}
    metadata = metadata_for(
        destination / "corpus.sqlite3",
        addresses
        | {own for own, _ in reports}
        | {r["threat_id_hex"] for _, r in reports if "threat_id_hex" in r},
    )
    catalog = watchlist_catalog(destination, manifest)
    metadata_as_of = manifest["captured_at"]
    metadata_source = "Frozen current aircraft metadata; not historical identity verification"
    findings, aircraft_days, prepared = [], defaultdict(dict), {}
    for source, grouped in by_format.items():
        tracks, scalar_tracks, pattern_tracks, cleaning[source] = prepared_tracks(grouped)
        prepared[source] = tracks
        counts[source + "_unique_positions"] = sum(
            sum(p["ts"] >= start for p in track) for track in tracks.values()
        )
        for address, points in tracks.items():
            retained = [p for p in points if p["ts"] >= start]
            if retained:
                record = {
                    "positions": len(retained),
                    "first": retained[0]["ts"],
                    "last": retained[-1]["ts"],
                    # Inputs of the cross-day phase: the day's first fix, the position that
                    # confirms the sighting, and the best sustained speed.
                    "first_fix": _fix(retained[0]),
                }
                if sighted := _sighting(retained):
                    record["sighted"] = sighted
                # The continuity reports before midnight join the day's windows; a window
                # counts on the day its last report falls in (completed_from/before).
                if not address.startswith("~") and (
                    best := sustained_speed(points, completed_from=start, completed_before=end)
                ):
                    record["sustained"] = best
                aircraft_days[address][source] = record
        findings.extend(
            analyze_scalars(
                scalar_tracks,
                metadata,
                start,
                end,
                source,
                metadata_as_of,
                episode_analysis=True,
                watchlist=catalog,
            )
        )
        episodes = analyze_tracks(
            pattern_tracks,
            metadata,
            metadata_as_of=metadata_as_of,
            metadata_source=metadata_source,
            max_episodes=10000,
            include_episodes=True,
            episode_tracks=scalar_tracks,
            episode_horizon=end,
            archive=source,
        )
        groups = episode_replay.group_episodes(pattern_tracks, episodes, metadata, start, end, archive=source)
        for episode in groups:
            annotate_episode(episode, metadata_as_of, metadata_source)
        for episode in [*episodes, *groups]:
            if (event := _episode_event(episode, source, tracks, start, end, metadata_as_of)) is not None:
                findings.append(event)
    for source, episode in episode_replay.tcas_episodes(reports, prepared, metadata, start, end):
        annotate_episode(episode, metadata_as_of, metadata_source)
        event = _episode_event(episode, source, prepared.get(source, {}), start, end, metadata_as_of)
        if event is not None:
            findings.append(event)
    enrich(findings, metadata, catalog)
    result = {
        "day": day,
        "completed_at": utc(),
        "dependencies": dependencies,
        "dependencies_sha256": digest(dependencies),
        "files_read": len(files),
        "aircraft": len(addresses),
        "counts": dict(counts),
        # Neighbouring days' reports are read only to place advisories that cross midnight.
        "advisory_reports": sum(start <= advisory["unix_timestamp"] < end for _, advisory in reports),
        # Same-time rows more than 0.01 NM apart; near-identical repeats are merged instead.
        "conflicting_positions": sum(item["conflicting_positions"] for item in cleaning.values()),
        # Same-time rows at one position whose altitude, rate or status disagree.
        "conflicting_readings": sum(item["conflicting_readings"] for item in cleaning.values()),
        "merged_duplicate_positions": sum(item["merged_duplicate_positions"] for item in cleaning.values()),
        # Trace fixes after a gap of 15 s or more; continuity rules decide what each gap breaks.
        "gap_flagged_trace_positions": cleaning["traces"]["gap_flagged_positions"],
        "errors": errors,
        "continuity_errors": continuity_errors,
        # Advisory exports still open at the cutoff whose unterminated last line was dropped.
        "advisory_unterminated_lines": unterminated,
        "archive_slice_intervals": merged_intervals(slices, start, end),
        "aircraft_days": dict(aircraft_days),
        "findings": findings,
    }
    report = destination / "days" / (day + ".json")
    write_private(report, result)
    return {key: value for key, value in result.items() if key not in {"findings", "aircraft_days"}}


def _insert_findings(database, day, events):
    for event in events:
        database.execute(
            "INSERT OR REPLACE INTO findings VALUES (?,?,?,?,?,?,?,?,?)",
            (
                event["id"],
                day,
                event["evidence"]["historical_corpus"]["source"],
                event["hex"],
                event["kind"],
                event["first_seen"],
                event["last_seen"],
                event["score"],
                encoded(event),
            ),
        )


def import_day(destination, summary):
    raw = (Path(destination) / "days" / (summary["day"] + ".json")).read_bytes()
    result = json.loads(raw)
    summary = {**summary, "artifact_sha256": sha256(raw)}
    with connect(Path(destination) / "corpus.sqlite3") as database:
        database.execute("DELETE FROM findings WHERE day=?", (result["day"],))
        _insert_findings(database, result["day"], result["findings"])
        database.execute("DELETE FROM aircraft_days WHERE day=?", (result["day"],))
        database.executemany(
            "INSERT INTO aircraft_days VALUES (?,?,?)",
            ((address, result["day"], encoded(value)) for address, value in result["aircraft_days"].items()),
        )
        database.execute(
            "INSERT OR REPLACE INTO jobs VALUES (?,?,?,?)",
            (
                result["day"],
                "complete" if not result["errors"] else "partial",
                result["completed_at"],
                encoded(summary),
            ),
        )


def _slice_seconds(receipt):
    return sum(b - a for a, b in receipt.get("archive_slice_intervals", []))


def coverage_days(database, minimum=COVERAGE_SECONDS):
    """Sorted complete analysis days with at least ``minimum`` seconds of recorded archive slices.

    ``database`` is an open corpus connection. Every cross-day rule counts coverage this way.
    """
    return sorted(
        day
        for day, content in database.execute("SELECT day,content FROM jobs WHERE status='complete'")
        if _slice_seconds(json.loads(content)) >= minimum
    )


def _earliest(formats, key):
    """(format, value) of a per-format record field, by time, then traces first."""
    found = [
        (value["ts"], name != "traces", name, value)
        for name, record in formats.items()
        if (value := record.get(key))
    ]
    return min(found, key=lambda item: item[:3])[2:] if found else (None, None)


def _fastest(formats):
    """(format, sustained window) with the highest sustained speed, then traces first."""
    found = [
        (-value["gs"], name != "traces", name, value)
        for name, record in formats.items()
        if (value := record.get("sustained"))
    ]
    return min(found, key=lambda item: item[:3])[2:] if found else (None, None)


def cross_day_findings(destination, *, code_sha256=None):
    """First sightings and seasonal speed records, in date order after every day is imported.

    Each UTC day sees only earlier days, as the collector does. Coverage is a
    complete analysis day with at least an hour of recorded archive slices.
    Types and allocation States come from the frozen metadata, so an address
    keeps one type across the corpus. A first sighting needs a position 8 s
    after the first, as the collector's confirmation does. Within a day,
    sustained speeds are taken in the order their windows ended; a record's
    baseline starts on the season's first covered day (the season's start when
    the corpus covers it), or on an earlier day whose reports joined it.
    Results never depend on how many workers analyzed the days.

    The findings are written to cross-day.json, then replace the findings
    table's cross-day rows. summary() records the artifact's hash, so a
    completed corpus binds them as each day's receipt binds its artifact (see
    cross_day_artifact). Returns the number of findings by kind.
    """
    destination = Path(destination)
    manifest = json.loads((destination / "manifest.json").read_text())
    catalog = watchlist_catalog(destination, manifest)
    database_path = destination / "corpus.sqlite3"
    with connect(database_path) as database:
        coverage = coverage_days(database)
        addresses = [
            row[0]
            for row in database.execute("SELECT DISTINCT hex FROM aircraft_days WHERE hex NOT LIKE '~%'")
        ]
        days = [
            [day, status, json.loads(content).get("artifact_sha256")]
            for day, status, content in database.execute("SELECT day,status,content FROM jobs ORDER BY day")
        ]
    metadata = metadata_for(database_path, addresses)
    phase = _CrossDay(coverage, metadata, manifest["captured_at"])
    with closing(sqlite3.connect(shared_uri(database_path), uri=True)) as database:
        rows = database.execute(
            "SELECT hex,day,content FROM aircraft_days WHERE hex NOT LIKE '~%' ORDER BY day,hex"
        )
        for day, group in itertools.groupby(rows, key=lambda row: row[1]):
            phase.day(day, group)
    events = sorted(
        enrich(phase.events, metadata, catalog),
        key=lambda event: (utc(event["first_seen"])[:10], event["id"]),
    )
    write_private(
        destination / CROSS_DAY_ARTIFACT,
        {
            # No timestamp: an unchanged rerun writes the same bytes, so completion still binds them.
            "version": CROSS_DAY_VERSION,
            "dependencies": {
                "code_sha256": code_sha256 or code_hashes(),
                "detector_versions": detector_versions(),
                **capture_dependencies(manifest),
                # The day receipts whose aircraft presence this phase read.
                "days": days,
            },
            "coverage_days": coverage,
            "findings": events,
        },
    )
    with connect(database_path) as database:
        database.execute(
            f"DELETE FROM findings WHERE kind IN ({','.join('?' for _ in CROSS_DAY_KINDS)})", CROSS_DAY_KINDS
        )
        for day, group in itertools.groupby(events, key=lambda event: utc(event["first_seen"])[:10]):
            _insert_findings(database, day, group)
    return dict(Counter(event["kind"] for event in events))


class _CrossDay:
    """State of the cross-day phase: counts and season maxima from earlier days only."""

    def __init__(self, coverage, metadata, captured_at):
        self.coverage, self.metadata, self.captured_at = coverage, metadata, captured_at
        self.countries, self.type_days, self.country_days, self.seasons = {}, Counter(), Counter(), {}
        self.events = []

    def _event(self, address, finding, aircraft, first, last, count, at, source):
        self.events.append(
            _event(
                address,
                finding,
                aircraft,
                first,
                last,
                count,
                at,
                source,
                self.captured_at,
                {"cross_day": CROSS_DAY_SCOPE},
            )
        )

    def day(self, day, rows):
        """One UTC day's first sightings and seasonal records, then its presence joins the counts.

        Only the aircraft seen earliest that day can be the first of a type or
        allocation State: a later one the same day is not first.
        """
        covered = bisect.bisect_left(self.coverage, day)
        base = {
            "coverage_days": covered,
            "first_day": self.coverage[0] if covered else None,
            "other_type_days": 0,
        }
        seen, earliest, records = [], {}, []
        for address, _, content in rows:
            formats = json.loads(content)
            typecode = reference.clean(self.metadata.get(address, {}).get("t"))
            if address not in self.countries:
                self.countries[address] = identity_checks({"hex": address})["allocation_country"]
            country = self.countries[address]
            # Any fix is presence; only a confirmed sighting can be a first one.
            _, present = _earliest(formats, "first_fix")
            sighted = {name: record["sighted"] for name, record in formats.items() if record.get("sighted")}
            seen.append((address, typecode, country, sighted))
            for key in (("type", typecode), ("country", country)):
                if key[1] and present:
                    earliest[key] = min(earliest.get(key, (math.inf, "")), (present["ts"], address))
            source, sustained = _fastest(formats)
            if sustained and typecode:
                records.append((sustained["ended_at"], address, source, typecode, sustained))
        for address, typecode, country, sighted in seen:
            source, fix = _earliest(sighted, "fix")
            first_type = bool(typecode) and earliest.get(("type", typecode), (0, None))[1] == address
            first_country = bool(country) and earliest.get(("country", country), (0, None))[1] == address
            if not fix or not (
                (first_type and not self.type_days[typecode])
                or (first_country and not self.country_days[country])
            ):
                continue
            aircraft = _aircraft(address, fix, self.metadata)
            history = {
                **base,
                # An aircraft of the type or State seen earlier the same day counts as a day.
                "type_days": self.type_days[typecode] + (not first_type) if typecode else None,
                "country_days": self.country_days[country] + (not first_country) if country else None,
            }
            confirmed = sighted[source]
            for finding in rules.first_sighting(aircraft, history):
                self._event(
                    address,
                    finding,
                    aircraft,
                    fix["ts"],
                    confirmed["at"],
                    confirmed["positions"],
                    fix["ts"],
                    source,
                )
        for _, address, source, typecode, sustained in sorted(records, key=lambda item: item[:2]):
            self._record(day, address, source, typecode, sustained)
        self.type_days.update({typecode for _, typecode, _, _ in seen} - {""})
        self.country_days.update({country for _, _, country, _ in seen} - {None})

    def _record(self, day, address, source, typecode, sustained):
        aircraft = _aircraft(address, sustained["point"], self.metadata)
        checks = identity_checks(aircraft)
        # A window belongs to the day, and so the season, of its last report.
        season, _ = rules.season_start(sustained["ended_at"])
        state = self.seasons.setdefault(
            (season, typecode), {"max_gs": None, "addresses": set(), "first_day": None}
        )
        window = {key: sustained[key] for key in ("gs", "duration_seconds", "samples", "source")}
        # A window the record rule would reject, or a speed beyond the listed type's
        # envelope or rated limit, never joins the season's baseline.
        probe = {
            "type": typecode,
            "season_start": season,
            "max_gs": 0,
            "addresses": rules.SEASON_MIN_ADDRESSES,
            "baseline_days": rules.SEASON_MIN_BASELINE_DAYS,
        }
        if (
            not rules.speed_plausible(aircraft, window["gs"])
            or rules.seasonal_speed_record(aircraft, probe, sustained=window, identity=checks) is None
        ):
            return
        index = bisect.bisect_left(self.coverage, season)
        since = self.coverage[index] if index < len(self.coverage) and self.coverage[index] <= day else None
        if state["max_gs"] is not None and since is not None:
            # The comparison covers reports since the season's first covered day, or an earlier
            # day that added to it, which the claim names; the meteorological season can have
            # begun before the corpus.
            baseline = min(since, state["first_day"])
            earlier = {
                "type": typecode,
                "season_start": baseline,
                "max_gs": state["max_gs"],
                "addresses": len(state["addresses"]),
                # As the collector counts them: from the baseline's first UTC midnight
                # to the window's end, so no record appears in a baseline's first 14 days.
                "baseline_days": (sustained["ended_at"] - day_stamp(baseline)) / 86400,
            }
            finding = rules.seasonal_speed_record(aircraft, earlier, sustained=window, identity=checks)
            if finding is not None:
                record = {
                    **finding.evidence["season_record"],
                    "season_start": season,
                    "baseline_start": baseline,
                }
                finding = replace(finding, evidence={**finding.evidence, "season_record": record})
                first, last = sustained["started_at"], sustained["ended_at"]
                self._event(
                    address, finding, aircraft, first, last, sustained["samples"], sustained["at"], source
                )
        state["max_gs"] = max(state["max_gs"] or 0, window["gs"])
        state["addresses"].add(address)
        state["first_day"] = state["first_day"] or day


def _bounded_artifact(path):
    """An analysis artifact's bytes: a regular file within the artifact bound."""
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > MAX_ARTIFACT_BYTES:
        raise ValueError("Invalid analysis artifact")
    return path.read_bytes()


def _unique_findings(findings):
    """Findings by id; an id that appears twice binds neither copy."""
    found = Counter(item.get("id") for item in findings)
    return {item["id"]: item for item in findings if item.get("id") is not None and found[item["id"]] == 1}


def cross_day_artifact(destination):
    """The completed corpus's verified cross-day artifact: (sha256, {finding id: finding}).

    Its hash must equal the one completion.json recorded in the summary
    (summary()["cross_day_sha256"]); a corpus completed before the artifact
    existed is refused.
    """
    destination = Path(destination)
    completion = json.loads((destination / "completion.json").read_text())
    recorded = (completion.get("summary") or {}).get("cross_day_sha256")
    if recorded is None:
        raise ValueError("The completed analysis recorded no cross-day artifact; analyze a new corpus")
    raw = _bounded_artifact(destination / CROSS_DAY_ARTIFACT)
    digest_value = sha256(raw)
    if digest_value != recorded:
        raise ValueError("The cross-day artifact differs from the one the completed analysis recorded")
    return digest_value, _unique_findings(json.loads(raw)["findings"])


def _checkpoint(database, day, allowed_partial):
    """The day's receipt; refused unless the day is complete, or partial and allowed."""
    job = database.execute("SELECT status,content FROM jobs WHERE day=?", (day,)).fetchone()
    if job is None or not (job[0] == "complete" or (job[0] == "partial" and day in allowed_partial)):
        raise ValueError("Selected finding has no completed analysis checkpoint")
    return json.loads(job[1])


def _day_artifact(destination, day, receipt):
    """A day's verified artifact: (sha256, {finding id: finding})."""
    raw = _bounded_artifact(Path(destination) / "days" / (day + ".json"))
    digest_value = sha256(raw)
    if digest_value != receipt.get("artifact_sha256"):
        raise ValueError("Completed analysis artifact hash changed")
    report = json.loads(raw)
    if report.get("day") != day:
        raise ValueError("Analysis artifact belongs to another day")
    return digest_value, _unique_findings(report["findings"])


def verified_finding(database, destination, finding_id, *, allowed_partial=(), cache=None):
    """One findings row bound to the completed artifact that holds it: (finding, artifact sha256).

    ``database`` is an open read-only corpus connection. The finding's day must
    be complete, or partial and named in ``allowed_partial``. A day finding
    must equal its entry in days/<day>.json, whose hash the day's receipt
    records; a cross-day finding (CROSS_DAY_KINDS) its entry in cross-day.json
    (see cross_day_artifact). ``cache``, a dict kept between calls, holds the
    last parsed day artifact and the cross-day one.
    """
    row = database.execute("SELECT day,kind,content FROM findings WHERE id=?", (finding_id,)).fetchone()
    if row is None:
        raise ValueError("Selected finding is missing from the corpus")
    day, kind, content = row
    if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("Invalid finding day")
    receipt = _checkpoint(database, day, set(allowed_partial))
    cache = {} if cache is None else cache
    if kind in CROSS_DAY_KINDS:
        if "cross_day" not in cache:
            cache["cross_day"] = cross_day_artifact(destination)
        digest_value, findings = cache["cross_day"]
    else:
        if cache.get("day", (None,))[0] != day:
            # Rows usually arrive in time order, so each day artifact is parsed about once.
            cache["day"] = (day, _day_artifact(destination, day, receipt))
        digest_value, findings = cache["day"][1]
    finding = json.loads(content)
    if findings.get(finding_id) != finding:
        raise ValueError("Selected finding differs from its completed analysis artifact")
    # Retained second-source type checks are separate from the pinned artifact;
    # they join the verified finding in memory.
    typecheck.fill(finding, typecheck.saved(database, str(finding.get("hex", "")).lower(), None))
    return finding, digest_value


def assemble_episodes(destination):
    """Deduplicate overlapping reasons and source formats into observation episodes."""
    from . import selection

    destination = Path(destination)
    with connect(destination / "corpus.sqlite3") as database:
        groups, active = [], {}
        for row in database.execute("SELECT * FROM findings ORDER BY hex,first_seen,last_seen,id"):
            event = json.loads(row["content"])
            group = active.get(row["hex"])
            if group is None or row["first_seen"] > group["last_seen"] + 600:
                group = {
                    "hex": row["hex"],
                    "first_seen": row["first_seen"],
                    "last_seen": row["last_seen"],
                    "events": [],
                }
                groups.append(group)
                active[row["hex"]] = group
            group["last_seen"] = max(group["last_seen"], row["last_seen"])
            event["selection"] = selection.assess(event, {})
            if event["kind"].startswith(("special_squawk_", "archive_squawk_")):
                event["selection"].update(review_required=True, story_eligible=False)
                event["selection"]["reasons"].append(
                    "Carried archive labels do not confirm repeated code messages"
                )
            group["events"].append(event)
        database.execute("DELETE FROM episodes")
        for group in groups:
            best = max(group["events"], key=lambda e: (e["selection"]["score"], e["last_seen"], e["id"]))
            identifier = digest([group["hex"], group["first_seen"]])
            body = {
                "id": identifier,
                "hex": group["hex"],
                "observation_start": utc(group["first_seen"]),
                "observation_end": utc(group["last_seen"]),
                "public_post_date": utc(group["first_seen"]),
                "primary_finding": best["id"],
                "finding_ids": [event["id"] for event in group["events"]],
                "kinds": sorted({event["kind"] for event in group["events"]}),
                "sources": sorted(
                    {event["evidence"]["historical_corpus"]["source"] for event in group["events"]}
                ),
                "score": best["selection"]["score"],
                "review_required": True,
                "review_status": "unreviewed",
                "actual_released_at": None,
                "grouping_policy": "Same address with overlapping finding intervals or gaps no greater than ten minutes; not a verified complete flight.",
            }
            database.execute(
                "INSERT INTO episodes VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    group["hex"],
                    group["first_seen"],
                    group["last_seen"],
                    best["id"],
                    best["selection"]["score"],
                    "unreviewed",
                    utc(),
                    encoded(body),
                ),
            )
    return summary(destination)


def assess_saved_journal(destination):
    """Rescore retained journal leads without treating coarse rows as new flights."""
    from . import selection

    with connect(Path(destination) / "corpus.sqlite3") as database:
        database.execute("DELETE FROM journal_assessments")
        for row in database.execute("SELECT source_id,content FROM journal_records"):
            event = json.loads(row["content"])
            assessment = selection.assess(event, {})
            assessment.update(
                assessed_at=utc(),
                review_status="unreviewed",
                scope="Saved journal row; may span multiple observations and is not counted as a new deduplicated episode.",
            )
            database.execute(
                "INSERT INTO journal_assessments VALUES (?,?,?)",
                (row["source_id"], assessment["score"], encoded(assessment)),
            )


def bounded_bytes(raw, limit=MAX_TRACE_BYTES):
    """Decompress gzip input with a fixed bound; plain input passes through."""
    if raw[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as handle:
            raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Decompressed archive input exceeds its bound")
    return raw


def _advisory_report(report):
    """One advisory report through the field allowlists: no receiver-relative, owner or new field."""
    indexed = {key: report[key] for key in ADVISORY_REPORT_FIELDS if key in report}
    indexed["acas_ra"] = {key: value for key, value in report["acas_ra"].items() if key in ACAS_RAW_FIELDS}
    return indexed


def index_advisories(destination, manifest):
    """Index readsb's experimental raw advisory reports without incident claims.

    readsb compresses completed days, so plain and gzip JSON reports are both
    read, with bounded decompression, and a report present in several files is
    indexed once. CSV files are companion exports, not additional reports; any
    other advisory file is an error. Only allowlisted aircraft and acas_ra fields
    are indexed, and reports at or after the analysis cutoff are left out. An
    export still open at the cutoff loses its unterminated last line, which is
    recorded (see advisory_lines). No advisory is promoted to a public event here.
    """
    errors, rows, dropped = [], {}, []
    end, cutoffs = analysis_end(manifest), export_cutoffs(manifest)
    for item in manifest["files"]:
        if item["format"] != "acas" or item["relative"].endswith(ACAS_COMPANIONS):
            continue
        path = Path(destination) / "inputs" / "objects" / item["object"]
        try:
            if not item["relative"].endswith(ACAS_REPORTS):
                raise ValueError("Unsupported advisory file type")
            raw = path.read_bytes()
            if len(raw) > MAX_TRACE_BYTES or sha256(raw) != item["sha256"]:
                raise ValueError("Advisory input bound or hash check failed")
            lines, unterminated = advisory_lines(raw, item, cutoffs)
            for line in lines:
                if not line.strip():
                    continue
                report = json.loads(line)
                advisory = report.get("acas_ra", {})
                stamp = number(advisory.get("unix_timestamp"))
                address, payload = report.get("hex"), advisory.get("full_bytes")
                if (
                    stamp is None
                    or not isinstance(address, str)
                    or not re.fullmatch(r"~?[0-9a-fA-F]{6}", address)
                    or not isinstance(payload, str)
                    or not payload
                ):
                    raise ValueError("Unsupported advisory record")
                if stamp >= end:
                    continue
                identifier = digest([address.lower(), stamp, payload])
                rows[identifier] = {
                    "id": identifier,
                    "observed_at": utc(stamp),
                    "raw_report": _advisory_report(report),
                    "review_status": "unreviewed",
                    "limitation": TCAS_LIMITATIONS[0],
                }
            if unterminated:
                dropped.append({"object": item["object"], "relative": item["relative"]})
        except (*ARCHIVE_ERRORS, TypeError, AttributeError) as error:
            errors.append({"object": item["object"], "error_type": error_type(error)})
    with connect(Path(destination) / "corpus.sqlite3") as database:
        database.execute("DELETE FROM advisories")
        database.executemany(
            "INSERT INTO advisories VALUES (?,?,?)",
            ((key, value["observed_at"][:10], encoded(value)) for key, value in rows.items()),
        )
        database.execute("INSERT OR REPLACE INTO meta VALUES ('advisory_errors',?)", (encoded(errors),))
        database.execute(
            "INSERT OR REPLACE INTO meta VALUES ('advisory_unterminated_lines',?)", (encoded(dropped),)
        )


def _advisory_events(database):
    """Reports from one aircraft about one threat, at most 60 s apart, are one advisory."""
    reports = []
    for (content,) in database.execute("SELECT content FROM advisories"):
        item = json.loads(content)
        report = item["raw_report"]
        advisory = report["acas_ra"]
        threat = str(advisory.get("threat_id_hex") or "").lower()
        reports.append(
            (
                report["hex"].lower(),
                threat if re.fullmatch(r"[0-9a-f]{6}", threat) else None,
                number(advisory["unix_timestamp"]),
                item["id"],
                advisory,
            )
        )
    events = []
    for own, threat, stamp, identifier, advisory in sorted(reports, key=lambda r: (r[0], r[1] or "", r[2])):
        event = events[-1] if events else None
        if event is None or (event["own"], event["threat"]) != (own, threat) or stamp - event["last"] > 60:
            event = {"own": own, "threat": threat, "first": stamp, "last": stamp, "reports": []}
            events.append(event)
        event["last"] = stamp
        event["reports"].append((stamp, identifier, advisory))
    return events


def _advisory_inputs(manifest, windows):
    """The trace files of the advisory aircraft and the compact files covering their windows."""
    days = {utc(stamp)[:10] for _, start, end in windows for stamp in (start, end)}
    names = tuple(f"trace_full_{address}.json" for address in {address for address, _, _ in windows})
    for row in manifest["files"]:
        if row["day"] not in days:
            continue
        if row["format"] == "traces" and row["relative"].endswith(names):
            yield row
        elif row["format"] == "heatmap":
            # readsb names each compact file after its half hour of the day (00-47).
            match = re.fullmatch(r"(\d\d)\.bin\.ttf", row["relative"].rsplit("/", 1)[-1])
            begin = day_stamp(row["day"]) + int(match[1]) * 1800 if match else None
            if begin is None or any(start < begin + 1800 and end >= begin for _, start, end in windows):
                yield row


def _advisory_tracks(destination, manifest, windows):
    """Archived positions of the advisory aircraft inside their +/-60-s windows."""
    spans, cutoff = defaultdict(list), analysis_end(manifest)
    for address, start, end in windows:
        spans[address].append((start, end))
    grouped, errors = {"heatmap": defaultdict(dict), "traces": defaultdict(dict)}, []
    for row in _advisory_inputs(manifest, windows):
        try:
            raw = frozen_input(destination, row)
            decoder = replay_points if row["format"] == "heatmap" else trace_points
            for address, point in decoder(raw):
                if point["ts"] >= cutoff or not any(
                    start <= point["ts"] <= end for start, end in spans.get(address, ())
                ):
                    continue
                point["archive"] = row["relative"] + "@" + row["sha256"]
                point["archives"] = [point["archive"]]
                collect_reading(grouped[row["format"]][address], point)
        except (*ARCHIVE_ERRORS, TypeError) as error:
            errors.append({"object": row["object"], "error_type": error_type(error)})
    return {source: prepared_tracks(points)[0] for source, points in grouped.items()}, errors


def _reported_rate(point):
    for field in ("baro_rate", "geom_rate"):
        if (value := number(point.get(field))) is not None:
            return value
    return None


def _vertical_response(points, onset):
    """Median reported rate in the minute before the first report and the minute after.

    Without reported rates (compact archives) the rate is derived from the
    altitude change across each minute. Either way this records whether the
    rate changed, not why it changed.
    """
    before = [p for p in points if onset - RA_WINDOW_SECONDS <= p["ts"] < onset]
    after = [p for p in points if onset < p["ts"] <= onset + RA_WINDOW_SECONDS]
    rates = [[rate for p in side if (rate := _reported_rate(p)) is not None] for side in (before, after)]
    source = "reported"
    if not all(rates):
        source, rates = "altitude_derived", []
        for side in (before, after):
            heights = [(p["ts"], number(p.get("archive_alt"))) for p in side]
            heights = [(stamp, value) for stamp, value in heights if value is not None]
            if len(heights) < 2 or heights[-1][0] - heights[0][0] < 10:
                return {"rate_source": None, "rate_change_followed": None}
            rates.append([(heights[-1][1] - heights[0][1]) * 60 / (heights[-1][0] - heights[0][0])])
    first, second = (statistics.median(values) for values in rates)
    return {
        "rate_source": source,
        "rate_before_fpm": round(first, 1),
        "rate_after_fpm": round(second, 1),
        "response_rate_change_fpm": round(second - first, 1),
        "rate_change_followed": abs(second - first) >= RA_RESPONSE_FPM,
        "threshold_fpm": RA_RESPONSE_FPM,
    }


def _closest_approach(tracks, own, threat, start, end):
    """The pair's closest approach, measured as the tcas_ra finding measures it, with its formats."""
    (own_format, own_points), (threat_format, threat_points) = (
        _window_points(tracks, address, start, end) for address in (own, threat)
    )
    approach = maneuvers._closest_approach(own_points, threat_points, start, end)
    if approach is None:
        return None
    horizontal, vertical, _ = approach
    return {
        "min_separation_nm": round(horizontal, 3),
        "vertical_separation_ft": None if vertical is None else round(vertical),
        "altitude_reference": None if vertical is None else "alt_baro",
        # A compact format's fixes are interpolated across up to 30 s.
        "formats": [own_format, threat_format],
    }


def _lead_track(tracks, address, start, end, onset):
    """Allowlisted archived samples for one aircraft; no receiver-relative fields."""
    keys = (
        "ts",
        "source",
        "lat",
        "lon",
        "gs",
        "archive_alt",
        "alt_baro",
        "alt_geom",
        "baro_rate",
        "geom_rate",
    )
    result = {}
    for source, grouped in tracks.items():
        points = [p for p in grouped.get(address, []) if start <= p["ts"] <= end]
        result[source] = [{key: p[key] for key in keys if key in p} for p in points[:RA_SAMPLE_LIMIT]]
    result["vertical_response"] = _vertical_response(_window_points(tracks, address, start, end)[1], onset)
    return result


def _window_points(tracks, address, start, end):
    """(format, fixes): trace fixes when retained (they carry reported rates), else compact ones."""
    for source in ("traces", "heatmap"):
        points = [p for p in tracks[source].get(address, []) if start <= p["ts"] <= end]
        if points:
            return source, points
    return None, []


def advisory_leads(destination, manifest, *, code_sha256=None):
    """Private review leads joining each advisory with both aircraft's archived tracks.

    These are experimental decoder outputs: never findings, episodes or
    publication candidates. A resume reuses the saved leads while the
    advisories, the input files they read and the code are unchanged.
    """
    database_path = Path(destination) / "corpus.sqlite3"
    with connect(database_path) as database:
        database.execute(
            "CREATE TABLE IF NOT EXISTS advisory_leads(id TEXT PRIMARY KEY,day TEXT NOT NULL,content TEXT NOT NULL)"
        )
        events = _advisory_events(database)
        saved = database.execute("SELECT value FROM meta WHERE key='advisory_lead_dependencies'").fetchone()
    windows = [
        (address, event["first"] - RA_WINDOW_SECONDS, event["last"] + RA_WINDOW_SECONDS)
        for event in events
        for address in (event["own"], event["threat"])
        if address
    ]
    dependencies = digest(
        [
            [[report[1] for report in event["reports"]] for event in events],
            sorted([row["relative"], row["sha256"]] for row in _advisory_inputs(manifest, windows)),
            code_sha256 or code_hashes(),
        ]
    )
    if saved and json.loads(saved[0]) == dependencies:
        with connect(database_path) as database:
            return [
                json.loads(row[0])
                for row in database.execute("SELECT content FROM advisory_leads ORDER BY id")
            ]
    tracks, errors = _advisory_tracks(destination, manifest, windows) if windows else ({}, [])
    leads = []
    for event in events:
        start, end = event["first"] - RA_WINDOW_SECONDS, event["last"] + RA_WINDOW_SECONDS
        sequence = []
        for stamp, _, advisory in event["reports"]:
            text = (advisory.get("advisory"), advisory.get("advisory_complement"))
            if not sequence or sequence[-1]["text"] != text:
                sequence.append({"text": text, "first": stamp, "reports": 0})
            sequence[-1].update(last=stamp, reports=sequence[-1]["reports"] + 1)
        own = _lead_track(tracks, event["own"], start, end, event["first"])
        threat = _lead_track(tracks, event["threat"], start, end, event["first"]) if event["threat"] else None
        identifier = digest(["acas-ra-lead-v1", event["own"], event["threat"], event["first"]])
        leads.append(
            {
                "id": identifier,
                "kind": "acas_resolution_advisory",
                "own_hex": event["own"],
                "threat_hex": event["threat"],
                "first_report_at": utc(event["first"]),
                "last_report_at": utc(event["last"]),
                "report_count": len(event["reports"]),
                "advisory_ids": [report_id for _, report_id, _ in event["reports"]],
                "advisories": [
                    {
                        "advisory": item["text"][0],
                        "complement": item["text"][1],
                        "first_at": utc(item["first"]),
                        "last_at": utc(item["last"]),
                        "reports": item["reports"],
                    }
                    for item in sequence
                ],
                "window_seconds": RA_WINDOW_SECONDS,
                "own": own,
                "threat": threat,
                "closest_approach": _closest_approach(tracks, event["own"], event["threat"], start, end)
                if event["threat"]
                else None,
                "input_errors": errors,
                "review_status": "unreviewed",
                "public_export_eligible": False,
                "story_eligible": False,
                "limitations": TCAS_LIMITATIONS[:3],
            }
        )
    with connect(database_path) as database:
        database.execute("DELETE FROM advisory_leads")
        database.executemany(
            "INSERT INTO advisory_leads VALUES (?,?,?)",
            ((lead["id"], lead["first_report_at"][:10], encoded(lead)) for lead in leads),
        )
        database.execute(
            "INSERT OR REPLACE INTO meta VALUES ('advisory_lead_dependencies',?)", (encoded(dependencies),)
        )
    return leads


def _calendar(first, last):
    day = datetime.fromisoformat(first)
    while (current := day.date().isoformat()) <= last:
        yield current
        day += timedelta(days=1)


def recurrence_leads(
    destination,
    *,
    warmup_days=RECURRENCE_WARMUP_DAYS,
    absence_days=RECURRENCE_ABSENCE_DAYS,
    coverage_seconds=COVERAGE_SECONDS,
    min_retained_days=RECURRENCE_MIN_RETAINED_DAYS,
):
    """Private first-retained-sighting and return-after-absence leads from aircraft_days.

    Coverage is a completed analysis day with at least an hour of recorded
    archive slices. Any other day is a gap: a return never spans one, and a
    first sighting on the day after one is withheld because the aircraft may
    have been present during the gap. Never findings, episodes or publication
    candidates.
    """
    destination = Path(destination)
    with connect(destination / "corpus.sqlite3") as database:
        covered = coverage_days(database, coverage_seconds)
        presence = defaultdict(dict)
        for address, day, content in database.execute("SELECT hex,day,content FROM aircraft_days"):
            if not address.startswith("~"):
                presence[address][day] = sorted(json.loads(content))
    leads = []
    if covered:
        coverage, ordinal = set(covered), {day: index for index, day in enumerate(covered)}
        seen = [day for days in presence.values() for day in days]
        calendar = list(_calendar(min([covered[0], *seen]), max([covered[-1], *seen])))
        position = {day: index for index, day in enumerate(calendar)}
        # gaps_before[i]: missing or partial days among calendar[:i].
        gaps_before = [0]
        for day in calendar:
            gaps_before.append(gaps_before[-1] + (day not in coverage))
        gaps = [day for day in calendar if day not in coverage]
        metadata = metadata_for(destination / "corpus.sqlite3", presence)

        def retained_first(day):
            # Enough earlier coverage, and the previous calendar day is not a gap.
            previous = utc(day_stamp(day) - 86400)[:10]
            return day in ordinal and ordinal[day] >= warmup_days and previous in coverage

        def context(day):
            return {
                "retained_since": covered[0],
                "covered_days_before": bisect.bisect_left(covered, day),
                "gap_days_before": gaps[: bisect.bisect_left(gaps, day)],
            }

        type_days = defaultdict(lambda: defaultdict(list))
        for address in sorted(presence):
            days = sorted(presence[address])
            identity = {key: metadata.get(address, {}).get(key, "") for key in ("r", "t")}
            # Group designators name a class of aircraft, not a type that can be "first".
            if identity["t"] and not rules.group_designator(identity["t"]):
                for day in days:
                    type_days[identity["t"]][day].append(address)
            profile = {
                "first_retained_day": days[0],
                "last_retained_day": days[-1],
                "retained_days": len(days),
            }
            if len(days) >= min_retained_days and retained_first(days[0]):
                leads.append(
                    {
                        "kind": "first_retained_sighting",
                        "hex": address,
                        "day": days[0],
                        **identity,
                        **profile,
                        "sources": presence[address][days[0]],
                        **context(days[0]),
                    }
                )
            for previous, day in zip(days, days[1:], strict=False):
                start, end = position[previous] + 1, position[day]
                # Every day of the absence was analyzed; a gap could hide a visit.
                if end - start < absence_days or gaps_before[end] != gaps_before[start]:
                    continue
                leads.append(
                    {
                        "kind": "returned_after_absence",
                        "hex": address,
                        "day": day,
                        "previous_day": previous,
                        "absence_covered_days": end - start,
                        **identity,
                        **profile,
                        "sources": presence[address][day],
                        **context(day),
                    }
                )
        for typecode, days in sorted(type_days.items()):
            first = min(days)
            if retained_first(first):
                leads.append(
                    {
                        "kind": "first_retained_type_sighting",
                        "t": typecode,
                        "day": first,
                        "addresses": sorted(days[first])[:20],
                        "address_count": len(days[first]),
                        "type_days": len(days),
                        **context(first),
                    }
                )
    for lead in leads:
        lead.update(
            id=digest(["recurrence-lead-v1", lead["kind"], lead.get("hex", lead.get("t")), lead["day"]]),
            review_status="unreviewed",
            public_export_eligible=False,
            story_eligible=False,
            limitations=RECURRENCE_LIMITATIONS,
        )
    with connect(destination / "corpus.sqlite3") as database:
        database.execute(
            "CREATE TABLE IF NOT EXISTS recurrence_leads(id TEXT PRIMARY KEY,kind TEXT NOT NULL,day TEXT NOT NULL,content TEXT NOT NULL)"
        )
        database.execute("DELETE FROM recurrence_leads")
        database.executemany(
            "INSERT INTO recurrence_leads VALUES (?,?,?,?)",
            ((lead["id"], lead["kind"], lead["day"], encoded(lead)) for lead in leads),
        )
    return leads


def summary(destination, *, persist=True, settled=False):
    """Corpus counts. ``settled`` readers (comparisons) never touch the database's side files;
    others read it like any SQLite reader, which is safe while an analysis is running and
    may leave an empty corpus.sqlite3-wal and a corpus.sqlite3-shm (mode 0600) behind."""
    destination = Path(destination)
    manifest = json.loads((destination / "manifest.json").read_text())
    days = sorted({row["day"] for row in manifest["files"] if row["format"] in {"heatmap", "traces"}})
    path, cross_day = destination / "corpus.sqlite3", destination / CROSS_DAY_ARTIFACT
    uri = read_only_uri(path, allow_wal=False) if settled else shared_uri(path)
    with closing(sqlite3.connect(uri, uri=True)) as database:
        counts = dict(database.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status"))
        positions, slice_seconds, errors, stale = Counter(), 0, 0, 0
        for day, status, content in database.execute("SELECT day,status,content FROM jobs"):
            job = json.loads(content)
            positions.update(job["counts"])
            errors += len(job["errors"])
            slice_seconds += _slice_seconds(job)
            stored = job.get("dependencies")
            # A receipt made before its inputs, metadata or cutoff changed is not complete work.
            if status == "complete" and stored is not None:
                stale += {key: stored.get(key) for key in INPUT_DEPENDENCIES} != input_dependencies(
                    manifest, day
                )
        missing = []
        if days:
            stamp = datetime.fromisoformat(days[0])
            while stamp.date().isoformat() <= days[-1]:
                if stamp.date().isoformat() not in days:
                    missing.append(stamp.date().isoformat())
                stamp += timedelta(days=1)
        advisory_errors = database.execute("SELECT value FROM meta WHERE key='advisory_errors'").fetchone()
        unterminated = database.execute(
            "SELECT value FROM meta WHERE key='advisory_unterminated_lines'"
        ).fetchone()
        result = {
            "captured_at": manifest["captured_at"],
            "analysis_cutoff": manifest.get("analysis_cutoff") or manifest["captured_at"],
            "first_day": days[0] if days else None,
            "last_day": days[-1] if days else None,
            "input_unique_versions": len(manifest["files"]),
            "input_distinct_paths": manifest["distinct_paths"],
            "paths_with_multiple_versions": manifest["paths_with_multiple_versions"],
            "capture_failures": len(manifest["capture_failures"]),
            "watchlist_commit": (manifest.get("watchlist") or {}).get("source_commit"),
            "available_dates": len(days),
            "jobs": counts,
            "remaining_dates": len(days) - counts.get("complete", 0) + stale,
            "missing_archive_dates": missing,
            "archive_slice_seconds": round(slice_seconds, 3),
            "archive_slice_scope": "Recorded compact archive intervals; not proof of continuous receiver operation or complete traffic coverage.",
            "analysis_file_errors": errors,
            "positions": dict(positions),
            "findings": database.execute("SELECT COUNT(*) FROM findings").fetchone()[0],
            "findings_by_kind": dict(database.execute("SELECT kind,COUNT(*) FROM findings GROUP BY kind")),
            "episodes": database.execute("SELECT COUNT(*) FROM episodes").fetchone()[0],
            "review_candidates_score_65": database.execute(
                "SELECT COUNT(*) FROM episodes WHERE score>=65"
            ).fetchone()[0],
            "saved_journal_records": database.execute("SELECT COUNT(*) FROM journal_records").fetchone()[0],
            "saved_journal_assessments": database.execute(
                "SELECT COUNT(*) FROM journal_assessments"
            ).fetchone()[0],
            "unique_aircraft_addresses": database.execute(
                "SELECT COUNT(DISTINCT hex) FROM aircraft_days"
            ).fetchone()[0],
            "experimental_advisory_reports": database.execute("SELECT COUNT(*) FROM advisories").fetchone()[
                0
            ],
            "advisory_file_errors": len(json.loads(advisory_errors[0])) if advisory_errors else None,
            # Exports still open at the cutoff whose unterminated last line was dropped.
            "advisory_unterminated_lines": len(json.loads(unterminated[0])) if unterminated else None,
            # Completion records this hash; it binds the cross-day findings (cross_day_artifact).
            "cross_day_sha256": sha256(cross_day.read_bytes()) if cross_day.is_file() else None,
        }
        # Corpora analyzed before these private lead tables existed keep their summaries.
        for table, key in PRIVATE_LEAD_TABLES:
            if database.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone():
                result[key] = database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if persist:
        write_private(destination / "summary.json", result)
    return result


# Runs a corpus's own pinned summary(); code pinned before settled reads existed reads normally.
PINNED_SUMMARY = """import inspect, json, os, sys
sys.dont_write_bytecode = True
os.environ["WTF_ADSB_REFERENCE_DIR"] = os.path.join(sys.argv[1], "data")
import types
package = types.ModuleType("wtf_adsb_frozen_analysis")
package.__path__ = [sys.argv[1]]
sys.modules[package.__name__] = package
from wtf_adsb_frozen_analysis import history_backfill
settled = sys.argv[3] == "settled"
if "settled" in inspect.signature(history_backfill.summary).parameters:
    summary = history_backfill.summary(sys.argv[2], persist=False, settled=settled)
else:
    summary = history_backfill.summary(sys.argv[2], persist=False)
print(json.dumps(summary))
"""


def pinned_summary(destination, *, settled=True):
    """(summary, "pinned" | "working_tree"): the summary from the corpus's own pinned code, else this checkout.

    Edits to the working tree after pinning, such as a changed input
    definition, then cannot make a completed corpus look incomplete. A pinned
    copy that no longer matches its recorded hashes is refused; pinned code that
    cannot run falls back before it opens the database. ``settled`` reads refuse
    WAL content and leave no side file (see summary).
    """
    pinned = Path(destination) / PINNED_SOURCE
    if pinned.is_dir():
        verify_pinned_source(destination)
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    PINNED_SUMMARY,
                    str(pinned),
                    str(destination),
                    "settled" if settled else "shared",
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                timeout=1800,
            )
        except subprocess.TimeoutExpired:
            result = None
        if result is not None and result.returncode == 0:
            return json.loads(result.stdout), "pinned"
    return summary(destination, persist=False, settled=settled), "working_tree"


def checkpoint(destination, attempts=10):
    """Fold the corpus WAL into the database once its writers are done, so settled readers accept it.

    A reader still in a read transaction can hold the WAL; the checkpoint is
    retried for about ten seconds and reports whether it completed.
    """
    with connect(Path(destination) / "corpus.sqlite3") as database:
        for attempt in range(attempts):
            if database.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0:
                return True
            if attempt + 1 < attempts:
                time.sleep(1)
    return False


def run(destination, workers=4, max_days=None):
    destination = Path(destination).resolve()
    pinned = destination / PINNED_SOURCE
    if pinned.exists() and Path(__file__).resolve().parent != pinned.resolve():
        raise ValueError("This corpus pins its analysis code; run its run_frozen.py instead")
    manifest = json.loads((destination / "manifest.json").read_text())
    if manifest.get("schema_version") != VERSION:
        raise ValueError("Unsupported input manifest version")
    if not 1 <= workers <= 8:
        raise ValueError("Use 1–8 workers")
    if max_days is not None and max_days < 1:
        raise ValueError("max_days must be positive")
    # Validate the entire frozen set before skipping completed work on resume.
    # Inputs shared by several snapshots are hashed only once.
    for name in sorted({row["object"] for row in manifest["files"]}):
        if sha256((destination / "inputs" / "objects" / name).read_bytes()) != name:
            raise ValueError("Frozen input hash changed")
    provenance = [
        ("journal.sqlite3", manifest["journal_sha256"]),
        ("aircraft-metadata.csv.gz", manifest["metadata_sha256"]),
        *(
            ("watchlist/" + name, expected)
            for name, expected in (manifest.get("watchlist") or {}).get("files", {}).items()
        ),
    ]
    for name, expected in provenance:
        if sha256((destination / "inputs" / name).read_bytes()) != expected:
            raise ValueError("Frozen provenance input hash changed")
    analysis_path = destination / "analysis.json"
    hashes, versions = code_hashes(), detector_versions()
    with connect(destination / "corpus.sqlite3") as database:
        receipts = database.execute("SELECT day,status,content FROM jobs").fetchall()
        frozen = database.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
    frozen = json.loads(frozen[0]) if frozen else {}
    if any(frozen.get(key) != manifest[key] for key in ("metadata_sha256", "journal_sha256")):
        # The metadata and journal tables are loaded once, at capture; they are never rebuilt.
        raise ValueError("The frozen metadata or journal tables do not match the manifest; use a new corpus")
    if analysis_path.exists():
        recorded = json.loads(analysis_path.read_text())
        if (
            recorded["code_sha256"] != hashes
            or recorded.get("detector_version") != DETECTOR_VERSION
            or recorded.get("detector_versions", versions) != versions
        ):
            raise ValueError(
                "Analysis code changed since the first run; use a new corpus for this detector revision"
            )
    elif receipts:
        # A deleted analysis record must not re-pin earlier day results to new code.
        raise ValueError("Day receipts exist without the analysis record; use a new corpus")
    else:
        write_private(
            analysis_path,
            {
                "started_at": utc(),
                "code_sha256": hashes,
                "detector_version": DETECTOR_VERSION,
                "detector_versions": versions,
            },
        )
    available = {row["day"] for row in manifest["files"] if row["format"] in {"heatmap", "traces"}}
    complete, changed = set(), []
    for row in receipts:
        if row["status"] != "complete":
            continue
        receipt = json.loads(row["content"])
        artifact = destination / "days" / (row["day"] + ".json")
        if sha256(artifact.read_bytes()) != receipt["artifact_sha256"]:
            raise ValueError("Completed day artifact hash changed")
        if row["day"] not in available:
            raise ValueError("A completed day is missing from the input manifest")
        stored, expected = receipt.get("dependencies"), day_dependencies(manifest, row["day"], hashes)
        if stored is None:
            raise ValueError("A completed day receipt predates input pinning; use a new corpus")
        if (stored["code_sha256"], stored["detector_version"]) != (hashes, DETECTOR_VERSION):
            raise ValueError("A completed day came from different analysis code; use a new corpus")
        if stored == expected:
            complete.add(row["day"])
        else:
            # Its inputs, metadata or cutoff changed since it was analyzed.
            changed.append(row["day"])
    if changed:
        # Until analyzed again, their old results stay out of every summary, grouping and lead.
        with connect(destination / "corpus.sqlite3") as database:
            for day in changed:
                database.execute("UPDATE jobs SET status='stale' WHERE day=?", (day,))
                database.execute("DELETE FROM findings WHERE day=?", (day,))
                database.execute("DELETE FROM aircraft_days WHERE day=?", (day,))
        print(encoded({"reanalyzing_changed_days": changed}), flush=True)
    index_advisories(destination, manifest)
    advisory_leads(destination, manifest, code_sha256=hashes)
    assess_saved_journal(destination)
    days = sorted(available - complete)
    if max_days:
        days = days[:max_days]
    # Days with retained traces take several times longer; starting the largest first
    # shortens the run. Days are independent, so the order never changes a result.
    size = Counter()
    for row in manifest["files"]:
        if row["format"] in {"heatmap", "traces"}:
            size[row["day"]] += row["bytes"]
    days.sort(key=lambda day: (-size[day], day))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(analyze_day, day, destination, code_sha256=hashes): day for day in days}
        for future in as_completed(jobs):
            result = future.result()
            import_day(destination, result)
            print(
                encoded(
                    {
                        "completed_day": result["day"],
                        "positions": result["counts"],
                        "errors": len(result["errors"]),
                    }
                ),
                flush=True,
            )
    # The sequential cross-day phase sees every imported day, whatever the worker count.
    recurrence_leads(destination)
    cross_day_findings(destination, code_sha256=hashes)
    result = assemble_episodes(destination)
    if not checkpoint(destination):
        print(
            encoded({"wal_checkpoint": "busy; close the corpus's readers, then run checkpoint"}), flush=True
        )
    return result
