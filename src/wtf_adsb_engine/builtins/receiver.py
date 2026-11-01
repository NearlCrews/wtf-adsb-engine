"""Continuous native collection while evidence projection runs independently.

This compatibility plugin wraps the existing stateful detector. Third-party
inputs can use the ordinary normalized snapshot protocol instead. Collection
and enrichment use their own durable journal, never the core outbox connection.
"""

import logging
import threading
import time
from dataclasses import replace

from ..plugin_api.v1 import InputBatch, Lifecycle, PluginSpec
from .io import ReceiverInput, options

LOG = logging.getLogger(__name__)


class ReceiverService(Lifecycle):
    spec = PluginSpec(
        "wtf_adsb.receiver_service",
        "inputs",
        config_schema=options(
            {
                **ReceiverInput.spec.config_schema["properties"],
                "poll_seconds": {"type": "number", "minimum": 1, "maximum": 60},
                "type_registry": {"type": "boolean"},
                "wind": {"type": "boolean"},
                "flightaware": {"type": "boolean"},
                "flightaware_key_file": {"type": "string", "minLength": 1},
                "flightaware_pause_file": {"type": "string", "minLength": 1},
                "flightaware_daily_budget_usd": {"type": "number", "minimum": 0, "maximum": 1000},
                "flightaware_monthly_budget_usd": {"type": "number", "minimum": 0, "maximum": 1000},
                "fleet_reference_file": {"type": "string", "minLength": 1},
            },
            ("url",),
        ),
        provides=("legacy_journal",),
    )
    mode = "live"

    def __init__(self, settings):
        self.settings = settings
        self.input = ReceiverInput(
            {
                key: value
                for key, value in settings.items()
                if key in ReceiverInput.spec.config_schema["properties"]
            }
        )
        self.stop = threading.Event()
        self.workers = []
        # The latest failed attempt as (exception class name, analysis fault), or None.
        self.failure = None
        self.enrichment_error = None
        self.collector = None
        self.last_attempt = None

    def initialize(self, context):
        from ..analysis.flightaware import Settings, money
        from ..analysis.native import NativeCollector

        self.context = context
        self.collector = NativeCollector(
            context.state_directory / "journal.sqlite3",
            type_registry=self.settings.get("type_registry", False),
            wind=self.settings.get("wind", False),
            flightaware_settings=Settings(
                enabled=self.settings.get("flightaware", False),
                key_file=self.settings.get("flightaware_key_file", "/run/secrets/flightaware_api_key"),
                daily_budget=money(self.settings.get("flightaware_daily_budget_usd", 0.50)),
                monthly_budget=money(self.settings.get("flightaware_monthly_budget_usd", 10)),
                fleet_reference_file=self.settings.get("fleet_reference_file"),
                pause_file=self.settings.get("flightaware_pause_file"),
            ),
        )
        self._collect()
        self.workers = [
            threading.Thread(target=self._collect_loop, name="native-receiver", daemon=True),
            threading.Thread(target=self._enrich_loop, name="native-enrichment", daemon=True),
        ]
        for worker in self.workers:
            worker.start()

    def _collect(self):
        from ..analysis.app import CollectorFault

        try:
            now = time.time()
            batch = self.input.poll(None, replace(self.context, now=now))
            self.collector.process(batch.records[0]["snapshot"], wall_now=now)
            if self.failure:
                LOG.warning("Receiver collection recovered after %s", self.failure[0])
            self.failure = None
        except Exception as error:
            failure = (type(error).__name__, isinstance(error, CollectorFault))
            # Tolerated attempts leave no other trace; log when the failure class changes.
            if failure != self.failure:
                LOG.warning("Receiver collection attempt failed (%s)", failure[0])
            self.failure = failure
        finally:
            self.last_attempt = time.time()

    def _collect_loop(self):
        while not self.stop.wait(self.settings.get("poll_seconds", 10)):
            self._collect()

    def _enrich_loop(self):
        while not self.stop.is_set():
            try:
                self.collector.enrich(time.time())
                self.enrichment_error = None
            except Exception as error:
                self.enrichment_error = type(error).__name__
            self.stop.wait(5)

    @property
    def max_age(self):
        return self.settings.get("max_age_seconds", 120)

    def poll(self, checkpoint, context):
        failure = self.failure
        health = self.collector.health()
        if (failure and failure[1]) or health["detector_errors"]:
            raise ValueError("Receiver analysis is incomplete")
        # The committed journal's age decides availability. Failed attempts (a busy
        # journal, a slow receiver) keep the committed evidence usable while it is at most
        # max_age_seconds old instead of failing the whole projection pass each time.
        watermark = health["last_receiver_timestamp"]
        if watermark is None or not 0 <= context.now - watermark <= self.max_age:
            raise ValueError(
                "Receiver collection is unavailable" if failure else "Receiver collection is stale"
            )
        return InputBatch(
            ({"type": "legacy_journal", "path": str(self.collector.path)},), watermark, watermark
        )

    def health(self):
        now = time.time()
        failure = self.failure
        # The same committed receiver timestamp that poll() reads from the journal.
        committed = self.collector.detector.last_poll if self.collector else 0
        fresh = committed > 0 and 0 <= now - committed <= self.max_age
        state = "ready"
        if failure and (failure[1] or not fresh):
            state = "failed"
        elif failure or self.enrichment_error:
            state = "degraded"
        return {
            "alive": bool(self.workers)
            and all(worker.is_alive() for worker in self.workers)
            and self.last_attempt is not None
            and 0 <= now - self.last_attempt <= 180,
            "state": state,
            "collection_error": failure[0] if failure else None,
            "enrichment_error": self.enrichment_error,
        }

    def close(self):
        self.stop.set()
        for worker in self.workers:
            worker.join(timeout=65)
