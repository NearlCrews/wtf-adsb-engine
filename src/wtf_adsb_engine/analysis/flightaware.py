"""Bounded AeroAPI enrichment for the private viewer, separate from detection.

Recent flight summaries and current registered owners can be queried.
Receiver positions are never sent.
The cache contains a small field allowlist, not raw API responses. Public story
packets do not consume this cache; their FlightAware data policy is separate.
"""

import fcntl
import hashlib
import json
import logging
import os
import re
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener

from . import registry_http, rules
from .curation import CONTEXT_KINDS
from .fleet import fleet_context
from .rules import number

BASE = "https://aeroapi.flightaware.com/aeroapi"
PROVIDER = "FlightAware AeroAPI"
MICRO_USD = 1_000_000
QUERY_COST = 5_000  # $0.005 for one /flights/{ident} result set, verified 2026-10-04.
OWNER_COST = 2_000  # $0.002 for /aircraft/{ident}/owner, verified 2026-10-05.
MAX_BYTES = 512 * 1024
RETENTION = 7 * 86400
BUCKET = 6 * 3600
INTERVAL = 300
QUIET_PERIOD = 600
LOG = logging.getLogger("flightaware")
TIMES = ("actual_off", "actual_on", "scheduled_off", "scheduled_on", "estimated_on")
IDENTS = ("ident", "ident_icao", "ident_iata", "atc_ident", "registration", "aircraft_type")
PLACEHOLDERS = frozenset({"UNKNOWN", "NONE", "NULL", "N-A", "NA", "NIL", "BLOCKED", "NOREG", "NO-REG"})
# A refused credential, account or plan waits registry_http.REFUSAL_BACKOFF; other failures this long.
REFUSALS = frozenset({401, 402, 403})
RETRY = 900


def money(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or not 0 <= amount <= 1000:
            raise ValueError
        return int(amount * MICRO_USD)
    except InvalidOperation, ValueError, OverflowError:
        raise ValueError("FlightAware budgets must be finite amounts between $0 and $1000") from None


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    key_file: Path = Path("/run/secrets/flightaware_api_key")
    daily_budget: int = 500_000
    monthly_budget: int = 10_000_000
    fleet_reference_file: Path | None = None
    pause_file: Path | None = None

    @property
    def paused(self):
        if self.pause_file is None:
            return False
        try:
            Path(self.pause_file).lstat()
        except FileNotFoundError:
            return False
        except OSError:
            # An unreadable control path must never authorize paid requests.
            return True
        return True

    @classmethod
    def from_env(cls):
        return cls(
            enabled=os.getenv("FLIGHTAWARE_ENABLED", "false").lower() in {"1", "true", "yes"},
            key_file=Path(os.getenv("FLIGHTAWARE_API_KEY_FILE", "/run/secrets/flightaware_api_key")),
            daily_budget=money(os.getenv("FLIGHTAWARE_DAILY_BUDGET_USD", "0.50")),
            monthly_budget=money(os.getenv("FLIGHTAWARE_MONTHLY_BUDGET_USD", "10.00")),
            fleet_reference_file=os.getenv("WTF_ADSB_FLEET_REFERENCE_FILE"),
            pause_file=os.getenv("FLIGHTAWARE_PAUSE_FILE"),
        )


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS flightaware_requests (
          id INTEGER PRIMARY KEY, requested_at REAL NOT NULL, completed_at REAL,
          cache_key TEXT NOT NULL, status TEXT NOT NULL, cost_microusd INTEGER NOT NULL,
          http_status INTEGER);
        CREATE INDEX IF NOT EXISTS flightaware_request_time ON flightaware_requests(requested_at);
        CREATE TABLE IF NOT EXISTS flightaware_cache (
          cache_key TEXT PRIMARY KEY, fetched_at REAL NOT NULL, expires_at REAL NOT NULL,
          payload TEXT NOT NULL);
    """)


class AeroAPIError(Exception):
    def __init__(self, status=None):
        super().__init__("FlightAware request unavailable")
        self.status = status


# Never forward the credential to a redirect target.
NoRedirect = registry_http.NoRedirect


class Client:
    def __init__(self, key_file, opener=None):
        self.key_file = Path(key_file)
        self.opener = opener or build_opener(NoRedirect()).open

    def get(self, path, params):
        try:
            info = self.key_file.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096:
                raise ValueError
            key = self.key_file.read_text().strip()
            if not 16 <= len(key) <= 512 or any(not 33 <= ord(c) <= 126 for c in key):
                raise ValueError
        except OSError, ValueError, UnicodeError:
            raise AeroAPIError() from None
        if path != "/account/usage" and not re.fullmatch(
            r"/(?:flights/[A-Z0-9-]{2,12}|aircraft/[A-Z0-9-]{2,12}/owner)", path
        ):
            raise ValueError("Unsupported FlightAware endpoint")
        request = Request(
            BASE + path + "?" + urlencode(params),
            headers={"x-apikey": key, "Accept": "application/json", "User-Agent": "wtf-adsb/1.0"},
            method="GET",
        )
        try:
            with self.opener(request, timeout=15) as response:
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError
                return result
        except HTTPError as error:
            error.close()
            raise AeroAPIError(error.code) from None
        except URLError, OSError, ValueError, UnicodeError:
            raise AeroAPIError() from None

    def usage(self, now):
        start = datetime.fromtimestamp(now, timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        result = self.get("/account/usage", {"start": utc(start.timestamp())})
        # Never print resource-level account details or arbitrary response text.
        return {key: number(result.get(key)) for key in ("total_calls", "total_cost", "total_pages")}

    def flights(self, query):
        return self.get(
            "/flights/" + quote(query["ident"], safe=""),
            {**{key: query[key] for key in ("ident_type", "start", "end")}, "max_pages": 1},
        )

    def owner(self, query):
        return self.get("/aircraft/" + quote(query["registration"], safe="") + "/owner", {})


def identifier(value):
    """A registration- or call-sign-shaped identifier, or None for a placeholder or other text."""
    value = rules.identifier(value, rules.IDENTIFIERS["registration"])
    return None if value in PLACEHOLDERS else value


def normalized(value):
    return (identifier(value) or "").replace("-", "")


def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None and 2000 <= parsed.year <= 2100:
            return parsed.timestamp()
    except ValueError, OverflowError:
        pass
    return None


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def label(value, limit=120):
    if (
        isinstance(value, str)
        and 0 < len(value.strip()) <= limit
        and not re.search(r"[\x00-\x1f<>]|https?://|www\.|\.ts\.net", value, re.I)
    ):
        return value.strip()
    return None


def request_for(event, now):
    evidence = event.get("evidence")
    aircraft = evidence.get("aircraft") if isinstance(evidence, dict) else None
    if not isinstance(aircraft, dict):
        return None
    observed = number(evidence.get("observed_at"))
    if observed is None or not now - RETENTION <= observed <= now:
        return None
    registration, callsign = identifier(aircraft.get("r")), identifier(aircraft.get("flight"))
    if not (registration or callsign):
        return None
    bucket = int(observed // BUCKET) * BUCKET
    query = {
        "ident": registration or callsign,
        "ident_type": "registration" if registration else "designator",
        # API date filters use scheduled departure, not the observation time.
        "start": utc(bucket - 86400),
        "end": utc(bucket + BUCKET),
        "max_pages": 1,
    }
    cache_key = hashlib.sha256(json.dumps(query, sort_keys=True).encode()).hexdigest()
    return cache_key, query


def airport(value):
    if not isinstance(value, dict):
        return None
    codes = {
        key: value[key]
        for key in ("code_icao", "code_iata", "code_lid")
        if isinstance(value.get(key), str) and re.fullmatch(r"[A-Z0-9]{3,4}", value[key])
    }
    if not codes:
        return None  # Position-only labels/coordinates are not airport identities.
    return {**codes, "name": label(value.get("name"))}


def clean_flight(value):
    if not isinstance(value, dict) or value.get("blocked") is not False:
        return None
    flight_id = value.get("fa_flight_id")
    if not isinstance(flight_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", flight_id):
        return None
    flight = {key: identifier(value.get(key)) for key in IDENTS}
    flight.update(
        {key: utc(stamp) if (stamp := timestamp(value.get(key))) is not None else None for key in TIMES}
    )
    flight.update(
        fa_flight_id=flight_id,
        origin=airport(value.get("origin")),
        destination=airport(value.get("destination")),
        status=label(value.get("status")),
        blocked=False,
    )
    for key in ("diverted", "cancelled", "position_only"):
        flight[key] = value[key] if type(value.get(key)) is bool else None
    return flight


def clean_response(result):
    if not isinstance(result, dict) or not isinstance(result.get("flights"), list):
        raise ValueError("Invalid FlightAware result")
    if (
        len(result["flights"]) > 15
        or type(result.get("num_pages")) is not int
        or not 0 <= result["num_pages"] <= 1
    ):
        raise ValueError("FlightAware result exceeds one page")
    links = result.get("links")
    if links is not None and not isinstance(links, dict):
        raise ValueError("Invalid FlightAware pagination")
    return {
        "partial_results": bool(links and links.get("next")),
        "flights": [clean for flight in result["flights"] if (clean := clean_flight(flight)) is not None],
    }


def match_context(event, payload, fetched_at, now):
    """Associate one observed moment; do not assign a route to an entire episode."""
    if not 0 <= now - fetched_at < RETENTION or payload.get("partial_results") is not False:
        return {"state": "partial_results" if payload.get("partial_results") else "expired"}
    aircraft = event["evidence"]["aircraft"]
    observed = number(event["evidence"].get("observed_at"))
    registration, callsign = normalized(aircraft.get("r")), normalized(aircraft.get("flight"))
    matches = []
    for raw in payload.get("flights", []):
        flight = clean_flight(raw)
        if flight is None or flight["cancelled"] is True:
            continue
        candidate_reg = normalized(flight["registration"])
        idents = {normalized(flight[key]) for key in ("ident", "ident_icao", "ident_iata", "atc_ident")}
        idents.discard("")
        if registration and candidate_reg != registration:
            continue
        if callsign and callsign != registration and callsign not in idents:
            continue
        if not registration and (not callsign or callsign not in idents):
            continue
        departure, arrival = timestamp(flight["actual_off"]), timestamp(flight["actual_on"])
        if arrival is not None and arrival > fetched_at:
            continue
        # A schedule alone cannot match observed airborne activity. A missing
        # landing time is bounded by retrieval time and a 24-hour maximum leg.
        end = arrival if arrival is not None else min(fetched_at, (departure or 0) + 86400)
        if departure is None or observed is None or not departure <= observed <= end:
            continue
        matches.append(flight)
    if len(matches) != 1:
        return {"state": "ambiguous" if matches else "no_match", "retrieved_at": fetched_at}
    return {
        "state": "matched" if registration else "candidate",
        "source": PROVIDER,
        "retrieved_at": fetched_at,
        "observed_at": observed,
        "match_basis": "registration_and_time" if registration else "callsign_and_time",
        "review_required": not bool(registration),
        "flight": matches[0],
        "source_url": "https://www.flightaware.com/live/flight/"
        + quote(matches[0]["ident"] or registration or callsign),
        "caveat": "FlightAware-reported context for this observed moment. Feeds may overlap; this is not independent confirmation of a maneuver, incident, owner or passenger. Status and estimates are as retrieved.",
    }


def cached_context(db, event, now):
    lookup = request_for(event, now)
    if lookup is None:
        return None
    # Read-only maintenance tools may inspect journals before the schema upgrade.
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='flightaware_cache'").fetchone():
        return None
    row = db.execute("SELECT * FROM flightaware_cache WHERE cache_key=?", (lookup[0],)).fetchone()
    if (
        row is None
        or row["expires_at"] <= now
        or number(event["evidence"]["observed_at"]) > row["fetched_at"]
    ):
        return None
    try:
        return match_context(event, json.loads(row["payload"]), row["fetched_at"], now)
    except ValueError, TypeError, KeyError, AttributeError:
        return None


def owner_request(registration):
    # Callers supply the aircraft's registration, never a fallback callsign.
    registration = identifier(registration)
    if registration is None:
        return None
    return "owner:" + registration, {"registration": registration}


def clean_owner(result):
    if not isinstance(result, dict) or "owner" not in result:
        raise ValueError("Invalid FlightAware owner result")
    owner = result["owner"]
    if owner is not None and not isinstance(owner, dict):
        raise ValueError("Invalid FlightAware owner result")
    # Discard street address, location, website and every unrecognized field.
    return {"owner": {"name": label((owner or {}).get("name"))}}


def owner_context(registration, payload, fetched_at):
    name = clean_owner(payload)["owner"]["name"]
    return {
        "state": "owner_unavailable"
        if payload.get("unavailable") is True
        else "owner_reported"
        if name
        else "owner_unknown",
        "source": PROVIDER,
        "registration": registration,
        "retrieved_at": fetched_at,
        "name": name,
        "training_hint": bool(
            name and re.search(r"\b(?:flying club|aero club|flight school|flight training)\b", name, re.I)
        ),
        "review_required": True,
        "caveat": "Current registered owner reported by FlightAware. Provider records refresh weekly and upstream updates can lag. A club or school name is a research lead; ownership does not establish the operator or purpose of this flight.",
    }


def cached_owner(db, registration, now):
    lookup = owner_request(registration)
    if (
        lookup is None
        or not db.execute("SELECT 1 FROM sqlite_master WHERE name='flightaware_cache'").fetchone()
    ):
        return None
    row = db.execute("SELECT * FROM flightaware_cache WHERE cache_key=?", (lookup[0],)).fetchone()
    if row is None or row["expires_at"] <= now or not 0 <= now - row["fetched_at"] < RETENTION:
        return None
    try:
        return owner_context(lookup[1]["registration"], json.loads(row["payload"]), row["fetched_at"])
    except ValueError, TypeError, KeyError, AttributeError:
        return None


def attach_context(db, event, now, *, fleet_reference_file=None):
    """Viewer-only reads; browser requests never trigger paid lookups."""
    aircraft = event["evidence"]["aircraft"]
    event["flightaware"] = cached_context(db, event, now)
    event["flightaware_owner"] = cached_owner(db, aircraft.get("r"), now)
    event["training_context"] = fleet_context(aircraft, now, fleet_reference_file)


def spending(db, now):
    current = datetime.fromtimestamp(now, timezone.utc)
    day = current.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    month = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    return tuple(
        db.execute(
            "SELECT COALESCE(SUM(CASE WHEN requested_at>=? THEN cost_microusd ELSE 0 END),0),COALESCE(SUM(cost_microusd),0) FROM flightaware_requests WHERE requested_at>=?",
            (day, month),
        ).fetchone()
    )


def status(db, now, settings=None):
    from .app import meta

    try:
        settings = settings or Settings.from_env()
    except ValueError:
        return {"enabled": False, "status": "Invalid FlightAware budget configuration"}
    daily, monthly = spending(db, now)
    return {
        "enabled": settings.enabled,
        "status": "Paused"
        if settings.paused
        else meta(db, "flightaware_status", "Waiting for selected sightings")
        if settings.enabled
        else "Disabled",
        "last_success": meta(db, "flightaware_last_success"),
        "daily_reserved_usd": daily / MICRO_USD,
        "monthly_reserved_usd": monthly / MICRO_USD,
        "daily_budget_usd": settings.daily_budget / MICRO_USD,
        "monthly_budget_usd": settings.monthly_budget / MICRO_USD,
        "budget_scope": "This app's reserved query fees; account usage, credits and subscription minimums are separate",
    }


def enrich_event(path, event, settings, now=None, requester=None):
    return _enrich(path, settings, now, requester, event=event)


def enrich_owner(path, registration, settings, now=None, requester=None):
    return _enrich(path, settings, now, requester, registration=registration)


def _enrich(path, settings, now, requester, *, event=None, registration=None):
    from .app import connect, meta, put_meta

    now = time.time() if now is None else now
    if settings.paused:
        return {"state": "paused"}
    if not settings.enabled:
        return {"state": "disabled"}
    owner = event is None
    lookup = owner_request(registration) if owner else request_for(event, now)
    if lookup is None:
        return {"state": "ineligible"}
    cache_key, query = lookup
    cost = OWNER_COST if owner else QUERY_COST
    with Path(path).with_suffix(".flightaware.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "busy"}
        with connect(path) as db:
            cached = cached_owner(db, registration, now) if owner else cached_context(db, event, now)
            if cached:
                return {**cached, "cached": True}
            db.execute("BEGIN IMMEDIATE")
            if now < meta(db, "flightaware_backoff_until", 0):
                return {"state": "backoff"}
            last = db.execute("SELECT MAX(requested_at) FROM flightaware_requests").fetchone()[0]
            if last is not None and now - last < 60:
                return {"state": "rate_limited"}
            daily, monthly = spending(db, now)
            if daily + cost > settings.daily_budget or monthly + cost > settings.monthly_budget:
                put_meta(db, "flightaware_status", "Local FlightAware query budget reached")
                return {"state": "budget_exhausted"}
            run_id = db.execute(
                "INSERT INTO flightaware_requests(requested_at,cache_key,status,cost_microusd) VALUES (?,?,'reserved',?)",
                (now, cache_key, cost),
            ).lastrowid
        # No database write lock or receiver polling thread is held over HTTP.
        try:
            client = Client(settings.key_file)
            raw = (requester or (client.owner if owner else client.flights))(query)
            payload = clean_owner(raw) if owner else clean_response(raw)
            result = (
                owner_context(query["registration"], payload, now)
                if owner
                else match_context(event, payload, now, now)
            )
        except (AeroAPIError, ValueError, TypeError, KeyError, OSError) as error:
            code = error.status if isinstance(error, AeroAPIError) else None
            with connect(path) as db:
                db.execute(
                    "UPDATE flightaware_requests SET completed_at=?,status='failed',http_status=? WHERE id=?",
                    (time.time(), code, run_id),
                )
                if owner and code in {400, 404}:
                    # A registration-specific refusal must not starve all other
                    # enrichment. Retain the fee and retry this owner tomorrow.
                    db.execute(
                        "INSERT OR REPLACE INTO flightaware_cache VALUES (?,?,?,?)",
                        (cache_key, now, now + 86400, json.dumps({"owner": None, "unavailable": True})),
                    )
                else:
                    put_meta(
                        db,
                        "flightaware_backoff_until",
                        now + registry_http.backoff_delay(code, refusals=REFUSALS, network=RETRY),
                    )
                put_meta(db, "flightaware_status", "FlightAware unavailable; query reservation retained")
            LOG.warning("FlightAware lookup unavailable (HTTP %s)", code or "unknown")
            return {"state": "unavailable", "http_status": code}
        with connect(path) as db:
            # Missing/ambiguous flights can improve later. Flight matches last
            # one day, owner responses seven; no payload survives longer.
            expires = now + (
                RETENTION if owner else 86400 if result["state"] in {"matched", "candidate"} else 7200
            )
            db.execute(
                "INSERT OR REPLACE INTO flightaware_cache VALUES (?,?,?,?)",
                (cache_key, now, expires, json.dumps(payload, allow_nan=False)),
            )
            db.execute(
                "UPDATE flightaware_requests SET completed_at=?,status='complete',http_status=200 WHERE id=?",
                (time.time(), run_id),
            )
            put_meta(db, "flightaware_last_success", now)
            put_meta(db, "flightaware_status", "FlightAware lookup: " + result["state"].replace("_", " "))
        return {**result, "cached": False}


def run_once(path, settings=None, now=None, requester=None, owner_requester=None):
    from .app import connect
    from .selection import select

    now = time.time() if now is None else now
    with connect(path) as db:
        db.execute("DELETE FROM flightaware_cache WHERE fetched_at<?", (now - RETENTION,))
    settings = settings or Settings.from_env()
    if settings.paused:
        return {"state": "paused"}
    if not settings.enabled:
        return {"state": "disabled"}
    with connect(path) as db:
        # Retain the inexpensive request ledger across month boundaries/restarts.
        events, _ = select(db, {"days": ["1"], "view": ["promoted"], "sort": ["recent"]}, now, limit=120)
        settled = [
            event
            for event in events
            if event["encounter"]["last_seen"] <= now - QUIET_PERIOD and request_for(event, now)
        ]
        for event in settled:
            aircraft = event["evidence"]["aircraft"]
            fleet = fleet_context(aircraft, now, settings.fleet_reference_file)
            if (
                event["kind"] in CONTEXT_KINDS
                and event["kind"] not in {"rapid_climb", "altitude_oscillation"}
                and not (fleet and fleet["fresh"])
                and owner_request(aircraft.get("r"))
                and not cached_owner(db, aircraft["r"], now)
            ):
                registration = aircraft["r"]
                break
        else:
            registration = None
        candidates = [event for event in settled if not cached_context(db, event, now)]
    if registration:
        return enrich_owner(path, registration, settings, now, owner_requester)
    if not candidates:
        return {"state": "idle"}
    return enrich_event(path, candidates[0], settings, now, requester)


def enrichment_loop(path):
    # Retention cleanup also runs with enrichment disabled or a stale receiver.
    registry_http.loop("flightaware", "FlightAware worker", run_once, path, lambda _result: INTERVAL)
