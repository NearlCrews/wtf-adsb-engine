"""Record public build inputs and installed packages without reading runtime state."""

import argparse
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import tomllib
from pathlib import Path

DIRECTORY = Path("/usr/local/share/wtf-adsb-engine")
BUILD_FILES = (
    "Dockerfile",
    ".dockerignore",
    ".python-version",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "tools/image_inventory.py",
)


def build_inputs(root):
    paths = [root / name for name in BUILD_FILES]
    for path in (root, root / "src", root / "tools", *paths):
        if path.is_symlink():
            raise ValueError("Image build inputs must not be symbolic links")
    for path in (root / "src").rglob("*"):
        if path.is_symlink():
            raise ValueError("Image build inputs must not be symbolic links")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            paths.append(path)
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def build_metadata(root):
    project = tomllib.loads((root / "pyproject.toml").read_text())
    images = {
        parts[-1]: parts[1]
        for line in (root / "Dockerfile").read_text().splitlines()
        if (parts := line.split()) and parts[0] == "FROM" and "@sha256:" in parts[1]
    }
    return {
        "schema_version": 1,
        "engine_version": project["project"]["version"],
        "python_version": platform.python_version(),
        "architecture": platform.machine(),
        "images": images,
        "build_inputs_sha256": build_inputs(root),
        "uv_lock_sha256": hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest(),
        "wheels": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((root / "dist").glob("*.whl"))
        },
    }


def installed_packages():
    packages = []
    for distribution in importlib.metadata.distributions():
        packages.append(
            {
                "name": distribution.metadata["Name"],
                "version": distribution.version,
                "license_expression": distribution.metadata.get("License-Expression"),
                "license_files": [
                    str(distribution.locate_file(name))
                    for name in distribution.files or ()
                    if "/licenses/" in str(name) or name.name.lower().startswith(("license", "copying"))
                ],
            }
        )
    output = subprocess.run(
        ["dpkg-query", "-W", "-f=${Package}\t${Version}\t${Architecture}\n"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {
        "schema_version": 1,
        "python_version": platform.python_version(),
        "python_packages": sorted(packages, key=lambda item: item["name"].lower()),
        "os_packages": [
            dict(zip(("name", "version", "architecture"), line.split("\t"), strict=True))
            for line in output.splitlines()
        ],
        "os_license_directory": "/usr/share/doc",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-root", type=Path)
    parser.add_argument("--output", type=Path, default=DIRECTORY / "packages.json")
    args = parser.parse_args()
    result = build_metadata(args.build_root) if args.build_root else installed_packages()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
