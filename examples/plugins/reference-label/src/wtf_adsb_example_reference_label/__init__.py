"""A separate distribution using only version 1 of the engine's public plugin API."""

import hashlib
import json

from wtf_adsb_engine.plugin_api.v1 import EnrichmentResult, Lifecycle, PluginSpec

NAMESPACE = "org.example.reference_label"
SCHEMA_VERSION = "1.0.0"


class ReferenceLabel(Lifecycle):
    spec = PluginSpec(
        id=NAMESPACE,
        kind="enrichers",
        version="0.1.0",
        requires=("observations",),
        provides=(NAMESPACE,),
        config_schema={
            "type": "object",
            "properties": {"label": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}$"}},
            "additionalProperties": False,
        },
    )

    def __init__(self, settings):
        self.label = settings.get("label", "Demonstration reference")

    def enrich(self, observation, context):
        # The fingerprint binds this result to the exact supplied evidence.
        # This public encoding is also used for wire-level evidence digests.
        encoded = json.dumps(
            observation, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return EnrichmentResult(
            input_digest=hashlib.sha256(encoded).hexdigest(),
            namespace=NAMESPACE,
            schema_version=SCHEMA_VERSION,
            payload={
                "label": self.label,
                "basis": "configured_reference",
                "source_id": "reference-label-example",
            },
        )
