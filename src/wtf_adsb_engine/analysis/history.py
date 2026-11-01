"""Read readsb's gzip-compressed 16-byte heatmap/replay records without altering archives."""

import argparse
import gzip
import io
import json
import struct
import zlib
from collections import Counter, defaultdict
from pathlib import Path

from .episode_evidence import octal_code

MAGIC = 0x0E7F7C9D
MAX_REPLAY_BYTES = 64 * 1024 * 1024
# A damaged deflate stream raises zlib.error, which is not an OSError or ValueError.
ARCHIVE_ERRORS = (OSError, ValueError, EOFError, zlib.error, gzip.BadGzipFile, UnicodeDecodeError)
TYPES = [
    "adsb_icao",
    "adsb_icao_nt",
    "adsr_icao",
    "tisb_icao",
    "adsc",
    "mlat",
    "other",
    "mode_s",
    "adsb_other",
    "adsr_other",
    "tisb_trackfile",
    "tisb_other",
    "mode_ac",
]


def error_type(error):
    """A recorded error name; zlib's own exception class is just called `error`."""
    return "zlib.error" if isinstance(error, zlib.error) else type(error).__name__


def read_replay(source):
    """Read one archive, a path or its raw bytes, with a fixed decompression bound before decoding it."""
    with gzip.open(io.BytesIO(source) if isinstance(source, bytes) else source, "rb") as handle:
        data = handle.read(MAX_REPLAY_BYTES + 1)
    if len(data) > MAX_REPLAY_BYTES or len(data) % 16:
        raise ValueError("Replay file exceeds 64 MiB or has invalid record length")
    return data


def label(record, stamp):
    """Decode one 16-byte label record; its saved time travels with the values.

    readsb writes squawk 0 and an empty callsign until either is known, so those
    placeholders stay missing. Each record replaces the previous label state.
    """
    code = struct.unpack_from("<i", record, 4)[0] & 0xFFFF
    flight = bytes(record[8:16]).decode("ascii", errors="replace").strip("\x00 ")
    fields = {"flight": flight} if flight else {}
    if squawk := octal_code(f"{code:04d}"):
        fields["squawk"] = squawk
    return {**fields, "label_at": stamp} if fields else {}


def points(path):
    data = read_replay(path)
    timestamp, labels = None, {}
    for offset in range(0, len(data), 16):
        address, lat, lon, alt, gs = struct.unpack_from("<Iiihh", data, offset)
        if address == MAGIC:
            timestamp = ((lat & 0xFFFFFFFF) * 4294967296 + (lon & 0xFFFFFFFF)) / 1000
            continue
        if timestamp is None:
            continue
        hexcode = ("~" if address & 0x1000000 else "") + f"{address & 0xFFFFFF:06x}"
        if lat >= 1 << 30:
            labels[hexcode] = label(data[offset : offset + 16], timestamp)
            continue
        if not -90000000 <= lat <= 90000000 or not -180000000 <= lon <= 180000000:
            continue
        source = (address >> 27) & 0x1F
        yield (
            timestamp,
            {
                "hex": hexcode,
                "lat": lat / 1e6,
                "lon": lon / 1e6,
                "archive_alt": "ground" if alt == -123 else None if alt == -124 else alt * 25,
                "gs": None if gs == -1 else gs / 10,
                "type": TYPES[source] if source < len(TYPES) else "unknown",
                **labels.get(hexcode, {}),
            },
        )


def scan(root, output):
    from .replay_patterns import private_report_directory, write_private_report

    output = Path(output)
    private_report_directory(output.parent)
    files = sorted(Path(root).glob("20??/*/*/heatmap/*.bin.ttf"))
    totals, addresses, by_day = Counter(), set(), defaultdict(set)
    peaks = {key: [] for key in ["speed", "altitude"]}
    special = {}
    first = last = None
    for index, path in enumerate(files):
        try:
            for stamp, a in points(path):
                first = stamp if first is None else min(first, stamp)
                last = stamp if last is None else max(last, stamp)
                totals["points"] += 1
                addresses.add(a["hex"])
                by_day[str(path.parent.parent.relative_to(Path(root)))].add(a["hex"])
                for key, field in [("speed", "gs"), ("altitude", "archive_alt")]:
                    value = a.get(field)
                    if not isinstance(value, (int, float)):
                        continue
                    if len(peaks[key]) < 30 or value > peaks[key][-1]["value"]:
                        match = next((p for p in peaks[key] if p["hex"] == a["hex"]), None)
                        item = {"value": value, "timestamp": stamp, **a, "archive": str(path)}
                        if match and match["value"] >= value:
                            continue
                        peaks[key] = [p for p in peaks[key] if p["hex"] != a["hex"]]
                        peaks[key].append(item)
                        peaks[key].sort(key=lambda x: x["value"], reverse=True)
                        peaks[key] = peaks[key][:30]
                if a.get("squawk") in {"7500", "7600", "7700"}:
                    k = f"{a['hex']}:{a['squawk']}:{int(stamp // 86400)}"
                    record = special.setdefault(
                        k,
                        {
                            "aircraft": a,
                            "first": stamp,
                            "last": stamp,
                            "inheriting_positions": 0,
                            "label_times": [],
                            "archive": str(path),
                        },
                    )
                    record["last"] = stamp
                    # Positions inherit one saved label; only label records count as code reports.
                    record["inheriting_positions"] += 1
                    if a["label_at"] not in record["label_times"]:
                        record["label_times"].append(a["label_at"])
            totals["files"] += 1
        except ARCHIVE_ERRORS:
            totals["unreadable_files"] += 1
        if index % 500 == 0:
            print(f"Scanned {index + 1}/{len(files)} archives; {len(addresses)} IDs", flush=True)
    result = {
        "counts": dict(totals),
        "unique_ids": len(addresses),
        "days": len(by_day),
        "first": first,
        "last": last,
        "peaks": peaks,
        "special_codes": [
            {**record, "label_records": len(record.pop("label_times"))} for record in special.values()
        ],
    }
    write_private_report(output.parent, result, output.name)
    print(json.dumps({k: v for k, v in result.items() if k not in {"peaks", "special_codes"}}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root")
    parser.add_argument("output")
    args = parser.parse_args()
    scan(args.root, args.output)
