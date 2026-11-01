"""Installed external plugin checks; wheel build, installation and execution stay offline."""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/plugins/reference-label"
NAMESPACE = "org.example.reference_label"
MODULE = "wtf_adsb_example_reference_label"

RUNTIME_DRIVER = """
import json
import sys
from dataclasses import replace
from pathlib import Path
from wtf_adsb_engine import wire
from wtf_adsb_engine.runtime import Runtime

runtime = Runtime(sys.argv[1])
with runtime.locked():
    first = runtime.once()
    before = runtime.store.checkpoint('replay')
    previous_head = runtime.store.state()['head_sequence']
    enricher = runtime.pipelines[0][3][0][1]
    original = enricher.enrich
    mode = sys.argv[2]
    if mode != 'good':
        def fault(observation, context):
            item = original(observation, context)
            if mode == 'stale':
                return replace(item, input_digest='0' * 64)
            if mode == 'namespace':
                return replace(item, namespace='org.example.unapproved')
            if mode == 'version':
                return replace(item, schema_version='2.0.0')
            if mode == 'unavailable':
                return replace(item, state='unavailable')
            if mode == 'payload':
                return replace(item, payload={**item.payload, 'undeclared': 'field'})
            raise AssertionError('Unknown test mode')
        enricher.enrich = fault
    second = runtime.once()
    after = runtime.store.checkpoint('replay')
    second_state = runtime.store.state()
    enricher.enrich = original
    retry = runtime.once()
    final = runtime.store.state()

export_root = Path(sys.argv[1]).parent / 'export'
manifest = wire.validate_manifest(wire.loads((export_root / 'manifest.json').read_bytes()))
last = manifest['batches'][-1]
batch = wire.validate_batch(wire.loads((export_root / last['path']).read_bytes()), allow_synthetic=True)
extension = batch['changes'][-1]['observation']['extensions'][0]
print(json.dumps({
    'first': first, 'second': second, 'retry': retry,
    'before': before, 'after': after, 'previous_head': previous_head,
    'second_head': second_state['head_sequence'], 'final_head': final['head_sequence'],
    'second_health': second_state['health'], 'final_health': final['health'],
    'extension': extension, 'declarations': batch['extension_schemas'],
    'manifest_head': manifest['head_sequence'],
}))
"""


class InstalledPluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if importlib.util.find_spec("hatchling") is None:
            raise AssertionError("Install the engine dev dependencies, including pinned Hatchling")
        uv = shutil.which("uv")
        if uv is None:
            raise AssertionError("The offline plugin installation check requires uv")
        cls.installed = tempfile.TemporaryDirectory(prefix="wtf-adsb-plugin-wheel-")
        cls.addClassCleanup(cls.installed.cleanup)
        folder = Path(cls.installed.name)
        cls.site = folder / "site"
        distribution = folder / "dist"
        environment = {**os.environ, "UV_OFFLINE": "1", "PIP_NO_INDEX": "1"}
        built = subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(distribution),
                str(EXAMPLE),
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if built.returncode:
            raise AssertionError(built.stdout + built.stderr)
        wheels = list(distribution.glob("*.whl"))
        if len(wheels) != 1:
            raise AssertionError("Expected one external plugin wheel")
        installed = subprocess.run(
            [
                uv,
                "pip",
                "install",
                "--offline",
                "--no-deps",
                "--python",
                sys.executable,
                "--target",
                str(cls.site),
                str(wheels[0]),
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if installed.returncode:
            raise AssertionError(installed.stdout + installed.stderr)
        if not list(cls.site.glob("*.dist-info/entry_points.txt")):
            raise AssertionError("Wheel installation did not install entry point metadata")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="wtf-adsb-plugin-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.probe_site = self.directory / "probe-site"
        self.probe_site.mkdir()

    def run_code(self, source, *args):
        environment = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(self.probe_site), str(self.site))),
            "UV_OFFLINE": "1",
            "PIP_NO_INDEX": "1",
        }
        offline = (
            "import socket\n"
            "def reject_network(*args, **kwargs):\n"
            "    raise AssertionError('Offline plugin test attempted network access')\n"
            "socket.create_connection = reject_network\n"
            "socket.socket.connect = reject_network\n"
            "socket.socket.connect_ex = reject_network\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", offline + textwrap.dedent(source), *map(str, args)],
            env=environment,
            cwd=self.directory,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def configuration(self):
        source = self.directory / "receiver.ndjson"
        with source.open("w") as output:
            for index in range(8):
                output.write(
                    json.dumps(
                        {
                            "now": 1790856000 + index * 10,
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
                        }
                    )
                    + "\n"
                )
        shutil.copyfile(EXAMPLE / "src" / MODULE / "schema.json", self.directory / "extension.schema.json")
        config = self.directory / "config.toml"
        config.write_text(
            textwrap.dedent("""
            version = 1
            state_directory = "state"
            source_instance = "external-plugin-test"
            synthetic = true

            [[extensions]]
            namespace = "org.example.reference_label"
            schema_version = "1.0.0"
            path = "extension.schema.json"

            [[pipelines]]
            name = "replay"
            input = { plugin = "wtf_adsb.json_replay", options = { path = "receiver.ndjson", records_per_poll = 4 } }
            detectors = [{ plugin = "wtf_adsb.collection" }]
            enrichers = [{ plugin = "org.example.reference_label", options = { label = "Offline reference" } }]

            [[outputs]]
            name = "files"
            plugin = "wtf_adsb.files"
            options = { directory = "export" }
        """)
        )
        return config

    def probe(self, entry_name, source):
        distribution = self.probe_site / "wtf_adsb_sdk_probe-0.0.0.dist-info"
        distribution.mkdir()
        (distribution / "METADATA").write_text(
            "Metadata-Version: 2.3\nName: wtf-adsb-sdk-probe\nVersion: 0.0.0\n"
        )
        (distribution / "entry_points.txt").write_text(
            f"[wtf_adsb_engine.enrichers]\n{entry_name} = sdk_probe:Factory\n"
        )
        (self.probe_site / "sdk_probe.py").write_text(textwrap.dedent(source))

    def test_wheel_metadata_discovery_does_not_import_implementation(self):
        value = self.run_code("""
            import json
            import sys
            from wtf_adsb_engine import registry
            before = 'wtf_adsb_example_reference_label' in sys.modules
            rows = [row for row in registry.discovered() if row['id'] == 'org.example.reference_label']
            after = 'wtf_adsb_example_reference_label' in sys.modules
            print(json.dumps({'before': before, 'after': after, 'rows': rows}))
        """)
        self.assertFalse(value["before"])
        self.assertFalse(value["after"])
        self.assertEqual(value["rows"][0]["distribution"], "wtf-adsb-example-reference-label")

    def test_unselected_plugin_code_is_never_imported_by_listing(self):
        self.probe("org.example.unselected", "raise AssertionError('Unselected code executed')")
        rows = self.run_code("""
            import json
            from wtf_adsb_engine import registry
            print(json.dumps(registry.discovered()))
        """)
        self.assertIn("org.example.unselected", {row["id"] for row in rows})

    def test_duplicate_registration_is_rejected_before_import(self):
        self.probe(NAMESPACE, "raise AssertionError('Ambiguous code executed')")
        value = self.run_code("""
            import json
            from wtf_adsb_engine import registry
            try:
                registry.load('enrichers', 'org.example.reference_label', {})
            except ValueError:
                print(json.dumps({'rejected': True}))
            else:
                raise AssertionError('Ambiguous plugin was accepted')
        """)
        self.assertTrue(value["rejected"])

    def test_incompatible_sdk_is_rejected_before_construction(self):
        self.probe(
            "org.example.incompatible",
            """
            from wtf_adsb_engine.plugin_api.v1 import PluginSpec
            class Factory:
                spec = PluginSpec('org.example.incompatible', 'enrichers', api_version=99)
                def __init__(self, settings):
                    raise AssertionError('Incompatible plugin was constructed')
        """,
        )
        value = self.run_code("""
            import json
            from wtf_adsb_engine import registry
            try:
                registry.load('enrichers', 'org.example.incompatible', {})
            except ValueError:
                print(json.dumps({'rejected': True}))
            else:
                raise AssertionError('Incompatible SDK was accepted')
        """)
        self.assertTrue(value["rejected"])

    def test_plugin_configuration_rejects_wrong_types_and_unknown_fields(self):
        result = self.run_code("""
            import json
            from jsonschema import ValidationError
            from wtf_adsb_engine import registry
            count = 0
            for options in ({'label': 7}, {'unknown': True}, {'label': ''}):
                try:
                    registry.load('enrichers', 'org.example.reference_label', options)
                except ValidationError:
                    count += 1
            plugin = registry.load('enrichers', 'org.example.reference_label', {'label': 'Valid label'})
            print(json.dumps({'rejected': count, 'label': plugin.label}))
        """)
        self.assertEqual(result, {"rejected": 3, "label": "Valid label"})

    def test_installed_plugin_runs_to_schema_declared_json(self):
        value = self.run_code(RUNTIME_DRIVER, self.configuration(), "good")
        self.assertEqual(value["first"]["errors"], {})
        self.assertEqual(value["second"]["errors"], {})
        self.assertEqual(value["retry"]["errors"], {})
        self.assertEqual(value["final_head"], value["second_head"])
        self.assertEqual(value["manifest_head"], value["final_head"])
        self.assertEqual(value["extension"]["namespace"], NAMESPACE)
        self.assertEqual(value["extension"]["payload"]["label"], "Offline reference")
        self.assertEqual(value["declarations"][0]["namespace"], NAMESPACE)
        schema = json.loads((EXAMPLE / "src" / MODULE / "schema.json").read_text())
        canonical = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(value["declarations"][0]["sha256"], hashlib.sha256(canonical).hexdigest())

    def test_failed_enrichment_preserves_checkpoint_and_can_retry(self):
        for mode in ("stale", "namespace", "version", "unavailable", "payload"):
            with self.subTest(mode=mode):
                # Each subprocess needs its own unmodified replay state.
                case = self.directory / mode
                case.mkdir()
                prior = self.directory
                self.directory = case
                try:
                    config = self.configuration()
                    value = self.run_code(RUNTIME_DRIVER, config, mode)
                finally:
                    self.directory = prior
                self.assertEqual(value["first"]["errors"], {})
                self.assertIn("replay", value["second"]["errors"])
                self.assertEqual(value["after"], value["before"])
                self.assertEqual(value["second_head"], value["previous_head"])
                self.assertFalse(value["second_health"]["ready"])
                self.assertEqual(value["retry"]["errors"], {})
                self.assertGreater(value["final_head"], value["second_head"])
                self.assertTrue(value["final_health"]["complete"])

    def test_approved_schemas_are_closed_objects_without_remote_resolution(self):
        config = self.configuration()
        schema_file = self.directory / "extension.schema.json"
        for schema in (
            {},
            {"type": ["object", "null"]},
            {"type": "object", "properties": {"label": {"type": "string"}}},
            {
                "type": "object",
                "additionalProperties": False,
                "$dynamicRef": "https://example.org/untrusted-schema.json",
            },
        ):
            with self.subTest(schema=schema):
                schema_file.write_text(json.dumps(schema))
                value = self.run_code(
                    """
                    import json
                    import socket
                    import sys
                    from unittest.mock import patch
                    from wtf_adsb_engine.runtime import Runtime
                    with patch.object(socket, 'socket', side_effect=AssertionError('Schema used network')):
                        try:
                            Runtime(sys.argv[1])
                        except ValueError:
                            print(json.dumps({'rejected': True}))
                        else:
                            raise AssertionError('Unsafe schema configuration was accepted')
                """,
                    config,
                )
                self.assertTrue(value["rejected"])


if __name__ == "__main__":
    unittest.main()
