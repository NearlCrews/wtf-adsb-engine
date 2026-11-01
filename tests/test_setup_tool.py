"""The setup tool's plan, configure, check, and deploy dry run work offline."""

import ast
import contextlib
import importlib.util
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
from unittest import mock

from wtf_adsb_engine.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "setup.py"
INTERVIEW = ROOT / "docs" / "setup-interview.md"
SPEC = importlib.util.spec_from_file_location("wtf_setup_tool", SCRIPT)
setup_tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup_tool)


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
        self.assertEqual(list(result["commands"]), ["up", "remove_viewer", "health"])
        self.assertEqual(result["commands"]["up"], [*base, "up", "-d", "engine"])
        self.assertEqual(result["commands"]["remove_viewer"], [*base, "rm", "--stop", "--force", "viewer"])
        self.assertEqual(
            result["commands"]["health"],
            [*base, "exec", "-T", "engine", "wtf-adsb-engine", "health", "--config", "/config/receiver.toml"],
        )
        self.assertEqual(self.configure(viewer=True)[0], 0)
        code, result = run("deploy", "--output", str(self.output), "--print-only", "--docker", self.docker)
        self.assertEqual((code, list(result["commands"])), (0, ["up", "health"]))
        self.assertEqual(result["commands"]["up"], [*base, "up", "-d"])

    def test_usage_errors_print_one_json_object_and_exit_2(self):
        for args in (
            ("deploy",),
            ("deploy", "--output", str(self.output), "--timeout", "0"),
            ("deploy", "--output", str(self.output), "--timeout", "-5"),
            ("deploy", "--output", str(self.output), "--timeout", "nan"),
            ("deploy", "--output", str(self.output), "--timeout", "inf"),
            ("deploy", "--output", str(self.output), "--timeout", "soon"),
        ):
            with self.subTest(args=args):
                code, result = run(*args)
                self.assertEqual(code, 2)
                self.assertFalse(result["ok"])
                self.assertTrue(result["error"].startswith("usage: "), result)

    def test_configure_refuses_an_answers_file_inside_the_repository(self):
        code, result = run(
            "configure", "--answers", str(ROOT / "answers-test.json"), "--output", str(self.output)
        )
        self.assertEqual(code, 1)
        self.assertIn("answers file outside this repository", result["error"])
        self.assertFalse(self.output.exists())

    def test_configure_refuses_paths_that_contain_the_repository_or_sit_near_the_root(self):
        url = "http://192.0.2.10/aircraft.json"
        if len(ROOT.parent.parts) >= 3:
            code, result = run("configure", "--answers", self.answers(url), "--output", str(ROOT.parent))
            self.assertEqual(code, 1)
            self.assertIn("must not contain it", result["error"])
        top = self.base / "top"
        top.symlink_to("/")
        for value in ("/srv", "/srv/", "/opt", str(top / "srv")):
            with self.subTest(value=value):
                code, result = self.configure(url, state_dir=value)
                self.assertEqual(code, 1)
                self.assertIn("two directories below /", result["error"])
        self.assertFalse(self.output.exists())

    def test_configure_refuses_overlapping_directories(self):
        for extra in (
            {"feed_dir": str(self.base / "state" / "feed")},
            {"reference_dir": str(self.output / "references")},
            {"state_dir": str(self.base)},
        ):
            with self.subTest(extra=extra):
                code, result = self.configure(**extra)
                self.assertEqual(code, 1)
                self.assertIn("must be separate", result["error"])

    def test_configure_will_not_take_over_a_directory_holding_other_files(self):
        busy = self.base / "busy"
        busy.mkdir(mode=0o755)
        busy.chmod(0o755)
        (busy / "notes.txt").write_text("keep me\n")
        code, result = self.configure(state_dir=str(busy))
        self.assertEqual(code, 1)
        self.assertIn("will not change it", result["error"])
        self.assertEqual(stat.S_IMODE(busy.stat().st_mode), 0o755)
        # A directory holding only the answers file and files configure writes is taken over.
        self.output.mkdir(mode=0o755, exist_ok=True)
        self.output.chmod(0o755)
        answers = Path(self.answers("http://192.0.2.10/aircraft.json"))
        moved = self.output / "answers.json"
        answers.rename(moved)
        code, result = run("configure", "--answers", str(moved), "--output", str(self.output))
        self.assertEqual(code, 0, result)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)

    @contextlib.contextmanager
    def as_root(self, sudo_uid=None):
        """Pretend configure runs as root, with SUDO_UID set or unset; yield the recorded fchown."""
        environment = {key: value for key, value in os.environ.items() if key != "SUDO_UID"}
        if sudo_uid is not None:
            environment["SUDO_UID"] = str(sudo_uid)
        with (
            mock.patch.object(setup_tool.os, "geteuid", return_value=0),
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(setup_tool.os, "fchown") as fchown,
        ):
            yield fchown

    def loose_directory(self, name, *contents):
        path = self.base / name
        path.mkdir(mode=0o755, parents=True)
        path.chmod(0o755)
        for item in contents:
            (path / item).write_text("keep me\n")
        return path

    def test_sudo_refuses_an_existing_directory_owned_by_another_account(self):
        # The directory belongs to the test's own account, while the target uid and the
        # sudo user are other accounts, as for a root-owned /usr/local/bin or /etc/sudoers.d.
        owner = os.getuid()
        values = {"uid": owner + 1, "gid": os.getgid()}
        refusal = f"belongs to uid {owner}, not uid {owner + 1}"
        for sudo_uid in (None, 0, owner + 2):
            for contents in ((), ("README",)):
                with self.subTest(sudo_uid=sudo_uid, contents=contents):
                    target = self.loose_directory(f"foreign-{sudo_uid}-{len(contents)}", *contents)
                    with self.as_root(sudo_uid) as fchown:
                        with self.assertRaisesRegex(setup_tool.SetupError, refusal):
                            setup_tool.make_directory(target, values, frozenset(contents))
                    fchown.assert_not_called()
                    self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    def test_sudo_takes_over_only_the_sudo_users_the_target_uids_or_new_directories(self):
        owner, group = os.getuid(), os.getgid()
        cases = (
            ("sudo-user", owner, {"uid": owner + 1, "gid": group}, ("answers.json",)),
            ("target-uid", None, {"uid": owner, "gid": group}, ()),
            ("created/state", None, {"uid": owner + 1, "gid": group}, None),
        )
        for name, sudo_uid, values, contents in cases:
            with self.subTest(name=name):
                target = self.base / name if contents is None else self.loose_directory(name, *contents)
                with self.as_root(sudo_uid) as fchown:
                    setup_tool.make_directory(target, values, frozenset(contents or ()))
                fchown.assert_called_once_with(mock.ANY, values["uid"], group)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)

    def test_configure_refuses_a_symlinked_output_directory_or_config_file(self):
        real = self.base / "real-config"
        real.mkdir(mode=0o700)
        self.output.symlink_to(real)
        code, result = self.configure()
        self.assertEqual(code, 1)
        self.assertIn("real directory", result["error"])
        self.output.unlink()
        self.assertEqual(self.configure()[0], 0)
        elsewhere = self.base / "elsewhere.toml"
        elsewhere.write_text("untouched\n")
        elsewhere.chmod(0o644)
        (self.output / "receiver.toml").unlink()
        (self.output / "receiver.toml").symlink_to(elsewhere)
        code, result = self.configure(**{"receiver.poll_seconds": 20})
        self.assertEqual(code, 1)
        self.assertIn("not a symbolic link", result["error"])
        self.assertEqual(elsewhere.read_text(), "untouched\n")
        self.assertEqual(stat.S_IMODE(elsewhere.stat().st_mode), 0o644)

    def test_null_answers_take_their_defaults(self):
        code, result = self.configure(uid=None, gid=None, memory_limit=None, flightaware_key_file=None)
        self.assertEqual(code, 0, result)
        env = (self.output / ".env").read_text()
        self.assertIn("ENGINE_UID=1000\n", env)
        self.assertIn("ENGINE_GID=1000\n", env)
        self.assertIn("ENGINE_MEMORY_LIMIT=2g\n", env)
        self.assertNotIn("None", env)
        code, result = self.configure(None)
        self.assertEqual(code, 1)
        self.assertIn("receiver.url is required", result["error"])

    @unittest.skipIf(os.geteuid() == 0, "root can read any directory")
    def test_unreadable_settings_ask_for_sudo(self):
        self.assertEqual(self.configure()[0], 0)
        self.output.chmod(0)
        try:
            for command in ("check", "deploy"):
                with self.subTest(command=command):
                    code, result = run(command, "--output", str(self.output), "--docker", self.docker)
                    self.assertEqual(code, 1)
                    self.assertIn("rerun this with sudo", result["error"])
                    self.assertNotIn("run configure first", result["error"])
        finally:
            self.output.chmod(0o700)

    def test_probe_reports_a_malformed_http_response(self):
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)

        def answer():
            connection, _ = listener.accept()
            with connection:
                connection.recv(65536)
                connection.sendall(b"not an HTTP status line\r\n\r\n")

        thread = threading.Thread(target=answer, daemon=True)
        thread.start()
        result = setup_tool.probe(f"http://127.0.0.1:{listener.getsockname()[1]}/aircraft.json")
        thread.join(5)
        self.assertEqual(result, {"ok": False, "error": "BadStatusLine"})

    def test_agents_and_claude_instructions_are_identical(self):
        self.assertEqual((ROOT / "AGENTS.md").read_bytes(), (ROOT / "CLAUDE.md").read_bytes())


if __name__ == "__main__":
    unittest.main()
