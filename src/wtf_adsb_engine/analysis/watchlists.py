"""Pinned, attributed aircraft watchlist enrichment; no runtime network access.

An address/registration match is a match to a source claim. It does not establish
current ownership, the operator of a particular flight, or who is aboard.
"""

import csv
import hashlib
import io
import json
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from . import rules

CSV_HEADER = [
    "$ICAO",
    "$Registration",
    "$Operator",
    "$Type",
    "$ICAO Type",
    "#CMPG",
    "$Tag 1",
    "$#Tag 2",
    "$#Tag 3",
    "Category",
    "$#Link",
]
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_CACHE_BYTES = 32 * 1024 * 1024
MAX_ROWS = 100000
REPOSITORY_URL = "https://github.com/sdr-enthusiasts/plane-alert-db"
LICENSES = [
    {
        "name": "Open Database License 1.0",
        "id": "ODbL-1.0",
        "url": "https://opendatacommons.org/licenses/odbl/1-0/",
    },
    {
        "name": "Database Contents License 1.0",
        "id": "DBCL-1.0",
        "url": "https://opendatacommons.org/licenses/dbcl/1-0/",
    },
]
LIMITATIONS = [
    "Operator/association labels are claims made by the source, not verified current ownership or flight operation.",
    "An associated person's name is not evidence that the person is aboard or traveling.",
    "Rows have no valid-from/valid-to dates or per-row verification dates; historical matches need separate verification.",
    "Source categories and tags are editorial labels, not evidence of a flight's mission or global rarity.",
    "Public reuse must retain attribution and comply with the source database licenses; linked pages and images have independent rights.",
]
# The neutral label of a source category with no reviewed mapping below.
GENERIC_CATEGORY = "Curated aircraft entry"
NEUTRAL_CATEGORIES = {
    "Don't you know who I am?": "Public figure association",
    "Police Forces": "Police aviation",
    "UK National Police Air Service": "Police aviation",
    "Historic": "Historic aircraft",
    "Distinctive": "Distinctive aircraft",
    "Dogs with Jobs": "Specialized aircraft",
    "Ptolemy would be proud": "Survey and mapping",
    "Aerial Firefighter": "Firefighting aircraft",
    "Flying Doctors": "Medical aviation",
    "As Seen on TV": "Organization or brand association",
    "Vanity Plate": "Distinctive registration",
    "Aerobatic Teams": "Aerobatic display aircraft",
    "Radiohead": "Specialized government aircraft",
    "Nuclear": "Specialized government aircraft",
    "Dictator Alert": "Political association",
    "Oligarch": "Business figure association",
    "PIA": "Privacy-related entry",
    "Gas Bags": "Lighter-than-air aircraft",
    "Watch Me Fly": "Flight training",
    "Governments": "Government aviation",
    "Royal Aircraft": "Royal aviation association",
    "UAV": "Uncrewed aircraft",
    "Oxcart": "Specialized military aircraft",
}
# The reviewed neutral labels; a story reads any other value as the generic one.
NEUTRAL_LABELS = frozenset(NEUTRAL_CATEGORIES.values())
# The entry's own tags and category name explain why the list carries an aircraft. Its
# public text fields keep to these lengths in the packet and the public record alike.
MAX_TAGS = 3
TEXT_LIMITS = {"association": 200, "list_category": 80, "tag": 60, "description": 120, "link": 300}


def entry_text(value, field):
    """One line of the entry's text within its field's limit, or None."""
    if not isinstance(value, str) or not value.strip() or len(value) > TEXT_LIMITS[field]:
        return None
    return " ".join(value.split())


# What each Plane Alert DB category collects, in plain words, paraphrased from the
# category table in the source README at the pinned commit; a story uses it to say
# why the list carries an aircraft. Unknown categories have no note.
CATEGORY_NOTES = {
    "Aerial Firefighter": "firefighting aircraft",
    "Aerobatic Teams": "display teams such as the Red Arrows and the Blue Angels",
    "Army Air Corps": "aircraft of the UK Army Air Corps, mainly helicopters",
    "As Seen on TV": "aircraft of well-known companies and brands",
    "Big Hello": "large helicopters",
    "Bizjets": "business jets",
    "CAP": "Civil Air Patrol aircraft",
    "Climate Crisis": "oil company aircraft and large business jets such as BBJs and ACJs",
    "Coastguard": "coast guard, customs, and border patrol aircraft",
    "Da Comrade": "Russian and Soviet aircraft",
    "Dictator Alert": "aircraft linked to leaders its editors consider controversial",
    "Distinctive": "unique or special aircraft, such as testbeds and NASA aircraft",
    "Dogs with Jobs": "aircraft with specific roles or modifications",
    "Don't you know who I am?": "aircraft linked to famous people",
    "Flying Doctors": "air ambulance and medical aircraft",
    "Football": "aircraft linked to football teams and leagues",
    "GAF": "aircraft of the German Air Force",
    "Gas Bags": "balloons and airships",
    "Governments": "government-registered aircraft",
    "Gunship": "gunships and attack aircraft",
    "Hired Gun": "contractors flying for militaries and governments",
    "Historic": "historic aircraft, mostly older propeller types",
    "Jesus he Knows me": "aircraft of religious organizations",
    "Joe Cool": "aircraft its editors simply find cool",
    "Jump Johnny Jump": "de Havilland Chipmunks",
    "Nuclear": "nuclear emergency support aircraft",
    "Oligarch": "aircraft linked to oligarchs",
    "Other Air Forces": "air force aircraft of countries other than Germany, the UK, and the US",
    "Other Navies": "naval aircraft of navies other than the Royal Navy and the US Navy",
    "Oxcart": "intelligence-gathering aircraft",
    "Perfectly Serviceable Aircraft": "skydiving aircraft",
    "PIA": "aircraft using the FAA's Privacy ICAO Address program",
    "Police Forces": "police aircraft",
    "Ptolemy would be proud": "mapping and aerial survey aircraft",
    "Quango": "aircraft of international bodies such as NATO and the United Nations",
    "Radiohead": "very special government aircraft such as the VC-25",
    "RAF": "aircraft of the Royal Air Force",
    "Royal Aircraft": "aircraft used by the UK royal family",
    "Royal Navy Fleet Air Arm": "aircraft of the Royal Navy Fleet Air Arm",
    "Special Forces": "special forces aircraft",
    "Toy Soldiers": "army aircraft from around the world",
    "UAV": "drones and other uncrewed aircraft",
    "UK National Police Air Service": "aircraft of the UK National Police Air Service",
    "Ukraine": "aircraft from Ukraine",
    "United States Marine Corps": "aircraft of the US Marine Corps",
    "United States Navy": "aircraft of the US Navy",
    "USAF": "aircraft of the US Air Force",
    "Vanity Plate": "aircraft with distinctive registrations",
    "Watch Me Fly": "flying school and training aircraft",
    "You came here in that thing?": "microlights and other very small aircraft",
    "Zoomies": "fast jets and fighters",
}
# Associations that name nobody; such an entry is carried by its category and tags.
VAGUE_ASSOCIATIONS = frozenset({"private", "private owner", "privately owned", "unknown", "n/a", "none"})
# The list's own category for privacy-program aircraft.
PRIVACY_CATEGORY = "PIA"


def describable(association, list_category, category):
    """Whether what an entry's link leads to may be described: an entry that names nobody, or
    one the list files for the privacy program (its category or the neutral label it reads as),
    is never looked up or described."""
    return (
        isinstance(association, str)
        and association.strip().lower() not in VAGUE_ASSOCIATIONS
        and list_category != PRIVACY_CATEGORY
        and not (
            isinstance(category, str)
            and category.strip().lower() == NEUTRAL_CATEGORIES[PRIVACY_CATEGORY].lower()
        )
    )


# The source's group column, as a story says it.
GROUPS = {"Mil": "military", "Gov": "government", "Civ": "civil"}


class WatchlistError(ValueError):
    pass


def source_commit(url, asset="plane-alert-db.csv"):
    if not isinstance(url, str):
        raise WatchlistError("Pinned source URL must be a string")
    if asset not in {"plane-alert-db.csv", "LICENSE"}:
        raise WatchlistError("Asset is not on the reviewed watchlist allowlist")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "raw.githubusercontent.com"
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/sdr-enthusiasts/plane-alert-db/[0-9a-f]{40}/" + re.escape(asset), parsed.path)
    ):
        raise WatchlistError("Use the allowlisted raw GitHub asset at an explicit 40-character commit")
    return parsed.path.split("/")[3]


def normalized_registration(value):
    """The compact registration a match compares, or None for a placeholder.

    The identity checks use the same normalizer (rules.parse_registration).
    """
    return rules.parse_registration(value)[0]


def _text(value, limit):
    value = value.strip()
    if len(value) > limit or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise WatchlistError("Unexpected control characters or oversized field")
    return value


def parse_catalog(data, url, expected_sha256, fetched_at=None, source_date=None):
    """Validate a pinned CSV and quarantine malformed or ambiguous rows."""
    commit = source_commit(url)
    expected_sha256 = expected_sha256.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise WatchlistError("Expected SHA-256 must contain 64 hexadecimal characters")
    if len(data) > MAX_SOURCE_BYTES:
        raise WatchlistError("CSV exceeds the fixed 8 MiB bound")
    actual_hash = hashlib.sha256(data).hexdigest()
    if actual_hash != expected_sha256:
        raise WatchlistError("Pinned CSV SHA-256 does not match; current cache was not changed")
    try:
        reader = csv.reader(io.StringIO(data.decode("utf-8-sig")), strict=True)
        if next(reader, None) != CSV_HEADER:
            raise WatchlistError("CSV schema differs from the reviewed Plane Alert header")
        entries, quarantine, row_count, seen = {}, [], 0, set()
        for row_number, values in enumerate(reader, 2):
            row_count += 1
            if row_count > MAX_ROWS:
                raise WatchlistError("CSV exceeds the row-count bound")
            address = ""
            try:
                if len(values) != len(CSV_HEADER):
                    raise WatchlistError("Wrong field count")
                row = dict(zip(CSV_HEADER, values, strict=False))
                address = row["$ICAO"].strip().lower()
                if not re.fullmatch(r"[0-9a-f]{6}", address):
                    raise WatchlistError("Invalid six-digit ICAO address")
                if address in seen:
                    previous = entries.pop(address, None)
                    if previous:
                        quarantine.append(
                            {
                                "row": previous["source_row"],
                                "hex": address,
                                "reason": "Duplicate ICAO address; all entries for this address are excluded",
                            }
                        )
                    raise WatchlistError("Duplicate ICAO address; all entries for this address are excluded")
                seen.add(address)
                registration = _text(row["$Registration"], 16).upper()
                normalized = normalized_registration(registration)
                if normalized is None:
                    raise WatchlistError("Invalid or unavailable registration")
                category = _text(row["Category"], 100)
                if not category or "://" in category or category.lower().startswith("www."):
                    raise WatchlistError("Invalid category label; URLs are not categories")
                group = row["#CMPG"].strip()
                if group not in {"Civ", "Mil", "Gov", "Pol"}:
                    raise WatchlistError("Unrecognized main category group")
                reference = _text(row["$#Link"], 1024)
                parsed_reference = urlsplit(reference)
                if reference and (
                    parsed_reference.scheme not in {"https", "http"}
                    or not parsed_reference.hostname
                    or parsed_reference.username
                    or parsed_reference.password
                ):
                    reference = ""
                entries[address] = {
                    "registration": registration,
                    "normalized_registration": normalized,
                    "association": _text(row["$Operator"], 200),
                    "aircraft_description": _text(row["$Type"], 160),
                    "icao_type": _text(row["$ICAO Type"], 16),
                    "group": group,
                    "category": category,
                    "tags": [
                        _text(row[key], 100) for key in ("$Tag 1", "$#Tag 2", "$#Tag 3") if row[key].strip()
                    ],
                    "reference_url": reference or None,
                    "source_row": row_number,
                }
            except WatchlistError as error:
                quarantine.append({"row": row_number, "hex": address, "reason": str(error)})
    except (UnicodeError, csv.Error) as error:
        raise WatchlistError("CSV cannot be decoded using the reviewed schema") from error
    if not entries:
        raise WatchlistError("No valid watchlist entries remain; current cache was not changed")
    if source_date:
        try:
            parsed_date = datetime.fromisoformat(source_date)
            if parsed_date.tzinfo is None:
                raise ValueError("Timezone required")
        except ValueError as error:
            raise WatchlistError("Source date must be an ISO timestamp with a timezone") from error
    return {
        "schema_version": 1,
        "source": {
            "name": "Plane Alert DB",
            "repository_url": REPOSITORY_URL,
            "url": url,
            "commit": commit,
            "sha256": actual_hash,
            "bytes": len(data),
            "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(),
            "source_date": source_date,
            "row_dates_available": False,
            "license_url": f"{REPOSITORY_URL}/blob/{commit}/LICENSE",
            "licenses": deepcopy(LICENSES),
            "attribution": "Aircraft associations and category labels: Plane Alert DB / SDR-Enthusiasts and contributors (ODbL 1.0; DBCL 1.0).",
        },
        "limitations": list(LIMITATIONS),
        "statistics": {
            "source_rows": row_count,
            "accepted_entries": len(entries),
            "quarantined_rows": len(quarantine),
        },
        "quarantine": quarantine,
        "entries": entries,
    }


def _load_catalog(path, size):
    if size > MAX_CACHE_BYTES:
        raise WatchlistError("Watchlist cache exceeds the fixed 32 MiB bound")
    with open(path, "rb") as source:
        data = source.read(MAX_CACHE_BYTES + 1)
    if len(data) > MAX_CACHE_BYTES:
        raise WatchlistError("Watchlist cache exceeds the fixed 32 MiB bound")
    try:
        catalog = json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise WatchlistError("Watchlist cache is not valid JSON") from error
    if (
        not isinstance(catalog, dict)
        or catalog.get("schema_version") != 1
        or not isinstance(catalog.get("entries"), dict)
    ):
        raise WatchlistError("Watchlist cache has an unsupported schema")
    source = catalog.get("source", {})
    if not isinstance(source, dict) or source_commit(source.get("url", "")) != source.get("commit"):
        raise WatchlistError("Watchlist cache provenance does not match its pinned URL")
    if not re.fullmatch(r"[0-9a-f]{64}", str(source.get("sha256", ""))) or len(catalog["entries"]) > MAX_ROWS:
        raise WatchlistError("Watchlist cache has invalid provenance or too many entries")
    return catalog


@lru_cache(maxsize=2)
def _read_catalog(path, modified, size, changed):
    """Cache failures as small diagnostics until the file signature changes.

    lru_cache does not cache raised exceptions. Returning a diagnostic avoids
    reparsing a broken catalog for every aircraft, without retaining traceback
    frames or repeatedly raising the same exception object.
    """
    try:
        return _load_catalog(path, size), None
    except WatchlistError as error:
        return None, str(error)
    except (OSError, ValueError, TypeError, RecursionError) as error:
        return None, f"Watchlist cache could not be loaded ({type(error).__name__})"


def lookup(aircraft, path=None, *, identity=None):
    """Return attributed metadata only for compatible ICAO/registration matches.

    A missing cache returns None. Invalid caches raise WatchlistError. Mismatches
    return review diagnostics without exposing the source association or tags.
    identity, when given, is rules.identity_checks() for the same report: a
    conflict withholds the claim, and a registration-format call sign that
    matches is the only independent corroboration (both sources key on the
    address).
    """
    address = str(aircraft.get("hex", "")).lower()
    if not re.fullmatch(r"[0-9a-f]{6}", address):
        return None
    path = Path(path or os.getenv("WATCHLIST_CACHE", "/data/watchlists/current.json"))
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    catalog, error = _read_catalog(str(path), info.st_mtime_ns, info.st_size, info.st_ctime_ns)
    if error is not None:
        raise WatchlistError(error)
    entry = catalog["entries"].get(address)
    if entry is None:
        return None
    if not isinstance(entry, dict):
        raise WatchlistError("Watchlist cache entry has an unsupported schema")
    current = normalized_registration(aircraft.get("r", aircraft.get("registration")))
    source_registration = normalized_registration(entry.get("registration"))
    matching = bool(current and source_registration and current == source_registration)
    conflict = isinstance(identity, dict) and identity.get("conflict") is True
    compatible = matching and not conflict
    result = {
        "hex": address,
        "source": deepcopy(catalog["source"]),
        "source_registration": entry.get("registration"),
        "observed_registration": aircraft.get("r", aircraft.get("registration")),
        "match_status": "identity_conflict"
        if matching and conflict
        else "icao_and_registration"
        if compatible
        else "registration_mismatch"
        if current
        else "registration_missing",
        "public_claim_allowed": compatible,
        "review_required": True,
        "source_row": entry.get("source_row"),
        "limitations": list(catalog.get("limitations", LIMITATIONS)),
    }
    if isinstance(identity, dict):
        result["callsign_corroborates_registration"] = identity.get("callsign_matches_registration")
    if compatible:
        result.update(
            {
                key: deepcopy(entry.get(key))
                for key in (
                    "association",
                    "category",
                    "group",
                    "tags",
                    "aircraft_description",
                    "icao_type",
                    "reference_url",
                )
            }
        )
        result["source_category"] = entry.get("category")
        result["neutral_category"] = NEUTRAL_CATEGORIES.get(entry.get("category"), GENERIC_CATEGORY)
        result["claim_scope"] = (
            "The source associates this registered aircraft with this label; current ownership, this flight's operator, and persons aboard are not established."
        )
    return result
