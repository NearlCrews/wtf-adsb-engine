"""Small, explicit SDK for trusted installed plugins.

Plugins never receive the core SQLite connection. Their state directory is
private to their configured instance. In-process plugins are trusted code,
not an OS security boundary.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

API_VERSION = 1
KINDS = frozenset({"inputs", "enrichers", "references", "detectors", "formats", "outputs"})


@dataclass(frozen=True)
class PluginSpec:
    id: str
    kind: str
    version: str = "0.1.0"
    api_version: int = API_VERSION
    config_schema: dict = field(default_factory=lambda: {"type": "object", "additionalProperties": False})
    requires: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()


@dataclass(frozen=True)
class Context:
    instance: str
    state_directory: Path
    source_instance: str
    now: float
    references: dict = field(default_factory=dict)
    mode: str = "live"


@dataclass(frozen=True)
class InputBatch:
    records: tuple[dict, ...]
    checkpoint: Any
    watermark: float | None
    complete: bool = True


@dataclass(frozen=True)
class Observation:
    # A private durable key, never serialized into the public envelope.
    key: str
    data: dict


@dataclass(frozen=True)
class Retraction:
    key: str
    reason_code: str
    replacement_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class DetectionResult:
    observations: tuple[Observation, ...]
    retractions: tuple[Retraction, ...] = ()


@dataclass(frozen=True)
class EnrichmentResult:
    input_digest: str
    namespace: str
    payload: dict
    schema_version: str = "1.0.0"
    state: str = "available"


class Input(Protocol):
    spec: PluginSpec

    def poll(self, checkpoint: Any, context: Context) -> InputBatch: ...


class Detector(Protocol):
    spec: PluginSpec

    def analyze(self, batch: InputBatch, context: Context) -> tuple[Observation, ...] | DetectionResult: ...


class Enricher(Protocol):
    spec: PluginSpec

    def enrich(self, observation: dict, context: Context) -> EnrichmentResult: ...


class Reference(Protocol):
    spec: PluginSpec

    def lookup(self, context: Context) -> dict: ...


class Format(Protocol):
    spec: PluginSpec

    def encode(self, batch: dict, context: Context) -> bytes: ...


class Output(Protocol):
    spec: PluginSpec

    def deliver(self, artifact: bytes, manifest: dict, context: Context) -> None: ...


class Lifecycle:
    """Default no-op lifecycle used by the built-in integrations."""

    def initialize(self, context: Context) -> None:
        pass

    def health(self) -> dict:
        return {"state": "ready"}

    def close(self) -> None:
        pass
