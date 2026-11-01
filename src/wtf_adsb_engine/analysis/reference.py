"""Pinned reference data for spotting rules: type classes, address allocations,
beacon codes, notable variants, conservative performance envelopes and rated
operating limits transcribed from type certificate data sheets.

Every lookup fails soft. A missing, altered or unreadable data file makes its
lookups return None, and callers keep their built-in behaviour. Nothing here
fetches data at runtime. Reference facts describe database records and
published allocations; they never verify an aircraft's identity or mission.
"""

import bisect
import hashlib
import json
import logging
import os
import re
from functools import lru_cache, wraps
from itertools import pairwise
from pathlib import Path

DATA = Path(os.getenv("WTF_ADSB_REFERENCE_DIR", str(Path(__file__).resolve().parent / "data")))
# Reviewed file hashes. Replacement reference bundles must match these pins;
# see docs/reference-data.md for source terms and update requirements.
FILES = {
    "aircraft-types": (
        "reference-aircraft-types-v1.json",
        "832ad6d88e0a7c6177d5d216a48504d68c8b6318c6365aebce0ba133813ae21b",
    ),
    "identity": (
        "reference-identity-v1.json",
        "de6d31ec6a27e90c49600c386b8fe82724ddba69c2c8bb1e5220ac30f60db06a",
    ),
    "beacon-codes": (
        "reference-beacon-codes-v1.json",
        "ea9a80f311634bb33cc900c19d71a2b0aaafa4a380bc90dc581f65bb6d1ce652",
    ),
    "notable-variants": (
        "reference-notable-variants-v1.json",
        "013ae95df6d85b2cc44b57eb324de8f6286a6b05559f384609e0de4a6fdbf94c",
    ),
    "envelopes": (
        "reference-envelopes-v1.json",
        "52a7fb77d8619da443d075277f18132fa2d4d53b2394ae73161d27e9f6ee106a",
    ),
    "limits": (
        "reference-limits-v1.json",
        "04ef387bfb8945c49d14dadbc7ec477735a147bd3f8d561ebc0b64aca285c3e4",
    ),
}
MAX_BYTES = 4 * 1024 * 1024
LOG = logging.getLogger("discoveries.reference")
# Used when the type table is unavailable; never a reason to skip the table.
GROUP_DESIGNATORS = frozenset({"ZZZZ", "GLID", "ULAC", "BALL", "SHIP", "GYRO", "UHEL", "PARA"})
FALLBACK_ROTORCRAFT = frozenset(
    {
        "R22",
        "R44",
        "R66",
        "B06",
        "B407",
        "B429",
        "B412",
        "AS50",
        "AS55",
        "EC35",
        "EC45",
        "H60",
        "S76",
        "S92",
        "H47",
        "AS65",
        "S61",
        "EC30",
        "B430",
        "EC55",
        "A109",
        "H500",
        "B505",
        "A139",
        "EC20",
        "MD52",
        "AS32",
        "EC25",
        "EC75",
        "H145",
        "H160",
        "UHEL",
    }
)
N_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # US N-numbers never use I or O.
N_SUFFIX = 1 + len(N_LETTERS) * (1 + len(N_LETTERS))  # none, one or two letters
N_BUCKET4 = 1 + len(N_LETTERS) + 10  # after four digits: none, a letter or a digit
N_BUCKET3 = 10 * N_BUCKET4 + N_SUFFIX
N_BUCKET2 = 10 * N_BUCKET3 + N_SUFFIX
N_BUCKET1 = 10 * N_BUCKET2 + N_SUFFIX
N_FIRST, N_LAST = 0xA00001, 0xADF7C7
N_PATTERN = re.compile(r"N[1-9][0-9]{0,4}|N[1-9][0-9]{0,3}[A-HJ-NP-Z]|N[1-9][0-9]{0,2}[A-HJ-NP-Z]{2}")
ICAO_ADDRESS = re.compile(r"[0-9a-f]{6}")
OCTAL_CODE = re.compile(r"[0-7]{1,4}")
# Classes of special designators, then of ICAO Doc 8643 description letters.
SPECIAL_CLASSES = {"BALL": "balloon", "SHIP": "airship", "ULAC": "ultralight", "PARA": "parachute"}
DESCRIPTION_CLASSES = {
    "V": "vehicle",
    "H": "helicopter",
    "G": "gyroplane",
    "R": "tiltrotor",
    "T": "tiltrotor",
    "D": "uav",
    "L": "airplane",
    "S": "airplane",
    "A": "airplane",
}


_FAILED = set()


def _fail_soft(default=None):
    """Lookups never raise: unexpected data or input means "unavailable"."""

    def decorate(function):
        @wraps(function)
        def wrapper(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except Exception as error:  # noqa: BLE001 - reference data must never stop the collector
                if function.__name__ not in _FAILED:
                    _FAILED.add(function.__name__)
                    LOG.warning("Reference lookup %s failed (%s)", function.__name__, type(error).__name__)
                return default() if callable(default) else default

        return wrapper

    return decorate


def canonical_hash(data):
    """SHA-256 of canonical JSON (sorted keys, compact, ASCII), as pinned data files record it."""
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def pinned_json(path, *, file_sha256=None, content_sha256=None, keys=None, schema=True, max_bytes=MAX_BYTES):
    """Read and verify a pinned JSON object; raise OSError or ValueError otherwise.

    The one verified loader for pinned data files. Checks, in order: the size
    bound; the serialized file's SHA-256 when given; a JSON object with
    schema_version 1 (unless ``schema`` is False); and the recorded
    "content_sha256" against the canonical hash of the content (of ``keys``
    only, when given) and against ``content_sha256`` when given.
    """
    with Path(path).open("rb") as source:
        raw = source.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("file exceeds the reference size bound")
    if file_sha256 is not None and hashlib.sha256(raw).hexdigest() != file_sha256:
        raise ValueError("file differs from the reviewed version")
    data = json.loads(raw)
    if not isinstance(data, dict) or (schema and data.get("schema_version") != 1):
        raise ValueError("unsupported schema")
    recorded = data.get("content_sha256")
    if keys is None:
        content = {key: value for key, value in data.items() if key != "content_sha256"}
    else:
        content = {key: data[key] for key in keys}
    if recorded != canonical_hash(content) or (content_sha256 is not None and recorded != content_sha256):
        raise ValueError("content hash differs")
    return data


@lru_cache(maxsize=None)
def _load(name):
    """Return (data, None) for a verified dataset, or (None, reason). Read once."""
    try:
        filename, expected = FILES[name]
        return pinned_json(DATA / filename, file_sha256=expected), None
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, RecursionError) as error:
        reason = f"{type(error).__name__}: {error}"
        LOG.warning("Reference data %s unavailable; built-in rules apply (%s)", name, reason)
        return None, reason


GENERATION = 0


def clear_cache():
    """Forget loaded datasets, for tests and after a reviewed data update.

    GENERATION changes so that callers memoizing derived results recompute them.
    """
    global GENERATION
    for function in (_load, _type_sets, _identity, _beacon_codes, _variants, _limits):
        function.cache_clear()
    _FAILED.clear()
    GENERATION += 1


def dataset(name):
    """The verified dataset, or None. Callers must treat the result as read-only."""
    return _load(name)[0]


def status():
    """Loaded versions and load errors, for collector status reporting."""
    result = {}
    for name in FILES:
        data, error = _load(name)
        result[name] = {"loaded": data is not None, "version": (data or {}).get("dataset_version")}
        if error:
            result[name]["error"] = error
    return result


def clean(code):
    """A stripped, upper-case code; "" for anything that is not text."""
    return code.strip().upper() if isinstance(code, str) else ""


@lru_cache(maxsize=None)
@_fail_soft()
def _type_sets():
    data = dataset("aircraft-types")
    if data is None:
        return None
    return {
        "types": data["types"],
        "groups": frozenset(data["group_designators"]),
        "uav": frozenset(data["uav_types"]),
        "glider": frozenset(data["glider_types"]),
        "years": data["earliest_year"],
    }


@_fail_soft(False)
def is_group_designator(code):
    sets = _type_sets()
    code = clean(code)
    return code in (sets["groups"] if sets else GROUP_DESIGNATORS)


@_fail_soft()
def type_info(code):
    """Doc 8643 facts for a designator, or None when unknown or unavailable."""
    sets = _type_sets()
    code = clean(code)
    if sets is None or code not in sets["types"]:
        return None
    description, wake = sets["types"][code]
    kind, engines, engine = description[:1], description[1:2], description[2:3]
    if code in sets["uav"]:
        category = "uav"
    elif code in sets["glider"]:
        category = "glider"
    elif code in SPECIAL_CLASSES:
        category = SPECIAL_CLASSES[code]
    elif kind == "T" and engines == "0":
        category = "obstacle"
    else:
        category = DESCRIPTION_CLASSES.get(kind, "unknown")
    return {
        "designator": code,
        "description": description,
        "wake_category": wake if wake in ("L", "M", "H", "J") else None,
        "class": category,
        "engine_type": engine if engine in ("P", "T", "J", "E", "R") else None,
        "engine_count": int(engines) if engines.isdigit() else 2 if engines == "C" else None,
        "group": code in sets["groups"],
    }


@_fail_soft()
def type_class(code):
    info = type_info(code)
    return info["class"] if info else None


@_fail_soft()
def rotorcraft(code):
    """True for helicopters and gyroplanes, False for other known types, None when unknown."""
    info = type_info(code)
    if info is None or info["class"] == "unknown":
        return True if clean(code) in FALLBACK_ROTORCRAFT else None
    return info["class"] in ("helicopter", "gyroplane")


@_fail_soft()
def earliest_year(code):
    sets = _type_sets()
    code = clean(code)
    if sets is None or code in sets["groups"]:
        return None
    year = sets["years"].get(code)
    return year if isinstance(year, int) else None


@_fail_soft()
def wind_allowance(altitude_ft=None):
    """(allowance_kn, [band floor, band ceiling or None]) for a pressure altitude.

    The envelope file lists bands as [below_ft, allowance_kn]; a reading at an
    unknown altitude takes the last, largest allowance. None without the file.
    """
    data = dataset("envelopes")
    if data is None:
        return None
    bands = data["wind_allowance_bands_kn"]
    altitude = (
        altitude_ft if isinstance(altitude_ft, (int, float)) and not isinstance(altitude_ft, bool) else None
    )
    floor = None
    for below, allowance in bands:
        if below is None:
            break
        if altitude is not None and altitude < below:
            return allowance, [floor, below]
        floor = below
    # The open-ended last band; an unknown altitude belongs to no band.
    return bands[-1][1], [floor, None] if altitude is not None else None


@_fail_soft()
def envelope(code, altitude_ft=None):
    """Conservative ground-speed and altitude caps for a listed type, or None.

    The ground-speed cap adds the wind allowance of the reading's
    pressure-altitude band; an unknown altitude takes the largest allowance.
    """
    data = dataset("envelopes")
    code = clean(code)
    if data is None or not code:
        return None
    wind, band = wind_allowance(altitude_ft)
    margin = data["altitude_margin_ft"]
    basis, values = None, None
    if code in data["types"]:
        basis, values = "type", data["types"][code]
    elif code in data["groups"]:
        basis, values = "group", data["groups"][code]
    else:
        info = type_info(code)
        if info is None or info["group"]:
            return None
        key = None
        if info["class"] in ("helicopter", "gyroplane", "tiltrotor"):
            key = {"helicopter": "H", "gyroplane": "G", "tiltrotor": "R"}[info["class"]]
        elif info["class"] == "airplane" and info["engine_type"] and info["engine_count"]:
            count = info["engine_count"]
            if info["engine_type"] == "E":
                key = "E"
            elif info["engine_type"] in ("P", "T"):
                key = info["engine_type"] + ("1" if count == 1 else "2" if count == 2 else "4")
            elif info["engine_type"] == "J":
                key = "J" + str(min(count, 4))
        if key not in data["classes"]:
            return None
        basis, values = "class:" + key, data["classes"][key]
    speed, ceiling = values
    return {
        "type": code,
        "basis": basis,
        "speed_bound_kt": speed,
        "wind_allowance_kt": wind,
        "wind_band_ft": band,
        "ground_speed_cap_kt": speed + wind if speed is not None else None,
        "ceiling_ft": ceiling,
        "altitude_margin_ft": margin,
        "altitude_cap_ft": ceiling + margin if ceiling is not None else None,
        "dataset_version": data["dataset_version"],
    }


LIMIT_FIELDS = ("name", "max_operating_altitude_ft", "vmo_kias", "mmo", "vne_kias", "source")
# The optional dataset field naming the sheet each limit is read from when that is not
# the entry's ``source``: an EASA sheet can defer its airspeeds to an FAA sheet.
LIMIT_SOURCES = {
    "max_operating_altitude_ft": "altitude_source",
    "vmo_kias": "speed_source",
    "mmo": "speed_source",
    "vne_kias": "speed_source",
}


def limits(code):
    """The rated operating limits transcribed for a type, or None without an entry.

    Each entry names its type certificate data sheet (``source``), and
    ``sources`` maps every limit it states to the sheet that limit is read
    from ({dataset key: {title, url}}), which is ``source`` unless the dataset
    names another (limits.source_for). These are certified limits, not
    spotting bounds: limits.py compares sustained readings with them. The
    detector asks for every report, so one entry is built per type; callers
    must treat it as read-only, like dataset().
    """
    return _limits(clean(code))


@lru_cache(maxsize=4096)
@_fail_soft()
def _limits(code):
    data = dataset("limits")
    if data is None or not code or code not in data["types"]:
        return None
    entry = data["types"][code]
    return {
        "type": code,
        **{field: entry[field] for field in LIMIT_FIELDS},
        "sources": {
            key: dict(entry.get(field) or entry["source"])
            for key, field in LIMIT_SOURCES.items()
            if entry[key] is not None
        },
        "basis": "type certificate data sheet",
        "dataset_version": data["dataset_version"],
    }


@lru_cache(maxsize=None)
@_fail_soft()
def _identity():
    data = dataset("identity")
    if data is None:
        return None
    groups = {}
    for members in data["country_groups"].values():
        for code in members:
            groups[code] = frozenset(members)
    ranges = _first_listed([(int(start, 16), int(end, 16), code) for start, end, code in data["allocations"]])
    military = _first_listed(
        [(int(start, 16), int(end, 16), True) for start, end in data["military_allocations"]]
    )
    return {
        "ranges": ranges,
        "starts": [start for start, _, _ in ranges],
        "military": military,
        "military_starts": [start for start, _, _ in military],
        "countries": data["countries"],
        "groups": groups,
        "prefixes": {prefix: tuple(codes) for prefix, codes in data["registration_prefixes"].items()},
        "unhyphenated": tuple(sorted(data["unhyphenated_prefixes"], key=len, reverse=True)),
        "operators": frozenset(data["operator_designators"].split()),
    }


def _first_listed(ranges):
    """Disjoint, sorted (start, end, value) pieces in which every address keeps the
    value of the first listed range that contains it (sub-blocks are listed first)."""
    edges = sorted({start for start, _, _ in ranges} | {end + 1 for _, end, _ in ranges})
    pieces = []
    for low, high in pairwise(edges):
        value = next((value for start, end, value in ranges if start <= low <= end), None)
        if value is None:
            continue
        if pieces and pieces[-1][2] == value and pieces[-1][1] == low - 1:
            pieces[-1] = (pieces[-1][0], high - 1, value)
        else:
            pieces.append((low, high - 1, value))
    return pieces


def _piece(pieces, starts, value):
    index = bisect.bisect_right(starts, value) - 1
    return pieces[index][2] if index >= 0 and value <= pieces[index][1] else None


@_fail_soft()
def allocation(address):
    """The ICAO allocation State for a 24-bit address, or None."""
    tables = _identity()
    address = clean(address).lower()
    if tables is None or not ICAO_ADDRESS.fullmatch(address):
        return None
    code = _piece(tables["ranges"], tables["starts"], int(address, 16))
    return {"country": code.upper(), "name": tables["countries"].get(code)} if code else None


@_fail_soft(frozenset)
def country_names():
    """The names of the States the allocation table lists ("Germany", "United States")."""
    tables = _identity()
    return frozenset(tables["countries"].values()) if tables else frozenset()


@_fail_soft()
def military_allocation(address):
    """True when an address lies in a range used for military aircraft; None when unavailable."""
    tables = _identity()
    address = clean(address).lower()
    if tables is None or not ICAO_ADDRESS.fullmatch(address):
        return None
    return _piece(tables["military"], tables["military_starts"], int(address, 16)) is True


@_fail_soft(frozenset)
def country_group(code):
    """Allocation codes treated as one State (parent blocks and their territories)."""
    tables = _identity()
    code = clean(code).lower()
    if tables is None or not code:
        return frozenset()
    return tables["groups"].get(code, frozenset({code}))


@_fail_soft()
def country_name(code):
    tables = _identity()
    return tables["countries"].get(clean(code).lower()) if tables else None


@_fail_soft()
def registration_parts(registration):
    """Split a registration into (nationality mark, remainder), or None.

    A bare mark such as "F-" returns an empty remainder. Military serials and
    unrecognised formats return None.
    """
    tables = _identity()
    value = clean(registration)
    if tables is None or not value:
        return None
    if "-" in value:
        prefix, mark = value.split("-", 1)
        return (prefix, mark) if prefix in tables["prefixes"] else None
    if value in tables["prefixes"]:
        return value, ""
    for prefix in tables["unhyphenated"]:
        rest = value[len(prefix) :]
        # Unhyphenated marks are accepted only before a digit (N123AB, JA123A, HI1045).
        if value.startswith(prefix) and rest[:1].isdigit() and rest.isalnum():
            return prefix, rest
    return None


@_fail_soft()
def registration_countries(registration):
    """Allocation codes implied by a registration's nationality mark (primary first), or None."""
    tables = _identity()
    parts = registration_parts(registration)
    if tables is None or parts is None:
        return None
    return tuple(code.upper() for code in tables["prefixes"][parts[0]])


@_fail_soft()
def operator_designator(code):
    """True/False for a three-letter ICAO operator designator; None when unavailable."""
    tables = _identity()
    if tables is None:
        return None
    return clean(code) in tables["operators"]


@lru_cache(maxsize=None)
@_fail_soft()
def _beacon_codes():
    data = dataset("beacon-codes")
    if data is None:
        return None
    return [(int(entry["first"], 8), int(entry["last"], 8), entry) for entry in data["codes"]]


@_fail_soft()
def beacon_code(code):
    """The pinned FAA allocation entry for a four-digit octal code, or None."""
    entries = _beacon_codes()
    code = str(code).strip() if isinstance(code, (str, int)) and not isinstance(code, bool) else ""
    if entries is None or not OCTAL_CODE.fullmatch(code):
        return None
    value = int(code, 8)
    for first, last, entry in entries:
        if first <= value <= last:
            return {**entry, "code": code.zfill(4)}
    return None


def _description_key(value):
    return " ".join(value.split()).upper() if isinstance(value, str) else ""


@lru_cache(maxsize=None)
@_fail_soft()
def _variants():
    data = dataset("notable-variants")
    if data is None:
        return None
    index = {}
    for variant in data["variants"]:
        for description in variant["descriptions"]:
            index[_description_key(description)] = variant
    # AF1/AF2 are complete call signs; the others take a flight number (EXEC1F, SAM27).
    missions = re.compile(
        "|".join(
            re.escape(prefix) + ("" if prefix[-1].isdigit() else r"[0-9]{1,4}[A-Z]?")
            for prefix in data["mission_callsign_prefixes"]
        )
        or "(?!)"
    )
    return index, missions, data["dataset_version"]


@_fail_soft()
def notable_variant(description):
    """The curated variant entry for an exact database description, or None."""
    tables = _variants()
    key = _description_key(description)
    if tables is None or not key or key not in tables[0]:
        return None
    return {**tables[0][key], "dataset_version": tables[2]}


@_fail_soft()
def mission_callsign(callsign):
    """True when a call sign uses a curated VIP-mission prefix; None when unavailable."""
    tables = _variants()
    if tables is None:
        return None
    return tables[1].fullmatch(clean(callsign)) is not None


def _n_suffix(offset):
    if offset == 0:
        return ""
    offset -= 1
    first, second = divmod(offset, len(N_LETTERS) + 1)
    return N_LETTERS[first] + (N_LETTERS[second - 1] if second else "")


@_fail_soft()
def us_registration(address):
    """The N-number that the US algorithm assigns to an address, or None.

    PIA (privacy ICAO address) assignments do not follow this algorithm; callers
    must skip them. Addresses outside A00001-ADF7C7 are not N-number addresses.
    """
    address = clean(address).lower()
    if not ICAO_ADDRESS.fullmatch(address):
        return None
    value = int(address, 16)
    if not N_FIRST <= value <= N_LAST:
        return None
    offset = value - N_FIRST
    digit, offset = divmod(offset, N_BUCKET1)
    result = "N" + str(digit + 1)
    for size in (N_BUCKET2, N_BUCKET3):
        if offset < N_SUFFIX:
            return result + _n_suffix(offset)
        digit, offset = divmod(offset - N_SUFFIX, size)
        result += str(digit)
    if offset < N_SUFFIX:
        return result + _n_suffix(offset)
    digit, offset = divmod(offset - N_SUFFIX, N_BUCKET4)
    result += str(digit)
    if offset == 0:
        return result
    offset -= 1
    return result + (N_LETTERS[offset] if offset < len(N_LETTERS) else str(offset - len(N_LETTERS)))


@_fail_soft()
def us_address(registration):
    """The address the US algorithm assigns to a valid N-number, or None."""
    value = clean(registration).replace("-", "")
    if not N_PATTERN.fullmatch(value):
        return None
    rest = value[1:]
    result = N_FIRST + (int(rest[0]) - 1) * N_BUCKET1
    rest = rest[1:]
    for size in (N_BUCKET2, N_BUCKET3, N_BUCKET4):
        if not rest:
            return f"{result:06x}"
        if rest[0] in N_LETTERS:
            first = N_LETTERS.index(rest[0])
            second = N_LETTERS.index(rest[1]) + 1 if len(rest) > 1 else 0
            return f"{result + first * (len(N_LETTERS) + 1) + second + 1:06x}"
        result += N_SUFFIX + int(rest[0]) * size
        rest = rest[1:]
    if not rest:
        return f"{result:06x}"
    character = rest[0]
    offset = N_LETTERS.index(character) if character in N_LETTERS else len(N_LETTERS) + int(character)
    return f"{result + 1 + offset:06x}"
