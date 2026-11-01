"""Journal transfer and process checks without starting input plugins."""

import fcntl
import hashlib
import json
import math
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .runtime import INSTANCE, configuration, state_lock
from .storage import Store

JOURNAL_TABLES = {"meta", "aircraft", "daily", "coverage", "events", "track_samples", "telemetry_samples"}


def write_json(path, value):
    """Replace a private control file atomically on its own filesystem."""
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, allow_nan=False, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def native_journal(config, pipeline=None):
    candidates = [
        item
        for item in config["pipelines"]
        if item["input"]["plugin"] == "wtf_adsb.receiver_service"
        and (pipeline is None or item["name"] == pipeline)
    ]
    if len(candidates) != 1:
        raise ValueError("Select exactly one native receiver pipeline")
    selected = candidates[0]
    name = selected["name"]
    instance = selected["input"].get("name", "wtf_adsb-receiver_service")
    if not all(isinstance(item, str) and INSTANCE.fullmatch(item) for item in (name, instance)):
        raise ValueError("Invalid native pipeline or instance name")
    return Path(config["state_directory"]) / "plugins" / name / instance / "journal.sqlite3"


def journal_info(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Journal integrity check failed")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not JOURNAL_TABLES <= tables:
            raise ValueError("Journal is missing required analysis tables")
        checkpoint = db.execute("SELECT value FROM meta WHERE key='last_receiver_timestamp'").fetchone()
        return {
            "tables": sorted(tables),
            "events": db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "last_receiver_timestamp": json.loads(checkpoint[0]) if checkpoint else None,
        }


def backup_journal(source, destination):
    """Copy committed SQLite state, including WAL contents, into a new file."""
    source, destination = Path(source), Path(destination)
    if source.is_symlink() or destination.is_symlink():
        raise ValueError("Journal transfer does not follow symbolic links")
    if not source.is_file() or source.resolve() == destination.resolve():
        raise ValueError("Journal source must be an existing, distinct file")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Exclusive creation also refuses hard-link aliases and prior partial copies.
    with destination.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
    with (
        closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original,
        closing(sqlite3.connect(destination)) as copied,
    ):
        original.backup(copied)
        copied.execute("PRAGMA journal_mode=DELETE")
    info = journal_info(destination)
    with destination.open("rb") as stream:
        info["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    return info


def migrate_journal(source, config_path, *, pipeline=None):
    config = configuration(config_path)
    directory = Path(config["state_directory"])
    destination = native_journal(config, pipeline)
    if any(path.is_symlink() for path in (directory, destination, *destination.parents)):
        raise ValueError("Journal destination cannot traverse symbolic links")
    with state_lock(directory):
        if any(path.name != "engine.lock" for path in directory.iterdir()):
            raise ValueError("Journal migration requires fresh engine state")
        marker = directory / "migration-in-progress.json"
        write_json(marker, {"version": 1, "started_at": time.time()})
        info = backup_journal(source, destination)
        receipt = {
            "version": 1,
            "captured_at": time.time(),
            "journal": str(destination.relative_to(directory)),
            **info,
        }
        write_json(directory / "migration.json", receipt)
        marker.unlink()
        descriptor = os.open(directory, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return receipt


def health(config_path, *, readiness=False, max_age=300, now=None):
    """Check saved progress only; never initialize plugins or make requests."""
    if not math.isfinite(max_age) or max_age <= 0:
        raise ValueError("Health age must be positive and finite")
    config = configuration(config_path)
    directory = Path(config["state_directory"])
    now = time.time() if now is None else now
    if readiness:
        state = Store.inspect(directory / "engine.sqlite3")
        status = state["health"]
        fresh = all(
            type(status.get(key)) in (float, int) and 0 <= now - status[key] <= max_age
            for key in ("last_receiver_poll", "analysis_watermark")
        )
        delivered = bool(state["outputs"]) and all(
            item["cursor"] == state["head_sequence"] and item["status"] == "ready"
            for item in state["outputs"]
        )
        return {
            "ready": bool(
                status["ready"] and status["complete"] and status["mode"] == "live" and fresh and delivered
            ),
            **state,
        }
    heartbeat = json.loads((directory / "heartbeat.json").read_text())
    with (directory / "engine.lock").open("r") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            running = True
        else:
            fcntl.flock(lock, fcntl.LOCK_UN)
            running = False
    stamp = heartbeat.get("updated_at")
    fresh = type(stamp) in (int, float) and 0 <= now - stamp <= max_age
    if not heartbeat.get("background_collection", False):
        completed = heartbeat.get("last_poll_completed", stamp)
        fresh = (
            fresh
            and type(completed) in (int, float)
            and 0 <= now - completed <= max(max_age, config.get("poll_interval_seconds", 10) + 60)
        )
    return {"alive": bool(running and fresh and heartbeat.get("inputs_alive")), "updated_at": stamp}
