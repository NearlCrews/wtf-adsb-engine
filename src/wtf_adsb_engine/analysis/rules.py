"""Explainable spotting heuristics, not flight-safety or mission determinations."""

import logging
import math
import re
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import lru_cache
from itertools import pairwise
from typing import NamedTuple

# Pinned reference data. Its lookups fail soft, so a missing or altered data
# file only switches the checks that need it off.
from . import limits, reference

# The one number parser and finite-number check; limits.py sits below this
# module and shares them, and other modules import both from here.
from .numeric import finite as finite
from .numeric import number

# Changes when per-report thresholds or identity semantics change (for archive provenance).
VERSION = "rules-v4"
LOG = logging.getLogger("discoveries.rules")


@dataclass
class Finding:
    kind: str
    category: str
    title: str
    value: float | None
    unit: str
    why: str
    explanation: str
    score: int
    confidence: str = "Repeated observations"
    evidence: dict | None = None

    def to_dict(self):
        return asdict(self)


WATCH_TYPES = {
    "B52": "Boeing B-52",
    "B1": "Rockwell B-1",
    "B2": "Northrop B-2",
    "U2": "Lockheed U-2",
    "SR71": "Lockheed SR-71",
    "E3TF": "Boeing E-3",
    "E3CF": "Boeing E-3",
    "E6": "Boeing E-6",
    "C5M": "Lockheed C-5M",
    "C5": "Lockheed C-5",
    "V22": "Bell Boeing V-22",
    "A3ST": "Airbus Beluga",
    "A337": "Airbus BelugaXL",
    "BLCF": "Boeing Dreamlifter",
    "A124": "Antonov An-124",
    "AN22": "Antonov An-22",
    "A388": "Airbus A380",
    "B17": "Boeing B-17",
    "B29": "Boeing B-29",
    "P51": "North American Mustang",
    "SPIT": "Supermarine Spitfire",
    "DC3": "Douglas DC-3",
    "DC6": "Douglas DC-6",
    "CONI": "Lockheed Constellation",
    "CAT": "Consolidated Catalina",
    "JU52": "Junkers Ju 52",
    "SHIP": "Airship",
    "BALL": "Balloon",
}
# A watchlist match is never described as proof of global rarity or a mission.
EMITTERS = {
    "B1": "Glider / sailplane",
    "B2": "Balloon / airship",
    "B3": "Parachutist",
    "B4": "Ultralight / hang glider",
    "B6": "Unmanned aircraft",
}
# A type with more addresses than this in the 30-day baseline is never locally rare.
RARE_TYPE_ADDRESSES = 3
# The one transponder-status table. maneuvers re-exports it, and other modules
# import it from there; a code and the aircraft's own emergency field describe
# one status.
STATUS_MEANINGS = {"7400": "lost_link", "7500": "unlawful", "7600": "nordo", "7700": "general"}
STATUS_CODES = frozenset(STATUS_MEANINGS)
STATUS_NAMES = frozenset({"general", "lifeguard", "minfuel", "nordo", "unlawful", "downed"})
# Wording for the per-report status findings. The live and episode-archive paths
# take statuses from maneuvers and drop these per-report findings.
SQUAWKS = {
    "7400": "unmanned aircraft lost link",
    "7500": "unlawful interference",
    "7600": "radio communication failure",
    "7700": "general emergency",
}
assert set(SQUAWKS) == STATUS_CODES
# Outlier bounds: a reading beyond them is a decoding or solver spike, never a record.
MAX_PLAUSIBLE_GS_KT = 900
# A report's own true airspeed is used within this range, by the detector and the wind
# worker alike; outside it the ground speed and the modelled wind decide.
REPORTED_TAS_KT = (50, MAX_PLAUSIBLE_GS_KT)
MAX_PLAUSIBLE_ALT_FT = 65000
# Balloons and the few types built to fly above that bound keep real altitude records.
HIGH_FLYING_TYPES = frozenset({"BALL", "U2", "WB57"})
HIGH_FLYING_ALT_FT = 150000
# The private "distant" finding and record keep only a range band (RULES-12).
RANGE_BAND_NM = 50
# Fixes inside this box around (0, 0) are decoder or receiver defaults, not positions.
NULL_ISLAND_DEGREES = 0.1
DEFAULT_SPEED_THRESHOLD = 600
DEFAULT_ALTITUDE_THRESHOLD = 49000
# Without reference data, the former R44 review threshold applies to rotorcraft.
FALLBACK_ROTORCRAFT_SPEED_CAP = 250
# Without the envelope file, the largest altitude band's wind allowance applies.
MAX_WIND_ALLOWANCE_KT = 170
# Weight classes in DO-260 emitter categories and the ICAO wake categories that
# contradict them. Light (A1) is below 15,500 lb; wake category L ends at 7,000 kg.
CATEGORY_WAKE_CONFLICTS = {
    "A1": {"H", "J"},
    "A2": {"L", "H", "J"},
    "A3": {"L"},
    "A4": {"L"},
    "A5": {"L", "M"},
}
CATEGORY_LABELS = {
    "A1": "light",
    "A2": "small",
    "A3": "large",
    "A4": "high-vortex large",
    "A5": "heavy",
    "A7": "rotorcraft",
    "B1": "glider",
    "B2": "lighter-than-air",
    "B4": "ultralight",
    "B6": "unmanned aircraft",
}
WAKE_LABELS = {"L": "light", "M": "medium", "H": "heavy", "J": "super"}
IDENTITY_CHECK_KEYS = (
    "us_registration_from_address",
    "registration_matches_address",
    "callsign_registration",
    "callsign_matches_registration",
    "allocation_country",
    "registration_country",
    "allocation_matches_registration",
    "airline_callsign_on_light_type",
    "pia",
    "year_plausible",
    "conflict",
)
# Claims about which aircraft this is need a corroborated, non-conflicting identity.
IDENTITY_LED_KINDS = frozenset(
    {
        "military",
        "watch_type",
        "vintage",
        "rare_here",
        "notable_variant",
        "first_type_here",
        "first_country_here",
        "seasonal_speed_record",
        "above_rated_altitude",
        "above_rated_speed",
    }
)
# Findings that compare a sustained reading with a type's certified limits.
RATED_KINDS = frozenset({"above_rated_altitude", "above_rated_speed"})
# Once the wind worker finds an encounter's rated-speed window implausible, its ground
# speeds are bad data: these findings of the encounter take the blocking flag below
# (wind.flag_ground_speed). A single report 1.5 times over its limit never becomes
# "fast" in the first place (_performance_candidates).
RATED_SPEED_IMPLAUSIBLE = "rated_speed_implausible"
GROUND_SPEED_KINDS = frozenset({"fast", "seasonal_speed_record"})
# No one-report stories: a spike, envelope, or rated-limit finding needs this many
# coherent reports over this long. The collector confirms envelope and rated
# readings over them (app), curation publishes no finding with fewer, and a
# rated speed needs this many consecutive corrected reports over its limit (wind).
MIN_REPORTS = limits.SUSTAINED_REPORTS
MIN_REPORT_SECONDS = limits.SUSTAINED_SECONDS
# The report field each numeric finding measures.
MEASURED_FIELDS = {
    "fast": "gs",
    "type_speed_mismatch": "gs",
    "outlier_gs": "gs",
    "wind_boost": "gs",
    "above_rated_speed": "gs",
    "vertical": "baro_rate",
    "outlier_baro_rate": "baro_rate",
}
AIRLINE_CALLSIGN = re.compile(r"[A-Z]{3}[0-9]{1,4}[A-Z]{0,2}")
MILITARY_SERIAL = re.compile(r"[0-9]{2}-[0-9]{3,5}|[0-9]{2}\+[0-9]{2}|[0-9]{3,6}|Z[A-Z][0-9]{3}")
ICAO_ADDRESS = re.compile(r"[0-9a-f]{6}")
TYPE_DESIGNATOR = re.compile(r"[A-Z0-9]{2,4}")
# Identifier patterns for identifier(), matched against the upper-case value.
IDENTIFIERS = {
    "registration": r"[A-Z0-9][A-Z0-9-]{1,11}",
    "callsign": r"[A-Z0-9]{2,8}",
    "icao_address": r"[0-9A-F]{6}",
    "type_code": TYPE_DESIGNATOR.pattern,
}
# Database placeholders and malformed text are not registrations.
PLACEHOLDER_REGISTRATIONS = frozenset({"UNKNOWN", "NONE", "TBC", "TBD", "UNASSIGNED", "UNREGISTERED"})
REGISTRATION_TEXT = re.compile(r"[A-Z0-9][A-Z0-9 -]{0,15}")
# German military markings carry a cross (43+29); placeholders such as 4X+XX do not count.
MILITARY_CROSS = re.compile(r"[0-9]{2}\+[0-9]{2}")
# Class A airspace in the United States and southern Canada begins at FL180.
CLASS_A_FLOOR_FT = 18000
FIRST_TYPE_COVERAGE_DAYS = 90
FIRST_COUNTRY_COVERAGE_DAYS = 30
FIRST_TYPE_MAX_OTHER_TYPE_DAYS = 2
SEASON_MIN_ADDRESSES = 20
SEASON_SUSTAINED_SECONDS = 60
SEASON_MIN_SAMPLES = 4
# Reports in a sustained window are at most this far apart.
SEASON_SAMPLE_GAP_SECONDS = 45
# A day counts toward coverage with at least an hour of collection.
COVERAGE_SECONDS = 3600
# A season record needs a baseline of at least this many days of saved reports.
SEASON_MIN_BASELINE_DAYS = 14
# Meteorological seasons begin on these UTC months.
SEASON_MONTHS = ((12, "winter"), (9, "autumn"), (6, "summer"), (3, "spring"))


def null_island(lat, lon):
    return abs(lat) < NULL_ISLAND_DEGREES and abs(lon) < NULL_ISLAND_DEGREES


def position(a):
    lat, lon, age = number(a.get("lat")), number(a.get("lon")), number(a.get("seen_pos"))
    return (
        lat is not None
        and lon is not None
        and -90 <= lat <= 90
        and -180 <= lon <= 180
        and not null_island(lat, lon)
        and age is not None
        and 0 <= age <= 15
    )


def distance_nm(a, b):
    p, q = math.radians(a[0]), math.radians(b[0])
    dlat, dlon = q - p, math.radians(b[1] - a[1])
    v = math.sin(dlat / 2) ** 2 + math.cos(p) * math.cos(q) * math.sin(dlon / 2) ** 2
    return 3440.065 * 2 * math.asin(math.sqrt(min(1, max(0, v))))


_text = reference.clean


def identifier(value, expression):
    """The stripped, upper-case text when it fully matches `expression` (IDENTIFIERS), else None."""
    value = _text(value)
    return value if re.fullmatch(expression, value) else None


def _db_flags(aircraft):
    """The aircraft database's flag bits (1 military, 2 interesting, 4 PIA)."""
    return int(number(aircraft.get("dbFlags")) or 0)


def _callsign(aircraft):
    return re.sub(r"\s+", "", _text(aircraft.get("flight")))


def _non_icao(aircraft):
    """A ~ track identifier rather than an ICAO address."""
    return str(aircraft.get("hex", "")).startswith("~")


def range_band(value):
    """The lower bound of a coarse range band; exact ranges are never saved."""
    value = number(value)
    return math.floor(value / RANGE_BAND_NM) * RANGE_BAND_NM if value is not None and value > 0 else 0


def altitude_bound(aircraft):
    """The highest plausible altitude record for this aircraft's category and type."""
    high = aircraft.get("category") == "B2" or _text(aircraft.get("t")) in HIGH_FLYING_TYPES
    return HIGH_FLYING_ALT_FT if high else MAX_PLAUSIBLE_ALT_FT


def group_designator(code):
    return reference.is_group_designator(code)


def rotorcraft(aircraft):
    """The one rotorcraft classifier: helicopter or gyroplane by reference type
    (with its fallback list), else the A7 emitter category of an unknown type."""
    code = _text(aircraft.get("t"))
    known = reference.rotorcraft(code) if code else None
    return known if known is not None else aircraft.get("category") == "A7"


def band_altitude(aircraft):
    """The altitude that picks a report's wind-allowance band, or None.

    Pressure altitude when reported, else geometric altitude, else an archive's
    numeric altitude, whose reference the archive does not record. The bands are
    10,000 ft deep, so the gap between the references moves a reading at most
    into the neighbouring band; without any altitude the largest allowance
    applies. Archive heatmap fixes carry only archive_alt and geometric trace
    fixes only alt_geom, so reading alt_baro alone would give them that largest
    allowance.
    """
    for field in ("alt_baro", "alt_geom", "archive_alt"):
        value = number(aircraft.get(field))
        if value is not None:
            return value
    return None


def type_envelope(aircraft, *, rotor=None):
    """Conservative speed/altitude caps for the listed type, or None.

    The ground-speed cap's wind allowance depends on the report's altitude
    (band_altitude): the jet stream can add far more above 30,000 ft than winds
    near the surface. An unknown altitude takes the largest allowance.
    """
    code = _text(aircraft.get("t"))
    found = reference.envelope(code, band_altitude(aircraft)) if code else None
    if found is None and (rotorcraft(aircraft) if rotor is None else rotor):
        found = {
            "type": code or None,
            "basis": "fallback:rotorcraft",
            "speed_bound_kt": None,
            "wind_allowance_kt": None,
            "wind_band_ft": None,
            "ground_speed_cap_kt": FALLBACK_ROTORCRAFT_SPEED_CAP,
            "ceiling_ft": None,
            "altitude_margin_ft": None,
            "altitude_cap_ft": None,
            "dataset_version": None,
        }
    return found


def speed_plausible(aircraft, gs):
    """Whether a sustained ground speed can be a record for the listed type.

    Beyond the outlier bound, the type envelope's cap, or a rated airspeed
    limit's true-airspeed equivalent plus the altitude band's wind allowance,
    the reading is bad data or a wrong type, never a record. Callers check it
    before seasonal_speed_record(), whose baseline must not take such a speed.
    """
    gs = number(gs)
    if gs is None or gs > MAX_PLAUSIBLE_GS_KT:
        return False
    envelope = type_envelope(aircraft)
    cap = (envelope or {}).get("ground_speed_cap_kt")
    if cap is not None and gs > cap:
        return False
    entry = reference.limits(_text(aircraft.get("t")))
    if entry is None:
        return True
    altitude = band_altitude(aircraft)
    wind = (envelope or {}).get("wind_allowance_kt")
    if wind is None:
        wind = (reference.wind_allowance(altitude) or (MAX_WIND_ALLOWANCE_KT, None))[0]
    ceiling = limits.max_plausible_ground_speed_kt(entry, altitude, wind)
    return ceiling is None or gs <= ceiling


def parse_registration(value):
    """(compact registration, nationality parts), or (None, parts) when it is no registration.

    The one registration normalizer for identity checks and watchlist matches.
    Placeholder words, malformed text and a bare nationality mark ("F-", "N",
    "OY-") are database placeholders, never a registration with a country.
    """
    text = _text(value)
    if "+" in text:
        return (text.replace("+", ""), None) if MILITARY_CROSS.fullmatch(text) else (None, None)
    if text in PLACEHOLDER_REGISTRATIONS or not REGISTRATION_TEXT.fullmatch(text):
        return None, None
    parts = reference.registration_parts(text)
    compact = re.sub(r"[ -]", "", text)
    if (parts is not None and parts[1] == "") or text.endswith("-") or len(compact) < 2:
        return None, parts
    return compact, parts


IDENTITY_FIELDS = ("hex", "r", "t", "flight", "dbFlags", "year")
# The checks when they cannot be computed (non-ICAO address or a failure).
UNCOMPUTED_IDENTITY = dict.fromkeys(IDENTITY_CHECK_KEYS) | {"conflict": False}


def identity_checks(a):
    """Deterministic consistency checks between address, registration and call sign.

    Values are None when a check cannot be computed. An N-number registration
    that the US algorithm assigns to another address, a registration-style call
    sign that differs, or an allocation/nationality disagreement sets "conflict".
    PIA and non-ICAO (~) addresses skip the algorithm. These checks compare
    database and broadcast fields; they never establish who operates the aircraft.
    Results are memoized on the identity fields, so call once per report.
    """
    key = tuple(_hashable(a.get(field)) for field in IDENTITY_FIELDS)
    return dict(_identity_checks(key, reference.GENERATION))


def _hashable(value):
    # Booleans are never identity values, and True == 1 would share a cache key.
    return value if isinstance(value, (str, int, float)) and not isinstance(value, bool) else None


@lru_cache(maxsize=4096)
def _identity_checks(key, generation):
    try:
        return _compute_identity(dict(zip(IDENTITY_FIELDS, key, strict=True)))
    except Exception:  # noqa: BLE001 - identity checks must never stop the collector
        LOG.exception("Identity checks failed; treating them as not computed")
        return dict(UNCOMPUTED_IDENTITY)


def _compute_identity(a):
    address = str(a.get("hex") or "").strip().lower()
    if not ICAO_ADDRESS.fullmatch(address):
        # Non-ICAO (~) track identifiers have no database identity to compare.
        return dict(UNCOMPUTED_IDENTITY)
    flags = number(a.get("dbFlags"))
    known_record = bool(a.get("r") or a.get("t"))
    pia = bool(int(flags) & 4) if flags is not None else (False if known_record else None)
    registration, parts = parse_registration(a.get("r"))
    callsign = _callsign(a)
    typecode = _text(a.get("t"))

    us_registration = reference.us_registration(address) if pia is not True else None
    registration_matches = None
    if registration and pia is not True:
        assigned = reference.us_address(registration)
        if assigned is not None:
            # An N-number belongs to one address, wherever this one was allocated.
            registration_matches = assigned == address
        elif us_registration and parts is not None and parts[0] != "N":
            registration_matches = False

    callsign_registration = None
    if callsign and reference.us_address(callsign):
        callsign_registration = callsign
    elif callsign and registration and callsign == registration:
        callsign_registration = callsign
    callsign_matches = None
    if callsign_registration:
        if registration:
            callsign_matches = callsign_registration == registration
        elif us_registration and pia is False:
            # Only a known, non-PIA record may be compared with the address's own N-number.
            callsign_matches = callsign_registration == us_registration

    allocation = reference.allocation(address)
    allocation_country = allocation["country"] if allocation else None
    # Only a real registration has a nationality: a bare mark such as "F-" is a
    # database placeholder, not a French registration.
    countries = reference.registration_countries(a.get("r")) if registration is not None else None
    registration_country = None
    allocation_matches = None
    if countries:
        group = {code.upper() for code in reference.country_group(allocation_country or "")}
        shared = [code for code in countries if code in group]
        registration_country = (
            allocation_country if allocation_country in shared else (shared or countries)[0]
        )
        if allocation_country:
            allocation_matches = bool(shared)

    airline = None
    info = reference.type_info(typecode) if typecode else None
    if callsign and info:
        operator = (
            reference.operator_designator(callsign[:3]) if AIRLINE_CALLSIGN.fullmatch(callsign) else False
        )
        if operator is not None:
            airline = bool(operator) and info["engine_type"] in ("P", "T")

    year = number(a.get("year"))
    earliest = reference.earliest_year(typecode) if typecode else None
    return {
        "us_registration_from_address": us_registration,
        "registration_matches_address": registration_matches,
        "callsign_registration": callsign_registration,
        "callsign_matches_registration": callsign_matches,
        "allocation_country": allocation_country,
        "registration_country": registration_country,
        "allocation_matches_registration": allocation_matches,
        "airline_callsign_on_light_type": airline,
        "pia": pia,
        # Additional to the shared contract: a database year before the type's
        # earliest plausible year. It suppresses vintage but is not a conflict.
        "year_plausible": None if year is None or earliest is None else year >= earliest,
        "conflict": any(
            value is False for value in (registration_matches, callsign_matches, allocation_matches)
        ),
    }


def performance_context(aircraft):
    altitude, rate = number(aircraft.get("alt_baro")), number(aircraft.get("baro_rate"))
    if altitude is None or rate is None:
        return None
    phase = "climb" if rate > 500 else "descent" if rate < -500 else "level"
    return phase, int(altitude // 10000)


def performance_thresholds(aircraft, profiles=None):
    """Local spotting thresholds from the learned profiles, else the defaults."""
    typecode = aircraft.get("t")
    profile = (profiles or {}).get(typecode, {}) if isinstance(typecode, str) else {}
    context = {}
    if phase_band := performance_context(aircraft):
        phase, band = phase_band
        context = profile.get("contexts", {}).get(f"{phase}:{band}", {})
    return (
        context.get("gs", profile.get("gs", DEFAULT_SPEED_THRESHOLD)),
        profile.get("alt", DEFAULT_ALTITUDE_THRESHOLD),
    )


def identity_corroborated(checks, aircraft):
    """At least one check ties the database identity to this address or broadcast.

    A military-flagged record in an address range used for military aircraft
    counts too: such aircraft usually carry serials, not civil registrations.
    """
    if any(
        checks.get(key) is True
        for key in (
            "registration_matches_address",
            "callsign_matches_registration",
            "allocation_matches_registration",
        )
    ):
        return True
    if not _db_flags(aircraft) & 1:
        return False
    return reference.military_allocation(aircraft.get("hex")) is True or bool(
        MILITARY_SERIAL.fullmatch(_text(aircraft.get("r")))
    )


def _solver_field(aircraft, field, sources=("mlat",)):
    """Whether a multilateration (or TIS-B) solution, not the aircraft, supplied the field."""
    for source in sources:
        listed = aircraft.get(source)
        if isinstance(listed, list):
            if field in listed:
                return True
        elif source == "mlat" and aircraft.get("type") == "mlat" and field in ("gs", "track", "lat", "lon"):
            return True
    return False


def _merge(evidence, key, values):
    if values:
        evidence[key] = sorted(set(evidence.get(key, [])) | set(values))


def _annotate(findings, checks, aircraft):
    """Attach identity checks and the shared non-blocking tags and blocking flags.

    Every per-report finding carries the identity checks. A conflict adds the
    metadata_identity_conflict tag. A claim about the aircraft's identity also
    gets the blocking identity_unverified flag when the identity is in conflict
    or nothing corroborates it. A value from a multilateration solution is tagged.
    """
    conflict = checks.get("conflict") is True
    # Without the identity tables nothing can corroborate an identity; only an
    # explicit conflict then withholds identity claims (the pre-reference behaviour).
    checkable = reference.dataset("identity") is not None
    unverified = conflict or (checkable and not identity_corroborated(checks, aircraft))
    for finding in findings:
        evidence = dict(finding.evidence or {})
        evidence["identity_checks"] = dict(checks)
        tags = ["metadata_identity_conflict"] if conflict else []
        field = MEASURED_FIELDS.get(finding.kind)
        if field and _solver_field(aircraft, field):
            tags.append("mlat_solver_estimate")
        _merge(evidence, "context_tags", tags)
        if finding.kind in IDENTITY_LED_KINDS and unverified:
            _merge(evidence, "quality_flags", ["identity_unverified"])
        finding.evidence = evidence
    return findings


def candidates(a, now, rarity=None, profiles=None, *, identity=None, band=None):
    """Per-report metadata findings, then performance findings for a fresh airborne position.

    ``band`` is the report's range band from the receiver (range_band), which
    the caller computes from the raw report; the exact range is never passed.
    """
    findings = []
    checks = identity if isinstance(identity, dict) else identity_checks(a)

    def add(*args, **kwargs):
        return findings.append(Finding(*args, **kwargs))

    flags = _db_flags(a)
    if flags & 1:
        add(
            "military",
            "airframes",
            "Military-tagged visitor",
            None,
            "",
            "The aircraft database marks this address as military.",
            "This is a database classification. It does not identify the current mission, crew, or purpose of the flight.",
            78,
            "Database tag",
        )
    if flags & 2:
        add(
            "database_watch",
            "airframes",
            "On the database's interesting list",
            None,
            "",
            "The aircraft database has its interesting-aircraft flag set.",
            "The database's maintainers selected this aircraft. The flag does not explain why and may be outdated.",
            66,
            "Database tag",
        )
    typecode = a.get("t", "")
    if typecode in WATCH_TYPES:
        add(
            "watch_type",
            "airframes",
            WATCH_TYPES[typecode] + " spotted",
            None,
            "",
            f"Type {typecode} is on the special-airframe watchlist.",
            "This curated list includes historic aircraft, unusual transports, and distinctive designs. A type code is metadata, not visual confirmation or a global rarity ranking.",
            82,
            "Type database",
        )
    year = number(a.get("year"))
    current_year = datetime.fromtimestamp(now, timezone.utc).year
    # A year before the type could have been built is a metadata conflict, not an old aircraft.
    if year is not None and 1900 <= year <= current_year - 50 and checks.get("year_plausible") is not False:
        age = current_year - int(year)
        add(
            "vintage",
            "airframes",
            "Still flying after half a century",
            age,
            "years",
            f"The database lists year {int(year)}: approximately {age} years ago.",
            "Age comes from the aircraft database's year field. Registry data can be incomplete or mistaken; this is a lead worth checking.",
            72,
            "Database year",
        )
    category = a.get("category")
    if category in EMITTERS:
        add(
            "unusual_emitter",
            "airframes",
            EMITTERS[category] + " on the radar",
            None,
            "",
            f"The transmitter declares emitter category {category} ({EMITTERS[category].lower()}).",
            "Emitter categories are configured aboard the aircraft. Incorrect configurations occur, so confirm against the type and track.",
            67,
            "Transmitted category",
        )
    if category == "B7":
        add(
            "space_category",
            "oddities",
            "A spacecraft category? Check that.",
            None,
            "",
            "The transmitter declares category B7, the space / trans-atmospheric category.",
            "A category field alone is not evidence of a spacecraft. A misconfigured transmitter or bad data is a plausible explanation.",
            90,
            "Category anomaly",
        )
    if rarity and rarity["ready"] and typecode and not _non_icao(a) and not group_designator(typecode):
        count, total = rarity["counts"].get(typecode, 0), rarity["total"]
        type_days = rarity.get("type_days", {}).get(typecode, 0)
        aircraft_days = rarity.get("aircraft_days", {}).get(a["hex"], 0)
        if (
            0 < count <= RARE_TYPE_ADDRESSES
            and count / max(1, rarity.get("known_total", total)) <= 0.01
            and type_days <= 3
            and aircraft_days <= 2
        ):
            add(
                "rare_here",
                "airframes",
                f"A rare visitor here: {typecode}",
                count,
                "IDs / 30 days",
                f"Only {count} of {total:,} aircraft IDs in this station's last 30 days have type {typecode}.",
                "Local rarity depends on this receiver's coverage and traffic. It says nothing about how many of this type exist worldwide.",
                70,
                "Local 30-day history",
                {"rarity": {"type_days": type_days, "aircraft_days": aircraft_days, "type_addresses": count}},
            )
    squawk = str(a.get("squawk", "")).zfill(4)
    emergency = a.get("emergency", "none")
    if squawk in SQUAWKS or emergency in STATUS_NAMES:
        meaning = SQUAWKS.get(squawk, str(emergency))
        status_key = squawk if squawk in SQUAWKS else str(emergency)
        add(
            "special_squawk_" + status_key,
            "signals",
            "Special transponder status",
            None,
            "",
            f"Reported squawk {squawk if squawk.strip('0') else 'unavailable'}; priority {emergency or 'unavailable'} ({meaning}).",
            "A transmitted code is not confirmation of an incident. Training, testing, transient selection, or a decoding issue can also explain it.",
            95,
            "Repeated code reports",
        )
    if position(a) and a.get("alt_baro") != "ground":
        findings += _performance_candidates(a, checks, profiles, band)
    return _annotate(findings, checks, a)


def _performance_candidates(a, checks, profiles, band=None):
    """Speed, altitude, rate and data-consistency findings from one fresh airborne report."""
    findings = []

    def add(*args, **kwargs):
        return findings.append(Finding(*args, **kwargs))

    typecode = a.get("t", "")
    gs, alt, rate = (number(a.get(x)) for x in ("gs", "alt_baro", "baro_rate"))
    conflict = checks.get("conflict") is True
    # An identity conflict means the listed type may be wrong: default thresholds apply.
    speed_threshold, altitude_threshold = performance_thresholds(a, None if conflict else profiles)
    profile_evidence = {
        "performance_profile": {
            "ground_speed_threshold_kn": speed_threshold,
            "altitude_threshold_ft": altitude_threshold,
            "type": typecode,
            "baseline": {} if conflict else (profiles or {}).get(typecode, {}),
            "scope": "Local spotting thresholds: encounter medians by phase and altitude band when available, otherwise per-address peaks; not aircraft operating limits",
        }
    }
    if conflict:
        profile_evidence["performance_profile"]["scope"] += (
            ". The identity checks disagree, so default thresholds apply"
        )
    rotor = rotorcraft(a)
    envelope = type_envelope(a, rotor=rotor)
    speed_cap = (envelope or {}).get("ground_speed_cap_kt")
    altitude_cap = (envelope or {}).get("altitude_cap_ft")
    rated, implausible = ([], {}) if conflict else _rated_candidates(a, gs, alt, rotor)
    # A certified limit outranks the envelope heuristic for the same reading: a
    # cited exceedance is not "a wrong type", and a reading 50% past the limit
    # is bad data for the data-quality path, never a performance finding. With
    # a conflicting identity the listed type, and so its limits, may be wrong,
    # so no rated finding fires.
    findings += rated
    rated_fields = {f.evidence["rated_limit"]["field"] for f in rated}
    if gs is not None and gs <= MAX_PLAUSIBLE_GS_KT:
        over_cap = speed_cap is not None and gs > speed_cap
        if over_cap and "airspeed" not in rated_fields:
            findings.append(_envelope_speed_mismatch(a, gs, envelope, rotor))
        elif "airspeed" in implausible:
            findings.append(implausible["airspeed"])
        # A speed past the envelope's cap is never an ordinary "fast" one, even while a
        # rated candidate takes the reading instead of the mismatch.
        elif not rotor and speed_threshold <= gs and not over_cap:
            add(
                "fast",
                "performance",
                "Unusual ground speed",
                gs,
                "kn",
                f"Reported ground speed reached {gs:,.1f} knots ({gs * 1.150779:,.0f} mph).",
                "Ground speed includes wind. A big number does not establish supersonic airspeed; strong tailwinds can make a normal jet look astonishingly fast.",
                70,
                evidence=profile_evidence,
            )
    if alt is not None and alt <= MAX_PLAUSIBLE_ALT_FT:
        if altitude_cap is not None and alt > altitude_cap and "alt_baro" not in rated_fields:
            findings.append(_envelope_altitude_mismatch(a, alt, envelope))
        elif "alt_baro" in implausible:
            findings.append(implausible["alt_baro"])
        elif altitude_threshold <= alt and "alt_baro" not in rated_fields:
            add(
                "high",
                "performance",
                "Above the usual traffic",
                alt,
                "ft",
                f"Reported barometric altitude reached {alt:,.0f} feet.",
                "Some business jets and specialized aircraft operate this high. This is pressure altitude, not terrain clearance.",
                70,
                evidence=profile_evidence,
            )
    if rate is not None and 4500 <= abs(rate) <= 12000:
        add(
            "vertical",
            "performance",
            "Taking the express elevator",
            abs(rate),
            "ft/min",
            f"Reported barometric {'climb' if rate > 0 else 'descent'} rate: {abs(rate):,.0f} feet per minute.",
            "A steep climb or descent can be intentional. Rate reports can also be noisy; this is not a claim of distress.",
            62,
        )
    if band is not None and 200 <= band < 600:
        add(
            "distant",
            "performance",
            "Way out on the horizon",
            band,
            "nm band",
            f"A position was reported between {band:,.0f} and {band + RANGE_BAND_NM:,.0f} nautical miles "
            f"from the receiver. Only this {RANGE_BAND_NM}-nm band is saved.",
            "High altitude helps line-of-sight range. This map merges ADS-B, UAT, and MLAT, so distance is not proof of a direct local radio reception record.",
            53,
        )
    geom = number(a.get("alt_geom"))
    if alt is not None and geom is not None:
        gap = geom - alt
        # Temperature makes geometric and pressure altitude diverge more at height.
        # A difference beyond 5,000 ft is treated as bad data, not as this finding.
        threshold = 3000 if alt < CLASS_A_FLOOR_FT else 4000
        if threshold <= abs(gap) <= 5000:
            add(
                "altitude_gap",
                "oddities",
                "Two altitudes, one aircraft",
                abs(gap),
                "ft apart",
                f"Geometric altitude {geom:,.0f} ft versus barometric altitude {alt:,.0f} ft.",
                "These use different references, so this is a data inconsistency to review. Weather and temperature can cause a real gap; stale fields or measurement errors are other possibilities. This is not a position-jump claim.",
                58,
                evidence={"altitude_gap": {"geometric_minus_barometric_ft": gap, "threshold_ft": threshold}},
            )
    tas = number(a.get("tas"))
    if gs is not None and tas is not None and 100 <= tas <= 700 and 100 <= gs - tas <= 300:
        add(
            "wind_boost",
            "performance",
            "A serious push from the wind",
            gs - tas,
            "kn difference",
            f"Ground speed {gs:,.0f} kn exceeds reported true airspeed {tas:,.0f} kn by {gs - tas:,.0f} kn.",
            "This is a speed difference, not a measured wind vector. Direction and the freshness of both reports matter.",
            64,
        )
    unusual = [
        ("gs", gs, lambda x: x > MAX_PLAUSIBLE_GS_KT, "kn"),
        ("alt_baro", alt, lambda x: x > MAX_PLAUSIBLE_ALT_FT or x < -1500, "ft"),
        ("baro_rate", rate, lambda x: abs(x) > 12000, "ft/min"),
    ]
    for field, value, test, unit in unusual:
        if value is not None and test(value):
            # The value keeps its sign; magnitude is only a ranking aid.
            add(
                "outlier_" + field,
                "oddities",
                "That number deserves a second look",
                value,
                unit,
                f"The receiver reported {field} = {value:,.1f} {unit}, outside this dashboard's normal spotting band.",
                "This threshold is a heuristic. Check decoding, source changes, and stale data before treating it as a real flight capability.",
                86,
                "Repeated unusual value",
                {"outlier": {"field": field, "magnitude": abs(value)}},
            )
    return findings


ENVELOPE_CLASS_LABELS = {
    "H": "helicopters",
    "G": "gyroplanes",
    "R": "tiltrotors",
    "P1": "single-engine piston aircraft",
    "P2": "twin-engine piston aircraft",
    "P4": "piston aircraft with three or more engines",
    "T1": "single-engine turboprops",
    "T2": "twin-engine turboprops",
    "T4": "turboprops with three or more engines",
    "E": "electric aircraft",
    "J1": "single-engine jets",
    "J2": "twin-engine jets",
    "J3": "three-engine jets",
    "J4": "jets with four or more engines",
}


def _envelope_subject(envelope, typecode):
    basis = str(envelope.get("basis") or "")
    if basis.startswith("class:") and basis[6:] in ENVELOPE_CLASS_LABELS:
        return f"{ENVELOPE_CLASS_LABELS[basis[6:]]}, the class of {typecode}"
    if basis == "group":
        return f"the {typecode} category"
    return f"type {typecode}"


def _band_text(band):
    """Wording for a wind-allowance altitude band ([floor, ceiling or None]), or unknown."""
    if not isinstance(band, list) or len(band) != 2:
        return "for an unknown altitude"
    floor, ceiling = band
    if ceiling is None:
        return f"at or above {floor:,.0f} ft"
    return f"below {ceiling:,.0f} ft" if floor is None else f"between {floor:,.0f} and {ceiling:,.0f} ft"


def _speed_mismatch(value, bound, rotor, envelope, reading="ground speed"):
    """type_speed_mismatch: a reported speed past `bound`, the bound's own wording, with the
    envelope (or certified-limit bound) behind it as evidence."""
    explanation = (
        "The listed type is a rotorcraft, which cannot sustain this ground speed. A decoding or multilateration error, or a wrong type in the aircraft database, is more likely. This is a review threshold, not an aircraft speed limit."
        if rotor
        else "The aircraft database's type, or the reading itself, is probably wrong. This bound is a spotting heuristic, not an operating limit; exceptional jet-stream winds can also exceed the allowance. It is not evidence of the aircraft's real performance."
    )
    return Finding(
        "type_speed_mismatch",
        "oddities",
        "Too fast for the listed rotorcraft" if rotor else "Too fast for the listed type",
        value,
        "kn",
        f"Reported {reading} {value:,.1f} kn exceeds {bound}.",
        explanation,
        74,
        "Type envelope check",
        {"envelope": {**envelope, "rotorcraft": rotor, "field": "gs"}},
    )


def _altitude_mismatch(a, alt, bound, envelope):
    """type_altitude_mismatch: a reported altitude past `bound`, the bound's own wording."""
    # Archived altitudes do not always say whether they are barometric.
    archived = "archive_alt" in a
    explanation = (
        "The archive does not record whether this altitude was barometric or geometric. Either the reading or the aircraft database's type is probably wrong; this bound is a spotting heuristic, not a performance claim."
        if archived
        else "Either the altitude report or the aircraft database's type is probably wrong. Pressure altitude is not height above terrain, and this bound is a spotting heuristic, not a performance claim."
    )
    return Finding(
        "type_altitude_mismatch",
        "oddities",
        "Higher than the listed type's ceiling",
        alt,
        "ft",
        f"Reported {'' if archived else 'barometric '}altitude {alt:,.0f} ft exceeds {bound}.",
        explanation,
        74,
        "Type envelope check",
        {"envelope": {**envelope, "field": "alt_baro"}},
    )


def _type_name(a):
    """The listed type code as the mismatch wording names it."""
    return _text(a.get("t")) or "unknown"


def _envelope_speed_mismatch(a, gs, envelope, rotor):
    """A ground speed past the listed type's envelope cap (or the rotorcraft review threshold)."""
    cap = envelope["ground_speed_cap_kt"]
    if envelope["basis"] == "fallback:rotorcraft":
        bound = f"a {cap:,.0f}-kn review threshold for rotorcraft"
    else:
        bound = (
            f"a conservative {cap:,.0f}-kn bound for {_envelope_subject(envelope, _type_name(a))} "
            f"(a rounded-up speed bound plus a {envelope['wind_allowance_kt']:,.0f}-kn wind allowance "
            f"{_band_text(envelope.get('wind_band_ft'))})"
        )
    return _speed_mismatch(gs, bound, rotor, envelope)


def _envelope_altitude_mismatch(a, alt, envelope):
    """An altitude past the listed type's envelope ceiling plus its margin."""
    bound = (
        f"a conservative {envelope['altitude_cap_ft']:,.0f}-ft bound for "
        f"{_envelope_subject(envelope, _type_name(a))} "
        f"(a rounded-up ceiling plus a {envelope['altitude_margin_ft']:,.0f}-ft margin)"
    )
    return _altitude_mismatch(a, alt, bound, envelope)


# What a certified-limit comparison is not; the public card states the same.
RATED_SCOPE = (
    "The limit is transcribed from the type's certificate data sheet. This compares received reports "
    "with that figure; it does not establish a violation, an emergency, a cause, or who was flying."
)


def _rated_candidates(a, gs, alt, rotor):
    """above_rated_altitude / above_rated_speed against a type's certified limits.

    Only types with a limits entry qualify. An altitude 1,000 ft or more above
    the maximum operating altitude is "exceeded". A ground speed above 110% of
    the airspeed limit's true-airspeed equivalent, with no wind assumed, is a
    candidate whose verdict waits for the wind enrichment, unless the aircraft
    reports its own true airspeed. Returns (findings, implausible): the second
    maps a field ("alt_baro", "airspeed") read at 1.5 times its limit or more
    to its type mismatch, so the caller reports bad data instead of a
    performance finding.
    """
    entry = reference.limits(_text(a.get("t")))
    if entry is None:
        return [], {}
    findings, implausible = [], {}
    if alt is not None and alt <= MAX_PLAUSIBLE_ALT_FT:
        verdict = limits.altitude_verdict(entry, alt)
        if verdict == "exceeded":
            findings.append(_rated_altitude(a, alt, entry))
        elif verdict == "implausible":
            implausible["alt_baro"] = _rated_altitude_mismatch(a, alt, entry)
    if gs is None or gs > MAX_PLAUSIBLE_GS_KT or alt is None:
        return findings, implausible
    bound = limits.binding_limit(entry, alt)
    if bound is None:
        return findings, implausible
    limiting, binding = bound
    tas = number(a.get("tas"))
    reported = tas is not None and REPORTED_TAS_KT[0] <= tas <= REPORTED_TAS_KT[1]
    reading = tas if reported else gs
    verdict = limits.ratio_verdict(reading / limiting)
    if verdict == "exceeded":
        findings.append(_rated_speed(a, gs, alt, entry, tas if reported else None))
    elif verdict == "implausible":
        implausible["airspeed"] = _rated_speed_mismatch(a, reading, reported, entry, bound, rotor)
    return findings, implausible


def _rated_bound(entry, limit_name):
    """The evidence behind a mismatch bound at 1.5 times a certified limit."""
    return {
        "type": entry["type"],
        "basis": "rated-limit",
        "limit_name": limit_name,
        "source": limits.source_for(entry, limit_name),
        "dataset_version": entry["dataset_version"],
    }


def _rated_altitude_mismatch(a, alt, entry):
    """An altitude 1.5 times the certified ceiling or more: bad data or a wrong type."""
    cap = round(entry["max_operating_altitude_ft"] * limits.IMPLAUSIBLE_RATIO)
    bound = f"{cap:,.0f} ft, 1.5 times the certified maximum operating altitude of type {_type_name(a)}"
    return _altitude_mismatch(
        a, alt, bound, {**_rated_bound(entry, "max_operating_altitude_ft"), "altitude_cap_ft": cap}
    )


def _rated_speed_mismatch(a, reading, reported, entry, bound, rotor):
    """An airspeed 1.5 times the limiting true airspeed or more: bad data or a wrong type,
    whatever the wind, so the bound carries no wind allowance. ``bound`` is
    limits.binding_limit(): the limiting true airspeed and the limit that binds."""
    limiting, binding = bound
    cap = round(limiting * limits.IMPLAUSIBLE_RATIO)
    bound = (
        f"{cap:,.0f} kn, 1.5 times the true airspeed at which type {_type_name(a)} reaches its "
        "certified airspeed limit at this altitude (no wind allowance applies this far past a certificate)"
    )
    evidence = {
        **_rated_bound(entry, binding),
        "ground_speed_cap_kt": cap,
        "reading_field": "tas" if reported else "gs",
    }
    return _speed_mismatch(reading, bound, rotor, evidence, "true airspeed" if reported else "ground speed")


def _rated_altitude(a, alt, entry):
    limit = entry["max_operating_altitude_ft"]
    return Finding(
        "above_rated_altitude",
        "performance",
        "Above the type's certified maximum altitude",
        alt,
        "ft",
        f"Reported barometric altitude {alt:,.0f} ft is {alt - limit:,.0f} ft above the {limit:,.0f}-ft {limits.LIMITS['max_operating_altitude_ft'].name} certified for {entry['name']} ({entry['type']}).",
        "Pressure altitude from ADS-B is not a verified flight level, and a temporary excursion or a non-standard altimeter setting can read above a limit. "
        + RATED_SCOPE,
        76,
        "Type certificate limit comparison",
        {
            "rated_limit": {
                "field": "alt_baro",
                "limit_name": "max_operating_altitude_ft",
                "limit": limit,
                "unit": "ft",
                "reading": alt,
                "margin_percent": round((alt - limit) / limit * 100, 1),
                "verdict": "exceeded",
                "significance": f"At least {limits.EXCEEDED_ALTITUDE_FT:,} ft above the certified maximum, sustained",
                "type": entry["type"],
                "source": limits.source_for(entry, "max_operating_altitude_ft"),
                "basis": entry["basis"],
                "dataset_version": entry["dataset_version"],
            }
        },
    )


def _rated_speed(a, gs, alt, entry, tas):
    """A rated-speed candidate; ``tas`` is the aircraft's own report at the peak, or None.

    The verdict is always "pending": the wind enrichment (wind.py) corrects
    every report of the confirmation window, from its reported true airspeed
    when the aircraft sends one, otherwise from ground speed, track and the
    modelled wind, and settles the verdict over the qualifying interval.
    """
    field, limit = limits.speed_limit(entry) or ("mmo", None)
    limiting = limits.limiting_tas_kt(entry, alt)
    # The reading that made the candidate: the aircraft's own true airspeed, else ground speed.
    reading = gs if tas is None else tas
    block = {
        "field": "airspeed",
        "limit_name": field,
        "limit": limit,
        "unit": "KIAS",
        "mmo": entry.get("mmo"),
        "reading": reading,
        "reading_field": "gs" if tas is None else "tas",
        "margin_percent": round((reading - limiting) / limiting * 100, 1),
        "verdict": "pending",
        "significance": f"At least {round((limits.EXCEEDED_SPEED_RATIO - 1) * 100):d}% over the airspeed limit, sustained",
        "type": entry["type"],
        # The block cites the sheet of the limit it names; Mmo shares the airspeeds' sheet.
        "source": limits.source_for(entry, field),
        "basis": entry["basis"],
        "dataset_version": entry["dataset_version"],
    }
    stated = [f"{limits.LIMITS[field].symbol} {limit:,.0f} {limits.LIMITS[field].unit}"] if limit else []
    if entry.get("mmo"):
        stated.append(f"{limits.LIMITS['mmo'].symbol} {entry['mmo']:.2f}")
    stated = " / ".join(stated)
    if tas is None:
        why = (
            f"Reported ground speed {gs:,.1f} kn exceeds {limiting:,.0f} kn, the true airspeed at which "
            f"{entry['name']} ({entry['type']}) reaches its {stated} limit at {alt:,.0f} ft, by {block['margin_percent']:.0f}% with no wind assumed."
        )
        explanation = (
            "Ground speed includes wind, so a tailwind can explain the whole margin. The verdict waits for the modelled wind at this altitude, applied to every report of the window. "
            + RATED_SCOPE
        )
    else:
        why = (
            f"Reported true airspeed {tas:,.0f} kn at {alt:,.0f} ft is {block['margin_percent']:.0f}% above {limiting:,.0f} kn, the true "
            f"airspeed at which {entry['name']} ({entry['type']}) reaches its {stated} limit there; "
            f"ground speed was {gs:,.1f} kn."
        )
        explanation = (
            "The aircraft's own true-airspeed reports are compared with the limit through the standard atmosphere; the verdict waits for every report of the window to be checked. "
            + RATED_SCOPE
        )
    return Finding(
        "above_rated_speed",
        "performance",
        "Faster than the type's rated airspeed limit (verdict pending)",
        gs,
        "kn",
        why,
        explanation,
        76,
        "Type certificate limit comparison",
        {"rated_limit": block},
    )


def _conflict_reasons(checks):
    """Readable reasons for an identity conflict; tolerates partial precomputed checks."""
    reasons = []
    if checks.get("registration_matches_address") is False:
        derived = checks.get("us_registration_from_address")
        reasons.append(
            f"The US address algorithm assigns this address to {derived}, but the aircraft database lists a different registration."
            if derived
            else "The aircraft database lists a US registration that the US address algorithm assigns to a different address."
        )
    callsign = checks.get("callsign_registration")
    if checks.get("callsign_matches_registration") is False and callsign:
        reasons.append(
            f"The broadcast flight ID {callsign} looks like a registration that differs from this aircraft's registration."
        )
    allocated, listed = checks.get("allocation_country"), checks.get("registration_country")
    if checks.get("allocation_matches_registration") is False and allocated and listed:
        allocated = reference.country_name(allocated) or allocated
        listed = reference.country_name(listed) or listed
        reasons.append(
            f"The address is in the {allocated} allocation block, but the database registration uses another State's nationality mark ({listed})."
        )
    return reasons or ["The identity checks disagree."]


def emitter_mismatch(a):
    """Describe a declared emitter category that contradicts the listed type, or None."""
    category = a.get("category")
    typecode = _text(a.get("t"))
    info = reference.type_info(typecode) if typecode else None
    if not isinstance(category, str) or category not in CATEGORY_LABELS or info is None:
        return None
    kind = info["class"]
    if category == "A7" and kind in ("airplane", "glider", "balloon", "airship", "ultralight"):
        return f"the rotorcraft category on a fixed-wing or lighter-than-air type ({typecode})"
    if category in CATEGORY_WAKE_CONFLICTS:
        if kind == "helicopter":
            return None
        if info["wake_category"] in CATEGORY_WAKE_CONFLICTS[category]:
            return (
                f"the {CATEGORY_LABELS[category]} category on {typecode}, "
                f"whose wake category is {WAKE_LABELS[info['wake_category']]}"
            )
    if (
        category == "B1"
        and kind != "glider"
        and (info["engine_type"] in ("J", "T") or (info["engine_count"] or 0) >= 2)
    ):
        return f"the glider category on a powered type ({typecode})"
    if category == "B2" and kind not in ("balloon", "airship"):
        return f"the lighter-than-air category on {typecode}"
    if category == "B4" and info["wake_category"] in ("M", "H", "J"):
        return f"the ultralight category on {typecode}"
    if category == "B6" and kind != "uav":
        return f"the unmanned-aircraft category on a crewed type ({typecode})"
    return None


def notable_variant(aircraft, *, identity=None):
    """A curated military/government variant, matched by exact database description.

    The finding carries the identity checks, and identity_unverified when nothing
    corroborates the record, like every other identity claim.
    """
    entry = reference.notable_variant(aircraft.get("desc"))
    if entry is None or _non_icao(aircraft):
        return None
    label, family = entry["label"], entry["family"]
    registration = _text(aircraft.get("r"))
    military_flag, serial = bool(_db_flags(aircraft) & 1), bool(MILITARY_SERIAL.fullmatch(registration))
    if not (military_flag or serial):
        return None
    callsign = _callsign(aircraft)
    mission = bool(callsign) and reference.mission_callsign(callsign) is True
    if entry.get("requires_mission_callsign") and not mission:
        return None
    description = " ".join(str(aircraft.get("desc")).split())
    explanation = "This is aircraft-database metadata, not visual confirmation. It does not establish the mission, the crew, or anyone aboard."
    if isinstance(entry.get("note"), str):
        explanation += " " + entry["note"]
    elif mission:
        explanation += " The flight ID uses a VIP-mission call-sign prefix, but a flight ID can be set by hand and does not confirm who is aboard."
    finding = Finding(
        "notable_variant",
        "airframes",
        f"{label} spotted",
        None,
        "",
        f"The aircraft database describes this address as {description} ({label}).",
        explanation,
        80,
        "Database description",
        {
            "variant": {
                "label": label,
                "family": family,
                "description": description,
                "corroboration": "military_database_flag" if military_flag else "military_serial",
                "mission_callsign": mission,
                "dataset_version": entry.get("dataset_version"),
            }
        },
    )
    checks = identity if isinstance(identity, dict) else identity_checks(aircraft)
    return _annotate([finding], checks, aircraft)[0]


def supplementary_candidates(a, now, *, identity=None):
    """Per-report identity and configuration findings for the collector to wire.

    These stay outside candidates() so archive replays adopt them deliberately.
    """
    checks = identity if isinstance(identity, dict) else identity_checks(a)
    findings = []
    if checks.get("conflict") is True:
        reasons = _conflict_reasons(checks)
        findings.append(
            Finding(
                "identity_conflict",
                "oddities",
                "Identity details disagree",
                None,
                "",
                " ".join(reasons),
                "The transmitting address, the aircraft database's registration and a registration-style flight ID normally agree. A re-registered aircraft that kept its old address, a copied or mistyped setting, or a database error can explain this. It does not establish who operates the aircraft.",
                72,
                "Deterministic identity checks",
                {"identity_checks": dict(checks), "context_tags": ["metadata_identity_conflict"]},
            )
        )
    if description := emitter_mismatch(a):
        findings.append(
            Finding(
                "emitter_type_mismatch",
                "oddities",
                "Declared category does not fit the listed type",
                None,
                "",
                f"The transmitter declares {description}.",
                "Emitter categories are set when a transmitter is installed and are often left wrong. Either that setting or the aircraft database's type may be mistaken; this is context, not a finding about the flight.",
                50,
                "Transmitted category versus type database",
                {"emitter": {"category": a.get("category"), "type": _text(a.get("t"))}},
            )
        )
    if variant := notable_variant(a, identity=checks):
        findings.append(variant)
    squawk = str(a.get("squawk", "")).zfill(4)
    altitude = number(a.get("alt_baro"))
    if squawk == "1200" and altitude is not None and altitude >= CLASS_A_FLOOR_FT:
        findings.append(
            Finding(
                "vfr_code_above_fl180",
                "signals",
                "VFR code at a flight level",
                altitude,
                "ft",
                f"Squawk 1200, the US VFR code, was reported at a pressure altitude of {altitude:,.0f} ft.",
                "In US and southern Canadian domestic airspace, flight at or above FL180 is normally IFR with an assigned code. A stale or mistaken code setting, a decoding issue, or a special authorization can explain it; this is context, not a finding about the flight.",
                45,
                "Transponder code versus altitude",
                {"beacon_code": reference.beacon_code("1200")},
            )
        )
    return _annotate(findings, checks, a)


def first_sighting(aircraft, history):
    """First retained sighting of a type or an address-allocation State.

    history holds counts from the daily and coverage tables, all for UTC days
    before the current one:
      coverage_days    days with at least one hour of coverage
      first_day        ISO date of the earliest such day (optional)
      type_days        days on which any non-~ address reported this type
      other_type_days  days on which this address reported another, non-empty type
      country_days     days on which any address from this allocation State
                       appeared (None when not computed)
    Records describe this station's retained history, never global rarity.
    """
    address = str(aircraft.get("hex", "")).strip().lower()
    if not ICAO_ADDRESS.fullmatch(address) or not isinstance(history, dict):
        return []
    coverage = number(history.get("coverage_days")) or 0
    since = history.get("first_day") if isinstance(history.get("first_day"), str) else None
    since_text = f" since {since}" if since else ""
    other_days = number(history.get("other_type_days"))
    checks = identity_checks(aircraft)
    findings = []
    typecode = _text(aircraft.get("t"))
    known = reference.type_info(typecode) is not None or reference.dataset("aircraft-types") is None
    if (
        TYPE_DESIGNATOR.fullmatch(typecode)
        and not group_designator(typecode)
        and known
        and coverage >= FIRST_TYPE_COVERAGE_DAYS
        and number(history.get("type_days")) == 0
        and other_days is not None
        and other_days <= FIRST_TYPE_MAX_OTHER_TYPE_DAYS
    ):
        findings.append(
            Finding(
                "first_type_here",
                "airframes",
                f"First {typecode} in this station's records",
                coverage,
                "coverage days",
                f"No address reported type {typecode} on any of the {coverage:,.0f} earlier days with at least an hour of coverage in this station's retained records{since_text}.",
                "This describes this receiver's retained records, not rarity elsewhere. The type comes from the aircraft database and can be wrong or newly assigned.",
                76,
                "Local retained history",
                {
                    "first_sighting": {
                        "scope": "type",
                        "type": typecode,
                        "coverage_days": coverage,
                        "since": since,
                        "other_type_days": other_days,
                    }
                },
            )
        )
    country = checks["allocation_country"]
    name = reference.country_name(country) if country else None
    if (
        country
        and name
        and coverage >= FIRST_COUNTRY_COVERAGE_DAYS
        and number(history.get("country_days")) == 0
        and checks["allocation_matches_registration"] is not False
    ):
        findings.append(
            Finding(
                "first_country_here",
                "airframes",
                f"First address from the {name} allocation block here",
                coverage,
                "coverage days",
                f"No address from the {name} allocation block appeared on any of the {coverage:,.0f} earlier days with at least an hour of coverage in this station's retained records{since_text}.",
                "Address blocks are allocated to the State of registry, but a re-registered aircraft can keep an old address and the aircraft database can be wrong. This describes this station's records only.",
                70,
                "Local retained history",
                {
                    "first_sighting": {
                        "scope": "allocation_country",
                        "country": country,
                        "coverage_days": coverage,
                        "since": since,
                    }
                },
            )
        )
    return _annotate(findings, checks, aircraft)


def season_start(stamp):
    """(ISO date, name) of the UTC meteorological season containing stamp."""
    moment = datetime.fromtimestamp(stamp, timezone.utc)
    for month, name in SEASON_MONTHS:
        if moment.month >= month:
            return f"{moment.year:04d}-{month:02d}-01", name
    return f"{moment.year - 1:04d}-12-01", "winter"


def seasonal_speed_record(aircraft, season_max, *, sustained=None, identity=None):
    """Fastest sustained direct ADS-B ground speed for a type over this season's baseline.

    season_max: {"type", "season_start", "max_gs", "addresses"} from earlier
    direct, quality-filtered reports of this type in the same season. Optional
    "baseline_start" is the ISO date the compared reports begin (default
    "season_start"); the claim says "since" that date, never the season's start
    unless the baseline reaches it. "baseline_days" is how many days the
    baseline covers, from its first whole UTC day to the window's end; a record
    needs SEASON_MIN_BASELINE_DAYS of them, and a missing count is a refusal.
    sustained: {"gs" (the lowest speed in the window), "duration_seconds",
    "samples", "source"} from the caller's confirmation window. The caller has
    already refused a speed beyond the listed type's envelope or rated limit
    (speed_plausible), which must not join the season's maxima either.
    """
    from .telemetry import DIRECT

    if not isinstance(season_max, dict) or not isinstance(sustained, dict):
        return None
    typecode = _text(aircraft.get("t"))
    checks = identity if isinstance(identity, dict) else identity_checks(aircraft)
    gs, previous = number(sustained.get("gs")), number(season_max.get("max_gs"))
    duration, samples = number(sustained.get("duration_seconds")), number(sustained.get("samples"))
    addresses, baseline_days = number(season_max.get("addresses")), number(season_max.get("baseline_days"))
    if (
        not typecode
        or group_designator(typecode)
        or _non_icao(aircraft)
        or _solver_field(aircraft, "gs", ("mlat", "tisb"))
        or checks.get("conflict") is True
        or _text(season_max.get("type")) != typecode
        or sustained.get("source") not in DIRECT
        or (duration or 0) < SEASON_SUSTAINED_SECONDS
        or (samples or 0) < SEASON_MIN_SAMPLES
        or (addresses or 0) < SEASON_MIN_ADDRESSES
        or baseline_days is None
        or baseline_days < SEASON_MIN_BASELINE_DAYS
        or gs is None
        or previous is None
        or not previous < gs <= MAX_PLAUSIBLE_GS_KT
    ):
        return None
    start = season_max.get("season_start")
    baseline = season_max.get("baseline_start") or start
    record = {
        "type": typecode,
        "season_start": start,
        "baseline_start": baseline,
        "previous_max_gs": previous,
        "addresses": addresses,
        "sustained_seconds": duration,
        "samples": samples,
        "source": sustained["source"],
        "baseline_days": baseline_days,
    }
    finding = Finding(
        "seasonal_speed_record",
        "performance",
        f"Fastest {typecode} ground speed here since {baseline}",
        gs,
        "kn",
        f"Direct ADS-B reports stayed at or above {gs:,.0f} kn for {duration:.0f} seconds, above this station's previous {typecode} maximum of {previous:,.0f} kn since {baseline}.",
        "Ground speed includes wind, and a tailwind usually explains most of a record. This compares retained reports from this station only; it is not an airspeed or aircraft performance record.",
        76,
        "Sustained direct ADS-B reports",
        {"season_record": record},
    )
    return _annotate([finding], checks, aircraft)[0]


def jump(previous, a, now):
    if not previous or not position(a) or not position(previous["a"]):
        return None
    earlier, later = previous["now"] - previous["a"]["seen_pos"], now - a["seen_pos"]
    elapsed = later - earlier
    if not 2 <= elapsed <= 45:
        return None
    distance = distance_nm((previous["a"]["lat"], previous["a"]["lon"]), (a["lat"], a["lon"]))
    speed = distance * 3600 / elapsed
    if distance < 12 or speed < 1200:
        return None
    return Finding(
        "position_jump",
        "oddities",
        "That track just teleported",
        speed,
        "kn implied",
        f"Two fresh positions are {distance:,.1f} nm apart only {elapsed:.1f} seconds apart.",
        "This is a discontinuity in the data, not a measured speed. Bad positions, mixed sources, or address conflicts can cause it. One position pair is enough to record this anomaly.",
        92,
        "One position pair",
    )


PATTERN_WINDOW_SECONDS = 1200
PATTERN_MAX_GAP_SECONDS = 90
PATTERN_MIN_STEP_NM = 0.025
# Drift-corrected loops must look like circles: residuals relative to radius.
ORBIT_MAX_RELATIVE_RESIDUAL = 0.25
# Orbit-type tags need round loops: the 90th-percentile distance from a loop's fitted
# centre is at most this multiple of the 10th. Rounded-rectangle circuits reach 1.5;
# orbits with 0.02-0.05 NM of position noise stay below about 1.43.
ORBIT_TAG_ROUNDNESS = 1.45
ORBIT_MAX_DRIFT_KN = 60
# Continuous turning starts where accumulated turn last sat this close to its minimum.
ORBIT_ONSET_DEGREES = 10
# Ground-referenced orbits keep every loop centre within this distance of the others.
ORBIT_FIXED_CENTRE_NM = 0.2
ORBIT_MIN_WIND_KN = 8
ORBIT_TAG_LOOPS = 3


class IdentityCache:
    """``normalize(raw)`` of each buffered report, kept by identity across calls.

    It holds the reports it describes, so an identity is never reused while
    cached; prune() keeps only the reports still in the buffer. Callers treat
    the cached values as read-only.
    """

    def __init__(self, normalize):
        self.normalize, self.entries = normalize, {}

    def get(self, raw):
        entry = self.entries.get(id(raw))
        if entry is None or entry[0] is not raw:
            entry = self.entries[id(raw)] = (raw, self.normalize(raw))
        return entry[1]

    def prune(self, buffer):
        if len(self.entries) > 2 * len(buffer) + 16:
            current = {id(raw) for raw in buffer}
            self.entries = {key: entry for key, entry in self.entries.items() if key in current}


def segment_point(raw):
    """A track sample as movement patterns read it, or None where a segment must break."""
    values = {key: number(raw.get(key)) for key in ("ts", "lat", "lon")}
    if (
        any(value is None for value in values.values())
        or not -90 <= values["lat"] <= 90
        or not -180 <= values["lon"] <= 180
        or null_island(values["lat"], values["lon"])
        or raw.get("alt", raw.get("archive_alt")) == "ground"
        or raw.get("ground") is True
    ):
        return None
    return {
        **values,
        "gs": number(raw.get("gs")),
        "alt": number(raw.get("alt", raw.get("archive_alt"))),
        "timestamp_resolution_seconds": number(raw.get("timestamp_resolution_seconds")),
        "source": str(raw.get("source", raw.get("type", "unknown")) or "unknown").lower(),
        **{key: raw[key] for key in ("mlat", "tisb", "nic", "nac_p") if key in raw},
    }


def _recent_segment(samples, cache=None):
    """Keep the latest plausible segment; never join across an outage or a jump.

    Ground speed, altitude and track are optional. Compact replay data has no
    heading, and hovering/slow helicopters often omit it in live snapshots too.
    These bounds are conservative data-quality heuristics, not aircraft limits.
    ``cache`` (an IdentityCache of segment_point) reuses each sample's normalized form.
    """
    segment = []
    for raw in samples:
        point = segment_point(raw) if cache is None else cache.get(raw)
        if point is None:
            segment = []
            continue
        if segment:
            previous = segment[-1]
            elapsed = point["ts"] - previous["ts"]
            if elapsed == 0 and (point["lat"], point["lon"]) == (previous["lat"], previous["lon"]):
                continue
            # Reject an out-of-order/conflicting timestamp, including its link
            # to any preceding track. A subsequent clean segment can recover.
            if elapsed <= 0:
                segment = []
                continue
            speed = (
                distance_nm((previous["lat"], previous["lon"]), (point["lat"], point["lon"])) * 3600 / elapsed
            )
            reported = [p["gs"] for p in (previous, point) if p["gs"] is not None and 0 <= p["gs"] <= 650]
            speed_bound = min(700, max(250, 3 * max(reported) + 60)) if reported else 700
            if elapsed > PATTERN_MAX_GAP_SECONDS or speed > speed_bound:
                segment = []
        segment.append(point)
    if not segment:
        return []
    cutoff = segment[-1]["ts"] - PATTERN_WINDOW_SECONDS
    return [point for point in segment if point["ts"] >= cutoff]


def _geometry(samples):
    """Use position changes, never changing heading values at a fixed point."""
    if len(samples) < 10 or samples[-1]["ts"] - samples[0]["ts"] < 120:
        return None
    origin = samples[0]
    latitude_scale = math.cos(math.radians(origin["lat"]))
    points = []
    for sample in samples:
        # Wrap longitude differences so the dateline cannot create a huge box.
        xy = (
            ((sample["lon"] - origin["lon"] + 180) % 360 - 180) * 60 * latitude_scale,
            (sample["lat"] - origin["lat"]) * 60,
        )
        if not points or math.dist(xy, points[-1]["xy"]) >= PATTERN_MIN_STEP_NM:
            points.append({**sample, "xy": xy})
    if len(points) < 10:
        return None
    legs = [
        (b["xy"][0] - a["xy"][0], b["xy"][1] - a["xy"][1], b["ts"] - a["ts"])
        for a, b in zip(points, points[1:], strict=False)
    ]
    distances = [math.hypot(dx, dy) for dx, dy, _ in legs]
    speeds = [distance * 3600 / leg[2] for distance, leg in zip(distances, legs, strict=False)]
    speed = statistics.median(speeds)
    path = sum(distances)
    if not 12 <= speed <= 350 or path < 1:
        return None
    # Reject a parked/hovering transmitter whose noisy positions appear to move.
    reported = [p["gs"] for p in samples if p["gs"] is not None and p["gs"] >= 0]
    if reported and statistics.median(reported) < 8:
        return None
    headings = [math.degrees(math.atan2(dx, dy)) % 360 for dx, dy, _ in legs]
    turns = [(b - a + 180) % 360 - 180 for a, b in zip(headings, headings[1:], strict=False)]
    ratios = []
    for a, b, leg_speed in zip(points, points[1:], speeds, strict=False):
        pair = [p["gs"] for p in (a, b) if p["gs"] is not None and 8 <= p["gs"] <= 650]
        if pair:
            ratios.append(leg_speed / (sum(pair) / len(pair)))
    speed_ratio = statistics.median(ratios) if len(ratios) >= 5 else None
    sharp_fraction = sum(abs(turn) >= 120 for turn in turns) / len(turns)
    has_mlat = any(point["source"] == "mlat" for point in samples)
    uncertain = (speed_ratio is not None and not 0.45 <= speed_ratio <= 1.8) or (
        has_mlat and (sharp_fraction > 0.15 or (speed_ratio is not None and not 0.7 <= speed_ratio <= 1.4))
    )
    xs, ys = [p["xy"][0] for p in points], [p["xy"][1] for p in points]
    span = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
    net, total = abs(sum(turns)), sum(abs(turn) for turn in turns)
    return {
        "samples": samples,
        "points": points,
        "headings": headings,
        "turns": turns,
        "duration": samples[-1]["ts"] - samples[0]["ts"],
        "speed": speed,
        "path": path,
        "span": span,
        "net_turn": net,
        "consistency": net / max(1, total),
        "displacement": math.dist(points[0]["xy"], points[-1]["xy"]),
        "largest_gap": max(b["ts"] - a["ts"] for a, b in zip(samples, samples[1:], strict=False)),
        "reported_speed": statistics.median(reported) if reported else None,
        "speed_ratio": speed_ratio,
        "sharp_fraction": sharp_fraction,
        "geometry_uncertain": uncertain,
        "has_mlat": has_mlat,
    }


def _pattern_evidence(kind, geometry, **metrics):
    from .telemetry import quality

    samples = geometry["samples"]
    sources = sorted({point["source"] for point in samples})
    time_resolution = max((point["timestamp_resolution_seconds"] or 0 for point in samples), default=0)
    limitations = [
        "Position geometry describes movement, not the operator or flight's purpose.",
        "Coverage gaps and position errors can hide or distort the pattern.",
    ]
    if "mlat" in sources:
        limitations.append(
            "Includes MLAT positions derived from signal arrival-time differences; estimation errors can distort small loops."
        )
    if len(sources) > 1:
        limitations.append("More than one position source was used; source transitions need review.")
    if "unknown" in sources:
        limitations.append("Some position sources are unknown; review their reliability before publication.")
    if geometry["largest_gap"] > 45:
        limitations.append("Some positions are over 45 seconds apart; exact turn counts are uncertain.")
    if geometry["geometry_uncertain"]:
        limitations.append(
            "Sharp position changes or disagreement with reported ground speed make the exact track shape uncertain."
        )
    if time_resolution:
        limitations.append(
            f"Position timestamps have {time_resolution:g}-second resolution; implied speed comparisons are approximate and may reflect timing quantization."
        )
    count = min(96, len(samples))
    selected = [samples[round(index * (len(samples) - 1) / (count - 1))] for index in range(count)]
    summary = {
        "schema_version": 1,
        "kind": kind,
        "started_at": samples[0]["ts"],
        "ended_at": samples[-1]["ts"],
        "duration_seconds": geometry["duration"],
        "sample_count": len(samples),
        "movement_sample_count": len(geometry["points"]),
        "evidence_sample_count": count,
        "evidence_samples_downsampled": len(samples) > count,
        "largest_gap_seconds": round(geometry["largest_gap"], 2),
        "timestamp_resolution_seconds": time_resolution or None,
        "path_distance_nm": round(geometry["path"], 3),
        "net_displacement_nm": round(geometry["displacement"], 3),
        "footprint_span_nm": round(geometry["span"], 3),
        "median_position_speed_kn": round(geometry["speed"], 1),
        "median_reported_speed_kn": round(geometry["reported_speed"], 1)
        if geometry["reported_speed"] is not None
        else None,
        "position_reported_speed_ratio": round(geometry["speed_ratio"], 3)
        if geometry["speed_ratio"] is not None
        else None,
        "sharp_turn_fraction": round(geometry["sharp_fraction"], 3),
        "quality_review_required": (geometry["geometry_uncertain"] or bool(quality(samples, ("lat", "lon")))),
        "observed_sources": sources,
        "limitations": limitations,
        **metrics,
    }
    # Exact aircraft positions remain private evidence. They contain no receiver
    # coordinate, range or bearing and must still pass a public export policy.
    return {
        "pattern": summary,
        "pattern_samples": [
            {
                key: p[key]
                for key in ("ts", "lat", "lon", "source", "gs", "alt", "timestamp_resolution_seconds")
            }
            for p in selected
        ],
    }


def _pattern_confidence(evidence):
    return (
        "Position pattern; source/quality review needed"
        if evidence["pattern"]["quality_review_required"]
        else "Position-derived movement pattern"
    )


def _quality_duration(geometry):
    sources = {p["source"] for p in geometry["samples"]}
    return 300 if geometry["has_mlat"] or len(sources) > 1 or "unknown" in sources else 120


def _fixed_loops(geometry):
    """Count returns near a loop's start after substantial directional turning."""
    points, turns = geometry["points"], geometry["turns"]
    start, accumulated, loops = points[0]["xy"], 0, 0
    low_x = high_x = start[0]
    low_y = high_y = start[1]
    for index, turn in enumerate(turns):
        accumulated += turn
        xy = points[index + 2]["xy"]
        low_x, high_x = min(low_x, xy[0]), max(high_x, xy[0])
        low_y, high_y = min(low_y, xy[1]), max(high_y, xy[1])
        tolerance = min(0.5, max(0.04, math.hypot(high_x - low_x, high_y - low_y) * 0.12))
        if abs(accumulated) >= 300 and math.dist(start, xy) <= tolerance:
            loops += 1
            start, accumulated = xy, 0
            low_x = high_x = xy[0]
            low_y = high_y = xy[1]
    return loops


def _turning_start(turns, direction):
    """Leg index where continuous turning toward the first full circle begins.

    An entry leg or an opposite turn before an orbit must not become the phase
    reference: start at the last leg, before the first complete 360°, whose
    accumulated turn sat within ORBIT_ONSET_DEGREES of its running minimum.
    """
    accumulated = minimum = 0.0
    start = 0
    for index in range(len(turns) + 1):
        if index:
            accumulated += turns[index - 1] * direction
        minimum = min(minimum, accumulated)
        if accumulated - minimum >= 360:
            break
        if accumulated <= minimum + ORBIT_ONSET_DEGREES:
            start = index
    return start


def _loop_boundaries(geometry):
    """Phase-matched loop ends: where the dominant-direction signed turn completes each 360°.

    Headings belong to leg midpoints, so boundaries are interpolated between them.
    Returns (x, y, ts) tuples; the first starts the first loop. Because phases
    are taken at leg midpoints, the final loop of an orbit is counted only when
    turning continues past it: an orbit of exactly N turns, whether the track ends
    there or leaves on a straight exit leg, yields N - 1 drift-corrected loops.
    """
    points, turns = geometry["points"], geometry["turns"]
    direction = 1 if sum(turns) >= 0 else -1
    middles = [
        ((a["xy"][0] + b["xy"][0]) / 2, (a["xy"][1] + b["xy"][1]) / 2, (a["ts"] + b["ts"]) / 2)
        for a, b in pairwise(points)
    ]
    start = _turning_start(turns, direction)
    boundaries, accumulated, target = [middles[start]], 0.0, 360.0
    for index in range(start, len(turns)):
        before, accumulated = accumulated, accumulated + turns[index] * direction
        while accumulated >= target:
            fraction = (target - before) / (accumulated - before)
            a, b = middles[index], middles[index + 1]
            boundaries.append(tuple(a[k] + fraction * (b[k] - a[k]) for k in range(3)))
            target += 360
    return boundaries


def _spread(values):
    """Half the distance between the 10th and 90th percentiles of sorted values."""
    return (values[int(0.9 * (len(values) - 1))] - values[int(0.1 * (len(values) - 1))]) / 2


class _LoopFit(NamedTuple):
    """One drift-corrected loop's fitted circle: centre, radius, RMS residual,
    mid-loop time after the first boundary, period, and whether it is round."""

    x: float
    y: float
    radius: float
    residual: float
    mid_t: float
    period: float
    round: bool


def _fit_circle(xys):
    """Algebraic least-squares circle: (centre x, centre y, radius, RMS residual) or None."""
    count = len(xys)
    if count < 5:
        return None
    mean_x, mean_y = sum(x for x, _ in xys) / count, sum(y for _, y in xys) / count
    us, vs = [x - mean_x for x, _ in xys], [y - mean_y for _, y in xys]
    suu, svv = sum(u * u for u in us), sum(v * v for v in vs)
    suv = sum(u * v for u, v in zip(us, vs, strict=True))
    suuu = sum(u**3 for u in us)
    svvv = sum(v**3 for v in vs)
    suvv = sum(u * v * v for u, v in zip(us, vs, strict=True))
    svuu = sum(v * u * u for u, v in zip(us, vs, strict=True))
    determinant = suu * svv - suv * suv
    if abs(determinant) < 1e-12:
        return None
    uc = (svv * (suuu + suvv) - suv * (svvv + svuu)) / (2 * determinant)
    vc = (suu * (svvv + svuu) - suv * (suuu + suvv)) / (2 * determinant)
    radius = math.sqrt(uc * uc + vc * vc + (suu + svv) / count)
    centre = (uc + mean_x, vc + mean_y)
    residual = math.sqrt(sum((math.dist(p, centre) - radius) ** 2 for p in xys) / count)
    return centre[0], centre[1], radius, residual


def _orbit(geometry):
    """Drift-tolerant loop count and orbit-centre motion.

    Each 360° of signed turn is a candidate loop. The drift (wind) velocity is
    estimated from phase-matched loop ends. In a frame moving with it, a real
    orbit's loops fit one circle with a steady radius and a fixed centre, so
    each loop also closes on its start. An air-mass orbit's ground speed swings by about
    the drift around each loop, which separates it from turns joined by straight
    legs. At least two loops are needed to tell a drift from a growing radius;
    slowly tightening or widening circles can still close at a fixed start.
    """
    boundaries = _loop_boundaries(geometry)
    result = {
        "turn_loops": len(boundaries) - 1,
        "drift_loops": 0,
        "drift_kn": None,
        "centre_drift_nm": None,
        "centre_spread_nm": None,
        "round_loops": False,
    }
    if len(boundaries) < 3:
        return result
    times = [b[2] for b in boundaries]
    mean_t = statistics.fmean(times)
    spread = sum((t - mean_t) ** 2 for t in times)
    vx, vy = (
        sum((t - mean_t) * b[k] for t, b in zip(times, boundaries, strict=True)) / spread for k in range(2)
    )
    drift_kn = math.hypot(vx, vy) * 3600
    if drift_kn > min(ORBIT_MAX_DRIFT_KN, 0.8 * geometry["speed"]):
        return result
    points = [(p["xy"][0], p["xy"][1], p["ts"]) for p in geometry["points"]]
    within = [p for p in points if times[0] <= p[2] <= times[-1]]
    speeds = sorted(math.dist(a[:2], b[:2]) * 3600 / (b[2] - a[2]) for a, b in pairwise(within))
    swing = _spread(speeds) if speeds else 0
    if len(speeds) >= 4 and drift_kn >= ORBIT_MIN_WIND_KN and swing < 0.5 * drift_kn:
        # Route progress between separate turns, not an orbit carried by the wind.
        return result
    origin = times[0]

    def corrected(x, y, t):
        return x - vx * (t - origin), y - vy * (t - origin)

    fits = []
    for start, end in pairwise(boundaries):
        loop = [corrected(*p) for p in points if start[2] <= p[2] <= end[2]]
        fit = _fit_circle(loop)
        if fit is not None and 0.03 <= fit[2] <= 5 and fit[3] <= ORBIT_MAX_RELATIVE_RESIDUAL * fit[2]:
            distances = sorted(math.dist(p, fit[:2]) for p in loop)
            low, high = distances[int(0.1 * (len(distances) - 1))], distances[int(0.9 * (len(distances) - 1))]
            round_loop = high <= ORBIT_TAG_ROUNDNESS * max(low, 1e-9)
            fits.append(_LoopFit(*fit, (start[2] + end[2]) / 2 - origin, end[2] - start[2], round_loop))
    if len(fits) < 2:
        return result
    radii = [fit.radius for fit in fits]
    tolerance = max(0.08, 0.3 * statistics.fmean(radii))
    if max(radii) > 1.3 * min(radii) or any(
        math.dist((a.x, a.y), (b.x, b.y)) > tolerance for a, b in pairwise(fits)
    ):
        return result
    result.update(drift_loops=len(fits), drift_kn=round(drift_kn, 1))
    if len(fits) >= max(ORBIT_TAG_LOOPS, len(boundaries) - 2):
        # Ground positions of the fitted centres; only their motion is reported.
        located = [(fit.x + vx * fit.mid_t, fit.y + vy * fit.mid_t, fit.mid_t) for fit in fits]
        period = statistics.median(fit.period for fit in fits)
        result["centre_drift_nm"] = statistics.median(
            math.dist(a[:2], b[:2]) / max(1.0, (b[2] - a[2]) / period) for a, b in pairwise(located)
        )
        result["centre_spread_nm"] = max(math.dist(a[:2], b[:2]) for a in located for b in located)
        # One noisy loop does not make an orbit square.
        result["round_loops"] = sum(not fit.round for fit in fits) <= 1
    return result


def _orbit_tags(geometry, orbit):
    """Ground-referenced (all centres within 0.2 NM despite wind) or drifting orbit.

    Tags need at least three round loops, so runway circuits and other
    rectangular patterns are never tagged.
    """
    if orbit["centre_spread_nm"] is None or not orbit["round_loops"]:
        return []
    if orbit["centre_spread_nm"] > ORBIT_FIXED_CENTRE_NM:
        return ["drifting_orbit"]
    speeds = sorted(p["gs"] for p in geometry["points"] if p["gs"] is not None)
    if len(speeds) < 0.6 * len(geometry["points"]) or len(speeds) < 10:
        return []
    # Constant airspeed in wind W makes ground speed vary by about ±W around a loop.
    return ["ground_referenced_orbit"] if _spread(speeds) >= ORBIT_MIN_WIND_KN else []


# Each window detector returns (rank, build) or None. movement_patterns keeps the
# best-ranked window of each detector and builds evidence for that one only.


def _circle(geometry):
    turns = geometry["turns"]
    minimum = _quality_duration(geometry)
    if (
        geometry["geometry_uncertain"]
        or geometry["duration"] < minimum
        or geometry["net_turn"] < 450
        or geometry["consistency"] < 0.78
        or geometry["span"] > 20
        or any(abs(turn) >= 150 for turn in turns)
    ):
        return None
    orbit = _orbit(geometry)
    ground_loops = _fixed_loops(geometry)
    closed_loops = max(ground_loops, orbit["drift_loops"])
    if closed_loops < (2 if minimum == 300 else 1):
        return None
    rotations = geometry["net_turn"] / 360

    def build():
        metrics = {}
        if orbit["drift_loops"]:
            metrics["orbit_drift_kn"] = orbit["drift_kn"]
        if orbit["centre_drift_nm"] is not None:
            metrics["orbit_center_drift_nm"] = round(orbit["centre_drift_nm"], 3)
        evidence = _pattern_evidence(
            "circling",
            geometry,
            accumulated_turns=round(rotations, 3),
            direction_consistency=round(geometry["consistency"], 3),
            turn_direction="right" if sum(turns) > 0 else "left",
            closed_loops=closed_loops,
            ground_closed_loops=ground_loops,
            drift_corrected_loops=orbit["drift_loops"],
            turn_loops=orbit["turn_loops"],
            shape_verified=True,
            **metrics,
        )
        if tags := _orbit_tags(geometry, orbit):
            evidence["context_tags"] = tags
        return Finding(
            "circling",
            "patterns",
            "Going around again",
            rotations,
            "turns",
            f"Successive positions show about {rotations:.1f} turns in mostly one direction over {geometry['duration'] / 60:.1f} minutes.",
            "This is a circling or holding-like ground track. Training, surveying, and many other ordinary operations can produce it. It does not identify the operator, mission, or what is beneath the aircraft.",
            73,
            _pattern_confidence(evidence),
            evidence,
        )

    # The window with the most closed loops wins, then the most turning: a longer
    # window with more rotation can close fewer loops.
    return (closed_loops, rotations), build


def _repeated_passes(geometry):
    # Project onto the track's principal axis. Count complete alternating
    # crossings of its middle 60%; tiny heading reversals are not passes.
    if geometry["geometry_uncertain"] or geometry["duration"] < _quality_duration(geometry):
        return None
    points = geometry["points"]
    xs, ys = [p["xy"][0] for p in points], [p["xy"][1] for p in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    xx = sum((x - mx) ** 2 for x in xs)
    yy = sum((y - my) ** 2 for y in ys)
    xy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=False))
    angle = 0.5 * math.atan2(2 * xy, xx - yy)
    along = [(x - mx) * math.cos(angle) + (y - my) * math.sin(angle) for x, y in zip(xs, ys, strict=False)]
    across = [-(x - mx) * math.sin(angle) + (y - my) * math.cos(angle) for x, y in zip(xs, ys, strict=False)]
    low, high = min(along), max(along)
    length, width = high - low, max(across) - min(across)
    if not 1 <= length <= 12 or width > min(2, length * 0.4) or geometry["path"] < length * 2.4:
        return None
    previous_side, passes, start_index = 0, 0, 0
    traversals = []
    for index, value in enumerate(along):
        side = -1 if value <= low + 0.2 * length else 1 if value >= high - 0.2 * length else 0
        if side:
            if previous_side and previous_side != side:
                passes += 1
                traversals.append(across[start_index : index + 1])
            previous_side = side
            start_index = index
    if passes < 3:
        return None
    centers = [statistics.median(values) for values in traversals]
    if max(centers) - min(centers) > max(0.1, min(0.5, length * 0.1)):
        return None

    def build():
        evidence = _pattern_evidence(
            "repeated_passes",
            geometry,
            complete_passes=passes,
            corridor_length_nm=round(length, 3),
            corridor_width_nm=round(width, 3),
            corridor_center_spread_nm=round(max(centers) - min(centers), 3),
            shape_verified=True,
        )
        return Finding(
            "repeated_passes",
            "patterns",
            "Back over the same stretch",
            passes,
            "passes",
            f"Positions cross the same elongated area at least {passes} times in alternating directions over {geometry['duration'] / 60:.1f} minutes.",
            "This is a repeated-pass pattern, which can occur during training, surveying, approaches, or other routine flying. It is not evidence that the aircraft was searching, policing, or watching any person or place.",
            70,
            _pattern_confidence(evidence),
            evidence,
        )

    return passes, build


def _loitering(geometry):
    turns = geometry["turns"]
    smooth_fraction = sum(abs(turn) <= 75 for turn in turns) / len(turns)
    if (
        geometry["geometry_uncertain"]
        or geometry["duration"] < 480
        or len(geometry["points"]) < 20
        or not 0.2 <= geometry["span"] <= 3
        or geometry["path"] < 6
        or geometry["displacement"] / geometry["path"] > 0.3
        or geometry["speed"] > 160
        or smooth_fraction < 0.8
        or len({int(heading // 60) for heading in geometry["headings"]}) < 4
    ):
        return None

    def build():
        evidence = _pattern_evidence(
            "loitering",
            geometry,
            direction_consistency=round(geometry["consistency"], 3),
            smooth_turn_fraction=round(smooth_fraction, 3),
            shape_verified=True,
        )
        return Finding(
            "loitering",
            "patterns",
            "Staying in one small area",
            geometry["duration"] / 60,
            "minutes",
            f"Positions trace {geometry['path']:.1f} nautical miles of flight inside a compact area over {geometry['duration'] / 60:.1f} minutes, with little overall progress.",
            "Loitering here describes only a compact, changing ground track. The turns need not form neat circles, and the data cannot establish why the aircraft stayed in the area. Training and other routine operations are possible.",
            63,
            _pattern_confidence(evidence),
            evidence,
        )

    return geometry["duration"] / 60, build


def _confined_activity(geometry):
    """Keep a useful lead without converting jagged MLAT into invented loops."""
    if (
        not geometry["geometry_uncertain"]
        or geometry["duration"] < 480
        or len(geometry["points"]) < 20
        or not 0.2 <= geometry["span"] <= 3
        or geometry["path"] < 3
        or geometry["displacement"] / geometry["path"] > 0.3
        or geometry["speed"] > 160
    ):
        return None

    def build():
        return Finding(
            "confined_activity",
            "patterns",
            "Lingering aircraft, uncertain track",
            geometry["duration"] / 60,
            "minutes",
            f"Positions remain in a compact area for {geometry['duration'] / 60:.1f} minutes, but sharp changes or speed disagreement make the detailed flight path uncertain.",
            "The observations support a confined-activity lead, not a reliable count of circles. Position noise, MLAT timing, source transitions, or coarse archive timestamps may distort the shape or speed comparison. Check the source and other evidence before describing it as circling; no mission or activity on the ground is established.",
            58,
            "Position quality needs review",
            _pattern_evidence("confined_activity", geometry),
        )

    return geometry["duration"] / 60, build


def movement_patterns(samples, *, orbit=None, cache=None):
    """Return compatible explainable movement patterns in the current segment.

    Examine multiple recent windows so an approach leg does not conceal later
    orbiting. Circles, repeated passes and uncertain confined activity can retain
    distinct evidence. Generic loitering is omitted when circling fits, or when
    the caller passes this evaluation's orbit verdict (see orbit_verdict).
    Input must be chronological
    and its timestamps must represent fresh positions, not repeated snapshots.
    ``cache`` is an IdentityCache(segment_point) the caller keeps for the buffer.
    """
    segment = _recent_segment(samples, cache)
    if not segment:
        return []
    geometries, starts = [], set()
    for window in (180, 300, 480, 720, PATTERN_WINDOW_SECONDS):
        subset = [point for point in segment if point["ts"] >= segment[-1]["ts"] - window]
        if not subset or subset[0]["ts"] in starts:
            continue
        starts.add(subset[0]["ts"])
        if geometry := _geometry(subset):
            geometries.append(geometry)
    findings = []
    for detector in (_circle, _repeated_passes, _loitering, _confined_activity):
        # A compact circle also satisfies generic loitering; retain distinct
        # compatible shapes without adding that redundant reason.
        if detector is _loitering and (
            _orbit_active(orbit, segment) or any(f.kind == "circling" for f in findings)
        ):
            continue
        ranked = [found for geometry in geometries if (found := detector(geometry))]
        if ranked:
            findings.append(max(ranked, key=lambda found: found[0])[1]())
    return findings


def _orbit_active(orbit, segment):
    """A passed verdict suppresses loitering only while it overlaps this segment."""
    if not orbit:
        return False
    ended = number(orbit.get("ended_at"))
    return ended is None or ended >= segment[0]["ts"]


def orbit_verdict(patterns):
    """This evaluation's circling verdict from movement_patterns() output.

    Returns the circling pattern summary plus its context tags, bounded samples
    and any airport context, as an independent copy, so one verdict can drive
    maneuvers.analyze(orbit=...) and movement_patterns(orbit=...). Returns False,
    never None, when the evaluation found no circling: None means "not provided"
    to maneuvers.analyze. An "airport_context" key (None away from runways)
    means the context was classified, so maneuvers does not classify it again.
    """
    circle = next((f for f in patterns if f.kind == "circling"), None)
    # A pattern without its saved samples cannot drive episode precedence.
    if circle is None or not (circle.evidence or {}).get("pattern_samples"):
        return False
    evidence = circle.evidence
    # The summary's few lists are copied too; samples hold only scalars.
    verdict = {
        key: list(value) if isinstance(value, list) else value for key, value in evidence["pattern"].items()
    }
    verdict.update(
        context_tags=list(evidence.get("context_tags", [])),
        pattern_samples=[dict(sample) for sample in evidence["pattern_samples"]],
    )
    if "airport_context" in evidence:
        context = evidence["airport_context"]
        verdict["airport_context"] = dict(context) if context else None
    return verdict


def circling(samples):
    """Compatibility entry point for callers interested only in circling."""
    return next((finding for finding in movement_patterns(samples) if finding.kind == "circling"), None)
