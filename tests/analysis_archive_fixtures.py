"""Real-format readsb archive fixtures with synthetic places, addresses and labels.

The byte and JSON layouts follow readsb's own files, checked against samples of
the frozen corpus (structure only; no positions, identities or receiver data
were copied):

- Compact heatmap: a slice-offset header, 15-s slice markers, then label and
  position records. A label's squawk is 0 and its callsign empty until known,
  and labels repeat roughly every 60-105 s.
- Traces: gzip-compressed JSON despite the .json name; 14-element rows; flag
  bits 1 (gap of about 15 s or more before a fresh fix), 2 (new leg),
  4 (geometric rate) and 8 (geometric altitude); full-state detail objects;
  MLAT fixes repeated at one timestamp a few metres apart, the first of each
  pair often carrying flag 1 and the detail object.
- ACAS: newline-delimited aircraft.json reports with an acas_ra object, plus
  a CSV companion export.

Every position sits in the open South Pacific, far from any receiver.
"""

import gzip
import hashlib
import json
import math
import sqlite3
import struct
from contextlib import closing
from pathlib import Path

from wtf_adsb_engine.analysis import history_backfill as backfill
from wtf_adsb_engine.analysis.history import MAGIC, TYPES

ORIGIN = (-47.25, -128.5)
OWN, THREAT, OTHER = "a00a01", "a00a02", "a00a03"
RECEIVER_SENTINEL = "receiver-field-sentinel"
# A synthetic, well-formed pinned commit; nothing is fetched.
WATCHLIST_COMMIT = "0123456789abcdef0123456789abcdef01234567"
WATCHLIST_URL = (
    f"https://raw.githubusercontent.com/sdr-enthusiasts/plane-alert-db/{WATCHLIST_COMMIT}/plane-alert-db.csv"
)


def place(north_nm=0.0, east_nm=0.0, origin=ORIGIN):
    lat = origin[0] + north_nm / 60
    return round(lat, 6), round(origin[1] + east_nm / (60 * math.cos(math.radians(origin[0]))), 6)


def compact_bytes(slices, interval_ms=15000):
    """`slices` is [(stamp, records)]; records are label or position tuples.

    ("label", address, squawk_digits_as_int, callsign) or
    ("position", address, lat, lon, altitude_ft | None | "ground", gs_kn | None, source)
    """
    body, markers = [], []
    for stamp, records in slices:
        markers.append(len(slices) + len(body))
        milliseconds = round(stamp * 1000)
        low = milliseconds & 0xFFFFFFFF
        body.append(
            struct.pack(
                "<Iiihh", MAGIC, milliseconds >> 32, low - 2**32 if low >= 2**31 else low, interval_ms, 0
            )
        )
        for record in records:
            if record[0] == "label":
                _, address, squawk, callsign = record
                text = callsign.ljust(8).encode() if callsign else b"\0" * 8
                body.append(struct.pack("<Ii8s", int(address, 16), (1 << 30) | squawk, text))
            else:
                _, address, lat, lon, altitude, speed, source = record
                encoded = -123 if altitude == "ground" else -124 if altitude is None else round(altitude / 25)
                body.append(
                    struct.pack(
                        "<Iiihh",
                        int(address, 16) | TYPES.index(source) << 27,
                        round(lat * 1e6),
                        round(lon * 1e6),
                        encoded,
                        -1 if speed is None else round(speed * 10),
                    )
                )
    # readsb starts each file with the record index of every slice marker.
    header = [struct.pack("<Iiihh", index, 0, 0, 0, 0) for index in markers]
    return gzip.compress(b"".join(header + body), mtime=0)


def write_compact(root, slices, name="20.bin.ttf"):
    day = slices[0][0]
    from datetime import datetime, timezone

    path = Path(root) / datetime.fromtimestamp(day, timezone.utc).strftime("%Y/%m/%d") / "heatmap" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(compact_bytes(slices))
    return path


def labelled_compact_track(start, address=OWN, squawk=7700, callsign="TEST12", minutes=6):
    """A flight whose first label is the readsb placeholder, then a code repeated every 90 s."""
    slices = []
    for index in range(minutes * 4):
        stamp = start + index * 15
        records = []
        if index == 0:
            records.append(("label", address, 0, ""))
        elif index == 2:
            records.append(("label", address, 0, callsign))
        elif index >= 4 and index % 6 == 4:
            records.append(("label", address, squawk, callsign))
        lat, lon = place(0.05 * index)
        records.append(("position", address, lat, lon, 8000, 180, "adsb_icao"))
        slices.append((stamp, records))
    return slices


def adsb_details(callsign=None, squawk=None, **values):
    details = {
        "type": "adsb_icao",
        "alt_geom": values.pop("alt_geom", 8125),
        "track": 90.0,
        "category": "A1",
        "nic": 8,
        "rc": 186,
        "version": 2,
        "nic_baro": 1,
        "nac_p": 10,
        "nac_v": 2,
        "sil": 3,
        "sil_type": "perhour",
        "gva": 2,
        "sda": 2,
        "alert": 0,
        "spi": 0,
        "emergency": "none",
        "nav_qnh": 1013.6,
        "nav_altitude_mcp": 8000,
    }
    if callsign is not None:
        details["flight"] = callsign.ljust(8)
    if squawk is not None:
        details["squawk"] = squawk
    details.update(values)
    return details


def mlat_details(callsign="TEST34"):
    return {
        "type": "mlat",
        "flight": callsign.ljust(8),
        "track": 272.15,
        "baro_rate": 0,
        "category": "A2",
        "nic": 0,
        "rc": 0,
        "version": 0,
        "alert": 0,
        "spi": 0,
    }


def row(dt, north_nm, east_nm, altitude, gs, track, flags, rate, details=None, source="adsb_icao"):
    lat, lon = place(north_nm, east_nm)
    return [round(dt, 2), lat, lon, altitude, gs, track, flags, rate, details, source, None, None, None, None]


def trace_bytes(address, origin, rows, *, registration="N0TEST", typecode="C172"):
    root = {
        "icao": address,
        "r": registration,
        "t": typecode,
        "dbFlags": 0,
        "desc": "Synthetic fixture aircraft",
        "year": "2001",
        "ownOp": "Synthetic fixture operator",
        "version": "readsb fixture",
        "timestamp": origin,
        "trace": rows,
    }
    return gzip.compress(json.dumps(root, separators=(",", ":")).encode(), mtime=0)


def write_trace(root, address, origin, rows, **kwargs):
    from datetime import datetime, timezone

    day = datetime.fromtimestamp(origin, timezone.utc).strftime("%Y/%m/%d")
    path = Path(root) / day / "traces" / address[-2:] / f"trace_full_{address}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(trace_bytes(address, origin, rows, **kwargs))
    return path


def mlat_pairs(start_dt, count, north0=0.0):
    """MLAT fixes 8 or 21 s apart, each repeated at one timestamp ~3 m away.

    As in readsb's traces, the first row of a pair carries the detail object,
    and flag 1 only after 15 s or more without a position (or at the start).
    """
    rows, dt, previous = [], start_dt, None
    for index in range(count):
        north = north0 + index * 0.02
        flag = 1 if previous is None or dt - previous >= 15 else 0
        rows.append(row(dt, north, 0, None, 346.2, 272.1, flag, 0, mlat_details(), "mlat"))
        rows.append(row(dt, north + 0.0016, 0.0005, None, 346.2, 272.1, 0, 0, None, "mlat"))
        previous, dt = dt, dt + (21 if index % 3 == 2 else 8)
    return rows


def mlat_descent(offset_east_nm, rate=-6000):
    """A steady MLAT descent where each fix is repeated at one timestamp; the
    repeat is `offset_east_nm` away (0.0005 NM is about 1 m, 2 NM a conflict)."""
    rows, altitude = [], 30000
    for index in range(12):
        details = adsb_details("TEST12", "2341") if index == 0 else None
        rows.append(row(index * 5, index * 0.5, 0, altitude, 300, 0, 0, rate, details, "mlat"))
        rows.append(
            row(index * 5, index * 0.5 + 0.0016, offset_east_nm, altitude, 300, 0, 0, rate, None, "mlat")
        )
        altitude = round(altitude + rate / 12)
    return rows


def circle_rows(
    start_dt,
    seconds,
    step=5,
    radius_nm=0.25,
    period=180,
    gap=None,
    altitude=1250,
    centre=(0.0, 0.0),
    callsign="TEST56",
):
    """A steady orbit; `gap` (from, to) removes fixes and flags the first after it."""
    rows, flag_next = [], False
    speed = round(math.tau * radius_nm / period * 3600, 1)
    for t in range(0, seconds + 1, step):
        if gap and gap[0] < t < gap[1]:
            flag_next = True
            continue
        angle = t / period * math.tau
        rows.append(
            row(
                start_dt + t,
                centre[0] + radius_nm * math.sin(angle),
                centre[1] + radius_nm * math.cos(angle),
                altitude,
                speed,
                (math.degrees(angle) + 90) % 360,
                1 if flag_next else 0,
                0,
                adsb_details(callsign, "1200", alt_geom=altitude + 125) if t % 60 == 0 else None,
            )
        )
        flag_next = False
    return rows


def straight_rows(
    start_dt, seconds, north_nm, altitude, *, speed=150.0, step=5, callsign="TEST60", east0=0.0
):
    """Level flight due east at `speed` kn; a detail object every 30 s."""
    rows = []
    for t in range(0, seconds + 1, step):
        east = east0 + speed * t / 3600
        details = adsb_details(callsign, "4521", alt_geom=altitude + 125) if t % 30 == 0 else None
        rows.append(row(start_dt + t, north_nm, east, altitude, speed, 90.0, 0, 0, details))
    return rows


def formation_slices(start, members, seconds, step=15):
    """Compact slices with each (address, north_nm, altitude) flying due east at 150 kn."""
    slices = []
    for t in range(0, seconds + 1, step):
        records = []
        for address, north, altitude in members:
            lat, lon = place(north, 150 * t / 3600)
            records.append(("position", address, lat, lon, altitude, 150, "adsb_icao"))
        slices.append((start + t, records))
    return slices


def integrity_rows(
    start_dt, good_seconds, lost_seconds, north_nm, *, step=5, altitude=24000, detail_every=20
):
    """ADS-B version-2 reports, NIC 8/NACp 10 then NIC 0/NACp 0.

    As in readsb's traces, only some rows carry the detail object with these
    fields: one every `detail_every` seconds.
    """
    rows = []
    for t in range(0, good_seconds + lost_seconds + 1, step):
        lost = t > good_seconds
        details = (
            adsb_details("TEST70", "3301", nic=0 if lost else 8, nac_p=0 if lost else 10)
            if t % detail_every == 0
            else None
        )
        rows.append(row(start_dt + t, north_nm, 400 * t / 3600, altitude, 400, 90.0, 0, 0, details))
    return rows


def survey_rows(
    start_dt, legs=8, length_nm=8.0, spacing_nm=1.0, speed=120.0, step=5, altitude=6000, source="adsb_icao"
):
    """Lawnmower legs alternating east and west, each `spacing_nm` further north,
    joined by half-circle turns; a detail object every 60 s."""
    path, t = [], 0.0
    for index in range(legs):
        north, direction = index * spacing_nm, 1 if index % 2 == 0 else -1
        start_east = 0.0 if direction == 1 else length_nm
        leg_seconds = length_nm / speed * 3600
        for s in range(0, int(leg_seconds), step):
            path.append(
                (t + s, north, start_east + direction * speed * s / 3600, 90.0 if direction == 1 else 270.0)
            )
        t += leg_seconds
        if index + 1 < legs:
            radius = spacing_nm / 2
            turn_seconds = math.pi * radius / speed * 3600
            end_east = start_east + direction * length_nm
            for s in range(0, int(turn_seconds), step):
                angle = math.pi * s / turn_seconds
                east = end_east + direction * radius * math.sin(angle)
                path.append((t + s, north + radius - radius * math.cos(angle), east, 0.0))
            t += turn_seconds
    return [
        row(
            start_dt + round(stamp),
            north,
            east,
            altitude,
            speed,
            track,
            0,
            0,
            adsb_details("TEST80", "4627", alt_geom=altitude + 125) if index % 12 == 0 else None,
            source,
        )
        for index, (stamp, north, east, track) in enumerate(path)
    ]


def acas_report(own, threat, stamp, advisory="Level Off", *, rat="0", ara="1110000"):
    north, east = 4.0, -2.0
    lat, lon = place(north, east)
    return {
        "hex": own,
        "type": "adsb_icao",
        "flight": "TEST78  ",
        "r": "N0TEST",
        "t": "C25C",
        "desc": "Synthetic fixture aircraft",
        "alt_baro": 31000,
        "alt_geom": 31600,
        "gs": 420.5,
        "track": 271.0,
        "geom_rate": 1920,
        "squawk": "2341",
        "emergency": "none",
        "category": "A2",
        "nav_qnh": 1013.6,
        "nav_altitude_mcp": 31008,
        "nav_heading": 270.0,
        "lat": lat,
        "lon": lon,
        "nic": 8,
        "rc": 186,
        "version": 2,
        "nic_baro": 1,
        "nac_p": 10,
        "nac_v": 2,
        "sil": 3,
        "sil_type": "perhour",
        "gva": 2,
        "sda": 2,
        "alert": 0,
        "spi": 0,
        "mlat": [],
        "tisb": [],
        "messages": 1234,
        "seen": 0.1,
        "seen_pos": 0.2,
        "dbFlags": 0,
        "year": "2015",
        "ownOp": "Synthetic fixture operator",
        # Sentinels for the receiver-relative fields the indexer must remove.
        "r_dst": RECEIVER_SENTINEL,
        "r_dir": RECEIVER_SENTINEL,
        "rssi": RECEIVER_SENTINEL,
        "rr_lat": RECEIVER_SENTINEL,
        "rr_lon": RECEIVER_SENTINEL,
        "acas_ra": {
            "utc": "fixture",
            "unix_timestamp": stamp,
            "df_type": 17,
            "bytes": "E2E00007000AB1",
            "full_bytes": f"8DA00A01E2E00007000AB1{int(stamp * 10) % 0xFFFF:04X}",
            "ARA": ara,
            "RAT": rat,
            "MTE": "0",
            "RAC": "0000",
            "advisory_complement": "",
            "advisory": advisory,
            "TTI": "01",
            "threat_id_hex": threat,
        },
    }


def acas_lines(reports):
    return b"".join(json.dumps(report, separators=(",", ":")).encode() + b"\n" for report in reports)


def acas_csv(reports):
    return b"".join(
        f"fixture,{report['acas_ra']['unix_timestamp']}, {report['hex']},DF:,17,bytes:,{report['acas_ra']['bytes']},"
        f" RA: {report['acas_ra']['advisory']}; TIDh: {report['acas_ra']['threat_id_hex']},\n".encode()
        for report in reports
    )


def full_day(root, day_start, address=OTHER):
    """All 48 compact half-hour files of one UTC day, one short slice each."""
    from datetime import datetime, timezone

    folder = Path(root) / datetime.fromtimestamp(day_start, timezone.utc).strftime("%Y/%m/%d") / "heatmap"
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(48):
        lat, lon = place(index * 0.1)
        slices = [(day_start + index * 1800, [("position", address, lat, lon, 5000, 150, "adsb_icao")])]
        (folder / f"{index:02d}.bin.ttf").write_bytes(compact_bytes(slices))
    return folder


WATCHLIST_HEADER = (
    "$ICAO,$Registration,$Operator,$Type,$ICAO Type,#CMPG,$Tag 1,$#Tag 2,$#Tag 3,Category,$#Link\n"
)


def write_watchlist_cache(directory, rows, *, source_date="2026-09-26T14:04:45+00:00"):
    """A pinned watchlist cache laid out as tools/update_watchlist.py writes it.

    `rows` are (address, registration, operator, category). Returns the directory.
    """
    from wtf_adsb_engine.analysis import watchlists

    directory = Path(directory)
    version = directory / WATCHLIST_COMMIT
    version.mkdir(parents=True, exist_ok=True)
    data = (
        WATCHLIST_HEADER
        + "".join(
            f"{address.upper()},{registration},{operator},Synthetic type,C25C,Civ,Fixture,,,{category},\n"
            for address, registration, operator, category in rows
        )
    ).encode()
    licence = b"Fixture licence: https://opendatacommons.org/licenses/odbl/ and https://opendatacommons.org/licenses/dbcl/\n"
    catalog = watchlists.parse_catalog(
        data,
        WATCHLIST_URL,
        hashlib.sha256(data).hexdigest(),
        fetched_at="2026-10-01T00:00:00+00:00",
        source_date=source_date,
    )
    catalog["source"]["license_sha256"] = hashlib.sha256(licence).hexdigest()
    (version / "plane-alert-db.csv").write_bytes(data)
    (version / "LICENSE").write_bytes(licence)
    (directory / "current.json").write_text(json.dumps(catalog, indent=2) + "\n")
    return directory


def corrupt_deflate(data):
    """A valid gzip header with a damaged deflate body: zlib.error, not OSError."""
    raw = bytearray(gzip.compress(data, mtime=0))
    raw[10] = 0xFF
    return bytes(raw)


class Corpus:
    """A disposable frozen corpus around real-format archive files."""

    def __init__(self, root, metadata=b"a00a01;N0TEST;C172;0000;Synthetic aircraft;2020\n"):
        self.root = Path(root)
        self.archive = self.root / "archive"
        self.output = self.root / "private"
        self.journal = self.root / "source.sqlite3"
        self.archive.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.journal)) as database, database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS events(id INTEGER,kind TEXT,first_seen REAL,last_seen REAL,evidence TEXT)"
            )
        self.metadata = self.root / "metadata.csv.gz"
        self.metadata.write_bytes(gzip.compress(metadata))

    def freeze(self):
        return backfill.freeze(self.archive, self.journal, self.metadata, self.output)

    def day(self, day):
        if not (self.output / "manifest.json").exists():
            self.freeze()
        result = backfill.analyze_day(day, self.output)
        self.result = result
        return json.loads((self.output / "days" / (day + ".json")).read_text())["findings"]
