"""Import compact replay into a private journal backup, never an active collector database.

Install the engine's archive extra for NumPy. Inputs and backup paths are explicit;
archive findings keep their weaker freshness and altitude-reference evidence.
"""

import argparse
import csv
import gzip
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import archive_paths
from .app import MAX_RECEIVER_AGE, add_missing_columns, connect, initialize, meta, put_meta, record, remember
from .episode_replay import CARRIED_LABEL, carried_label
from .history import ARCHIVE_ERRORS, MAGIC, TYPES, error_type, label, read_replay
from .history_backfill import code_hashes, digest, writer_lock
from .replay_patterns import live_row
from .rules import WATCH_TYPES, candidates, number

# v2: placeholder labels stay missing, label records (not inheriting positions)
# count as code reports, archive altitude keeps its unknown reference, and
# positions at or after live collection are refused.
IMPORT_VERSION = "compact-scalars-v2"
IMPORT_COLUMNS = (
    ("detector_version", "TEXT"),
    ("code_sha256", "TEXT"),
    ("live_boundary", "REAL"),
    ("refused_live_positions", "INTEGER"),
)


def load_metadata(path):
    result = {}
    with gzip.open(path, "rt") as f:
        for r in csv.reader(f, delimiter=";"):
            if len(r) < 6:
                continue
            try:
                address = int(r[0], 16)
            except ValueError:
                continue
            flags = sum((1 << i) for i, char in enumerate(r[3]) if char == "1")
            result[address] = {"r": r[1], "t": r[2], "dbFlags": flags, "desc": r[4], "year": r[5]}
    return result


def slice_coverage(data, marker_indices, stamps, *, before=None):
    """Count recorded slices before a cutoff, split across UTC calendar days."""
    if before is not None:
        before = number(before)
        if before is None:
            raise ValueError("Coverage cutoff must be a finite timestamp")
    intervals = sorted(
        (float(stamps[index]), float(stamps[index]) + int(data["alt"][index]) / 1000)
        for index in marker_indices
        if int(data["alt"][index]) > 0
    )
    merged = []
    for start, end in intervals:
        if before is not None:
            if start >= before:
                break
            end = min(end, before)
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    coverage = defaultdict(float)
    for start, end in merged:
        while start < end:
            boundary = min(end, (int(start // 86400) + 1) * 86400)
            day = datetime.fromtimestamp(start, timezone.utc).date().isoformat()
            coverage[day] += boundary - start
            start = boundary
    return coverage


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Readsb globe_history archive directory")
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
        help="Existing private SQLite backup to update; never pass the active collector journal",
    )
    parser.add_argument("--metadata", type=Path, required=True, help="Aircraft metadata CSV gzip file")
    parser.add_argument(
        "--live-journal", type=Path, help="Optional active journal path to refuse, including hard links"
    )
    parser.add_argument(
        "--allow-live-overlap",
        action="store_true",
        help="Also import positions at or after the backup's live installation (normally refused). "
        "This does not permit writing to a running journal or merging archive evidence into live rows. "
        "Applies to files not yet imported; a clipped file keeps its recorded boundary.",
    )
    return parser


def import_archives(args):
    try:
        import numpy as np
    except ModuleNotFoundError:
        raise ValueError("Install wtf-adsb-engine[archive] to use compact replay import") from None
    dtype = np.dtype([("addr", "<u4"), ("lat", "<i4"), ("lon", "<i4"), ("alt", "<i2"), ("gs", "<i2")])
    root, database = args.root, args.database
    metadata = load_metadata(args.metadata)
    metadata_as_of = datetime.now(timezone.utc).isoformat()
    # The rules, decoders and any reference data they read decide what an import finds.
    code = code_hashes()
    provenance = {"detector_version": IMPORT_VERSION, "code_sha256": digest(code)}
    vintage_cutoff = datetime.now(timezone.utc).year - 50
    special = np.array(
        [
            n
            for n, a in metadata.items()
            if (a["dbFlags"] & 3)
            or a["t"] in WATCH_TYPES
            or (a["year"].isdigit() and 1900 <= int(a["year"]) <= vintage_cutoff)
        ],
        dtype=np.uint32,
    )
    helicopters = np.array([n for n, a in metadata.items() if a["t"] == "R44"], dtype=np.uint32)
    files = sorted(root.glob("20??/*/*/heatmap/*.bin.ttf"))
    current_paths = {str(path.relative_to(root)) for path in files}
    initialize(database)
    with connect(database) as db:
        installed_at = number(meta(db, "installed_at"))
        if installed_at is None:
            raise ValueError("Journal installation time is unavailable")
        # Live coverage uses receiver time, which may lag installation wall
        # time. Exclude that startup margin as well as all later replay slices.
        coverage_before = installed_at - MAX_RECEIVER_AGE
        # Archive rows would otherwise merge into live rows for the same
        # aircraft and double count them, so live-era positions are refused.
        live_boundary = None if args.allow_live_overlap else coverage_before
        db.execute(
            "CREATE TABLE IF NOT EXISTS replay_imports(path TEXT PRIMARY KEY, imported_at REAL, points INTEGER)"
        )
        # Legacy rows keep NULL: they predate recorded versions and boundaries.
        add_missing_columns(db, "replay_imports", IMPORT_COLUMNS)
        # Every digest that imported rows keeps its file map.
        known = meta(db, "replay_import_code", {}) or {}
        put_meta(db, "replay_import_code", {**known, provenance["code_sha256"]: code})
        done = {r[0] for r in db.execute("SELECT path FROM replay_imports")}
    total_points, refused_points, live_merges, unreadable = 0, 0, 0, []
    for index, path in enumerate(files):
        relative = str(path.relative_to(root))
        if relative in done:
            continue
        try:
            raw = read_replay(path)
            data = np.frombuffer(raw, dtype=dtype)
            markers = data["addr"] == MAGIC
            marker_indices = np.flatnonzero(markers)
            if not len(marker_indices):
                raise ValueError("Compact replay contains no slice markers")
        except ARCHIVE_ERRORS as error:
            # Unread files stay out of replay_imports, so the scan stays incomplete.
            unreadable.append({"archive": relative, "error_type": error_type(error)})
            continue
        nearest = np.maximum.accumulate(np.where(markers, np.arange(len(data)), 0))
        stamps = (
            data["lat"][nearest].astype(np.uint32).astype(np.uint64) * 4294967296
            + data["lon"][nearest].astype(np.uint32)
        ) / 1000
        address = data["addr"] & 0x1FFFFFF
        valid = (
            (np.arange(len(data)) >= marker_indices[0])
            & ~markers
            & (data["lat"] >= -90000000)
            & (data["lat"] <= 90000000)
            & (data["lon"] >= -180000000)
            & (data["lon"] <= 180000000)
        )
        refused = 0
        if live_boundary is not None:
            refused = int(np.count_nonzero(valid & (stamps >= live_boundary)))
            refused_points += refused
            valid &= stamps < live_boundary
        valid_indices = np.flatnonzero(valid)
        total_points += len(valid_indices)
        marked = np.isin(address, special)
        unusual = valid & (
            (data["gs"] >= 5500)
            | ((data["gs"] > 2500) & np.isin(address, helicopters))
            | (data["alt"] >= 1800)
            | ((data["alt"] < -60) & (data["alt"] != -123) & (data["alt"] != -124))
            | marked
        )
        selected = np.flatnonzero(unusual)
        # Callsign and squawk records are metadata interleaved with positions.
        label_indices = np.flatnonzero(
            (np.arange(len(data)) >= marker_indices[0]) & ~markers & (data["lat"] >= 1 << 30)
        )
        labels = defaultdict(list)
        for i in label_indices:
            # Squawk 0 and an empty callsign are placeholders; each label keeps its saved time.
            labels[int(address[i])].append((int(i), label(raw[16 * i : 16 * i + 16], float(stamps[i]))))
        code_addresses = {
            addr
            for addr, values in labels.items()
            if any(v.get("squawk") in {"7500", "7600", "7700"} for _, v in values)
        }
        if code_addresses:
            selected = np.union1d(
                selected,
                np.flatnonzero(valid & np.isin(address, np.array(list(code_addresses), dtype=np.uint32))),
            )

        def observation(i, include_labels=True, address=address, data=data, labels=labels):
            ident = int(address[i])
            kind = int(data["addr"][i] >> 27)
            a = {
                "hex": ("~" if ident & 0x1000000 else "") + f"{ident & 0xFFFFFF:06x}",
                **(metadata.get(ident, {}) if not ident & 0x1000000 else {}),
                "lat": int(data["lat"][i]) / 1e6,
                "lon": int(data["lon"][i]) / 1e6,
                "gs": float(data["gs"][i]) / 10 if data["gs"][i] >= 0 else None,
                "archive_alt": "ground"
                if data["alt"][i] == -123
                else None
                if data["alt"][i] == -124
                else int(data["alt"][i]) * 25,
                "type": TYPES[kind] if kind < len(TYPES) else "unknown",
            }
            if include_labels:
                # The latest label record is the current label state, placeholders included.
                for loc, fields in reversed(labels.get(ident, [])):
                    if loc <= i:
                        a.update(fields)
                        break
            return a

        groups = {}
        for i in selected:
            stamp = float(stamps[i])
            a = observation(i)
            evaluation = {**a, "alt_baro": a["archive_alt"], "seen_pos": 0, "seen": 0}
            for finding in candidates(evaluation, stamp):
                if finding.kind == "space_category":
                    continue
                if finding.kind.startswith("special_squawk_"):
                    finding.kind = "archive_squawk_" + a.get("squawk", "unknown")
                    finding.explanation += " Saved labels are carried across positions; only distinct label records count as code reports."
                finding.confidence = "Archived observations"
                finding.explanation += " Compact replay preserves sampled values, not full message-level freshness. Registration/type/year/flags were joined from the current aircraft database and may have changed since this date."
                if finding.kind == "outlier_alt_baro":
                    # The archive does not retain whether this altitude was barometric.
                    finding.kind = "outlier_archive_alt"
                if finding.kind in {"high", "outlier_archive_alt"}:
                    finding.why = f"Archived altitude reached {a['archive_alt']:,.0f} feet."
                    finding.explanation += (
                        " The archive altitude can be barometric or geometric; it does not identify which."
                    )
                key = (a["hex"], finding.kind)
                entry = groups.get(key)
                if entry is None:
                    entry = groups[key] = {
                        "a": a,
                        "finding": finding,
                        "first": stamp,
                        "last": stamp,
                        "count": 0,
                        "peak_time": stamp,
                        "label_times": set(),
                    }
                else:
                    entry["last"] = stamp
                    if (
                        finding.value is None
                        or entry["finding"].value is None
                        or abs(finding.value) > abs(entry["finding"].value)
                    ):
                        entry.update(a=a, finding=finding, peak_time=stamp)
                entry["count"] += 1
                if "label_at" in a:
                    entry["label_times"].add(a["label_at"])
        with connect(database) as db:
            # One representative observation per address per archive, with separate numeric maxima.
            order = valid_indices[np.argsort(address[valid_indices], kind="stable")]
            if len(order):
                ids, starts, counts = np.unique(address[order], return_index=True, return_counts=True)
                maxima_gs = np.maximum.reduceat(data["gs"][order], starts) / 10
                maxima_alt = np.maximum.reduceat(data["alt"][order], starts).astype(float) * 25
                for slot, (_ident, start, count) in enumerate(zip(ids, starts, counts, strict=False)):
                    first_index, last_index = order[start], order[start + count - 1]
                    a = observation(last_index, False)
                    # Do not replace a complete recent live observation with sparse replay fields.
                    remember(
                        db, a, float(stamps[first_index]), float(stamps[last_index]), observations=int(count)
                    )
                    db.execute(
                        "UPDATE aircraft SET max_gs=MAX(max_gs,?),max_alt=MAX(max_alt,?) WHERE hex=?",
                        (max(0, float(maxima_gs[slot])), max(0, float(maxima_alt[slot])), a["hex"]),
                    )
            for item in groups.values():
                status = item["finding"].kind.startswith("archive_squawk_")
                # Positions inherit one label record; only distinct records are code reports.
                count = len(item["label_times"]) if status else item["count"]
                isolated = count < 2
                if isolated and not (item["finding"].kind.startswith("outlier_") or status):
                    continue
                if isolated:
                    item["finding"].confidence = "Single archived sample · unconfirmed"
                if live_row(db, item["a"]["hex"], item["finding"], item["first"], item["last"]):
                    # Never merge archive samples into a live row; keep the live evidence intact.
                    live_merges += 1
                    continue
                extra = {
                    "archive": relative,
                    "observed_at": item["peak_time"],
                    "metadata_as_of": metadata_as_of,
                    "imported": True,
                    "replay_import": provenance,
                }
                if status:
                    extra["archive_labels"] = {
                        "label_records": count,
                        "inheriting_positions": item["count"],
                        "first_label_at": min(item["label_times"]),
                        "last_label_at": max(item["label_times"]),
                        "scope": "Saved label states, not received transponder replies.",
                    }
                # record() spreads extra over the finding's evidence: keep the finding's own tags.
                tags = set((item["finding"].evidence or {}).get("context_tags", []))
                if carried_label(item["a"], item["peak_time"]):
                    tags.add(CARRIED_LABEL)
                if tags:
                    extra["context_tags"] = sorted(tags)
                record(
                    db,
                    item["a"],
                    item["finding"],
                    item["first"],
                    item["last"],
                    count,
                    "Saved compact replay",
                    extra,
                )
            for day, seconds in slice_coverage(data, marker_indices, stamps, before=coverage_before).items():
                db.execute(
                    "INSERT INTO coverage VALUES (?,?,0) ON CONFLICT(day) DO UPDATE SET seconds=seconds+excluded.seconds",
                    (day, seconds),
                )
            # A clipped file stays done; its boundary and refused positions are on record.
            db.execute(
                "INSERT INTO replay_imports(path,imported_at,points,detector_version,code_sha256,live_boundary,refused_live_positions) VALUES (?,?,?,?,?,?,?)",
                (
                    relative,
                    time.time(),
                    len(valid_indices),
                    IMPORT_VERSION,
                    provenance["code_sha256"],
                    live_boundary,
                    refused,
                ),
            )
            done.add(relative)
            if index % 100 == 0 or index == len(files) - 1:
                done_count = len(done & current_paths)
                day_count = db.execute("SELECT COUNT(DISTINCT day) FROM coverage").fetchone()[0]
                put_meta(
                    db,
                    "archive_scan",
                    {
                        "files": done_count,
                        "days": day_count,
                        "total_files": len(files),
                        "complete": current_paths <= done and not unreadable,
                    },
                )
        if index % 500 == 0:
            print(f"Imported {index + 1}/{len(files)} archives", flush=True)
    with connect(database) as db:
        done_count = len(done & current_paths)
        put_meta(
            db,
            "archive_scan",
            {
                "files": done_count,
                "days": db.execute("SELECT COUNT(DISTINCT day) FROM coverage").fetchone()[0],
                "total_files": len(files),
                "complete": current_paths <= done and not unreadable,
                "unreadable_files": len(unreadable),
            },
        )
        print(
            json.dumps(
                {
                    "events": db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                    "unique_ids": db.execute("SELECT COUNT(*) FROM aircraft").fetchone()[0],
                    "archive_files": db.execute("SELECT COUNT(*) FROM replay_imports").fetchone()[0],
                    "refused_live_overlap_positions": refused_points,
                    "refused_live_merge_findings": live_merges,
                    "unreadable_files": unreadable,
                }
            ),
            flush=True,
        )


def main(argv=None):
    parser = arguments()
    args = parser.parse_args(argv)
    try:
        if not args.root.is_dir():
            raise ValueError("--root must be an existing archive directory")
        with archive_paths.private_umask():
            args.database = archive_paths.journal_backup(args.database, live=args.live_journal)
            with writer_lock(args.database.parent):
                import_archives(args)
    except ARCHIVE_ERRORS as error:
        reason = f": {error}" if isinstance(error, ValueError) else ""
        parser.exit(1, f"Replay import stopped ({type(error).__name__}{reason}).\n")


if __name__ == "__main__":
    main()
