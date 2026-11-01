"""Run modular ADS-B analysis without importing any publishing application."""

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from . import __version__, registry
from .runtime import Runtime, configuration, state_lock
from .storage import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plugins", help="List installed plugin metadata without importing plugins")
    viewer = commands.add_parser("serve", help="Serve the private factual viewer without starting workers")
    viewer.add_argument("--config", required=True)
    viewer.add_argument("--pipeline")
    viewer.add_argument("--archive", type=Path)
    viewer.add_argument("--captured-at", type=float)
    viewer.add_argument("--host", default="127.0.0.1")
    viewer.add_argument("--port", type=int, default=8080)
    viewer.add_argument("--map-url", help="Optional map URL prefix to which the address is appended")
    for name in ("check", "run-once", "run", "status", "retract", "migrate-journal", "health"):
        command = commands.add_parser(name)
        command.add_argument("--config", required=True)
        if name == "migrate-journal":
            command.add_argument("--source", required=True, type=Path)
            command.add_argument("--pipeline")
        if name == "health":
            command.add_argument("--readiness", action="store_true")
            command.add_argument("--max-age", type=float, default=300)
        if name == "retract":
            command.add_argument("--event-id", required=True)
            command.add_argument(
                "--reason", required=True, choices=("invalid_evidence", "superseded", "out_of_scope")
            )
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.command == "plugins":
            result = registry.discovered()
        elif args.command == "serve":
            from .viewer import serve

            serve(
                args.config,
                pipeline=args.pipeline,
                archive=args.archive,
                captured_at=args.captured_at,
                host=args.host,
                port=args.port,
                map_url=args.map_url,
            )
            return 0
        elif args.command == "migrate-journal":
            from .maintenance import migrate_journal

            result = migrate_journal(args.source, args.config, pipeline=args.pipeline)
        elif args.command == "health":
            from .maintenance import health

            result = health(args.config, readiness=args.readiness, max_age=args.max_age)
            print(json.dumps(result, allow_nan=False))
            return 0 if result.get("ready" if args.readiness else "alive") else 1
        elif args.command in {"status", "retract"}:
            config = configuration(args.config)
            database = Path(config["state_directory"]) / "engine.sqlite3"
            if args.command == "status":
                result = Store.inspect(database)
            else:
                with state_lock(database.parent):
                    if not database.is_file():
                        raise ValueError("No engine state exists at the configured directory")
                    store = Store(database, synthetic=config.get("synthetic", False))
                    store.bind_source(config["source_instance"])
                    store.retract(args.event_id, args.reason, timestamp=time.time())
                result = {"retracted": args.event_id}
        else:
            runtime = Runtime(args.config)
            if args.command == "check":
                result = {"valid": True, "pipelines": len(runtime.pipelines), "outputs": len(runtime.outputs)}
            else:
                with runtime.locked():
                    if args.command == "run":
                        stop = threading.Event()
                        for signum in (signal.SIGTERM, signal.SIGINT):
                            signal.signal(signum, lambda *_, stop=stop: stop.set())
                        runtime.run(stop)
                        return 0
                    result = runtime.once()
        print(json.dumps(result, allow_nan=False))
        if isinstance(result, dict) and (
            result.get("errors")
            or (
                isinstance(result.get("outputs"), dict)
                and any(value != "ready" for value in result["outputs"].values())
            )
        ):
            return 1
        return 0
    except Exception as error:
        # Plugin errors may contain provider URLs and credentials. Preserve the
        # type for diagnostics while keeping normal service logs private-safe.
        print(json.dumps({"error": type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
