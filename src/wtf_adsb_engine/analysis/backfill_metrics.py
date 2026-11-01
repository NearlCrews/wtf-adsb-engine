#!/usr/bin/env python3
"""Explicitly capture local aircraft metrics or analyze saved responses offline."""

import argparse
from pathlib import Path

from . import archive_paths, history_backfill, history_metrics


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--start", required=True)
    freeze.add_argument("--end", required=True)
    freeze.add_argument("--endpoint", required=True, help="Explicit loopback Prometheus endpoint")
    analyze = commands.add_parser("analyze")
    analyze.add_argument("--corpus", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        destination = archive_paths.destination(args.output)
        with archive_paths.private_umask(), history_backfill.writer_lock(destination):
            result = (
                history_metrics.freeze(destination, start=args.start, end=args.end, endpoint=args.endpoint)
                if args.command == "freeze"
                else history_metrics.analyze(destination, args.corpus.resolve())
            )
    except ValueError as error:
        parser.error(str(error))
    except (OSError, TimeoutError) as error:
        parser.exit(1, f"The {args.command} command stopped ({type(error).__name__}).\n")
    print(history_backfill.encoded(result))


if __name__ == "__main__":
    main()
