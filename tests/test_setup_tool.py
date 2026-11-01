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


class StaticTests(unittest.TestCase):
    """Tests that write nothing, so they also run as root."""

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

    def test_plan_and_interview_list_the_same_configuration_files(self):
        files = "answers file, receiver.toml, .env, setup.json, and any provider key file"
        self.assertIn(files, setup_tool.plan()["configuration_directory"])
        self.assertIn(files, " ".join(INTERVIEW.read_text().replace("`", "").split()))
        self.assertIn("chown -R", setup_tool.plan()["fixed_answers"])

    def test_usage_errors_print_one_json_object_and_exit_2(self):
        output = "/srv/wtf-adsb/config"  # never opened: argument parsing fails first
        for args in (
            ("deploy",),
            ("deploy", "--output", output, "--timeout", "0"),
            ("deploy", "--output", output, "--timeout", "-5"),
            ("deploy", "--output", output, "--timeout", "nan"),
            ("deploy", "--output", output, "--timeout", "inf"),
            ("deploy", "--output", output, "--timeout", "soon"),
        ):
            with self.subTest(args=args):
                code, result = run(*args)
                self.assertEqual(code, 2)
                self.assertFalse(result["ok"])
                self.assertTrue(result["error"].startswith("usage: "), result)

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


@unittest.skipIf(os.geteuid() == 0, "configure and check expect an unprivileged account")
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
        for name, mode in (("receiver.toml", 0o640), (".env", 0o640), ("setup.json", 0o600)):
            self.assertEqual(stat.S_IMODE((self.output / name).stat().st_mode), mode, name)
        for name, mode in (("config", 0o750), ("state", 0o700), ("feed", 0o700), ("references", 0o700)):
            self.assertEqual(stat.S_IMODE((self.base / name).stat().st_mode), mode, name)
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

    def test_check_does_not_follow_a_symlinked_reference_table(self):
        self.assertEqual(self.configure()[0], 0)
        table = next((self.base / "references").iterdir())
        elsewhere = self.base / "elsewhere.json"
        elsewhere.write_text("{}\n")
        table.unlink()
        table.symlink_to(elsewhere)
        code, result = run("check", "--output", str(self.output), "--docker", self.docker)
        problems = result["checks"]["directories"]["problems"]
        self.assertEqual(code, 1)
        self.assertIn(f"{table}: not a regular file", problems)
        self.assertFalse(any("differs from the bundled table" in problem for problem in problems), problems)

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
        moved.chmod(0o644)
        code, result = run("configure", "--answers", str(moved), "--output", str(self.output))
        self.assertEqual(code, 0, result)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o750)
        self.assertEqual(stat.S_IMODE(moved.stat().st_mode), 0o600)

    @contextlib.contextmanager
    def as_root(self, sudo_uid=None, ancestors=True):
        """Pretend the tool runs as root, with SUDO_UID set or unset; yield the recorded fchown.

        `fchown.owners` maps each path given an owner to its (uid, gid). With `ancestors`
        false, the parent check is skipped, since a test's temporary directory belongs to the
        test's account rather than root.
        """
        environment = {key: value for key, value in os.environ.items() if key != "SUDO_UID"}
        if sudo_uid is not None:
            environment["SUDO_UID"] = str(sudo_uid)
        owners = {}

        def record(descriptor, uid, gid):
            path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            name = path.name[1:-4] if path.name.startswith(".") and path.name.endswith(".tmp") else path.name
            owners[path.with_name(name)] = (uid, gid)

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(setup_tool.os, "geteuid", return_value=0))
            stack.enter_context(mock.patch.dict(os.environ, environment, clear=True))
            fchown = stack.enter_context(mock.patch.object(setup_tool.os, "fchown", side_effect=record))
            if not ancestors:
                stack.enter_context(mock.patch.object(setup_tool, "check_ancestors"))
            fchown.owners = owners
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
                    with self.as_root(sudo_uid, ancestors=False) as fchown:
                        with self.assertRaisesRegex(setup_tool.SetupError, refusal):
                            setup_tool.make_directory(target, values, "data", frozenset(contents))
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
                with self.as_root(sudo_uid, ancestors=False) as fchown:
                    os.close(setup_tool.make_directory(target, values, "data", frozenset(contents or ())))
                fchown.assert_called_once_with(mock.ANY, values["uid"], group)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        # The configuration directory goes to the sudo user, or to root without one, never to uid.
        for sudo_uid, expected in ((owner, owner), (None, 0)):
            with self.subTest(config=sudo_uid):
                target = self.base / f"config-{sudo_uid}"
                with self.as_root(sudo_uid, ancestors=False) as fchown:
                    os.close(setup_tool.make_directory(target, {"uid": owner + 1, "gid": group}, "config"))
                fchown.assert_called_once_with(mock.ANY, expected, group)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o750)

    def sudo_configure(self, sudo_uid):
        """Run configure in process as simulated root, with the answers kept in the config directory."""
        self.output.mkdir(mode=0o700)
        answers = Path(self.answers("http://192.0.2.10/aircraft.json", uid=os.getuid() + 1))
        moved = self.output / "answers.json"
        answers.rename(moved)
        moved.chmod(0o644)
        with self.as_root(sudo_uid) as fchown:
            result = setup_tool.configure(str(moved), str(self.output))
        return result, fchown.owners

    def test_sudo_configure_keeps_the_configuration_with_the_operator(self):
        owner, group = os.getuid(), os.getgid()
        result, owners = self.sudo_configure(owner)
        self.assertTrue(result["ok"], result)
        operator, container = (owner, group), (owner + 1, group)
        expected = {self.output: operator}
        for name in ("receiver.toml", ".env", "setup.json", "answers.json"):
            expected[self.output / name] = operator
        for name in ("state", "feed", "references"):
            expected[self.base / name] = container
        for table in (ROOT / "src/wtf_adsb_engine/analysis/data").glob("*.json"):
            expected[self.base / "references" / table.name] = container
        self.assertEqual(owners, expected)
        modes = {"receiver.toml": 0o640, ".env": 0o640, "setup.json": 0o600, "answers.json": 0o600}
        for name, mode in modes.items():
            self.assertEqual(stat.S_IMODE((self.output / name).stat().st_mode), mode, name)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o750)
        for name in ("state", "feed", "references"):
            self.assertEqual(stat.S_IMODE((self.base / name).stat().st_mode), 0o700, name)
        # The configuration files already have their owner and stay. The simulated fchown never
        # gave the tables to uid, so they are replaced again rather than re-owned in place.
        with self.as_root(owner):
            changed = setup_tool.configure(str(self.output / "answers.json"), str(self.output))["changed"]
        tables = (ROOT / "src/wtf_adsb_engine/analysis/data").glob("*.json")
        self.assertEqual(sorted(changed), sorted(str(self.base / "references" / t.name) for t in tables))

    def test_sudo_replaces_an_unchanged_file_whose_owner_changes(self):
        owner, group = os.getuid(), os.getgid()
        values = {"uid": owner, "gid": group}
        self.output.mkdir(mode=0o750)
        settings = self.output / "setup.json"
        settings.write_text("{}\n")
        settings.chmod(0o600)
        before = settings.stat().st_ino
        descriptor = os.open(self.output, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with open(settings, "r+b") as held:
                # The file belongs to uid, and the sudo user takes the configuration over.
                with self.as_root(owner + 5):
                    self.assertTrue(
                        setup_tool.write_private(
                            descriptor,
                            "setup.json",
                            "{}\n",
                            setup_tool.owner_for("config", values),
                            0o600,
                            settings,
                        )
                    )
                held.write(b"tampered")
                held.flush()
            replaced = settings.stat().st_ino
            self.assertNotEqual(replaced, before)
            self.assertEqual(settings.read_text(), "{}\n")
            # A file that already has the operator as owner stays in place.
            with self.as_root(owner):
                self.assertFalse(
                    setup_tool.write_private(
                        descriptor,
                        "setup.json",
                        "{}\n",
                        setup_tool.owner_for("config", values),
                        0o600,
                        settings,
                    )
                )
            self.assertEqual(settings.stat().st_ino, replaced)
        finally:
            os.close(descriptor)

    def test_sudo_rechecks_parents_created_while_making_a_directory(self):
        owner, values = os.getuid(), {"uid": os.getuid() + 1, "gid": os.getgid()}
        middle = self.base / "middle"
        create = os.makedirs

        def racing_makedirs(path, mode=0o777, exist_ok=False):
            # Another account creates the missing parent first, open to everyone.
            middle.mkdir()
            middle.chmod(0o777)
            create(path, mode=mode, exist_ok=exist_ok)

        with (
            self.as_root(owner),
            mock.patch.object(setup_tool.os, "makedirs", side_effect=racing_makedirs),
            self.assertRaisesRegex(setup_tool.SetupError, f"{middle} is writable by group or others"),
        ):
            os.close(setup_tool.make_directory(middle / "state", values, "data"))

    def test_sudo_refuses_parents_another_account_could_swap(self):
        owner, values = os.getuid(), {"uid": os.getuid() + 1, "gid": os.getgid()}
        # The temporary directory belongs to the test's account, which is not the sudo user here.
        # The refusal names the first such parent, which may sit above it under a custom TMPDIR.
        for sudo_uid in (None, owner + 2):
            with self.subTest(sudo_uid=sudo_uid), self.as_root(sudo_uid) as fchown:
                with self.assertRaisesRegex(setup_tool.SetupError, f"belongs to uid {owner}\\."):
                    setup_tool.make_directory(self.base / "state", values, "data")
                fchown.assert_not_called()
            self.assertFalse((self.base / "state").exists())
        self.base.chmod(0o775)
        try:
            with (
                self.as_root(owner),
                self.assertRaisesRegex(setup_tool.SetupError, "writable by group or others"),
            ):
                setup_tool.make_directory(self.base / "state", values, "data")
        finally:
            self.base.chmod(0o700)
        self.assertFalse((self.base / "state").exists())
        link = self.base / "link"
        link.symlink_to(self.base)
        with self.as_root(owner):
            os.close(setup_tool.make_directory(link / "state", values, "data"))
        self.assertTrue((self.base / "state").is_dir())
        self.assertEqual(self.configure()[0], 0)
        with self.as_root(None), self.assertRaisesRegex(setup_tool.SetupError, f"belongs to uid {owner}\\."):
            setup_tool.check(str(self.output), self.docker)

    def test_sudo_refuses_an_answers_file_another_account_owns(self):
        answers = self.answers("http://192.0.2.10/aircraft.json")
        with (
            self.as_root(os.getuid() + 3),
            self.assertRaisesRegex(setup_tool.SetupError, f"answers file belongs to uid {os.getuid()}"),
        ):
            setup_tool.configure(answers, str(self.output))
        self.assertFalse(self.output.exists())

    def test_writes_stay_in_the_opened_directory_after_a_swap(self):
        values = {"uid": os.getuid(), "gid": os.getgid()}
        target, elsewhere = self.base / "references", self.base / "elsewhere"
        elsewhere.mkdir()
        descriptor = setup_tool.make_directory(target, values, "data")
        try:
            target.rename(self.base / "moved")
            target.symlink_to(elsewhere)
            setup_tool.write_private(descriptor, "table.json", "{}\n", None, 0o644, target / "table.json")
        finally:
            os.close(descriptor)
        self.assertEqual((self.base / "moved" / "table.json").read_text(), "{}\n")
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_write_private_replaces_a_hard_link_instead_of_securing_it(self):
        outside = self.base / "outside.json"
        outside.write_text("{}\n")
        outside.chmod(0o644)
        target = self.base / "references"
        target.mkdir(mode=0o700)
        os.link(outside, target / "table.json")
        descriptor = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
        try:
            changed = setup_tool.write_private(descriptor, "table.json", "{}\n", None, 0o600, "table.json")
        finally:
            os.close(descriptor)
        self.assertTrue(changed)
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o644)
        self.assertEqual(outside.stat().st_nlink, 1)
        self.assertEqual(stat.S_IMODE((target / "table.json").stat().st_mode), 0o600)

    def test_check_and_deploy_refuse_files_the_container_account_could_change(self):
        self.assertEqual(self.configure(viewer=False)[0], 0)
        env = self.output / ".env"
        original = env.read_bytes()
        for path, loose, restore in (
            (self.output, 0o770, 0o750),
            (self.output / "setup.json", 0o620, 0o600),
            (env, 0o660, 0o640),
        ):
            path.chmod(loose)
            try:
                for command in ("check", "deploy"):
                    with self.subTest(path=path.name, command=command):
                        code, result = run(command, "--output", str(self.output), "--docker", self.docker)
                        self.assertEqual(code, 1)
                        self.assertIn(f"{path} is writable by group or others", result["error"])
            finally:
                path.chmod(restore)
        self.assertEqual(env.read_bytes(), original)
        # Here the configured uid is the test's account, and the sudo user is someone else.
        owner = os.getuid()
        for call in (
            lambda: setup_tool.check(str(self.output), self.docker),
            lambda: setup_tool.deploy(str(self.output), self.docker, False, 1),
        ):
            with self.as_root(owner + 5, ancestors=False):
                with self.assertRaisesRegex(setup_tool.SetupError, f"{self.output} belongs to uid {owner}"):
                    call()
        descriptor = os.open(self.output, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for name in (".env", "setup.json"):
                with self.subTest(name=name), self.as_root(owner + 5):
                    with self.assertRaisesRegex(setup_tool.SetupError, f"{name} belongs to uid {owner}"):
                        setup_tool.open_trusted(descriptor, name, self.output / name)
        finally:
            os.close(descriptor)
        self.assertEqual(env.read_bytes(), original)

    def test_deploy_renders_env_from_setup_json_before_compose_reads_it(self):
        self.assertEqual(self.configure(viewer=False)[0], 0)
        env = self.output / ".env"
        rendered = env.read_text()
        env.write_text(rendered.replace(f"ENGINE_UID={os.getuid()}\n", "ENGINE_UID=0\n"))
        self.assertIn("ENGINE_UID=0\n", env.read_text())
        code, result = run("check", "--output", str(self.output), "--docker", self.docker)
        self.assertIn(
            f"{env}: differs from setup.json; rerun configure", result["checks"]["directories"]["problems"]
        )
        seen = self.base / "seen.env"
        docker = self.base / "recording-docker"
        docker.write_text(
            "#!/bin/sh\n"
            f'case " $* " in *" up "*) cat "$3" > {seen} ;; esac\n'
            "echo 'Docker Compose version v0.0.0-test'\n"
        )
        docker.chmod(0o700)
        code, result = run("deploy", "--output", str(self.output), "--docker", str(docker), "--timeout", "1")
        self.assertEqual(code, 0, result)
        self.assertEqual(seen.read_text(), rendered)
        self.assertEqual(env.read_text(), rendered)
        self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o640)

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
            self.output.chmod(0o750)


if __name__ == "__main__":
    unittest.main()
