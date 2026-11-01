#!/usr/bin/env python3
"""Freeze, pin, analyze and inspect an independent private historical ADS-B corpus."""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from . import archive_paths, history_backfill

ERRORS = (*history_backfill.ARCHIVE_ERRORS, TypeError, KeyError, sqlite3.Error, OverflowError)


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--root", type=Path, required=True, help="Readsb globe_history archive directory")
    freeze.add_argument(
        "--journal",
        type=Path,
        required=True,
        help="A user-made SQLite backup copy of the collector journal; never pass the active journal",
    )
    freeze.add_argument(
        "--live-journal", type=Path, help="Optional active journal path to refuse, including hard links"
    )
    freeze.add_argument("--metadata", type=Path, required=True, help="Aircraft metadata CSV gzip file")
    freeze.add_argument("--snapshots", type=Path)
    freeze.add_argument("--through", help="Last UTC day to freeze (YYYY-MM-DD); analysis stops at its end")
    freeze.add_argument(
        "--allow-incomplete-through",
        metavar="REASON",
        help="Freeze an ended last day that fails its completeness checks; the reason is recorded",
    )
    freeze.add_argument(
        "--include-prior",
        type=Path,
        action="append",
        default=[],
        metavar="CORPUS",
        help="Copy an earlier corpus's hash-checked inputs (repeatable)",
    )
    watchlist = freeze.add_mutually_exclusive_group(required=True)
    watchlist.add_argument(
        "--watchlist",
        type=Path,
        help="Explicit watchlist cache directory or current.json to pin",
    )
    watchlist.add_argument("--no-watchlist", action="store_true", help="Freeze without watchlist matching")
    freeze.add_argument("--owner-uid", type=int)
    freeze.add_argument("--owner-gid", type=int)
    run = commands.add_parser("run")
    run.add_argument("--workers", type=int, default=4)
    run.add_argument("--max-days", type=int)
    commands.add_parser("pin-source", help="Copy the analysis code into the corpus and write run_frozen.py")
    clone = commands.add_parser("clone-inputs", help="Start a new corpus from another corpus's frozen inputs")
    clone.add_argument("--from", dest="source", type=Path, required=True, metavar="CORPUS")
    commands.add_parser("status")
    commands.add_parser(
        "checkpoint", help="Fold a WAL left by readers into the corpus database once no run is writing"
    )
    return parser


def give_back(paths, uid, gid):
    """Hand every path the root freeze created to the given owner, as far as it exists."""
    for path in paths:
        if not path.exists():
            continue
        for item in [path, *(path.rglob("*") if path.is_dir() else ())]:
            os.chown(item, uid, gid, follow_symlinks=False)


def freeze(args, destination):
    if (args.owner_uid is None) != (args.owner_gid is None) or (
        args.owner_uid is not None and os.geteuid() != 0
    ):
        raise ValueError("Owner assignment requires root and an explicit user and group")
    if archive_paths.same_file(args.journal, args.live_journal):
        raise ValueError("Freeze a user-made backup copy of the journal, not the live journal")
    cache = None if args.no_watchlist else args.watchlist
    if cache is not None and not cache.exists():
        raise ValueError("No watchlist cache; pass --watchlist DIR or --no-watchlist")
    created = []
    parent = destination.parent
    while not parent.exists():
        created.append(parent)
        parent = parent.parent
    try:
        with history_backfill.writer_lock(destination):
            return history_backfill.freeze(
                args.root,
                args.journal,
                args.metadata,
                destination,
                snapshots=args.snapshots,
                through=args.through,
                include_prior=args.include_prior,
                watchlist=cache,
                allow_incomplete=args.allow_incomplete_through,
            )
    finally:
        # Even a refused or failed freeze leaves nothing that only root can remove.
        if args.owner_uid is not None:
            give_back([*reversed(created), destination], args.owner_uid, args.owner_gid)


def _execute(args, parser):
    destination = archive_paths.destination(args.output)
    if args.command == "freeze" and args.allow_incomplete_through and not args.through:
        parser.error("--allow-incomplete-through needs --through")
    if (
        args.command in {"run", "pin-source", "status", "checkpoint"}
        and not (destination / "manifest.json").is_file()
    ):
        parser.error("No frozen corpus at --output")
    try:
        if args.command == "freeze":
            result = freeze(args, destination)
            print(
                history_backfill.encoded(
                    {
                        "unique_input_versions": len(result["files"]),
                        "captured_at": result["captured_at"],
                        "analysis_cutoff": result["analysis_cutoff"],
                        "cutoff_basis": result["cutoff_basis"],
                        "capture_failures": len(result["capture_failures"]),
                        "prior_captures": [
                            {
                                key: item[key]
                                for key in ("label", "versions_included", "prior_only_versions_added")
                            }
                            for item in result["prior_captures"]
                        ],
                        "watchlist_commit": (result["watchlist"] or {}).get("source_commit"),
                    }
                )
            )
        elif args.command == "run":
            with history_backfill.writer_lock(destination):
                result = history_backfill.run(destination, args.workers, args.max_days)
            print(history_backfill.encoded(result))
        elif args.command == "pin-source":
            with history_backfill.writer_lock(destination):
                pin = history_backfill.pin_source(destination)
            print(
                history_backfill.encoded(
                    {
                        "pinned_files": len(pin["code_sha256"]),
                        "git": pin["git"],
                        "detector_version": pin["detector_version"],
                        "runner": str(destination / history_backfill.RUNNER),
                    }
                )
            )
        elif args.command == "checkpoint":
            with history_backfill.writer_lock(destination):
                folded = history_backfill.checkpoint(destination)
            print(history_backfill.encoded({"wal_checkpointed": folded}))
            if not folded:
                parser.exit(1, "A reader still holds the corpus WAL; close it and checkpoint again.\n")
        elif args.command == "clone-inputs":
            # Refuse a nested or reused directory before the lock creates anything.
            history_backfill.clone_paths(args.source, destination)
            with history_backfill.writer_lock(destination):
                print(history_backfill.encoded(history_backfill.clone_inputs(args.source, destination)))
        else:
            result, _source = history_backfill.pinned_summary(destination, settled=False)
            print(history_backfill.encoded(result))
    except ERRORS as error:
        # Refusals explain themselves; other errors name only their type.
        reason = f": {error}" if isinstance(error, ValueError) else ""
        parser.exit(
            1,
            f"The {args.command} command stopped ({type(error).__name__}{reason}); "
            "completed inputs, receipts and day artifacts were not removed.\n",
        )


def main(argv=None):
    parser = arguments()
    args = parser.parse_args(argv)
    # A privileged archive capture must not leave bytecode owned by root in the install.
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        with archive_paths.private_umask():
            _execute(args, parser)
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
