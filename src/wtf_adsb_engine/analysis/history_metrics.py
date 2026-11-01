"""Freeze local Prometheus aircraft series, then analyze the saved responses offline.

Raw metric labels can be private. Both inputs and results belong in .private;
this module neither publishes nor modifies the receiver's collector journal.
"""

import gzip
import json
import math
import os
import re
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

from . import history_backfill as history
from .rules import number
from .stories import EMERGENCY_STATUSES

MAX_RESPONSE_BYTES = 128 * 1024 * 1024
MAX_JOINED_SNAPSHOTS = 1_000_000
QUERY_POLICY = "raw-range-vectors-v2-first-boundary-overlap"
READSB_FIELDS = {
    "lat",
    "lon",
    "gs",
    "alt_baro",
    "alt_geom",
    "baro_rate",
    "geom_rate",
    "ias",
    "tas",
    "mach",
    "seen",
    "seen_pos",
    "track",
    "roll",
    "nic",
    "nac_p",
    "dbFlags",
}
UAT_FIELDS = {
    "position_lat": "lat",
    "position_lon": "lon",
    "ground_speed": "gs",
    "pressure_altitude": "alt_baro",
    "geometric_altitude": "alt_geom",
    "vertical_velocity_barometric": "baro_rate",
    "vertical_velocity_geometric": "geom_rate",
    "true_track": "track",
    "nic": "nic",
    "nac_p": "nac_p",
}
LABEL_FIELDS = {"r", "t", "flight", "desc", "year", "category", "type", "squawk", "emergency", "ground"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Metric endpoint redirects are not allowed")


def open_local(url, *, timeout):
    return build_opener(NoRedirect).open(url, timeout=timeout)


def chunk_windows(start, end):
    return [
        (begin, min(begin + 3600, end))
        for begin in range(math.floor(start / 3600) * 3600, math.ceil(end), 3600)
    ]


def validate_manifest(manifest, *, complete):
    if (
        manifest.get("schema_version") != 1
        or number(manifest.get("start")) is None
        or number(manifest.get("end")) is None
    ):
        raise ValueError("Invalid metric manifest")
    if not 0 < manifest["end"] - manifest["start"] <= 366 * 86400:
        raise ValueError("Invalid metric manifest interval")
    expected = dict(enumerate(chunk_windows(manifest["start"], manifest["end"])))
    seen = set()
    for chunk in manifest.get("chunks", []):
        index = chunk.get("index")
        if (
            type(index) is not int
            or index not in expected
            or index in seen
            or (chunk.get("begin"), chunk.get("end")) != expected[index]
        ):
            raise ValueError("Metric chunk coverage is inconsistent")
        if not isinstance(chunk.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", chunk["sha256"]):
            raise ValueError("Invalid metric chunk hash")
        seen.add(index)
    if complete and (manifest.get("capture_complete") is not True or seen != set(expected)):
        raise ValueError("Metric capture is incomplete")
    policy = manifest.get("query_policy", "raw-range-vectors-v1-hour-boundaries")
    if policy not in {QUERY_POLICY, "raw-range-vectors-v1-hour-boundaries"}:
        raise ValueError("Unknown metric query policy")
    if policy != QUERY_POLICY and manifest["start"] % 3600 == 0 and 0 in seen:
        raise ValueError("Legacy capture starts on an excluded query boundary; recapture in a new directory")
    return manifest


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Explicit UTC offset is required")
    return parsed.timestamp()


def freeze(destination, *, start, end, endpoint="http://127.0.0.1:9090/prometheus"):
    """Explicit loopback-only, bounded raw range-vector queries; never resample."""
    destination = history.private_directory(destination)
    address = urlsplit(endpoint)
    if (
        address.scheme != "http"
        or address.hostname not in {"127.0.0.1", "localhost", "::1"}
        or address.username
        or address.password
    ):
        raise ValueError("Only the explicitly selected local Prometheus endpoint is supported")
    first, last = timestamp(start), timestamp(end)
    if not 0 < last - first <= 366 * 86400 or last > time.time():
        raise ValueError("Choose a completed historical interval of at most 366 days")
    manifest_path = destination / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        validate_manifest(manifest, complete=manifest.get("capture_complete") is True)
        if (manifest["start"], manifest["end"], manifest["endpoint"]) != (first, last, endpoint):
            raise ValueError("Capture parameters differ from the existing manifest")
    else:
        manifest = {
            "schema_version": 1,
            "query_policy": QUERY_POLICY,
            "start": first,
            "end": last,
            "endpoint": endpoint,
            "created_at": history.utc(),
            "chunks": [],
        }
    by_index = {chunk["index"]: chunk for chunk in manifest["chunks"]}
    chunks = history.private_directory(destination / "chunks")
    for index, (begin, finish) in enumerate(chunk_windows(first, last)):
        # Only one raw range selector per hour; range selectors are left-open.
        path = chunks / f"{index:05d}.json.gz"
        if index in by_index:
            if history.sha256(path.read_bytes()) != by_index[index]["sha256"]:
                raise ValueError("Saved metric response hash changed")
            continue
        duration_ms = math.ceil((finish - begin) * 1000) + (1 if index == 0 else 0)
        selector = '{__name__=~"readsb_aircraft_.+|aircraft_.+"}[' + str(duration_ms) + "ms]"
        query = urlencode({"query": selector, "time": finish, "timeout": "30s", "limit": 0})
        with open_local(endpoint.rstrip("/") + "/api/v1/query?" + query, timeout=45) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("Metric response exceeds size bound")
        payload = json.loads(raw)
        if (
            payload.get("status") != "success"
            or payload.get("warnings")
            or payload.get("data", {}).get("resultType") != "matrix"
        ):
            raise ValueError("Metric query is incomplete or returned warnings")
        compressed = gzip.compress(raw, compresslevel=3, mtime=0)
        temporary = path.with_suffix(".tmp")
        with temporary.open("wb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(compressed)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        chunk = {
            "index": index,
            "begin": begin,
            "end": finish,
            "sha256": history.sha256(compressed),
            "bytes": len(compressed),
            "captured_at": history.utc(),
            "series": len(payload["data"]["result"]),
            "samples": sum(len(series.get("values", [])) for series in payload["data"]["result"]),
        }
        manifest["chunks"].append(chunk)
        manifest["chunks"].sort(key=lambda item: item["index"])
        history.write_private(manifest_path, manifest)
        print(history.encoded({"metrics_chunk": index, "samples": chunk["samples"]}), flush=True)
    manifest["capture_complete"] = True
    manifest["captured_at"] = history.utc()
    history.write_private(manifest_path, manifest)
    return {
        "chunks": len(manifest["chunks"]),
        "samples_loaded": sum(chunk["samples"] for chunk in manifest["chunks"]),
    }


def joined_points(payloads, *, start, end):
    """Join only exact scrape timestamps with the same safe aircraft labels."""
    points, counts, fields = {}, Counter(), Counter()
    for payload, reference in payloads:
        for series in payload["data"]["result"]:
            labels = series.get("metric", {})
            name = labels.get("__name__", "")
            address = labels.get("hex", labels.get("address", "")).lower()
            if not re.fullmatch(r"~?[a-f0-9]{6}", address):
                counts["aggregate_series"] += 1
                continue
            uat = name.startswith("aircraft_")
            source = labels.get("address_qualifier", "unknown") if uat else labels.get("type", "unknown")
            if uat and source not in {"adsb_icao", "tisb_icao"} and not address.startswith("~"):
                address = "~" + address
            field = (
                UAT_FIELDS.get(name.removeprefix("aircraft_"))
                if uat
                else name.removeprefix("readsb_aircraft_")
            )
            if not uat and field not in READSB_FIELDS:
                field = None
            samples = [sample for sample in series.get("values", []) if start <= sample[0] < end]
            fields[name] += len(samples)
            counts["aircraft_metric_samples"] += len(samples)
            if field is None:
                continue
            identity = {key: value for key, value in labels.items() if key in LABEL_FIELDS}
            if uat:
                identity.update(
                    flight=labels.get("callsign", ""),
                    category=labels.get("emitter_category", ""),
                    type=source,
                    airground_state=labels.get("airground_state", "unknown"),
                    emergency="lifeguard"
                    if labels.get("emergency") == "medical"
                    else labels.get("emergency"),
                    squawk=labels.get("flightplan_id"),
                )
            if identity.get("emergency") not in EMERGENCY_STATUSES:
                identity.pop("emergency", None)
            if not re.fullmatch(r"[0-7]{4}", str(identity.get("squawk", ""))):
                identity.pop("squawk", None)
            label_key = history.digest(identity)
            # A cached old tag set can coexist with a newer one at the same
            # scrape time. Keep the entire source family private and separate.
            partition = history.digest({key: value for key, value in labels.items() if key != "__name__"})
            for stamp, raw in samples:
                value = number(raw)
                if value is None:
                    continue
                key = ("uat" if uat else "readsb", address, stamp, label_key, partition)
                if key not in points and len(points) >= MAX_JOINED_SNAPSHOTS:
                    raise ValueError(
                        "Metric analysis exceeds the joined-snapshot bound; use a narrower frozen interval"
                    )
                point = points.setdefault(
                    key,
                    {
                        "ts": stamp,
                        **identity,
                        "source": source,
                        "scrape_partition": partition,
                        "labels": identity,
                        "archive": reference,
                        "archives": [reference],
                        "timestamp_resolution_seconds": None,
                    },
                )
                if reference not in point["archives"]:
                    point["archives"].append(reference)
                if field in point and point[field] != value:
                    point.setdefault("conflicting_fields", []).append(field)
                point[field] = value
                if uat:
                    point["ground"] = labels.get("airground_state") == "ground"
                else:
                    point["ground"] = labels.get("ground") in {"ground", "true", "1"}
    tracks = {"readsb": defaultdict(list), "uat": defaultdict(list)}
    for (source, address, _, _, _), point in points.items():
        point.setdefault("gs", None)
        point["archive_alt"] = (
            "ground" if point.get("ground") else point.get("alt_baro", point.get("alt_geom"))
        )
        if point.get("ground"):
            point["alt_baro"] = "ground"
        lat, lon = number(point.get("lat")), number(point.get("lon"))
        has_position = lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180
        point["stale"] = (
            not has_position
            or bool(point.get("conflicting_fields"))
            or (source == "readsb" and not 0 <= point.get("seen_pos", math.inf) <= 15)
        )
        # A recorded age gauge can itself be cached. Neither exporter retains
        # acquisition timestamps here, so no metric source establishes motion.
        point["position_age_gauge_present"] = source == "readsb" and "seen_pos" in point
        tracks[source][address].append(point)
    for grouped in tracks.values():
        for items in grouped.values():
            items.sort(key=lambda item: item["ts"])
    counts["joined_aircraft_snapshots"] = len(points)
    return tracks, dict(counts), dict(fields)


def metadata_runs(points):
    """Keep each exporter chronological; identity changes break continuity."""
    streams = defaultdict(list)
    for point in points:
        streams[point["scrape_partition"]].append(point)
    for partition, stream in streams.items():
        current, previous, retained = [], None, {}
        for point in stream:
            observed = {
                key: point["labels"][key] for key in ("r", "t", "desc", "year") if point["labels"].get(key)
            }
            if number(point.get("dbFlags")) is not None:
                observed["dbFlags"] = int(point["dbFlags"])
            identity = history.digest(observed)
            if current and identity != previous:
                yield partition, previous, retained, current
                current = []
            previous, retained = identity, observed
            current.append(point)
        if current:
            yield partition, previous, retained, current


def analyze(destination, corpus):
    """Analyze frozen metric responses into a separate private result, offline."""
    destination, corpus = Path(destination), Path(corpus)
    manifest = json.loads((destination / "manifest.json").read_text())
    validate_manifest(manifest, complete=True)
    corpus_manifest = json.loads((corpus / "manifest.json").read_text())
    metadata_as_of = corpus_manifest["captured_at"]
    metadata_hash = corpus_manifest["metadata_sha256"]
    metadata_path = corpus / "inputs" / "aircraft-metadata.csv.gz"
    if history.sha256(metadata_path.read_bytes()) != metadata_hash:
        raise ValueError("Corpus metadata input hash changed")
    # Lookups read the corpus metadata table, which is loaded once at capture.
    with closing(sqlite3.connect(history.shared_uri(corpus / "corpus.sqlite3"), uri=True)) as database:
        frozen = database.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
    if frozen is None or json.loads(frozen[0]).get("metadata_sha256") != metadata_hash:
        raise ValueError("The corpus metadata table does not match its manifest")
    # history_metrics.py and stories.py are among the analysis files code_hashes() pins.
    code = history.code_hashes()
    inputs = {
        "metric_start": manifest["start"],
        "metric_end": manifest["end"],
        "query_policy": manifest.get("query_policy", "raw-range-vectors-v1-hour-boundaries"),
        "metric_chunks": [[chunk["index"], chunk["sha256"]] for chunk in manifest["chunks"]],
        "fallback_metadata_sha256": metadata_hash,
        "fallback_metadata_as_of": metadata_as_of,
    }
    saved = destination / "findings.json"
    if saved.exists():
        previous = json.loads(saved.read_text())
        if previous.get("code_sha256") != code or previous.get("inputs") != inputs:
            # A rerun may only reproduce a result, never replace it with another revision's.
            raise ValueError("Saved metric findings came from other code or inputs; analyze a copy elsewhere")

    def payloads():
        for chunk in manifest["chunks"]:
            path = destination / "chunks" / f"{chunk['index']:05d}.json.gz"
            if history.sha256(path.read_bytes()) != chunk["sha256"]:
                raise ValueError("Saved metric response hash changed")
            with gzip.open(path, "rb") as handle:
                raw = handle.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("Decoded metric response exceeds bound")
            yield json.loads(raw), path.name + "@" + chunk["sha256"]

    tracks, counts, fields = joined_points(payloads(), start=manifest["start"], end=manifest["end"])
    metadata = history.metadata_for(
        corpus / "corpus.sqlite3", {address for source in tracks.values() for address in source}
    )
    findings = []
    for source, grouped in tracks.items():
        for address, points in grouped.items():
            for partition, identity, retained, samples in metadata_runs(points):
                matched = {address: {**metadata.get(address, {}), **retained}}
                provenance = {
                    "retained_metric_metadata_fields": sorted(retained),
                    "retained_labels_first_at": history.utc(samples[0]["ts"]),
                    "retained_labels_last_at": history.utc(samples[-1]["ts"]),
                    "fallback_metadata_as_of": metadata_as_of,
                    "fallback_metadata_sha256": metadata_hash,
                    "scope": "Retained exporter metadata labels take precedence; missing fields use the frozen current database. Neither is independent historical identity verification.",
                }
                events = history.analyze_scalars(
                    {address: samples},
                    matched,
                    manifest["start"],
                    manifest["end"],
                    "prometheus_" + source,
                    metadata_as_of,
                    allow_jumps=False,
                )
                for event in events:
                    event["id"] = history.digest([event["id"], partition, identity])
                    event["public_export_eligible"] = False
                    event["story_eligible"] = False
                    event["evidence"].update(scrape_partition=partition, metadata_provenance=provenance)
                    event["evidence"]["historical_corpus"].update(
                        altitude_reference="explicit_metric_fields",
                        message_freshness="Unknown: acquisition timestamps were not exported and even position-age gauges can be cached.",
                        count_meaning="Qualifying scrape snapshots; repeated cached values are not independent aircraft messages or proof of sustained behavior.",
                        observation_time_meaning="Scrape time, not aircraft message or position acquisition time.",
                        publication_policy="Private scalar lead only; corroborate with dated archive or journal evidence before public writing.",
                    )
                    findings.append(event)
    result = {
        "schema_version": 1,
        "code_sha256": code,
        "inputs": inputs,
        "source": "frozen_prometheus_aircraft_metrics",
        "capture_query_policy": manifest.get("query_policy", "raw-range-vectors-v1-hour-boundaries"),
        "start": history.utc(manifest["start"]),
        "end": history.utc(manifest["end"]),
        "analyzed_at": history.utc(),
        "counts": counts,
        "metric_sample_counts": fields,
        "movement_evaluated": False,
        "public_export_eligible": False,
        "findings_by_kind": dict(Counter(event["kind"] for event in findings)),
        "findings": findings,
        "review_status": "unreviewed",
        "limitations": [
            "Metric samples are individual fields; their count is not a count of aircraft positions or messages.",
            "Fields are joined only at identical scrape timestamps and the same complete private tag set.",
            "Acquisition timestamps were not exported; even position-age gauges can be cached. No movement or position discontinuities are inferred from these metrics.",
            "Scalar candidates are private, freshness-unknown leads and are ineligible for automatic public writing until corroborated by dated archive or journal evidence.",
            "These sources overlap the compact archive and trace corpus; their findings must not be added as new flights.",
        ],
    }
    history.write_private(destination / "findings.json", result)
    summary = {key: value for key, value in result.items() if key != "findings"}
    summary["findings"] = len(findings)
    history.write_private(destination / "summary.json", summary)
    return summary
