"""Composable local reference provider and namespaced lookup enrichment."""

from pathlib import Path

from .. import wire
from ..plugin_api.v1 import EnrichmentResult, Lifecycle, PluginSpec
from .io import options


class JsonReference(Lifecycle):
    spec = PluginSpec(
        "wtf_adsb.json_reference",
        "references",
        config_schema=options(
            {
                "path": {"type": "string", "minLength": 1},
            },
            ("path",),
        ),
    )

    def __init__(self, settings):
        self.path = Path(settings["path"])

    def lookup(self, context):
        with self.path.open("rb") as stream:
            value = wire.loads(stream.read(wire.MAX_BYTES + 1))
        if not isinstance(value, dict):
            raise ValueError("Reference must be an object keyed by aircraft type")
        return value


class ReferenceMatch(Lifecycle):
    spec = PluginSpec(
        "wtf_adsb.reference_match",
        "enrichers",
        config_schema=options(
            {
                "reference": {"type": "string", "minLength": 1},
                "namespace": {"type": "string", "pattern": "^" + wire.NAMESPACE + "$"},
                "schema_version": {"type": "string", "pattern": "^" + wire.SEMVER + "$"},
            },
            ("reference", "namespace", "schema_version"),
        ),
        requires=("observations",),
    )

    def __init__(self, settings):
        self.settings = settings

    def enrich(self, observation, context):
        reference = context.references[self.settings["reference"]]
        codes = sorted(
            {
                event.get("evidence", {}).get("aircraft", {}).get("t", "")
                for event in observation["group"]["events"]
            }
            - {""}
        )
        matches = {code: reference[code] for code in codes if code in reference}
        return EnrichmentResult(
            wire.digest(observation),
            self.settings["namespace"],
            {"matches": matches},
            self.settings["schema_version"],
        )
