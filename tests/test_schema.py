"""Offline structural and semantic contract checks against synthetic evidence."""

import copy
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator, ValidationError

from wtf_adsb_engine import wire

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = Path(wire.__file__).resolve().parent
NAMES = ("observation-v1.schema.json", "change-batch-v1.schema.json", "manifest-v1.schema.json")


def example():
    return json.loads((ROOT / "examples/change-batch.example.json").read_text())


def manifest():
    batch = example()
    return {
        "schema": "wtf-adsb.manifest",
        "schema_version": "1.0.0",
        "profile": "wtf-adsb.collection.v1",
        "stream_id": batch["stream_id"],
        "generation": batch["generation"],
        "head_sequence": 0,
        "first_sequence": 1,
        "updated_at": batch["generated_at"],
        "health": {
            "ready": True,
            "complete": True,
            "mode": "replay",
            "last_receiver_poll": None,
            "analysis_watermark": 1790856060,
        },
        "batches": [],
    }


class ContractSchemaTests(unittest.TestCase):
    def setUp(self):
        self.validators = {
            name: Draft202012Validator(json.loads((ROOT / "contracts" / name).read_text())) for name in NAMES
        }

    def test_released_assets_match_bundled_offline_schemas(self):
        def references(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "$ref":
                        self.assertTrue(item.startswith("#/$defs/"), item)
                    references(item)
            elif isinstance(value, list):
                for item in value:
                    references(item)

        with patch.object(socket, "socket", side_effect=AssertionError("Schema validation used the network")):
            for name, validator in self.validators.items():
                with self.subTest(name=name):
                    self.assertEqual(
                        (ROOT / "contracts" / name).read_bytes(), (PACKAGE / "contracts" / name).read_bytes()
                    )
                    Draft202012Validator.check_schema(validator.schema)
                    references(validator.schema)
            self.validators[NAMES[1]].validate(example())
            wire.validate_batch(example(), allow_synthetic=True)

    def test_synthetic_example_validates_but_requires_explicit_opt_in(self):
        batch = example()
        self.validators[NAMES[0]].validate(batch["changes"][0]["observation"])
        wire.validate_batch(batch, allow_synthetic=True)
        with self.assertRaisesRegex(ValueError, "Synthetic"):
            wire.validate_batch(batch)

    def test_malformed_producer_is_rejected_without_echoing_payload(self):
        for producer in (None, [], {"name": "wtf-adsb-engine"}, {"name": "PRIVATE VALUE", "version": "1"}):
            with self.subTest(producer=producer):
                batch = example()
                batch["producer"] = producer
                with self.assertRaises(ValidationError):
                    self.validators[NAMES[1]].validate(batch)
                with self.assertRaises(ValueError) as rejected:
                    wire.validate_batch(batch, allow_synthetic=True)
                self.assertNotIn("PRIVATE VALUE", str(rejected.exception))

    def test_finding_tracking_identifier_preserves_non_icao_marker(self):
        for address in ("a00001", "ABCDEF", "~a00001", "~ABCDEF"):
            with self.subTest(address=address):
                batch = example()
                observation = batch["changes"][0]["observation"]
                event = observation["group"]["events"][0]
                event["hex"] = address
                if address.startswith("~"):
                    del event["evidence"]["aircraft"]["hex"]
                batch["changes"][0]["evidence_digest"] = wire.digest(observation)
                self.validators[NAMES[0]].validate(observation)
                self.validators[NAMES[1]].validate(batch)
                wire.validate_batch(batch, allow_synthetic=True)
        for address in (
            "~~a00001",
            "~a0000",
            "~a000001",
            "~g00001",
            "a00001\n",
            "~a00001\n",
            " ~a00001",
            "~a00001 ",
            "~a00001/private",
        ):
            with self.subTest(address=address):
                observation = example()["changes"][0]["observation"]
                observation["group"]["events"][0]["hex"] = address
                with self.assertRaises(ValidationError):
                    self.validators[NAMES[0]].validate(observation)
                with self.assertRaises(ValueError):
                    wire.validate_observation(observation)

    def test_ready_live_health_requires_typed_freshness(self):
        value = manifest()
        self.validators[NAMES[2]].validate(value)
        wire.validate_manifest(value)
        for replacement in (
            {"ready": "true"},
            {"mode": "live", "last_receiver_poll": None},
            {"analysis_watermark": -1},
            {"analysis_watermark": True},
            {"unexpected": "field"},
        ):
            with self.subTest(replacement=replacement):
                broken = copy.deepcopy(value)
                broken["health"].update(replacement)
                with self.assertRaises(ValidationError):
                    self.validators[NAMES[2]].validate(broken)
                with self.assertRaises(ValueError):
                    wire.validate_manifest(broken)

    def test_non_icao_projection_does_not_claim_an_icao_identity(self):
        from wtf_adsb_engine.analysis import stories
        from wtf_adsb_engine.analysis.export import project_source

        source = example()["changes"][0]["observation"]["group"]["events"][0]
        source["id"] = 1
        source["hex"] = "~a00001"
        source["evidence"]["aircraft"]["hex"] = "~a00001"
        projected = project_source(source, source_instance="synthetic-track")
        self.assertEqual(projected["hex"], "~a00001")
        self.assertNotIn("hex", projected["evidence"]["aircraft"])
        self.assertEqual(stories.aircraft_links({"hex": projected["hex"]}), [])

    def test_extensions_require_declared_version_and_hash(self):
        batch = example()
        observation = batch["changes"][0]["observation"]
        extension = {"namespace": "org.example.synthetic", "schema_version": "1.0.0", "payload": {"count": 2}}
        observation["extensions"] = [extension]
        batch["changes"][0]["evidence_digest"] = wire.digest(observation)
        self.validators[NAMES[1]].validate(batch)
        with self.assertRaisesRegex(ValueError, "matching schema declaration"):
            wire.validate_batch(batch, allow_synthetic=True)
        batch["extension_schemas"] = [
            {"namespace": extension["namespace"], "schema_version": "1.0.0", "sha256": "a" * 64}
        ]
        wire.validate_batch(batch, allow_synthetic=True)
        for field, invalid in (("namespace", "."), ("schema_version", "latest"), ("sha256", "bad-digest")):
            with self.subTest(field=field):
                broken = copy.deepcopy(batch)
                broken["extension_schemas"][0][field] = invalid
                with self.assertRaises(ValidationError):
                    self.validators[NAMES[1]].validate(broken)
                with self.assertRaises(ValueError):
                    wire.validate_batch(broken, allow_synthetic=True)
        broken = copy.deepcopy(batch)
        broken["extension_schemas"][0]["url"] = "https://example.org/schema.json"
        with self.assertRaises(ValueError):
            wire.validate_batch(broken, allow_synthetic=True)

    def test_integrity_continuity_and_privacy_need_semantic_checks(self):
        digest = example()
        digest["changes"][0]["observation"]["group"]["events"][0]["value"] = 613
        gap = example()
        gap["changes"][0]["sequence"] = 2
        private = example()
        observation = private["changes"][0]["observation"]
        observation["group"]["events"][0]["evidence"]["receiver_lat"] = 0
        private["changes"][0]["evidence_digest"] = wire.digest(observation)
        for broken in (digest, gap, private):
            with self.subTest(broken=broken is private):
                self.validators[NAMES[1]].validate(broken)
                with self.assertRaises(ValueError):
                    wire.validate_batch(broken, allow_synthetic=True)

    def test_retractions_cannot_carry_observations(self):
        batch = example()
        change = batch["changes"][0]
        change.update(operation="retract", reason_code="invalid_evidence")
        with self.assertRaises(ValidationError):
            self.validators[NAMES[1]].validate(batch)
        del change["observation"], change["evidence_digest"]
        self.validators[NAMES[1]].validate(batch)
        wire.validate_batch(batch, allow_synthetic=True)

    def test_detector_projection_matches_released_schema(self):
        from wtf_adsb_engine.analysis.native import NativeCollector

        with tempfile.TemporaryDirectory() as directory:
            collector = NativeCollector(Path(directory) / "journal.sqlite3")
            for index in range(8):
                stamp = 1790856000 + index * 10
                collector.process(
                    {
                        "now": stamp,
                        "aircraft": [
                            {
                                "hex": "a00001",
                                "r": "N1",
                                "t": "C172",
                                "type": "adsb_icao",
                                "squawk": "7700",
                                "lat": 40 + index * 0.001,
                                "lon": -100,
                                "alt_baro": 5000,
                                "gs": 80,
                                "seen": 0,
                                "seen_pos": 0,
                                "messages": 100 + index,
                            }
                        ],
                    },
                    wall_now=stamp,
                )
            observations = collector.snapshot(1790856100)
        self.assertTrue(observations)
        for item in observations:
            self.validators[NAMES[0]].validate(item["observation"])
            wire.validate_observation(item["observation"])

    def test_manifest_inventory_paths_and_integrity_metadata(self):
        value = manifest()
        value.update(head_sequence=1)
        value["batches"] = [
            {
                "path": "batches/000001.json",
                "sha256": "a" * 64,
                "bytes": 100,
                "sequence_start": 1,
                "sequence_end": 1,
            }
        ]
        self.validators[NAMES[2]].validate(value)
        wire.validate_manifest(value)
        for field, invalid in (("path", "../private.json"), ("sha256", "A" * 64), ("bytes", 8388609)):
            with self.subTest(field=field):
                broken = copy.deepcopy(value)
                broken["batches"][0][field] = invalid
                with self.assertRaises(ValueError):
                    wire.validate_manifest(broken)


def previous_version(batch):
    """The example as a 1.0.0 batch: no type_norms, and the digest of what remains."""
    batch = copy.deepcopy(batch)
    batch["schema_version"] = "1.0.0"
    change = batch["changes"][0]
    del change["observation"]["context"]["type_norms"]
    change["evidence_digest"] = wire.digest(change["observation"])
    return batch


class ContractVersionTests(unittest.TestCase):
    """Contract 1.1.0 adds context.type_norms; readers keep accepting 1.0.0."""

    def test_this_release_writes_1_1_0_and_reads_both_versions(self):
        self.assertEqual(wire.VERSION, "1.1.0")
        self.assertEqual(wire.SUPPORTED, {"1.0.0", "1.1.0"})
        batch = example()
        self.assertEqual(batch["schema_version"], wire.VERSION)
        self.assertTrue(batch["changes"][0]["observation"]["context"]["type_norms"])
        wire.validate_batch(batch, allow_synthetic=True)
        wire.validate_batch(previous_version(batch), allow_synthetic=True)
        for version in sorted(wire.SUPPORTED):
            with self.subTest(version=version):
                wire.validate_manifest({**manifest(), "schema_version": version})
        validator = Draft202012Validator(json.loads((ROOT / "contracts" / NAMES[1]).read_text()))
        validator.validate(previous_version(batch))

    def test_an_unknown_version_is_refused_by_name(self):
        for version in ("1.2.0", "2.0.0", "0.9.0", "", None, ["1.1.0"]):
            with self.subTest(version=version):
                batch = {**example(), "schema_version": version}
                with self.assertRaisesRegex(ValueError, "^Unsupported observation contract$"):
                    wire.validate_batch(batch, allow_synthetic=True)
                with self.assertRaisesRegex(ValueError, "^Unsupported export manifest$"):
                    wire.validate_manifest({**manifest(), "schema_version": version})

    def test_a_1_0_0_batch_cannot_carry_type_norms(self):
        batch = example()
        batch["schema_version"] = "1.0.0"
        with self.assertRaisesRegex(ValueError, "^Unsupported observation contract$"):
            wire.validate_batch(batch, allow_synthetic=True)

    def test_type_norms_are_typed_and_name_findings_of_the_group(self):
        observation = example()["changes"][0]["observation"]
        finding = observation["group"]["events"][0]["id"]
        norm = observation["context"]["type_norms"][finding]
        for norms in ({finding: None}, {finding: norm}, {}):
            with self.subTest(norms=norms):
                wire.validate_observation(
                    {**observation, "context": {**observation["context"], "type_norms": norms}}
                )
        for norms in (
            {finding: {**norm, "extra": 1}},
            {finding: {key: value for key, value in norm.items() if key != "window_days"}},
            {finding: {**norm, "low": 412.5}},
            {finding: {**norm, "phase": "cruise"}},
            {"not-a-uuid": None},
            {"00000000-0000-4000-8000-0000000000ff": None},
            [],
        ):
            with self.subTest(norms=norms), self.assertRaises(ValueError):
                wire.validate_observation(
                    {**observation, "context": {**observation["context"], "type_norms": norms}}
                )


if __name__ == "__main__":
    unittest.main()
