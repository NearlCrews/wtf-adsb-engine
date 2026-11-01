"""Configure, check, and start the engine's Docker Compose deployment.

An AI assistant or a person runs these subcommands after the interview in
docs/setup-interview.md. Each one prints exactly one JSON object on stdout and
exits nonzero on failure. The tool uses only the Python standard library and
runs on Python 3.11 or newer, since the host's Python may be older than the
engine's. It never reads provider key files; it checks their metadata only.
"""

import argparse
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INTERVIEW = ROOT / "docs" / "setup-interview.md"
COMPOSE = ROOT / "deploy" / "compose.yaml"
REFERENCE_SOURCE = ROOT / "src" / "wtf_adsb_engine" / "analysis" / "data"
CONFIG_NAME = "receiver.toml"
ENV_NAME = ".env"
SETTINGS_NAME = "setup.json"
CONTAINER_CONFIG = "/config/" + CONFIG_NAME
SAFE_PATH = re.compile(r"/[A-Za-z0-9._/-]+")
INSTANCE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
MEMORY = re.compile(r"[1-9][0-9]*[bkmg]")
IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}")
PROBE_TIMEOUT = 5
HEALTH_INTERVAL = 5


class SetupError(Exception):
    """A user-facing failure; the message is safe to print."""


@dataclass(frozen=True)
class Question:
    group: str
    key: str
    type: str
    default: object = None
    required: bool = False
    minimum: float | None = None
    maximum: float | None = None


QUESTIONS = (
    Question("Receiver", "receiver.url", "url", required=True),
    Question("Receiver", "receiver.poll_seconds", "number", 10, minimum=1, maximum=60),
    Question("Receiver", "receiver.max_age_seconds", "number", 120, minimum=1, maximum=3600),
    Question("Directories", "state_dir", "path", "/srv/wtf-adsb/state"),
    Question("Directories", "feed_dir", "path", "/srv/wtf-adsb/feed"),
    Question("Directories", "reference_dir", "path", "/srv/wtf-adsb/references"),
    Question("Providers", "type_registry", "boolean", False),
    Question("Providers", "wind", "boolean", False),
    Question("Providers", "flightaware", "boolean", False),
    Question("Providers", "flightaware_key_file", "path"),
    Question("Providers", "flightaware_daily_budget_usd", "number", 0.5, minimum=0, maximum=1000),
    Question("Providers", "flightaware_monthly_budget_usd", "number", 10, minimum=0, maximum=1000),
    Question("Providers", "flightaware_pause_file", "path"),
    Question("Providers", "fleet_reference_file", "path"),
    Question("Identity", "source_instance", "string", "receiver"),
    Question("Identity", "uid", "integer", 1000, minimum=1, maximum=65534),
    Question("Identity", "gid", "integer", 1000, minimum=1, maximum=65534),
    Question("Runtime", "poll_interval_seconds", "number", 60, minimum=0.1, maximum=3600),
    Question("Runtime", "image", "string", "wtf-adsb-engine:local"),
    Question("Runtime", "memory_limit", "string", "2g"),
    Question("Runtime", "viewer", "boolean", True),
    Question("Runtime", "viewer_memory_limit", "string", "512m"),
)
GROUPS = ("Receiver", "Directories", "Providers", "Identity", "Runtime")
BY_KEY = {question.key: question for question in QUESTIONS}


class JsonParser(argparse.ArgumentParser):
    """Report usage errors as one JSON object, like every other result."""

    def error(self, message):
        emit({"ok": False, "error": "usage: " + message})
        raise SystemExit(2)


def emit(result):
    print(json.dumps(result, indent=2, sort_keys=False))


def interview_text():
    """Read the Validation and Why columns from the interview document."""
    rows = {}
    if not INTERVIEW.is_file():
        return rows
    for line in INTERVIEW.read_text().splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[0].startswith("`") and cells[0].endswith("`"):
            rows[cells[0].strip("`")] = {"validation": cells[3], "why": cells[4]}
    return rows


def plan():
    text = interview_text()
    groups = []
    for group in GROUPS:
        questions = []
        for question in QUESTIONS:
            if question.group != group:
                continue
            questions.append(
                {
                    "key": question.key,
                    "type": question.type,
                    "default": question.default,
                    "required": question.required,
                    **text.get(question.key, {"validation": "", "why": ""}),
                }
            )
        groups.append({"name": group, "questions": questions})
    return {
        "ok": True,
        "document": str(INTERVIEW.relative_to(ROOT)),
        "configuration_directory": (
            "The private directory passed as --output holds the answers, receiver.toml, and .env. "
            "Compose mounts it read-only at /config."
        ),
        "groups": groups,
    }


def inside_repository(path):
    return Path(os.path.abspath(path)).resolve().is_relative_to(ROOT)


def is_number(value):
    return type(value) in (int, float)


def check_path(key, value):
    if not isinstance(value, str) or not SAFE_PATH.fullmatch(value) or "/../" in value + "/":
        raise SetupError(
            f"{key} must be an absolute path of letters, digits, '.', '_', '-', and '/' without '..'"
        )
    if inside_repository(value):
        raise SetupError(f"{key} must be outside this repository")
    return os.path.normpath(value)


def check_url(value):
    if not isinstance(value, str) or any(ord(character) < 33 or ord(character) == 127 for character in value):
        raise SetupError("receiver.url must be a URL without spaces or control characters")
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise SetupError("receiver.url must start with http:// or https:// and name a host")
    if parts.username is not None or parts.password is not None:
        raise SetupError("receiver.url must not contain a user name or password")
    if not parts.path.endswith("aircraft.json"):
        raise SetupError("receiver.url must point at the receiver's aircraft.json")
    return value


def loopback(url):
    host = urllib.parse.urlsplit(url).hostname or ""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalize(answers):
    """Validate an answers object and fill in the defaults."""
    if not isinstance(answers, dict):
        raise SetupError("The answers file must hold one JSON object")
    unknown = sorted(set(answers) - set(BY_KEY))
    if unknown:
        raise SetupError("Unknown answer keys: " + ", ".join(unknown))
    values = {}
    for question in QUESTIONS:
        value = answers.get(question.key, question.default)
        if value is None:
            if question.required:
                raise SetupError(f"{question.key} is required")
            values[question.key] = None
            continue
        if question.type == "boolean" and type(value) is not bool:
            raise SetupError(f"{question.key} must be true or false")
        if question.type == "integer" and type(value) is not int:
            raise SetupError(f"{question.key} must be a whole number")
        if question.type == "number" and not is_number(value):
            raise SetupError(f"{question.key} must be a number")
        if question.minimum is not None and not question.minimum <= value <= question.maximum:
            raise SetupError(f"{question.key} must be between {question.minimum:g} and {question.maximum:g}")
        if question.type == "path":
            value = check_path(question.key, value)
        elif question.type == "url":
            value = check_url(value)
        elif question.type == "string" and not isinstance(value, str):
            raise SetupError(f"{question.key} must be a string")
        values[question.key] = value
    if not INSTANCE.fullmatch(values["source_instance"]):
        raise SetupError(
            "source_instance must be a lowercase letter followed by letters, digits, '_', or '-'"
        )
    if not IMAGE.fullmatch(values["image"]):
        raise SetupError("image must be a Docker image reference")
    for key in ("memory_limit", "viewer_memory_limit"):
        if not MEMORY.fullmatch(values[key]):
            raise SetupError(f"{key} must be a whole number followed by b, k, m, or g, such as 2g")
    if values["flightaware"]:
        if values["flightaware_key_file"] is None:
            raise SetupError("flightaware_key_file is required when flightaware is true")
        if values["flightaware_daily_budget_usd"] > values["flightaware_monthly_budget_usd"]:
            raise SetupError("flightaware_daily_budget_usd must not exceed the monthly budget")
    return values


def directories(values, output):
    found = {
        "config": output,
        "state": Path(values["state_dir"]),
        "feed": Path(values["feed_dir"]),
        "reference": Path(values["reference_dir"]),
    }
    paths = list(found.values())
    for index, first in enumerate(paths):
        for second in paths[index + 1 :]:
            if first == second or first.is_relative_to(second) or second.is_relative_to(first):
                raise SetupError("The configuration, state, feed, and reference directories must be separate")
    return found


MOUNTS = {"config": "/config", "state": "/state", "reference": "/references"}


def container_path(key, value, places, allowed):
    """Translate a host path in a mounted directory to the path the container sees."""
    host = Path(value)
    for name in allowed:
        if host.is_relative_to(places[name]) and host != places[name]:
            return MOUNTS[name] + "/" + host.relative_to(places[name]).as_posix()
    names = " or ".join(f"the {name} directory" for name in allowed)
    raise SetupError(f"{key} must be inside {names} so the container can see it")


def toml_value(value):
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if type(value) is float:
        return repr(value)
    return json.dumps(value, ensure_ascii=False)


def receiver_options(values, places):
    options = {
        "url": values["receiver.url"],
        "poll_seconds": values["receiver.poll_seconds"],
        "max_age_seconds": values["receiver.max_age_seconds"],
        "type_registry": values["type_registry"],
        "wind": values["wind"],
        "flightaware": values["flightaware"],
    }
    if values["flightaware"]:
        options["flightaware_key_file"] = container_path(
            "flightaware_key_file", values["flightaware_key_file"], places, ("config",)
        )
        options["flightaware_daily_budget_usd"] = values["flightaware_daily_budget_usd"]
        options["flightaware_monthly_budget_usd"] = values["flightaware_monthly_budget_usd"]
    if values["flightaware_pause_file"] is not None:
        options["flightaware_pause_file"] = container_path(
            "flightaware_pause_file", values["flightaware_pause_file"], places, ("state", "config")
        )
    if values["fleet_reference_file"] is not None:
        options["fleet_reference_file"] = container_path(
            "fleet_reference_file", values["fleet_reference_file"], places, ("config", "reference")
        )
    return options


def render_config(values, places):
    lines = [
        "# Written by tools/setup.py configure. Rerun configure to change it.",
        "# Paths are as the container sees them.",
        "version = 1",
        'state_directory = "/state"',
        f"source_instance = {toml_value(values['source_instance'])}",
        f"poll_interval_seconds = {toml_value(values['poll_interval_seconds'])}",
        "",
        "[[pipelines]]",
        'name = "receiver"',
        'detectors = [{ plugin = "wtf_adsb.collection" }]',
        "",
        "[pipelines.input]",
        'plugin = "wtf_adsb.receiver_service"',
        "",
        "[pipelines.input.options]",
    ]
    lines += [f"{key} = {toml_value(value)}" for key, value in receiver_options(values, places).items()]
    lines += [
        "",
        "[[outputs]]",
        'name = "files"',
        'plugin = "wtf_adsb.files"',
        'options = { directory = "/feed" }',
        "",
    ]
    return "\n".join(lines)


def render_env(values, places):
    settings = {
        "ENGINE_CONFIG_DIR": places["config"],
        "ENGINE_STATE_DIR": places["state"],
        "ENGINE_FEED_DIR": places["feed"],
        "ENGINE_REFERENCE_DIR": places["reference"],
        "ENGINE_IMAGE": values["image"],
        "ENGINE_UID": values["uid"],
        "ENGINE_GID": values["gid"],
        "ENGINE_MEMORY_LIMIT": values["memory_limit"],
        "VIEWER_MEMORY_LIMIT": values["viewer_memory_limit"],
    }
    lines = ["# Written by tools/setup.py configure for deploy/compose.yaml."]
    lines += [f"{key}={value}" for key, value in settings.items()]
    return "\n".join(lines) + "\n"


def owner(path, values):
    if os.geteuid() == 0:
        os.chown(path, values["uid"], values["gid"], follow_symlinks=False)


def make_directory(path, values):
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise SetupError(f"{path} must be a real directory")
        path.chmod(0o700)
        owner(path, values)
    except PermissionError:
        raise SetupError(
            f"No permission to create or secure {path}. Create its parent with your account as the owner, "
            "or rerun configure with sudo."
        ) from None


def write_private(path, content, values, mode=0o600):
    """Write a file atomically with the given mode; report whether it changed."""
    data = content if isinstance(content, bytes) else content.encode()
    if path.is_symlink():
        raise SetupError(f"{path} must not be a symbolic link")
    unchanged = path.is_file() and path.read_bytes() == data
    if not unchanged:
        temporary = path.with_name("." + path.name + ".tmp")
        temporary.unlink(missing_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    path.chmod(mode)
    owner(path, values)
    return not unchanged


def reference_tables():
    return sorted(REFERENCE_SOURCE.glob("*.json"))


def guard_persisted_input(target, new_text, state):
    """Refuse a changed input once engine state exists, since the engine would reject it."""
    if not target.is_file() or not (state / "engine.sqlite3").exists():
        return
    try:
        old = tomllib.loads(target.read_text())
    except (tomllib.TOMLDecodeError, OSError):
        return
    new = tomllib.loads(new_text)
    for key in ("source_instance", "pipelines", "outputs"):
        if old.get(key) != new.get(key):
            raise SetupError(
                "Engine state already exists and these answers change the receiver input, outputs, or "
                "source_instance, which the engine would refuse. Keep the earlier answers or use a new "
                "state directory."
            )


def configure(answers_path, output):
    if inside_repository(answers_path):
        raise SetupError("Keep the answers file outside this repository")
    try:
        answers = json.loads(Path(answers_path).read_text())
    except (OSError, ValueError) as error:
        raise SetupError(f"Cannot read the answers file ({type(error).__name__})") from None
    output = Path(check_path("--output", str(Path(os.path.abspath(output)))))
    values = normalize(answers)
    places = directories(values, output)
    config_text = render_config(values, places)
    parsed = tomllib.loads(config_text)
    if parsed["pipelines"][0]["input"]["options"]["url"] != values["receiver.url"]:
        raise SetupError("receiver.url did not survive TOML encoding")
    for path in places.values():
        make_directory(path, values)
    target = output / CONFIG_NAME
    guard_persisted_input(target, config_text, places["state"])
    changed = []
    settings = {"answers": values, "directories": {name: str(path) for name, path in places.items()}}
    for name, content in (
        (CONFIG_NAME, config_text),
        (ENV_NAME, render_env(values, places)),
        (SETTINGS_NAME, json.dumps(settings, indent=2) + "\n"),
    ):
        if write_private(output / name, content, values):
            changed.append(str(output / name))
    for table in reference_tables():
        if write_private(places["reference"] / table.name, table.read_bytes(), values, mode=0o644):
            changed.append(str(places["reference"] / table.name))
    warnings = []
    if loopback(values["receiver.url"]):
        warnings.append(
            "receiver.url names a loopback address. Inside the container that is the container itself; "
            "use an address on your network."
        )
    if os.geteuid() != 0 and (os.getuid(), os.getgid()) != (values["uid"], values["gid"]):
        warnings.append(
            f"The files belong to your account, not {values['uid']}:{values['gid']}. Rerun configure "
            "with sudo, or set uid and gid to your own account."
        )
    if values["flightaware"]:
        warnings.append(
            "FlightAware is enabled. It is a paid service; budgets are "
            f"{values['flightaware_daily_budget_usd']} USD a day and "
            f"{values['flightaware_monthly_budget_usd']} USD a month."
        )
    return {
        "ok": True,
        "output": str(output),
        "files": [str(output / name) for name in (CONFIG_NAME, ENV_NAME, SETTINGS_NAME)],
        "directories": {name: str(path) for name, path in places.items()},
        "changed": changed,
        "warnings": warnings,
    }


def load_settings(output):
    path = Path(output) / SETTINGS_NAME
    try:
        settings = json.loads(path.read_text())
        values = normalize(settings["answers"])
        places = {name: Path(value) for name, value in settings["directories"].items()}
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SetupError(f"Cannot read {path} ({type(error).__name__}); run configure first") from None
    return values, places


def engine_command():
    """Find a way to run the engine's own configuration check, or return None."""
    if importlib.util.find_spec("wtf_adsb_engine") is not None:
        return [sys.executable, "-m", "wtf_adsb_engine.cli"]
    for candidate in (ROOT / ".venv" / "bin" / "wtf-adsb-engine", shutil.which("wtf-adsb-engine")):
        if candidate and os.access(candidate, os.X_OK):
            return [str(candidate)]
    return None


def check_configuration(output):
    path = Path(output) / CONFIG_NAME
    result = {"ok": False, "path": str(path)}
    try:
        parsed = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        result["error"] = type(error).__name__
        return result, None
    command = engine_command()
    if command is None:
        result.update(ok=True, engine_validation="skipped: wtf_adsb_engine is not installed here")
    else:
        try:
            completed = subprocess.run(
                [*command, "check", "--config", str(path)], capture_output=True, text=True, timeout=120
            )
            result["ok"] = completed.returncode == 0
            result["engine_validation"] = "passed" if result["ok"] else "failed"
            if not result["ok"]:
                result["error"] = (completed.stderr.strip() or completed.stdout.strip())[-300:]
        except (OSError, subprocess.TimeoutExpired) as error:
            result.update(engine_validation="failed", error=type(error).__name__)
    try:
        url = parsed["pipelines"][0]["input"]["options"]["url"]
    except (KeyError, IndexError, TypeError):
        url = None
    return result, url


def check_tree(values, places, output):
    expected = (values["uid"], values["gid"])
    report = {"ok": True, "expected_owner": f"{expected[0]}:{expected[1]}", "problems": []}

    def require(path, kind, mode=None):
        try:
            info = path.lstat()
        except OSError as error:
            report["problems"].append(f"{path}: {type(error).__name__}")
            return
        if (
            kind == "dir"
            and not stat.S_ISDIR(info.st_mode)
            or kind == "file"
            and not stat.S_ISREG(info.st_mode)
        ):
            report["problems"].append(f"{path}: not a regular {'directory' if kind == 'dir' else 'file'}")
            return
        if mode is not None and stat.S_IMODE(info.st_mode) != mode:
            report["problems"].append(f"{path}: mode {stat.S_IMODE(info.st_mode):o}, expected {mode:o}")
        if (info.st_uid, info.st_gid) != expected:
            report["problems"].append(f"{path}: owner {info.st_uid}:{info.st_gid}")

    for path in places.values():
        require(path, "dir", 0o700)
    for name in (CONFIG_NAME, ENV_NAME, SETTINGS_NAME):
        require(Path(output) / name, "file", 0o600)
    for table in reference_tables():
        copy = places["reference"] / table.name
        require(copy, "file")
        if copy.is_file() and copy.read_bytes() != table.read_bytes():
            report["problems"].append(f"{copy}: differs from the bundled table; rerun configure")
    if values["flightaware"]:
        key = Path(values["flightaware_key_file"])
        try:
            info = key.lstat()
            if not stat.S_ISREG(info.st_mode):
                report["problems"].append(f"{key}: not a regular file")
            elif info.st_mode & 0o077:
                report["problems"].append(f"{key}: readable by group or others; use chmod 0600")
            elif info.st_uid != expected[0]:
                report["problems"].append(f"{key}: owner {info.st_uid}, expected {expected[0]}")
        except OSError as error:
            report["problems"].append(f"{key}: {type(error).__name__}; create the key file first")
    report["ok"] = not report["problems"]
    return report


def check_docker(docker, output):
    """Run `docker compose version`, then let Compose validate the generated .env."""
    result = {"ok": False}
    base = [docker, "compose", "--env-file", str(Path(output) / ENV_NAME), "-f", str(COMPOSE)]
    for name, command in (
        ("version", [docker, "compose", "version"]),
        ("config", [*base, "config", "--quiet"]),
    ):
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            result["error"] = f"{name}: {type(error).__name__}"
            return result
        if completed.returncode:
            result["error"] = (
                f"{name}: " + (completed.stderr.strip() or "exit " + str(completed.returncode))[-300:]
            )
            return result
        if name == "version":
            lines = completed.stdout.strip().splitlines()
            result["version"] = lines[0][:120] if lines else ""
    result["ok"] = True
    return result


def probe(url):
    """GET the receiver once and report only the status, or the error class."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with opener.open(request, timeout=PROBE_TIMEOUT) as response:
            status = response.status
            body = response.read(16 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        return {"ok": False, "status": error.code}
    except (OSError, ValueError) as error:
        return {"ok": False, "error": type(error).__name__}
    try:
        document = json.loads(body)
        shaped = isinstance(document, dict) and is_number(document.get("now"))
        shaped = shaped and isinstance(document.get("aircraft"), list)
    except ValueError:
        shaped = False
    return {"ok": status == 200 and shaped, "status": status, "aircraft_json": shaped}


def check(output, docker):
    values, places = load_settings(output)
    configuration, url = check_configuration(output)
    checks = {
        "configuration": configuration,
        "directories": check_tree(values, places, output),
        "docker_compose": check_docker(docker, output),
        "receiver": probe(url) if url else {"ok": False, "error": "no receiver URL in the configuration"},
    }
    warnings = []
    if url and loopback(url):
        warnings.append("The receiver URL is a loopback address, which the container cannot reach.")
    if values["fleet_reference_file"] and not Path(values["fleet_reference_file"]).is_file():
        warnings.append("fleet_reference_file does not exist yet; fleet context stays off until it does.")
    return {"ok": all(item["ok"] for item in checks.values()), "checks": checks, "warnings": warnings}


def compose_commands(output, docker, viewer):
    base = [docker, "compose", "--env-file", str(Path(output) / ENV_NAME), "-f", str(COMPOSE)]
    up = [*base, "up", "-d"] + ([] if viewer else ["engine"])
    health = [*base, "exec", "-T", "engine", "wtf-adsb-engine", "health", "--config", CONTAINER_CONFIG]
    return up, health


def deploy(output, docker, print_only, timeout):
    values, _ = load_settings(output)
    if not (Path(output) / ENV_NAME).is_file():
        raise SetupError("Run configure before deploy")
    up, health = compose_commands(output, docker, values["viewer"])
    if print_only:
        return {"ok": True, "commands": {"up": up, "health": health}}
    try:
        started = subprocess.run(up, capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SetupError(f"docker compose up failed ({type(error).__name__})") from None
    if started.returncode:
        return {"ok": False, "step": "up", "returncode": started.returncode, "error": started.stderr[-600:]}
    deadline = time.monotonic() + timeout
    last = {}
    while True:
        try:
            completed = subprocess.run(health, capture_output=True, text=True, timeout=60)
            try:
                last = json.loads(completed.stdout.strip().splitlines()[-1])
            except (IndexError, ValueError):
                last = {"returncode": completed.returncode}
            if completed.returncode == 0:
                return {"ok": True, "health": last}
        except (OSError, subprocess.TimeoutExpired) as error:
            last = {"error": type(error).__name__}
        if time.monotonic() + HEALTH_INTERVAL > deadline:
            return {"ok": False, "step": "health", "waited_seconds": timeout, "health": last}
        time.sleep(HEALTH_INTERVAL)


def main(argv=None):
    parser = JsonParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True, parser_class=JsonParser)
    commands.add_parser("plan", help="Print the interview questions")
    configure_parser = commands.add_parser("configure", help="Write receiver.toml and .env")
    configure_parser.add_argument("--answers", required=True)
    configure_parser.add_argument("--output", required=True)
    check_parser = commands.add_parser("check", help="Validate files, directories, Docker, and the receiver")
    check_parser.add_argument("--output", required=True)
    check_parser.add_argument("--docker", default="docker", help=argparse.SUPPRESS)
    deploy_parser = commands.add_parser("deploy", help="Start the services and wait for health")
    deploy_parser.add_argument("--output", required=True)
    deploy_parser.add_argument("--print-only", action="store_true")
    deploy_parser.add_argument("--timeout", type=float, default=120)
    deploy_parser.add_argument("--docker", default="docker", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            result = plan()
        elif args.command == "configure":
            result = configure(args.answers, args.output)
        elif args.command == "check":
            result = check(args.output, args.docker)
        else:
            result = deploy(args.output, args.docker, args.print_only, args.timeout)
    except SetupError as error:
        result = {"ok": False, "error": str(error)}
    except Exception as error:
        result = {"ok": False, "error": type(error).__name__}
    emit(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
