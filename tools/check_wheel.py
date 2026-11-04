"""Install the base wheel offline and replay outside the source checkout."""

import email
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

from image_inventory import BUILD_FILES

ROOT = Path(__file__).resolve().parents[1]


def main():
    wheels = list((ROOT / "dist").glob("wtf_adsb_engine-*.whl"))
    if len(wheels) != 1:
        raise SystemExit("Build exactly one engine wheel before running this check")
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()
        assert "wtf_adsb_engine/py.typed" in names
        for asset in ("index.html", "app.js", "style.css", "favicon.svg"):
            packaged = "wtf_adsb_engine/static/" + asset
            assert wheel.read(packaged) == (ROOT / "src" / packaged).read_bytes()
        metadata_path = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = email.message_from_bytes(wheel.read(metadata_path))
        assert metadata["License-Expression"] == "MIT"
        assert metadata["Description-Content-Type"] == "text/markdown"
        assert metadata.get_payload().strip() == (ROOT / "README.md").read_text().strip()
        license_root = metadata_path.rsplit("/", 1)[0] + "/licenses/"
        for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
            assert wheel.read(license_root + name) == (ROOT / name).read_bytes()
    source_archives = list((ROOT / "dist").glob("wtf_adsb_engine-*.tar.gz"))
    if len(source_archives) != 1:
        raise SystemExit("Build exactly one engine source archive before running this check")
    with tarfile.open(source_archives[0]) as source:
        source_names = source.getnames()
        base = source_names[0].split("/", 1)[0]
        for name in (
            *BUILD_FILES,
            "CONTRIBUTING.md",
            "DEVELOPMENT.md",
            "SECURITY.md",
            "CODE_OF_CONDUCT.md",
            "CHANGELOG.md",
            "SUPPORT.md",
            "AUTHORS.md",
            "AGENTS.md",
            "CLAUDE.md",
            "docs/README.md",
            "docs/configuration.md",
            "docs/operations.md",
            "docs/deployment.md",
            "docs/evidence-format.md",
            "docs/setup-interview.md",
            "tools/check_image.py",
            "tools/check_installed.py",
            "tools/release_notes.py",
            "tools/setup.py",
            "examples/plugins/reference-label/Dockerfile",
            "examples/plugins/reference-label/Dockerfile.wheel",
        ):
            assert base + "/" + name in source_names, f"Source archive lacks {name}"
        names.extend(source_names)
    for name in names:
        path = Path(name)
        assert not {".private", ".venv", "state", "artifacts", "__pycache__"}.intersection(path.parts)
        assert path.suffix not in {".sqlite", ".sqlite3", ".db", ".pem", ".key"}
        assert path.name not in {
            "reference-aircraft-types-v1.json",
            "reference-identity-v1.json",
            "reference-notable-variants-v1.json",
        }
    with tempfile.TemporaryDirectory(prefix="wtf-engine-wheel-") as temporary:
        work = Path(temporary)
        environment = {key: value for key, value in os.environ.items() if not key.startswith("WTF_ADSB_")}
        # The locked dependencies come from uv's cache, which a fresh checkout fills
        # with `uv sync`; an offline index lookup would not. The wheel itself then
        # installs without dependency resolution, which proves it needs nothing else.
        subprocess.run(
            [
                "uv",
                "sync",
                "--locked",
                "--offline",
                "--no-dev",
                "--no-install-project",
                "--python",
                sys.executable,
            ],
            check=True,
            env={**environment, "UV_PROJECT_ENVIRONMENT": str(work / "venv")},
            capture_output=True,
            cwd=ROOT,
        )
        python = work / "venv/bin/python"
        subprocess.run(
            ["uv", "pip", "install", "--offline", "--no-deps", "--python", str(python), str(wheels[0])],
            check=True,
            env=environment,
            capture_output=True,
        )
        shutil.copyfile(ROOT / "examples/receiver.synthetic.ndjson", work / "receiver.synthetic.ndjson")
        config = (
            (ROOT / "examples/replay.toml")
            .read_text()
            .replace('"../state/demo"', '"state"')
            .replace('"../state/export"', '"export"')
        )
        (work / "replay.toml").write_text(config)
        subprocess.run(
            [str(python), "-I", str(ROOT / "tools/check_installed.py")],
            check=True,
            cwd=work,
            env=environment,
        )


if __name__ == "__main__":
    main()
