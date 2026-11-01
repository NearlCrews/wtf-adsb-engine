"""Check a local engine image and an installed derived plugin without network access."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from image_inventory import build_inputs

ROOT = Path(__file__).resolve().parents[1]
SHARE = "/usr/local/share/wtf-adsb-engine"


def command(*arguments, check=True, timeout=120):
    result = subprocess.run(arguments, check=False, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {arguments[0]}\n{result.stdout}{result.stderr}"
        )
    return result


def container_run(*arguments):
    name = "wtf-engine-check-" + uuid.uuid4().hex
    try:
        return command("docker", "run", "--rm", "--init", "--name", name, *arguments)
    finally:
        # A timed-out Docker client can leave its container running.
        command("docker", "rm", "--force", name, check=False)


def run_options(work):
    return [
        "--pull=never",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--tmpfs=/tmp:size=64m,mode=1777",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--mount",
        f"type=bind,source={work},target=/work",
        "--mount",
        f"type=bind,source={ROOT / 'tools/check_installed.py'},target=/checks/check_installed.py,readonly",
        "--workdir=/work",
    ]


def workspace(parent, *, plugin=False):
    work = parent / ("plugin" if plugin else "base")
    work.mkdir(mode=0o700)
    shutil.copyfile(ROOT / "examples/receiver.synthetic.ndjson", work / "receiver.synthetic.ndjson")
    if plugin:
        example = ROOT / "examples/plugins/reference-label"
        config = (example / "example.toml").read_text()
        for old, new in (
            ("../../../state/reference-label-demo", "state"),
            ("../../../state/reference-label-export", "export"),
            ("../../receiver.synthetic.ndjson", "receiver.synthetic.ndjson"),
            ("src/wtf_adsb_example_reference_label/schema.json", "extension.schema.json"),
        ):
            config = config.replace(old, new)
        shutil.copyfile(
            example / "src/wtf_adsb_example_reference_label/schema.json", work / "extension.schema.json"
        )
    else:
        config = (ROOT / "examples/replay.toml").read_text()
        config = config.replace("../state/demo", "state").replace("../state/export", "export")
    (work / "replay.toml").write_text(config)
    return work


def smoke(image, work, *, plugin=False):
    result = container_run(
        *run_options(work),
        "--entrypoint=python",
        image,
        "-I",
        "/checks/check_installed.py",
        "--archive",
        *(["--plugin"] if plugin else []),
    )
    return json.loads(result.stdout)


def lifecycle(image, work):
    container = "wtf-engine-lifecycle-" + uuid.uuid4().hex
    try:
        command(
            "docker",
            "run",
            "--detach",
            "--init",
            "--name",
            container,
            *run_options(work),
            image,
            "run",
            "--config",
            "/work/replay.toml",
        )
        deadline = time.monotonic() + 30
        while True:
            result = command(
                "docker",
                "exec",
                container,
                "wtf-adsb-engine",
                "health",
                "--config",
                "/work/replay.toml",
                check=False,
            )
            if result.returncode == 0:
                assert json.loads(result.stdout)["alive"]
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Container did not become live: " + command("docker", "logs", container).stdout
                )
            time.sleep(0.2)
        readiness = command(
            "docker",
            "exec",
            container,
            "wtf-adsb-engine",
            "health",
            "--config",
            "/work/replay.toml",
            "--readiness",
            check=False,
        )
        assert readiness.returncode == 1 and not json.loads(readiness.stdout)["ready"]
        command("docker", "stop", "--time=20", container, timeout=30)
        state = json.loads(command("docker", "inspect", container).stdout)[0]["State"]
        assert state["ExitCode"] == 0 and not state["OOMKilled"], state
        result = container_run(*run_options(work), image, "run-once", "--config", "/work/replay.toml")
        assert json.loads(result.stdout)["changes"] == {"replay": 0}
    finally:
        command("docker", "rm", "--force", container, check=False)


def plugin_image(image, build_image, parent, tag):
    builder = tag + "-wheel"
    engine_ref, build_ref = tag + "-engine", tag + "-build"
    container = "wtf-engine-wheel-" + uuid.uuid4().hex
    try:
        # BuildKit resolves named local images; raw local IDs are not valid FROM references.
        command("docker", "tag", image, engine_ref)
        command("docker", "tag", build_image, build_ref)
        command(
            "docker",
            "build",
            "--force-rm",
            "--network=none",
            "--pull=false",
            "--platform=linux/amd64",
            "--build-arg",
            f"BUILDER_IMAGE={build_ref}",
            "--file",
            str(ROOT / "examples/plugins/reference-label/Dockerfile.wheel"),
            "--tag",
            builder,
            str(ROOT),
            timeout=300,
        )
        command("docker", "create", "--name", container, "--entrypoint=python", builder, "--version")
        wheels = parent / "wheels"
        wheels.mkdir()
        command("docker", "cp", f"{container}:/plugin-dist/.", str(wheels))
        (wheel,) = wheels.glob("*.whl")
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        (wheels / "requirements.txt").write_text(
            f"wtf-adsb-example-reference-label==0.1.0 --hash=sha256:{digest}\n"
        )
        command(
            "docker",
            "build",
            "--force-rm",
            "--network=none",
            "--pull=false",
            "--platform=linux/amd64",
            "--build-arg",
            f"ENGINE_IMAGE={engine_ref}",
            "--build-arg",
            f"BUILDER_IMAGE={build_ref}",
            "--file",
            str(ROOT / "examples/plugins/reference-label/Dockerfile"),
            "--tag",
            tag,
            str(wheels),
            timeout=300,
        )
        inventory = json.loads(
            container_run(
                "--pull=never",
                "--network=none",
                "--entrypoint=cat",
                tag,
                SHARE + "/packages.json",
            ).stdout
        )
        assert any(
            item["name"] == "wtf-adsb-example-reference-label" for item in inventory["python_packages"]
        )
    finally:
        command("docker", "rm", container, check=False)
        for reference in (builder, engine_ref, build_ref):
            command("docker", "image", "rm", reference, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="wtf-adsb-engine:local")
    parser.add_argument("--builder-image", default="wtf-adsb-engine:local-build")
    args = parser.parse_args()
    if os.getuid() == 0:
        parser.error("Run the image check as an unprivileged user with access to Docker")
    image = json.loads(command("docker", "image", "inspect", args.image).stdout)[0]
    identity = image["Id"]
    assert image["Os"] == "linux" and image["Architecture"] == "amd64"
    assert image["Config"]["User"] == "engine:engine"
    information = container_run(
        "--pull=never",
        "--network=none",
        "--read-only",
        "--entrypoint=python",
        identity,
        "-I",
        "-c",
        "import json,os,pwd; from pathlib import Path; "
        "assert os.getuid()==1000 and pwd.getpwuid(1000).pw_name=='engine'; "
        f"print(json.dumps([json.loads((Path('{SHARE}')/p).read_text()) for p in ('build.json','packages.json')]))",
    )
    metadata, inventory = json.loads(information.stdout)
    builder = json.loads(command("docker", "image", "inspect", args.builder_image).stdout)[0]["Id"]
    builder_metadata = json.loads(
        container_run(
            "--pull=never",
            "--network=none",
            "--read-only",
            "--entrypoint=cat",
            builder,
            "/metadata/build.json",
        ).stdout
    )
    assert builder_metadata == metadata, "Builder and runtime images must come from the same inputs"
    assert metadata["build_inputs_sha256"] == build_inputs(ROOT), (
        "Rebuild the image after changing build inputs"
    )
    assert metadata["engine_version"] == image["Config"]["Labels"]["org.opencontainers.image.version"]
    packages = {item["name"].lower() for item in inventory["python_packages"]}
    assert {"wtf-adsb-engine", "numpy"} <= packages and not {"uv", "hatchling", "ruff"} & packages
    assert inventory["os_packages"]
    assert "usage:" in container_run("--pull=never", "--network=none", identity, "--help").stdout
    tag = "wtf-adsb-engine:plugin-check-" + uuid.uuid4().hex
    try:
        with tempfile.TemporaryDirectory(prefix="wtf-engine-image-") as temporary:
            parent = Path(temporary)
            work = workspace(parent)
            base = smoke(identity, work)
            lifecycle(identity, work)
            plugin_image(identity, builder, parent, tag)
            plugin = smoke(tag, workspace(parent, plugin=True), plugin=True)
    finally:
        command("docker", "image", "rm", tag, check=False)
    print(
        json.dumps(
            {"image_id": identity, "offline": True, "base": base, "graceful_restart": True, "plugin": plugin}
        )
    )


if __name__ == "__main__":
    main()
