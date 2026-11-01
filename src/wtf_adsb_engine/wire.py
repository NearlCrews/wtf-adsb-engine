"""Version 1 transport validation, shared as source with independent consumers.

No imports of collection, plugins, publisher policy, or engine storage.
"""

import hashlib
import json
import math
import os
import re
import tempfile
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from threading import Lock

from jsonschema import Draft202012Validator, ValidationError

# The contract version this copy writes, and the versions it reads (1.1.0 adds context.type_norms).
VERSION = "1.1.0"
SUPPORTED = frozenset({"1.0.0", "1.1.0"})
PROFILE = "wtf-adsb.collection.v1"
NAMESPACE = r"[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+"
SEMVER = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
MAX_BYTES = 8 * 1024 * 1024
MAX_EVENT_BYTES = 1024 * 1024
_VALIDATED_OBSERVATIONS = OrderedDict()
_VALIDATION_LOCK = Lock()
_VALIDATION_CACHE_LIMIT = 50000
PRIVATE_KEYS = frozenset(
    {
        "r_dst",
        "r_dir",
        "rssi",
        "rr_lat",
        "rr_lon",
        "receiver_lat",
        "receiver_lon",
        "receiver_latitude",
        "receiver_longitude",
        "receiver_coordinates",
        "receiver_location",
        "receiver_marker",
        "receiver_range",
        "receiver_bearing",
        "coverage_rings",
        "archive_reference",
        "archive_references",
        "source_path",
        "database_path",
        "notes",
        "note",
        "explanation",
        "private",
        "password",
        "api_key",
        "token",
    }
)
PRIVATE_TEXT = re.compile(
    r"\.ts\.net\b|\blocalhost\b|/\.private\b|/home/|/opt/docker-compose/|/opt/adsb/"
    r"|https?://(?:127\.|10\.|192\.168\.|172\.(?:1[6-9]|2[0-9]|3[01])\.|169\.254\.|100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.|\[?::1\b)"
    r"|https?://[^/\s]+\.(?:local|internal|lan)(?:[/:\s]|$)",
    re.I,
)


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def utc(timestamp=None):
    value = (
        datetime.now(timezone.utc) if timestamp is None else datetime.fromtimestamp(timestamp, timezone.utc)
    )
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def instant(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("Expected UTC timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError("Invalid integer")
    return value


def identifier(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("Invalid UUID")
    return value


def keys(value, required, optional=()):
    if (
        not isinstance(value, dict)
        or set(value) - set(required) - set(optional)
        or set(required) - set(value)
    ):
        raise ValueError("Unexpected or missing fields")


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = item
    return value


def _constant(value):
    raise ValueError("Nonfinite JSON constant")


def loads(data):
    if len(data) > MAX_BYTES:
        raise ValueError("JSON exceeds the transport limit")
    return json.loads(data, object_pairs_hook=_object, parse_constant=_constant)


def privacy(value, depth=0):
    if depth > 32:
        raise ValueError("Evidence nesting exceeds the limit")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or key.lower() in PRIVATE_KEYS:
                raise ValueError("Private or invalid evidence field")
            privacy(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 50000:
            raise ValueError("Evidence array exceeds the limit")
        for item in value:
            privacy(item, depth + 1)
    elif isinstance(value, str):
        if len(value) > 20000 or PRIVATE_TEXT.search(value):
            raise ValueError("Private or oversized evidence text")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Nonfinite evidence")
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        raise ValueError("Evidence must be JSON")


@lru_cache(maxsize=3)
def _validator(name):
    """Load the released local contract; received records cannot choose a schema."""
    schema = json.loads(files(__package__).joinpath("contracts", name).read_text())
    Draft202012Validator.check_schema(schema)
    if name == "change-batch-v1.schema.json":
        # Validate each embedded observation exactly once through the public
        # observation validator below, including its semantic/privacy checks.
        # The published batch schema remains complete for other consumers.
        schema["$defs"]["observation"] = {"type": "object"}
    return Draft202012Validator(schema)


def _structure(name, value):
    try:
        _validator(name).validate(value)
    except ValidationError:
        # ValidationError includes the rejected data. Do not leak it in logs.
        raise ValueError("Record does not match the structural contract") from None


def validate_observation(value):
    privacy(value)
    encoded = canonical(value)
    if len(encoded) > MAX_EVENT_BYTES:
        raise ValueError("Observation exceeds the event limit")
    fingerprint = hashlib.sha256(encoded).digest()
    with _VALIDATION_LOCK:
        if fingerprint in _VALIDATED_OBSERVATIONS:
            _VALIDATED_OBSERVATIONS.move_to_end(fingerprint)
            return value
    _structure("observation-v1.schema.json", value)
    keys(value, ("group", "context", "media"), ("extensions",))
    group = value["group"]
    if not isinstance(group, dict) or not isinstance(group.get("events"), list):
        raise ValueError("An observation needs a group of findings")
    identifier(group.get("id"))
    for name in ("first_seen", "last_seen"):
        if type(group.get(name)) not in (int, float) or not math.isfinite(group[name]):
            raise ValueError("Invalid observation interval")
    if group["first_seen"] > group["last_seen"]:
        raise ValueError("Reversed observation interval")
    seen = set()
    for event in group["events"]:
        if not isinstance(event, dict):
            raise ValueError("Invalid finding")
        event_id = identifier(event.get("id"))
        if event_id in seen:
            raise ValueError("Duplicate finding")
        seen.add(event_id)
        if not isinstance(event.get("kind"), str) or not isinstance(event.get("evidence"), dict):
            raise ValueError("Finding lacks typed evidence")
    if not isinstance(value["context"], dict) or not isinstance(value["media"], dict):
        raise ValueError("Invalid observation context")
    norms = value["context"].get("type_norms", {})
    if not isinstance(norms, dict) or not set(norms) <= seen:
        raise ValueError("A type norm names no finding of its group")
    extensions = value.get("extensions", [])
    if not isinstance(extensions, list) or len(extensions) > 64:
        raise ValueError("Invalid extension envelopes")
    names = set()
    for extension in extensions:
        keys(extension, ("namespace", "schema_version", "payload"))
        name = extension["namespace"]
        if not isinstance(name, str) or len(name) > 160 or not re.fullmatch(NAMESPACE, name) or name in names:
            raise ValueError("Invalid extension namespace")
        names.add(name)
        version = extension["schema_version"]
        if (
            not isinstance(version, str)
            or not re.fullmatch(SEMVER, version)
            or not isinstance(extension["payload"], dict)
        ):
            raise ValueError("Invalid extension data")
    with _VALIDATION_LOCK:
        _VALIDATED_OBSERVATIONS[fingerprint] = True
        if len(_VALIDATED_OBSERVATIONS) > _VALIDATION_CACHE_LIMIT:
            _VALIDATED_OBSERVATIONS.popitem(last=False)
    return value


def _contract(record, schema, error):
    """Refuse another contract or an unsupported version by name, before the structural check."""
    version = record.get("schema_version") if isinstance(record, dict) else None
    if (
        not isinstance(version, str)
        or version not in SUPPORTED
        or record.get("schema") != schema
        or record.get("profile") != PROFILE
    ):
        raise ValueError(error)


def validate_batch(batch, *, allow_synthetic=False):
    _contract(batch, "wtf-adsb.observations", "Unsupported observation contract")
    _structure("change-batch-v1.schema.json", batch)
    keys(
        batch,
        (
            "schema",
            "schema_version",
            "profile",
            "stream_id",
            "generation",
            "generated_at",
            "producer",
            "sequence_start",
            "sequence_end",
            "synthetic",
            "changes",
        ),
        ("extension_schemas",),
    )
    identifier(batch["stream_id"])
    identifier(batch["generation"])
    instant(batch["generated_at"])
    keys(batch["producer"], ("name", "version"))
    for token in batch["producer"].values():
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,159}", token):
            raise ValueError("Invalid producer identity")
    privacy(batch["producer"])
    declarations = batch.get("extension_schemas", [])
    if not isinstance(declarations, list) or len(declarations) > 64:
        raise ValueError("Invalid extension schema declarations")
    declared = {}
    for declaration in declarations:
        keys(declaration, ("namespace", "schema_version", "sha256"))
        name, version, checksum = (declaration[key] for key in ("namespace", "schema_version", "sha256"))
        if (
            not isinstance(name, str)
            or len(name) > 160
            or not re.fullmatch(NAMESPACE, name)
            or name in declared
        ):
            raise ValueError("Invalid extension schema namespace")
        if not isinstance(version, str) or not re.fullmatch(SEMVER, version):
            raise ValueError("Invalid extension schema version")
        if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError("Invalid extension schema checksum")
        declared[name] = version
    if type(batch["synthetic"]) is not bool or (batch["synthetic"] and not allow_synthetic):
        raise ValueError("Synthetic observations are not enabled")
    start, end = integer(batch["sequence_start"], 1), integer(batch["sequence_end"], 1)
    if not isinstance(batch["changes"], list) or end - start + 1 != len(batch["changes"]) or end < start:
        raise ValueError("Invalid batch sequence interval")
    for expected, change in enumerate(batch["changes"], start):
        keys(
            change,
            ("sequence", "operation", "event_id", "revision", "updated_at"),
            ("evidence_digest", "observation", "reason_code", "superseded_by"),
        )
        if integer(change["sequence"], 1) != expected:
            raise ValueError("Noncontiguous change sequence")
        identifier(change["event_id"])
        integer(change["revision"], 1)
        instant(change["updated_at"])
        if change["operation"] == "upsert":
            if set(change) != {
                "sequence",
                "operation",
                "event_id",
                "revision",
                "updated_at",
                "evidence_digest",
                "observation",
            }:
                raise ValueError("Invalid upsert")
            validate_observation(change["observation"])
            if batch["schema_version"] == "1.0.0" and "type_norms" in change["observation"]["context"]:
                raise ValueError("Unsupported observation contract")
            if digest(change["observation"]) != change["evidence_digest"]:
                raise ValueError("Evidence digest mismatch")
            for extension in change["observation"].get("extensions", []):
                if declared.get(extension["namespace"]) != extension["schema_version"]:
                    raise ValueError("Extension has no matching schema declaration")
        elif change["operation"] == "retract":
            if (
                "observation" in change
                or "evidence_digest" in change
                or change.get("reason_code")
                not in {
                    "invalid_evidence",
                    "privacy_removal",
                    "superseded",
                    "out_of_scope",
                }
            ):
                raise ValueError("Invalid retraction")
            replacements = change.get("superseded_by", [])
            if not isinstance(replacements, list) or len(replacements) > 100:
                raise ValueError("Invalid replacement identities")
            for replacement in replacements:
                identifier(replacement)
        else:
            raise ValueError("Unknown operation")
    if len(canonical(batch)) > MAX_BYTES:
        raise ValueError("Batch exceeds the transport limit")
    return batch


def validate_health(health):
    keys(health, ("ready", "complete", "mode", "last_receiver_poll", "analysis_watermark"))
    if (
        type(health["ready"]) is not bool
        or type(health["complete"]) is not bool
        or health["mode"] not in {"live", "replay"}
    ):
        raise ValueError("Invalid stream health status")
    for key in ("last_receiver_poll", "analysis_watermark"):
        stamp = health[key]
        if stamp is not None and (type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0):
            raise ValueError("Invalid stream health timestamp")
        if health["ready"] and health["mode"] == "live" and stamp is None:
            raise ValueError("A ready live stream requires freshness timestamps")
    return health


def validate_manifest(manifest):
    _contract(manifest, "wtf-adsb.manifest", "Unsupported export manifest")
    _structure("manifest-v1.schema.json", manifest)
    keys(
        manifest,
        (
            "schema",
            "schema_version",
            "profile",
            "stream_id",
            "generation",
            "head_sequence",
            "first_sequence",
            "updated_at",
            "health",
            "batches",
        ),
    )
    identifier(manifest["stream_id"])
    identifier(manifest["generation"])
    instant(manifest["updated_at"])
    integer(manifest["head_sequence"])
    integer(manifest["first_sequence"], 1)
    validate_health(manifest["health"])
    if not isinstance(manifest["batches"], list):
        raise ValueError("Invalid manifest inventory")
    expected = manifest["first_sequence"]
    for entry in manifest["batches"]:
        keys(entry, ("path", "sha256", "bytes", "sequence_start", "sequence_end"))
        if not isinstance(entry["path"], str) or not re.fullmatch(
            r"batches/[a-zA-Z0-9_.-]+\.json", entry["path"]
        ):
            raise ValueError("Invalid batch path")
        if (
            not isinstance(entry["sha256"], str)
            or not re.fullmatch("[0-9a-f]{64}", entry["sha256"])
            or integer(entry["bytes"], 1) > MAX_BYTES
        ):
            raise ValueError("Invalid batch integrity metadata")
        if integer(entry["sequence_start"], 1) != expected:
            raise ValueError("Manifest contains a gap")
        expected = integer(entry["sequence_end"], entry["sequence_start"]) + 1
    if expected - 1 != manifest["head_sequence"]:
        raise ValueError("Manifest head differs from its inventory")
    return manifest


def read_file(root, relative, maximum=MAX_BYTES):
    root = Path(root).resolve()
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in {"..", "."} for p in parts):
        raise ValueError("Unsafe export path")
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("Export symlinks are forbidden")
    if not path.is_file() or path.stat().st_size > maximum:
        raise ValueError("Missing or oversized export file")
    with path.open("rb") as stream:
        value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise ValueError("Export file grew past the limit")
    return value


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("Cannot replace an export symlink")
    descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)
