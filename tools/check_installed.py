"""Exercise an installed package with synthetic input and loopback-only HTTP."""

import argparse
import http.client
import importlib.util
import json
import socket
import subprocess
import sys
import threading
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", action="store_true")
    parser.add_argument("--plugin", action="store_true")
    args = parser.parse_args()

    import wtf_adsb_engine
    from wtf_adsb_engine import registry, wire
    from wtf_adsb_engine.analysis import app
    from wtf_adsb_engine.maintenance import health
    from wtf_adsb_engine.runtime import Runtime
    from wtf_adsb_engine.viewer import ViewerServer

    assert Path(wtf_adsb_engine.__file__).parent.parent.name == "site-packages"
    assert (importlib.util.find_spec("numpy") is not None) == args.archive
    if args.archive:
        import numpy

        assert numpy.frombuffer(b"\x01\x00\x00\x00", dtype="<u4").tolist() == [1]
    plugins = registry.discovered()
    assert any(item["id"] == "org.example.reference_label" for item in plugins) == args.plugin

    original_connect = socket.socket.connect

    def denied(self, address):
        if isinstance(address, tuple) and address[0] == "127.0.0.1":
            return original_connect(self, address)
        raise AssertionError("Installed-package replay attempted network I/O")

    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    runtime = Runtime("replay.toml")
    with runtime.locked():
        first = runtime.once()
        head = runtime.store.state()["head_sequence"]
        assert not first["errors"] and first["outputs"] == {"files": "ready"} and head > 0, first
        second = runtime.once()
        assert not second["errors"] and second["changes"] == {"replay": 0}, second
        assert runtime.store.state()["head_sequence"] == head
    assert not health("replay.toml", readiness=True)["ready"], "Synthetic input must never become live ready"
    manifest = wire.validate_manifest(wire.loads(Path("export/manifest.json").read_bytes()))
    observations = []
    for item in manifest["batches"]:
        batch = wire.validate_batch(
            wire.loads((Path("export") / item["path"]).read_bytes()), allow_synthetic=True
        )
        observations.extend(change["observation"] for change in batch["changes"] if "observation" in change)
    assert observations
    for observation in observations:
        extensions = observation.get("extensions", [])
        assert bool(extensions) == args.plugin
        if args.plugin:
            assert extensions[0]["namespace"] == "org.example.reference_label"
            assert extensions[0]["payload"]["label"] == "Demonstration reference"

    app.initialize(Path("viewer.sqlite3"))
    server = ViewerServer(("127.0.0.1", 0), "viewer.sqlite3")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for route in ("/wtf/", "/wtf/app.js", "/wtf/style.css", "/wtf/health", "/wtf/api/summary"):
            client = http.client.HTTPConnection(*server.server_address, timeout=5)
            try:
                client.request("GET", route)
                response = client.getresponse()
                assert response.status == 200, (route, response.status)
                assert response.read()
            finally:
                client.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    for module in ("backfill_history", "backfill_metrics", "import_replay"):
        subprocess.run(
            [sys.executable, "-I", "-m", f"wtf_adsb_engine.analysis.{module}", "--help"],
            check=True,
            capture_output=True,
            timeout=30,
        )
    print(
        json.dumps({"offline_replay": True, "stable_repeat": True, "installed_viewer": True, "changes": head})
    )


if __name__ == "__main__":
    main()
