#!/usr/bin/env python3
"""Compare direct dependency pins with live upstream releases without changing files.

Requires the project's pinned ``packaging`` dependency. Network access is explicit:
this maintenance command is separate from offline tests and release builds.
"""

import argparse
import concurrent.futures
import json
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

MAX_RESPONSE_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class Pin:
    ecosystem: str
    name: str
    version: str
    source: str


def python_pin(value, source):
    requirement = Requirement(value)
    specs = list(requirement.specifier)
    if requirement.url or len(specs) != 1 or specs[0].operator != "==" or "*" in specs[0].version:
        raise ValueError(f"{source}: {requirement.name} needs an exact == version pin")
    return Pin("pypi", requirement.name, specs[0].version, source)


def collect_pins(project):
    pins = []
    pyproject = project / "pyproject.toml"
    if pyproject.is_file():
        data = tomllib.loads(pyproject.read_text())
        groups = {
            "build-system": data.get("build-system", {}).get("requires", []),
            "project": data.get("project", {}).get("dependencies", []),
            **{
                f"extra:{name}": values
                for name, values in data.get("project", {}).get("optional-dependencies", {}).items()
            },
        }
        for name, values in data.get("dependency-groups", {}).items():
            # Includes only repeat groups already inspected separately.
            groups[f"group:{name}"] = [value for value in values if isinstance(value, str)]
        for group, values in groups.items():
            pins.extend(python_pin(value, f"pyproject.toml:{group}") for value in values)
    for path in sorted(project.glob("requirements*.txt")):
        for line in path.read_text().splitlines():
            value = line.split("#", 1)[0].strip()
            if value:
                pins.append(python_pin(value, path.name))
    for relative in ("package.json", "tools/ghost-theme/package.json"):
        path = project / relative
        if path.is_file():
            data = json.loads(path.read_text())
            for group in ("dependencies", "devDependencies", "optionalDependencies"):
                for name, version in data.get(group, {}).items():
                    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
                        raise ValueError(f"{relative}: {name} needs an exact stable version pin")
                    pins.append(Pin("npm", name, version, relative))
    if not pins:
        raise ValueError(f"No dependency pins found in {project}")
    return sorted(set(pins), key=lambda pin: (pin.ecosystem, pin.name, pin.source))


def fetch_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "wtf-adsb-dependency-check/1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError("Registry response exceeds the size limit")
    return json.loads(payload)


def newest_python_release(data):
    releases = []
    for text, files in data["releases"].items():
        try:
            version = Version(text)
        except InvalidVersion:
            continue
        if (
            not version.is_prerelease
            and not version.is_devrelease
            and any(not file.get("yanked", False) for file in files)
        ):
            releases.append(version)
    if not releases:
        raise ValueError("Registry has no stable, unyanked release with downloadable files")
    return str(max(releases))


def check_pin(pin, node_major=None, fetch=fetch_json):
    result = {
        "ecosystem": pin.ecosystem,
        "name": pin.name,
        "pinned": pin.version,
        "manifest": pin.source,
    }
    try:
        quoted_name = urllib.parse.quote(pin.name, safe="")
        if pin.ecosystem == "pypi":
            url = f"https://pypi.org/pypi/{quoted_name}/json"
            data = fetch(url)
            latest = newest_python_release(data)
            info = data["info"]
            if Version(info["version"]) != Version(latest):
                info = fetch(f"https://pypi.org/pypi/{quoted_name}/{latest}/json")["info"]
            result["requires_python"] = info.get("requires_python")
            result["license"] = info.get("license_expression") or info.get("license")
        else:
            url = f"https://registry.npmjs.org/{quoted_name}/latest"
            info = fetch(url)
            latest = info["version"]
            if not re.fullmatch(r"\d+\.\d+\.\d+", latest):
                raise ValueError("The npm latest tag is not a stable release")
            result["engines"] = info.get("engines", {})
            result["license"] = info.get("license")
        expected = latest
        if pin.ecosystem == "npm" and pin.name == "node" and node_major is not None:
            index_url = "https://nodejs.org/dist/index.json"
            versions = [
                Version(entry["version"].removeprefix("v"))
                for entry in fetch(index_url)
                if re.fullmatch(rf"v{node_major}\.\d+\.\d+", entry["version"])
            ]
            if not versions:
                raise ValueError(f"No stable Node {node_major} release found")
            expected = str(max(versions))
            # Ensure the selected official release is available through this npm wrapper.
            fetch(f"https://registry.npmjs.org/node/{expected}")
            result["compatibility_exception"] = (
                f"Explicit --node-major={node_major}; confirm dependent tools support this line"
            )
            result["line_source"] = index_url
        result.update(latest=latest, expected=expected, source=url)
        result["status"] = "current" if Version(pin.version) == Version(expected) else "outdated"
        if result["status"] == "current" and expected != latest:
            result["status"] = "current-compatible"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result.update(status="error", error=str(exc))
    return result


def check_pins(pins, node_major=None):
    """Audit repeated pins once, retaining each manifest's result and ordering."""
    unique = list(dict.fromkeys(replace(pin, source="") for pin in pins))
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        checked = dict(zip(unique, executor.map(lambda pin: check_pin(pin, node_major), unique), strict=True))
    return [{**checked[replace(pin, source="")], "manifest": pin.source} for pin in pins]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true", help="Print the audit as JSON")
    parser.add_argument(
        "--node-major",
        type=int,
        help="Explicit supported Node line for an npm node pin; still reports newest upstream",
    )
    args = parser.parse_args(argv)
    try:
        pins = collect_pins(args.project)
    except (OSError, ValueError, InvalidRequirement) as exc:
        parser.exit(2, f"Dependency audit failed: {exc}\n")
    results = check_pins(pins, args.node_major)
    if args.json:
        print(json.dumps({"checked_at": datetime.now(UTC).isoformat(), "dependencies": results}, indent=2))
    else:
        for result in results:
            comparison = result.get("error") or (f"expected {result['expected']}, newest {result['latest']}")
            print(
                f"{result['status']}: {result['ecosystem']} {result['name']} "
                f"{result['pinned']} ({comparison}; {result['manifest']})"
            )
    return int(any(result["status"] in {"error", "outdated"} for result in results))


if __name__ == "__main__":
    sys.exit(main())
