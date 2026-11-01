"""Repeated native projections cannot bypass validation through the digest cache."""

import copy
import json
import unittest
from pathlib import Path
from uuid import uuid4

from wtf_adsb_engine import wire

ROOT = Path(__file__).resolve().parents[1]


class CachedValidationTests(unittest.TestCase):
    def setUp(self):
        self.batch = json.loads((ROOT / "examples/change-batch.example.json").read_text())
        self.observation = self.batch["changes"][0]["observation"]
        self.observation["group"]["id"] = str(uuid4())
        self.batch["changes"][0]["evidence_digest"] = wire.digest(self.observation)

    def test_mutating_a_validated_object_rechecks_structure_and_privacy(self):
        wire.validate_observation(self.observation)
        event = self.observation["group"]["events"][0]
        event["unexpected"] = "unapproved field"
        for _ in range(2):
            with self.assertRaises(ValueError):
                wire.validate_observation(self.observation)
        del event["unexpected"]
        wire.validate_observation(self.observation)
        event["evidence"]["receiver_lat"] = 0
        with self.assertRaisesRegex(ValueError, "Private"):
            wire.validate_observation(self.observation)

    def test_failed_semantics_cannot_poison_a_later_cache_hit(self):
        events = self.observation["group"]["events"]
        events.append(copy.deepcopy(events[0]))
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, "Duplicate finding"):
                wire.validate_observation(self.observation)
        events[1]["id"] = str(uuid4())
        wire.validate_observation(self.observation)
        events[1]["id"] = events[0]["id"]
        with self.assertRaisesRegex(ValueError, "Duplicate finding"):
            wire.validate_observation(self.observation)

    def test_batch_delegation_still_validates_changed_nested_observation(self):
        wire.validate_batch(self.batch, allow_synthetic=True)
        self.observation["group"]["events"][0]["hits"] = "invalid count"
        self.batch["changes"][0]["evidence_digest"] = wire.digest(self.observation)
        with self.assertRaises(ValueError):
            wire.validate_batch(self.batch, allow_synthetic=True)

    def test_cached_observation_does_not_authorize_missing_extension_declaration(self):
        self.observation["extensions"] = [
            {"namespace": "org.example.analysis", "schema_version": "1.0.0", "payload": {"count": 2}}
        ]
        self.batch["changes"][0]["evidence_digest"] = wire.digest(self.observation)
        self.batch["extension_schemas"] = [
            {"namespace": "org.example.analysis", "schema_version": "1.0.0", "sha256": "a" * 64}
        ]
        wire.validate_batch(self.batch, allow_synthetic=True)
        del self.batch["extension_schemas"]
        with self.assertRaisesRegex(ValueError, "matching schema declaration"):
            wire.validate_batch(self.batch, allow_synthetic=True)
        with self.assertRaisesRegex(ValueError, "Synthetic"):
            wire.validate_batch(self.batch)


if __name__ == "__main__":
    unittest.main()
