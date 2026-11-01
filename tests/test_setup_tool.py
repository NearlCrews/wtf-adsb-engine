"""The setup tool's plan, configure, check, and deploy dry run work offline."""

import ast
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from wtf_adsb_engine.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "setup.py"
INTERVIEW = ROOT / "docs" / "setup-interview.md"


def run(*args):
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=120, check=False
    )
    return completed.returncode, json.loads(completed.stdout)


def document_rows():
    """Yield (group, key, type, default) from the interview document's tables."""
    group = None
    for line in INTERVIEW.read_text().splitlines():
        heading = re.match(r"## (.+)", line)
        if heading:
            group = heading[1]
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 5 and re.fullmatch(r"`[^`]+`", cells[0]):
            default = cells[2]
            if default.startswith("none"):
                value = None
            else:
                text = default.strip("`")
                try:
                    value = json.loads(text)
                except ValueError:
                    value = text
            yield group, cells[0].strip("`"), cells[1], value


class Receiver(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"now": time.time() - 1, "aircraft": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class SetupToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wtf-setup-")
        self.base = Path(self.temporary.name)
        self.output = self.base / "config"
        docker = self.base / "fake-docker"
        docker.write_text("#!/bin/sh\necho 'Docker Compose version v0.0.0-test'\n")
        docker.chmod(0o700)
        self.docker = str(docker)

    def tearDown(self):
        self.temporary.cleanup()

    def answers(self, url, **extra):
        values = {
            "receiver.url": url,
            "state_dir": str(self.base / "state"),
            "feed_dir": str(self.base / "feed"),
            "reference_dir": str(self.base / "references"),
            "uid": os.getuid(),
            "gid": os.getgid(),
            **extra,
        }
        path = self.base / "answers.json"
        path.write_text(json.dumps(values))
        return str(path)

    def configure(self, url="http://192.0.2.10:8080/data/aircraft.json", **extra):
        return run("configure", "--answers", self.answers(url, **extra), "--output", str(self.output))

    def test_tool_parses_on_the_oldest_supported_host_python(self):
        ast.parse(SCRIPT.read_text(), feature_version=(3, 11))

    def test_plan_lists_every_key_in_the_interview_document(self):
        code, result = run("plan")
        self.assertEqual(code, 0)
        planned = [
            (group["name"], question["key"], question["type"], question["default"])
            for group in result["groups"]
            for question in group["questions"]
        ]
        self.assertEqual(planned, list(document_rows()))
        self.assertTrue(all(q["validation"] and q["why"] for g in result["groups"] for q in g["questions"]))

    def test_configure_writes_private_files_the_engine_accepts(self):
        code, result = self.configure(flightaware=True, flightaware_key_file=str(self.output / "fa.key"))
        self.assertEqual(code, 0, result)
        config = self.output / "receiver.toml"
        parsed = tomllib.loads(config.read_text())
        options = parsed["pipelines"][0]["input"]["options"]
        self.assertEqual(parsed["pipelines"][0]["input"]["plugin"], "wtf_adsb.receiver_service")
        self.assertEqual(options["flightaware_key_file"], "/config/fa.key")
        self.assertEqual(options["flightaware_daily_budget_usd"], 0.5)
        self.assertEqual(
            parsed["outputs"][0],
            {"name": "files", "plugin": "wtf_adsb.files", "options": {"directory": "/feed"}},
        )
        runtime = Runtime(config)
        self.assertEqual((len(runtime.pipelines), len(runtime.outputs)), (1, 1))
        for name in ("receiver.toml", ".env", "setup.json"):
            self.assertEqual(stat.S_IMODE((self.output / name).stat().st_mode), 0o600, name)
        for name in ("config", "state", "feed", "references"):
            self.assertEqual(stat.S_IMODE((self.base / name).stat().st_mode), 0o700, name)
        env = (self.output / ".env").read_text()
        self.assertIn(f"ENGINE_CONFIG_DIR={self.output}\n", env)
        self.assertIn(f"ENGINE_REFERENCE_DIR={self.base / 'references'}\n", env)
        bundled = sorted(path.name for path in (ROOT / "src/wtf_adsb_engine/analysis/data").glob("*.json"))
        self.assertEqual(sorted(path.name for path in (self.base / "references").iterdir()), bundled)
        code, again = self.configure(flightaware=True, flightaware_key_file=str(self.output / "fa.key"))
        self.assertEqual((code, again["changed"]), (0, []))

    def test_configure_refuses_an_output_directory_inside_the_repository(self):
        target = ROOT / "state" / "setup-tool-test"
        code, result = run(
            "configure", "--answers", self.answers("http://192.0.2.10/aircraft.json"), "--output", str(target)
        )
        self.assertEqual(code, 1)
        self.assertIn("outside this repository", result["error"])
        self.assertFalse(target.exists())

    def test_configure_rejects_invalid_and_unsafe_answers(self):
        cases = (
            ({"receiver.url": "ftp://192.0.2.10/aircraft.json"}, "http"),
            ({"receiver.url": "http://user:secret@192.0.2.10/aircraft.json"}, "password"),
            ({"receiver.url": "http://192.0.2.10/index.html"}, "aircraft.json"),
            ({"flightaware": True}, "flightaware_key_file is required"),
            ({"flightaware": True, "flightaware_key_file": "/etc/fa.key"}, "inside the config directory"),
            ({"receiver.poll_seconds": 0}, "between"),
            ({"surprise": 1}, "Unknown answer keys"),
            ({"feed_dir": str(Path("/tmp") / "x" / ".." / "y")}, "without '..'"),
        )
        for extra, message in cases:
            with self.subTest(extra=extra):
                url = extra.pop("receiver.url", "http://192.0.2.10/aircraft.json")
                code, result = self.configure(url, **extra)
                self.assertEqual(code, 1)
                self.assertIn(message, result["error"])

    def test_configure_refuses_to_change_the_input_once_state_exists(self):
        self.assertEqual(self.configure()[0], 0)
        (self.base / "state" / "engine.sqlite3").touch()
        code, result = self.configure(**{"receiver.poll_seconds": 20})
        self.assertEqual(code, 1)
        self.assertIn("new state directory", result["error"])
        self.assertEqual(self.configure()[0], 0)

    def test_check_passes_against_a_reachable_receiver(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/data/aircraft.json"
        self.assertEqual(self.configure(url)[0], 0)
        code, result = run("check", "--output", str(self.output), "--docker", self.docker)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["checks"]["receiver"], {"ok": True, "status": 200, "aircraft_json": True})
        self.assertEqual(result["checks"]["configuration"]["engine_validation"], "passed")
        self.assertTrue(result["checks"]["directories"]["ok"])
        self.assertTrue(any("loopback" in warning for warning in result["warnings"]))

    def test_check_fails_cleanly_when_the_receiver_is_unreachable(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.assertEqual(self.configure(f"http://127.0.0.1:{port}/data/aircraft.json")[0], 0)
        code, result = run("check", "--output", str(self.output), "--docker", self.docker)
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["checks"]["receiver"], {"ok": False, "error": "URLError"})
        self.assertTrue(result["checks"]["configuration"]["ok"])

    def test_check_reports_a_missing_key_file_and_loose_modes(self):
        self.assertEqual(
            self.configure(flightaware=True, flightaware_key_file=str(self.output / "fa.key"))[0], 0
        )
        (self.base / "feed").chmod(0o755)
        code, result = run("check", "--output", str(self.output), "--docker", self.docker)
        problems = " ".join(result["checks"]["directories"]["problems"])
        self.assertEqual(code, 1)
        self.assertIn("fa.key: FileNotFoundError", problems)
        self.assertIn("mode 755", problems)

    def test_deploy_print_only_shows_the_compose_commands(self):
        self.assertEqual(self.configure(viewer=False)[0], 0)
        code, result = run("deploy", "--output", str(self.output), "--print-only", "--docker", self.docker)
        self.assertEqual(code, 0, result)
        base = [
            self.docker,
            "compose",
            "--env-file",
            str(self.output / ".env"),
            "-f",
            str(ROOT / "deploy/compose.yaml"),
        ]
        self.assertEqual(result["commands"]["up"], [*base, "up", "-d", "engine"])
        self.assertEqual(
            result["commands"]["health"],
            [*base, "exec", "-T", "engine", "wtf-adsb-engine", "health", "--config", "/config/receiver.toml"],
        )


if __name__ == "__main__":
    unittest.main()
