"""Receiver/replay inputs, lossless JSON formatting, and an atomic file sink."""

import hashlib
import os
import sqlite3
import urllib.request
from contextlib import closing
from pathlib import Path

from .. import wire
from ..plugin_api.v1 import InputBatch, Lifecycle, PluginSpec


def options(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


class ReceiverInput(Lifecycle):
    spec = PluginSpec(
        "wtf_adsb.receiver_json",
        "inputs",
        config_schema=options(
            {
                "url": {"type": "string", "pattern": "^https?://"},
                "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 60},
                "max_age_seconds": {"type": "number", "minimum": 1, "maximum": 3600},
            },
            ("url",),
        ),
        provides=("receiver_snapshot",),
    )
    mode = "live"

    def __init__(self, settings):
        self.settings = settings

    def poll(self, checkpoint, context):
        request = urllib.request.Request(self.settings["url"], headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=self.settings.get("timeout_seconds", 10)) as response:
            data = response.read(wire.MAX_BYTES + 1)
        value = wire.loads(data)
        stamp = value.get("now") if isinstance(value, dict) else None
        if type(stamp) not in (int, float) or not isinstance(value.get("aircraft"), list):
            raise ValueError("Receiver response lacks a timestamp and aircraft array")
        if not 0 <= context.now - stamp <= self.settings.get("max_age_seconds", 120):
            raise ValueError("Receiver response is stale or from the future")
        return InputBatch(({"type": "receiver_snapshot", "snapshot": value},), stamp, stamp)


class ReplayInput(Lifecycle):
    spec = PluginSpec(
        "wtf_adsb.json_replay",
        "inputs",
        config_schema=options(
            {
                "path": {"type": "string", "minLength": 1},
                "records_per_poll": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            ("path",),
        ),
        provides=("receiver_snapshot",),
    )
    mode = "replay"

    def __init__(self, settings):
        self.settings = settings

    def poll(self, checkpoint, context):
        path = Path(self.settings["path"])
        offset = checkpoint["offset"] if checkpoint else 0
        watermark = checkpoint.get("watermark") if checkpoint else None
        records = []
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            identity = hashlib.file_digest(stream, "sha256").hexdigest()
            if checkpoint and checkpoint["sha256"] != identity:
                raise ValueError("Replay input changed; use a new input instance/state directory")
            stream.seek(offset)
            for _ in range(self.settings.get("records_per_poll", 100)):
                line = stream.readline(wire.MAX_BYTES + 1)
                if not line:
                    break
                value = wire.loads(line)
                stamp = value.get("now") if isinstance(value, dict) else None
                if type(stamp) not in (int, float) or not isinstance(value.get("aircraft"), list):
                    raise ValueError("Replay requires one receiver snapshot per line")
                if watermark is not None and stamp <= watermark:
                    raise ValueError("Replay timestamps must increase")
                records.append({"type": "receiver_snapshot", "snapshot": value})
                watermark = stamp
            offset = stream.tell()
            complete = not bool(stream.read(1))
            after = os.fstat(stream.fileno())
            if any(
                getattr(before, field) != getattr(after, field)
                for field in ("st_size", "st_mtime_ns", "st_ctime_ns")
            ):
                raise ValueError("Replay input changed while reading; keep recordings immutable")
        return InputBatch(
            tuple(records),
            {"sha256": identity, "offset": offset, "watermark": watermark},
            watermark,
            complete,
        )


class JournalInput(Lifecycle):
    """Read-only bridge for a running legacy collector during migration."""

    spec = PluginSpec(
        "wtf_adsb.journal",
        "inputs",
        config_schema=options(
            {
                "path": {"type": "string", "minLength": 1},
                "mode": {"enum": ["live", "replay"]},
            },
            ("path",),
        ),
        provides=("legacy_journal",),
    )

    def __init__(self, settings):
        self.path = Path(settings["path"]).resolve()
        self.mode = settings.get("mode", "live")

    def poll(self, checkpoint, context):
        from ..analysis.journal_health import watermark as journal_watermark

        uri = self.path.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            db.execute("BEGIN")
            watermark = journal_watermark(db, self.mode)
        return InputBatch(({"type": "legacy_journal", "path": str(self.path)},), watermark, watermark)


class JsonFormat(Lifecycle):
    spec = PluginSpec("wtf_adsb.json", "formats")

    def __init__(self, settings):
        pass

    def encode(self, batch, context):
        return wire.canonical(batch) + b"\n"


class FileOutput(Lifecycle):
    spec = PluginSpec(
        "wtf_adsb.files",
        "outputs",
        config_schema=options(
            {
                "directory": {"type": "string", "minLength": 1},
            },
            ("directory",),
        ),
    )

    def __init__(self, settings):
        self.directory = Path(settings["directory"])

    def deliver(self, artifact, manifest, context):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.directory / "manifest.json"
        previous = (
            wire.validate_manifest(wire.loads(wire.read_file(self.directory, "manifest.json")))
            if target.exists()
            else None
        )
        if previous and any(previous[key] != manifest[key] for key in ("stream_id", "generation")):
            raise ValueError(
                "Output directory belongs to another stream/generation; use an empty destination"
            )
        entries = previous["batches"] if previous else []
        cursor = previous["head_sequence"] if previous else 0
        if artifact:
            batch = wire.validate_batch(wire.loads(artifact), allow_synthetic=True)
            if any(batch[key] != manifest[key] for key in ("stream_id", "generation")):
                raise ValueError("Batch belongs to another stream")
            start, end = batch["sequence_start"], batch["sequence_end"]
            relative = f"batches/{start:020d}-{end:020d}.json"
            entry = {
                "path": relative,
                "sha256": hashlib.sha256(artifact).hexdigest(),
                "bytes": len(artifact),
                "sequence_start": start,
                "sequence_end": end,
            }
            if start <= cursor:
                if entry not in entries or wire.read_file(self.directory, relative) != artifact:
                    raise ValueError("An acknowledged batch changed")
            elif start == cursor + 1:
                if (self.directory / "batches").is_symlink():
                    raise ValueError("Output directory cannot contain symlinks")
                wire.atomic_bytes(self.directory / relative, artifact)
                entries.append(entry)
                cursor = end
            else:
                raise ValueError("Output contains a sequence gap")
        health = {
            **manifest["health"],
            "complete": manifest["health"]["complete"] and cursor == manifest["head_sequence"],
        }
        result = {
            **manifest,
            "head_sequence": cursor,
            "first_sequence": 1,
            "health": health,
            "batches": entries,
        }
        wire.validate_manifest(result)
        encoded = wire.canonical(result) + b"\n"
        if len(encoded) > wire.MAX_BYTES:
            raise ValueError("Manifest limit reached; this release does not prune delivery history")
        wire.atomic_bytes(target, encoded)
