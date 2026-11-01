"""Private journal adapter for the extracted detector implementation.

This first-party migration component owns a separate legacy journal. It is not
the public detector SDK and never shares the engine outbox connection. Network
enrichment is opt-in through separate adapters; collection itself is offline.
"""

from pathlib import Path

from . import app, flightaware, typecheck
from . import wind as wind_provider


class NativeCollector:
    def __init__(self, path, *, type_registry=False, wind=False, flightaware_settings=None):
        self.path = Path(path)
        self.type_registry = bool(type_registry)
        self.wind = bool(wind)
        self.flightaware_settings = flightaware_settings
        self.next_flightaware = 0
        app.initialize(self.path)
        self.detector = app.Detector(self.path, require_complete=True)
        with app.connect(self.path) as database:
            self.detector.last_poll = app.number(app.meta(database, "last_receiver_timestamp")) or 0

    def process(self, receiver_snapshot, wall_now=None):
        """Commit one receiver snapshot; repeated committed timestamps are no-ops.

        Replay callers pass the recorded snapshot time as ``wall_now``. Live
        callers omit it so the original freshness check uses the current clock.
        """
        self.detector.process(receiver_snapshot, wall_now=wall_now)

    def health(self):
        with app.connect(self.path) as database:
            return {
                key: app.meta(database, key)
                for key in ("last_success", "last_receiver_timestamp", "detector_errors", "reference_data")
            }

    def enrich(self, now, *, registry_opener=None, wind_opener=None, flightaware_requester=None):
        """One bounded enrichment pass; the runtime schedules it separately.

        Explicit instance configuration controls network requests. Disabled wind
        requests still permit a verdict based on retained true-airspeed reports.
        Failed requests retain the providers' existing durable backoff state.
        """
        result = {
            "type_registry": typecheck.run_once(
                self.path, now=now, opener=registry_opener, enabled_override=self.type_registry
            ),
            "wind": wind_provider.run_once(
                self.path, now=now, opener=wind_opener, enabled_override=self.wind
            ),
        }
        if (
            self.flightaware_settings is not None
            and self.flightaware_settings.enabled
            and now >= self.next_flightaware
        ):
            self.next_flightaware = now + flightaware.INTERVAL
            result["flightaware"] = flightaware.run_once(
                self.path,
                settings=self.flightaware_settings,
                now=now,
                requester=flightaware_requester,
            )
        return result

    def snapshot(self, now, *, source_instance="receiver"):
        return self.snapshot_state(now, source_instance=source_instance)["observations"]

    def snapshot_state(self, now, *, source_instance="receiver"):
        from .export import snapshot_state

        return snapshot_state(
            self.path,
            now,
            source_instance=source_instance,
            identity_path=self.path.with_name(self.path.name + ".identities.sqlite3"),
        )
