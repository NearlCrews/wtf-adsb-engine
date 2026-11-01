"""Durability, retries, native replay, and output isolation at the real boundary."""

import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from wtf_adsb_engine import wire
from wtf_adsb_engine.builtins.io import FileOutput, ReplayInput
from wtf_adsb_engine.builtins.receiver import ReceiverService
from wtf_adsb_engine.cli import main
from wtf_adsb_engine.plugin_api.v1 import Context, InputBatch, Observation, Retraction
from wtf_adsb_engine.runtime import Runtime
from wtf_adsb_engine.storage import Store

ROOT = Path(__file__).resolve().parents[1]
NOW = 1791150000
HEALTH = {
    "ready": True,
    "complete": True,
    "mode": "live",
    "last_receiver_poll": NOW,
    "analysis_watermark": NOW,
}


def observation():
    return json.loads((ROOT / "examples/change-batch.example.json").read_text())["changes"][0]["observation"]


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "engine.sqlite3", synthetic=True)

    def commit(self, value=None):
        return self.store.commit(
            "input", 1, [Observation("stable", value or observation())], HEALTH, timestamp=NOW
        )

    def test_revision_and_checkpoint_are_atomic_and_noop_does_not_churn(self):
        self.assertEqual(self.commit(), 1)
        first = self.store.next_batch(0)
        self.assertEqual(self.commit(), 0)
        changed = observation()
        changed["group"]["events"][0]["hits"] += 1
        self.assertEqual(self.commit(changed), 1)
        second = self.store.next_batch(1)["changes"][0]
        self.assertEqual(first["changes"][0]["event_id"], second["event_id"])
        self.assertEqual(second["revision"], 2)
        invalid = observation()
        invalid["group"]["events"][0]["evidence"]["receiver_lat"] = 1
        with self.assertRaises(ValueError):
            self.store.commit("input", 2, [Observation("stable", invalid)], HEALTH, timestamp=NOW)
        self.assertEqual(self.store.checkpoint("input"), 1)
        self.assertEqual(self.store.state()["head_sequence"], 2)

    def test_assigned_batch_bytes_survive_new_changes_and_software_upgrade(self):
        self.commit()
        first = self.store.next_batch(0)
        changed = observation()
        changed["group"]["events"][0]["hits"] += 1
        with patch("wtf_adsb_engine.storage.__version__", "0.2.0"):
            self.commit(changed)
            self.assertEqual(first, self.store.next_batch(0))
            self.assertEqual(self.store.next_batch(1)["producer"]["version"], "0.2.0")

    def test_unassigned_batches_are_bounded_and_shared_by_destinations(self):
        rows = [Observation(str(index), observation()) for index in range(205)]
        self.store.commit("input", 205, rows, HEALTH, timestamp=NOW)
        first = self.store.next_batch(0)
        self.assertEqual(first["sequence_end"], 100)
        self.assertEqual(self.store.next_batch(100)["sequence_end"], 200)
        self.assertEqual(self.store.next_batch(200)["sequence_end"], 205)
        self.assertEqual(first, self.store.next_batch(0))

    def test_retraction_is_not_undone_by_unchanged_poll_or_retention(self):
        self.commit()
        event = self.store.next_batch(0)["changes"][0]["event_id"]
        self.store.retract(event, "invalid_evidence", timestamp=NOW + 1)
        self.assertEqual(self.commit(), 0)
        self.store.commit("input", 2, [], HEALTH, timestamp=NOW + 2)
        self.assertEqual(self.store.state()["head_sequence"], 2)
        self.assertEqual(self.store.next_batch(1)["changes"][0]["operation"], "retract")

    def test_failed_new_input_preserves_live_mode_and_valid_health(self):
        self.commit()
        self.store.input_failed("second", NOW)
        health = self.store.state()["health"]
        self.assertFalse(health["ready"])
        self.assertEqual(health["mode"], "live")
        wire.validate_health(health)

    def test_only_active_inputs_contribute_and_replay_does_not_age_live_watermark(self):
        self.store.active_inputs(["current", "replay"])
        self.store.input_failed("removed", NOW)
        self.store.commit("current", 1, [], HEALTH, timestamp=NOW)
        self.assertFalse(self.store.state()["health"]["ready"])
        replay = {**HEALTH, "mode": "replay", "last_receiver_poll": None, "analysis_watermark": 1}
        self.store.commit("replay", 1, [], replay, timestamp=NOW)
        health = self.store.state()["health"]
        self.assertTrue(health["ready"])
        self.assertEqual(health["analysis_watermark"], NOW)

    def test_supersession_and_replacements_commit_atomically_and_retry_once(self):
        self.commit()
        original = self.store.next_batch(0)["changes"][0]["event_id"]
        replacements = [Observation("part-one", observation()), Observation("part-two", observation())]
        retirement = Retraction("stable", "superseded", ("part-one", "part-two"))
        self.store.commit("input", 2, replacements, HEALTH, timestamp=NOW + 1, retractions=(retirement,))
        batch = self.store.next_batch(1)
        self.assertEqual([c["operation"] for c in batch["changes"]], ["upsert", "upsert", "retract"])
        tombstone = batch["changes"][-1]
        self.assertEqual(tombstone["event_id"], original)
        self.assertEqual(set(tombstone["superseded_by"]), {c["event_id"] for c in batch["changes"][:-1]})
        self.assertEqual(
            self.store.commit("input", 2, replacements, HEALTH, timestamp=NOW + 2, retractions=(retirement,)),
            0,
        )
        self.assertEqual(self.store.state()["head_sequence"], 4)

    def test_missing_supersession_target_rolls_back_checkpoint_and_evidence(self):
        self.commit()
        with self.assertRaises(ValueError):
            self.store.commit(
                "input",
                2,
                [Observation("replacement", observation())],
                HEALTH,
                timestamp=NOW + 1,
                retractions=(Retraction("stable", "superseded", ("not-issued",)),),
            )
        self.assertEqual(self.store.checkpoint("input"), 1)
        self.assertEqual(self.store.state()["head_sequence"], 1)

    def test_supersession_chain_survives_a_never_issued_intermediate(self):
        self.commit()
        self.store.next_batch(0)
        changes = (
            Retraction("stable", "superseded", ("intermediate",)),
            Retraction("intermediate", "superseded", ("terminal",)),
        )
        self.store.commit(
            "input",
            2,
            [Observation("terminal", observation())],
            HEALTH,
            timestamp=NOW + 1,
            retractions=changes,
        )
        batch = self.store.next_batch(1)
        self.assertEqual(batch["changes"][-1]["superseded_by"], [batch["changes"][0]["event_id"]])

    def test_partial_file_delivery_can_retry_without_changing_inventory(self):
        self.commit()
        batch = self.store.next_batch(0)
        state = self.store.state()
        manifest = {
            "schema": "wtf-adsb.manifest",
            "schema_version": wire.VERSION,
            "profile": wire.PROFILE,
            "stream_id": state["stream_id"],
            "generation": state["generation"],
            "head_sequence": 1,
            "first_sequence": 1,
            "updated_at": wire.utc(NOW),
            "health": HEALTH,
            "batches": [],
        }
        sink = FileOutput({"directory": str(self.root / "export")})
        context = Context("files", self.root, "test", NOW)
        artifact = wire.canonical(batch)
        sink.deliver(artifact, manifest, context)
        sink.deliver(artifact, manifest, context)
        inventory = wire.loads(wire.read_file(self.root / "export", "manifest.json"))
        self.assertEqual(len(inventory["batches"]), 1)
        self.assertEqual(inventory["head_sequence"], 1)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "receiver.synthetic.ndjson").write_bytes(
            (ROOT / "examples/receiver.synthetic.ndjson").read_bytes()
        )
        config = (
            (ROOT / "examples/replay.toml")
            .read_text()
            .replace('"../state/demo"', '"state"')
            .replace('"../state/export"', '"export"')
        )
        self.config = self.root / "config.toml"
        self.config.write_text(config)

    def test_shared_detector_data_and_enricher_mutations_are_isolated(self):
        from wtf_adsb_engine.plugin_api.v1 import EnrichmentResult, Lifecycle, PluginSpec

        (self.root / "extension.json").write_text(
            json.dumps(
                {
                    "type": "object",
                    "properties": {"index": {"type": "integer"}},
                    "required": ["index"],
                    "additionalProperties": False,
                }
            )
        )
        with self.config.open("a") as stream:
            stream.write(
                '\n[[extensions]]\nnamespace="org.example.isolation"\nschema_version="1.0.0"\npath="extension.json"\n'
            )
        runtime = Runtime(self.config)

        class Enricher(Lifecycle):
            spec = PluginSpec("org.example.isolation", "enrichers")
            settings = {}
            index = 0

            def enrich(self, data, context):
                self.index += 1
                before = wire.digest(data)
                data["context"]["provider_mutation"] = True
                return EnrichmentResult(before, "org.example.isolation", {"index": self.index})

        pipeline = runtime.pipelines[0]
        runtime.pipelines[0] = (*pipeline[:3], [("isolation", Enricher())])
        detector = pipeline[2][0][1]
        source = observation()
        with (
            runtime.locked(),
            patch.object(
                detector, "analyze", return_value=(Observation("a", source), Observation("b", source))
            ),
        ):
            result = runtime.once()
            self.assertFalse(result["errors"])
            changes = runtime.store.next_batch(0)["changes"]
        self.assertEqual([c["observation"]["extensions"][0]["payload"]["index"] for c in changes], [1, 2])
        self.assertNotIn("extensions", source)
        self.assertNotIn("provider_mutation", source["context"])
        self.assertTrue(all("provider_mutation" not in c["observation"]["context"] for c in changes))

    def test_detector_extensions_are_approved_and_valid_before_checkpoint_commit(self):
        namespace = "org.example.detector"
        schema = {
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": ["label"],
            "additionalProperties": False,
        }
        (self.root / "extension.json").write_text(json.dumps(schema))
        self.config.write_text(
            self.config.read_text()
            + '\n[[extensions]]\nnamespace="org.example.detector"\n'
            + 'schema_version="1.0.0"\npath="extension.json"\n'
        )
        runtime = Runtime(self.config)
        detector = runtime.pipelines[0][2][0][1]
        with runtime.locked():
            for name, payload in (("org.example.unapproved", {"label": "ok"}), (namespace, {"label": 1})):
                with self.subTest(namespace=name, payload=payload):
                    data = observation()
                    data["extensions"] = [{"namespace": name, "schema_version": "1.0.0", "payload": payload}]
                    with patch.object(detector, "analyze", return_value=(Observation("custom", data),)):
                        result = runtime.once()
                    self.assertIn("replay", result["errors"])
                    self.assertIsNone(runtime.store.checkpoint("replay"))
                    self.assertEqual(runtime.store.state()["head_sequence"], 0)
            data["extensions"][0]["payload"] = {"label": "ok"}
            with patch.object(detector, "analyze", return_value=(Observation("custom", data),)):
                result = runtime.once()
            self.assertFalse(result["errors"])
            self.assertEqual(result["outputs"], {"files": "ready"})
            self.assertIsNotNone(runtime.store.checkpoint("replay"))
            self.assertEqual(runtime.store.state()["head_sequence"], 1)

    def test_replay_rejects_a_file_replaced_while_reading(self):
        path = self.root / "receiver.synthetic.ndjson"
        replacement = self.root / "replacement.ndjson"
        replacement.write_text(json.dumps({"now": NOW, "aircraft": []}) + "\n")
        file_digest = hashlib.file_digest

        def replace_after_hash(stream, algorithm):
            result = file_digest(stream, algorithm)
            replacement.replace(path)
            return result

        source = ReplayInput({"path": str(path)})
        context = Context("replay", self.root, "test", NOW)
        with (
            patch("wtf_adsb_engine.builtins.io.hashlib.file_digest", side_effect=replace_after_hash),
            self.assertRaisesRegex(ValueError, "Replay input changed while reading"),
        ):
            source.poll(None, context)

    def test_replay_rejects_an_inplace_write_even_if_size_and_mtime_are_preserved(self):
        path = self.root / "receiver.synthetic.ndjson"
        path.write_bytes(b'{"now":100,"aircraft":[]}\n')
        before = path.stat()
        # Ensure the original mtime and ctime differ, so restoring mtime after
        # the write cannot conceal the inode's changed metadata.
        os.utime(path, ns=(before.st_atime_ns, 1))
        before = path.stat()
        file_digest = hashlib.file_digest

        def rewrite_after_hash(stream, algorithm):
            result = file_digest(stream, algorithm)
            path.write_bytes(b'{"now":200,"aircraft":[]}\n')
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            return result

        source = ReplayInput({"path": str(path)})
        with (
            patch("wtf_adsb_engine.builtins.io.hashlib.file_digest", side_effect=rewrite_after_hash),
            self.assertRaisesRegex(ValueError, "Replay input changed while reading"),
        ):
            source.poll(None, Context("replay", self.root, "test", NOW))

    def test_real_native_replay_is_repeatable_across_process_restart(self):
        with Runtime(self.config).locked() as ignored:
            self.assertIsNone(ignored)
        first = Runtime(self.config)
        with first.locked():
            result = first.once()
            self.assertFalse(result["errors"])
            self.assertGreater(result["changes"]["replay"], 0)
            head = first.store.state()["head_sequence"]
        second = Runtime(self.config)
        with second.locked():
            result = second.once()
            self.assertFalse(result["errors"])
            self.assertEqual(second.store.state()["head_sequence"], head)
        manifest = wire.validate_manifest(wire.loads(wire.read_file(self.root / "export", "manifest.json")))
        self.assertEqual(manifest["head_sequence"], head)
        self.assertEqual(manifest["health"]["mode"], "replay")

    def test_bad_sink_does_not_stop_other_sink_and_retry_uses_saved_artifact(self):
        self.config.write_text(
            self.config.read_text()
            + '\n[[outputs]]\nname="bad"\nplugin="wtf_adsb.files"\noptions={directory="bad-export"}\n'
        )
        runtime = Runtime(self.config)
        with runtime.locked():
            bad = runtime.outputs[1]
            with patch.object(bad[1], "deliver", side_effect=OSError("unavailable")):
                result = runtime.once()
            self.assertEqual(result["outputs"], {"files": "ready", "bad": "OSError"})
            self.assertEqual(runtime.store.cursor("bad"), 0)
            with patch.object(bad[2], "encode", side_effect=AssertionError("retry must reuse bytes")):
                self.assertIsNone(runtime.deliver(bad))
            self.assertEqual(runtime.store.cursor("bad"), runtime.store.cursor("files"))

    def test_output_worker_recovers_when_cursor_and_failure_status_are_unavailable(self):
        runtime = Runtime(self.config)
        runtime.config["poll_interval_seconds"] = 0.1
        stop = threading.Event()
        delivered = threading.Event()
        with runtime.locked():
            runtime.poll()
            cursor = runtime.store.cursor
            sink = runtime.outputs[0][1]
            deliver = sink.deliver
            attempts = []

            def unavailable_once(name):
                attempts.append(name)
                if len(attempts) == 1:
                    raise sqlite3.OperationalError("synthetic locked database")
                return cursor(name)

            def record_delivery(artifact, manifest, context):
                deliver(artifact, manifest, context)
                if not artifact:
                    delivered.set()
                    stop.set()

            with (
                patch.object(runtime.store, "cursor", side_effect=unavailable_once),
                patch.object(runtime.store, "failed", side_effect=sqlite3.OperationalError("locked")),
                patch.object(sink, "deliver", side_effect=record_delivery),
                patch.object(runtime, "poll", return_value={}),
                patch("builtins.print"),
                self.assertLogs("wtf_adsb_engine.runtime", level="WARNING") as logs,
            ):
                worker = threading.Thread(target=runtime.run, args=(stop,))
                worker.start()
                try:
                    self.assertTrue(delivered.wait(3), "Output worker must retry after a database error")
                finally:
                    stop.set()
                    worker.join(timeout=3)
                self.assertFalse(worker.is_alive())
            self.assertGreaterEqual(len(attempts), 2)
            self.assertEqual(cursor("files"), runtime.store.state()["head_sequence"])
            self.assertEqual(len(logs.output), 1)
            self.assertNotIn("synthetic locked database", logs.output[0])

    def test_format_state_survives_initialization_and_is_separate_from_sink_state(self):
        runtime = Runtime(self.config)
        _, sink, formatter = runtime.outputs[0]
        initialized = []
        encoded = []
        encode, deliver = formatter.encode, sink.deliver

        def initialize(context):
            initialized.append(context.instance)
            (context.state_directory / "format-state").write_text("initialized")

        def encode_with_state(batch, context):
            self.assertEqual(context.instance, initialized[0])
            self.assertEqual((context.state_directory / "format-state").read_text(), "initialized")
            encoded.append(context.state_directory)
            return encode(batch, context)

        def deliver_without_format_state(artifact, manifest, context):
            self.assertNotIn(context.state_directory, encoded)
            self.assertFalse((context.state_directory / "format-state").exists())
            return deliver(artifact, manifest, context)

        with (
            patch.object(formatter, "initialize", side_effect=initialize),
            patch.object(formatter, "encode", side_effect=encode_with_state),
            patch.object(sink, "deliver", side_effect=deliver_without_format_state),
            runtime.locked(),
        ):
            self.assertEqual(runtime.once()["outputs"], {"files": "ready"})
        self.assertTrue(encoded)

    def test_input_dependency_must_be_available_before_its_consumer(self):
        for capability in ("catalog", "receiver_snapshot"):
            with (
                self.subTest(capability=capability),
                patch.object(ReplayInput, "spec", replace(ReplayInput.spec, requires=(capability,))),
                self.assertRaisesRegex(ValueError, "dependency"),
            ):
                Runtime(self.config)
        self.assertFalse((self.root / "state").exists())

    def test_input_reference_failure_holds_checkpoint_and_retry_gets_isolated_context(self):
        reference = {"C172": {"label": "Synthetic reference"}}
        (self.root / "reference.json").write_text(json.dumps(reference))
        self.config.write_text(
            self.config.read_text()
            + '\n[[references]]\nname="catalog"\nplugin="wtf_adsb.json_reference"\n'
            + 'options={path="reference.json"}\n'
        )
        with patch.object(ReplayInput, "spec", replace(ReplayInput.spec, requires=("catalog",))):
            runtime = Runtime(self.config)
        source = runtime.pipelines[0][1][1]
        # Keep the declared dependency on this instance after restoring the class.
        source.spec = replace(source.spec, requires=("catalog",))
        detector = runtime.pipelines[0][2][0][1]
        poll, analyze = source.poll, detector.analyze

        def input_with_reference(checkpoint, context):
            self.assertEqual(context.mode, "replay")
            self.assertEqual(context.references["catalog"], reference)
            context.references["catalog"]["C172"]["label"] = "Input-local change"
            return poll(checkpoint, context)

        def detector_with_reference(batch, context):
            self.assertEqual(context.mode, "replay")
            self.assertEqual(context.references["catalog"], reference)
            return analyze(batch, context)

        with runtime.locked():
            with (
                patch.object(runtime.references[0][1], "lookup", side_effect=OSError("unavailable")),
                patch.object(source, "poll") as held,
            ):
                result = runtime.poll()
            held.assert_not_called()
            self.assertEqual(result["errors"], {"replay": "ValueError"})
            self.assertIsNone(runtime.store.checkpoint("replay"))
            with (
                patch.object(source, "poll", side_effect=input_with_reference) as retried,
                patch.object(detector, "analyze", side_effect=detector_with_reference),
            ):
                result = runtime.once()
            retried.assert_called_once()
            self.assertFalse(result["errors"])
            self.assertEqual(result["outputs"], {"files": "ready"})
            self.assertIsNotNone(runtime.store.checkpoint("replay"))

    def test_output_format_cannot_change_evidence(self):
        runtime = Runtime(self.config)
        with runtime.locked():
            runtime.poll()
            binding = runtime.outputs[0]

            def altered(batch, context):
                value = copy.deepcopy(batch)
                value["changes"][0]["observation"]["group"]["events"][0]["hits"] += 1
                return wire.canonical(value)

            with patch.object(binding[2], "encode", side_effect=altered):
                self.assertEqual(runtime.deliver(binding), "ValueError")
            self.assertEqual(runtime.store.cursor(binding[0]), 0)
            self.assertFalse((self.root / "export" / "manifest.json").exists())

    def test_check_does_not_start_input_or_create_state(self):
        with patch("builtins.print"):
            self.assertEqual(main(["check", "--config", str(self.config)]), 0)
        self.assertFalse((self.root / "state").exists())

    def test_changed_output_destination_cannot_reuse_a_delivery_cursor(self):
        runtime = Runtime(self.config)
        with runtime.locked():
            self.assertEqual(runtime.once()["outputs"]["files"], "ready")
        self.config.write_text(
            self.config.read_text().replace('directory = "export"', 'directory = "changed"')
        )
        with self.assertRaises(ValueError), Runtime(self.config).locked():
            self.fail("Changed destination must fail before initialization")

    def test_status_reads_while_runtime_owns_lock_and_starts_no_plugins(self):
        runtime = Runtime(self.config)
        with (
            runtime.locked(),
            patch("wtf_adsb_engine.cli.Runtime", side_effect=AssertionError("no plugin startup")),
            patch("builtins.print"),
        ):
            self.assertEqual(main(["status", "--config", str(self.config)]), 0)

    def test_partial_plugin_initialization_is_closed_on_failure(self):
        runtime = Runtime(self.config)
        source = runtime.pipelines[0][1][1]
        with (
            patch.object(source, "initialize", side_effect=RuntimeError("after allocation")),
            patch.object(source, "close") as closed,
        ):
            with self.assertRaises(RuntimeError), runtime.locked():
                self.fail("Initialization should fail")
        closed.assert_called_once()

    def test_continuous_receiver_collects_independently_of_projection(self):
        service = ReceiverService({"url": "http://127.0.0.1/fixture", "poll_seconds": 1})
        context = Context("receiver", self.root, "fixture", time.time())
        second = threading.Event()
        calls = []

        def poll(checkpoint, current):
            calls.append(current.now)
            if len(calls) > 1:
                second.set()
            return InputBatch(({"snapshot": {"now": current.now, "aircraft": []}},), current.now, current.now)

        with patch.object(service.input, "poll", side_effect=poll):
            try:
                service.initialize(context)
                self.assertTrue(second.wait(3), "Collector must keep polling while projection is occupied")
            finally:
                service.close()
        self.assertGreater(len(calls), 1)
        self.assertIsNone(service.error)

    def test_receiver_tolerates_one_failed_attempt_while_the_journal_is_fresh(self):
        service = ReceiverService({"url": "http://127.0.0.1/fixture", "poll_seconds": 60})
        context = Context("receiver", self.root, "fixture", time.time())

        def poll(checkpoint, current):
            return InputBatch(({"snapshot": {"now": current.now, "aircraft": []}},), current.now, current.now)

        with patch.object(service.input, "poll", side_effect=poll):
            try:
                service.initialize(context)
                self.assertEqual(service.health()["state"], "ready")
                service.error = "OperationalError"
                batch = service.poll(None, replace(context, now=time.time()))
                self.assertIsNotNone(batch.watermark)
                self.assertEqual(service.health()["state"], "degraded")
                self.assertEqual(service.health()["collection_error"], "OperationalError")
                with self.assertRaisesRegex(ValueError, "unavailable"):
                    service.poll(None, replace(context, now=time.time() + 121))
                service.last_success = time.time() - 121
                self.assertEqual(service.health()["state"], "failed")
                service.error = None
                with self.assertRaisesRegex(ValueError, "stale"):
                    service.poll(None, replace(context, now=time.time() + 121))
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
