"""Adapter around the production detector, isolated from the core outbox."""

import threading
import time

from ..plugin_api.v1 import DetectionResult, Lifecycle, Observation, PluginSpec, Retraction
from .io import options


class CollectionDetector(Lifecycle):
    spec = PluginSpec(
        "wtf_adsb.collection",
        "detectors",
        config_schema=options(
            {
                "type_registry": {"type": "boolean"},
                "wind": {"type": "boolean"},
            }
        ),
        provides=("observations",),
    )

    def __init__(self, settings):
        self.settings = settings
        self.collector = None
        self.path = None
        self.stop = threading.Event()
        self.worker = None
        self.enrichment_error = None

    def _enrich(self):
        while not self.stop.is_set():
            try:
                self.collector.enrich(time.time())
                self.enrichment_error = None
            except Exception as error:
                self.enrichment_error = type(error).__name__
            self.stop.wait(5)

    def health(self):
        return {
            "state": "degraded" if self.enrichment_error else "ready",
            "enrichment_error": self.enrichment_error,
        }

    def close(self):
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=65)

    def analyze(self, batch, context):
        from ..analysis.export import snapshot_state
        from ..analysis.native import NativeCollector

        for record in batch.records:
            if record.get("type") == "legacy_journal":
                self.path = record["path"]
            elif record.get("type") == "receiver_snapshot":
                if self.collector is None:
                    self.collector = NativeCollector(
                        context.state_directory / "journal.sqlite3", **self.settings
                    )
                    if any(self.settings.values()):
                        self.worker = threading.Thread(
                            target=self._enrich, name="native-enrichment", daemon=True
                        )
                        self.worker.start()
                self.path = self.collector.path
                self.collector.process(
                    record["snapshot"],
                    wall_now=record["snapshot"]["now"] if context.mode == "replay" else context.now,
                )
                health = self.collector.health()
                if (
                    health["last_receiver_timestamp"] is None
                    or health["last_receiver_timestamp"] < record["snapshot"]["now"]
                    or health["detector_errors"]
                ):
                    raise ValueError("Native detector did not complete this snapshot")
            else:
                raise ValueError("Collection detector requires a receiver snapshot or journal")
        if self.path is None:
            candidate = context.state_directory / "journal.sqlite3"
            if not candidate.exists():
                return ()
            self.path = candidate
        if self.collector and not any(self.settings.values()):
            self.collector.enrich(context.now)
        result = snapshot_state(
            self.path,
            context.now,
            source_instance=context.source_instance,
            identity_path=context.state_directory / "identities.sqlite3",
            health_mode=context.mode,
        )
        return DetectionResult(
            tuple(Observation(row["key"], row["observation"]) for row in result["observations"]),
            tuple(
                Retraction(row["key"], row["reason_code"], tuple(row["replacement_keys"]))
                for row in result["retractions"]
            ),
        )
