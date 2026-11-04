"""Second-source aircraft type check for findings whose premise depends on the type.

The receiver's aircraft database sometimes lists the wrong type. A story about
a type envelope, a seasonal speed record, a first sighting of a type, or a
rare or notable variant then rests on a wrong premise. This worker asks two public
registries, adsbdb and hexdb, for the address's type and keeps a compact
answer in the journal's ``type_checks`` table for 30 days. ``attach`` writes
``evidence.type_check`` into each type-dependent event, and curation holds a
type-dependent event whose second source disagrees, except a mismatch story,
which names the registry's type instead. No position, receiver, or private
value is ever sent: the request carries only the 24-bit address.

The worker runs beside the collector and never blocks it: each pass reads
first and writes in one short transaction at its end, and the request itself
is made outside any transaction.
"""

import json
import os
import time

from . import registry_http

# The registry pages, the kinds that depend on the type, and a check's state live in
# enrichment_rules, which the facts layer reads without loading this worker. The worker's
# tests read REGISTRY_TYPE_KINDS and type_check_state here.
from .enrichment_rules import (  # noqa: F401
    ADSBDB,
    HEXDB,
    REGISTRY_TYPE_KINDS,
    SETTLED,
    TYPE_DEPENDENT_KINDS,
    type_check_state,
)
from .reference import clean
from .rules import ICAO_ADDRESS, IDENTIFIERS, identifier, number

NAME = "typecheck"
MAX_BYTES = 64 * 1024
RETENTION = 30 * 86400
# At most one lookup per second; the loop idles longer with nothing to check.
INTERVAL = 1
IDLE = 30
# Events older than this are no longer checked; the publisher works on recent encounters.
LOOKBACK = 7 * 86400
# An idle pass reads only events updated since the previous idle pass, less this
# overlap: the collector can write a report time a little older than its commit.
SCANNED = "typecheck_scanned_until"
SCAN_OVERLAP = 3600
# The most pending events one pass reads, and the most of one address it updates.
PENDING_LIMIT = 500
# Registries lag new registrations, so a finding about an address no registry lists is
# looked up again once its saved check is this old, while the finding was first seen
# within RECHECK_WINDOW, whether or not the aircraft is still in view.
RECHECK_SECONDS = 7 * 86400
RECHECK_WINDOW = 30 * 86400
RECHECK_KINDS = ("unregistered_address",)
COLUMNS = ("hex", "source", "type_code", "registration", "fetched_at")


def enabled():
    return os.getenv("TYPE_CHECK_ENABLED", "true").lower() in {"1", "true", "yes"}


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS type_checks (
          hex TEXT PRIMARY KEY, source TEXT, type_code TEXT, registration TEXT,
          fetched_at REAL NOT NULL);
    """)


def _code(value):
    return identifier(value, IDENTIFIERS["type_code"])


def _adsbdb(payload):
    """adsbdb: response.aircraft.{icao_type, type, registration}; icao_type is the ICAO code."""
    response = payload.get("response")
    aircraft = response.get("aircraft") if isinstance(response, dict) else None
    if not isinstance(aircraft, dict):
        return None
    return {
        "type_code": _code(aircraft.get("icao_type")),
        "registration": identifier(aircraft.get("registration"), IDENTIFIERS["registration"]),
    }


def _hexdb(payload):
    """hexdb: ICAOTypeCode, Manufacturer, Type, Registration; empty fields when unknown."""
    if str(payload.get("status", "")) == "404":
        return None
    return {
        "type_code": _code(payload.get("ICAOTypeCode")),
        "registration": identifier(payload.get("Registration"), IDENTIFIERS["registration"]),
    }


# The registries in the order lookup() asks them: (name, base URL, response reader). Their
# names are enrichment_rules.REGISTRIES.
SOURCES = (("adsbdb", ADSBDB, _adsbdb), ("hexdb", HEXDB, _hexdb))


def lookup(address, *, opener=None, now=None):
    """The first registry that lists the address's type, else the first that knows the
    address at all, or None when neither does.

    Returns {"source", "type_code", "registration", "fetched_at"}; the type code
    or registration is None when that source lists none. A registry that knows the
    registration but not the type does not end the search: the next one may list
    the type. Raises registry_http.Unavailable when a registry could not be asked,
    so the caller backs off.
    """
    address = str(address or "").strip().lower()
    if not ICAO_ADDRESS.fullmatch(address):
        return None
    now = time.time() if now is None else now
    known = None
    for source, base, parse in SOURCES:
        payload = registry_http.get_json(base + address.upper(), opener, max_bytes=MAX_BYTES, absent={404})
        found = parse(payload) if isinstance(payload, dict) else None
        if found and found["type_code"]:
            return {"source": source, **found, "fetched_at": now}
        if found and found["registration"] and known is None:
            known = {"source": source, **found, "fetched_at": now}
    return known


def agreement(listed, found):
    """Whether the second source's ICAO type code matches the listed one; None without one."""
    listed, found = _code(listed), _code(found)
    if listed is None or found is None:
        return None
    return listed == found


def _current(row, now):
    """A retained check, or None when it is missing or expired. now=None reads a frozen
    corpus, whose checks are kept as long as the corpus; the journal's expire after RETENTION."""
    if row is None or (now is not None and not 0 <= now - row["fetched_at"] <= RETENTION):
        return None
    return row


def _has_table(db):
    # Read-only tools may open a journal or corpus without the table.
    return db.execute("SELECT 1 FROM sqlite_master WHERE name='type_checks'").fetchone() is not None


def saved(db, address, now):
    """The retained check for an address, or None when it is missing or expired."""
    if not _has_table(db):
        return None
    row = db.execute(f"SELECT {','.join(COLUMNS)} FROM type_checks WHERE hex=?", (address,)).fetchone()
    return _current(None if row is None else dict(zip(COLUMNS, tuple(row), strict=True)), now)


def saved_all(db):
    """Every retained check by address, read at once; {} without the table. Expiry is
    the reader's (fill() applies it), so one read serves a whole selection pass."""
    if not _has_table(db):
        return {}
    return {
        row[0]: dict(zip(COLUMNS, tuple(row), strict=True))
        for row in db.execute(f"SELECT {','.join(COLUMNS)} FROM type_checks")
    }


def remember(db, address, result, now):
    """Keep one row per address, and return it; a lookup that found nothing is kept as checked."""
    result = result or {}
    row = {
        "hex": address,
        "source": result.get("source"),
        "type_code": result.get("type_code"),
        "registration": result.get("registration"),
        "fetched_at": now,
    }
    db.execute("INSERT OR REPLACE INTO type_checks VALUES (?,?,?,?,?)", tuple(row[key] for key in COLUMNS))
    return row


def check_evidence(aircraft, row, now):
    """The evidence block attach() writes: the source's answer, the listed type it
    was compared with and the resulting state."""
    listed = clean((aircraft or {}).get("t"))
    agrees = agreement(listed, row.get("type_code"))
    return {
        "state": "unavailable" if agrees is None else "agrees" if agrees else "disagrees",
        "source": row.get("source"),
        "type_code": row.get("type_code"),
        "registration": row.get("registration"),
        # The type the answer was compared with; a changed listing is checked again.
        "listed_type": listed,
        "checked_at": now,
    }


def failure_evidence(now, status=None):
    """The block written when no registry could be asked; the worker retries after its backoff."""
    return {
        "state": "failed",
        "source": None,
        "type_code": None,
        "registration": None,
        "listed_type": None,
        "checked_at": now,
        "http_status": status,
    }


def _write(db, event, block):
    """Set evidence.type_check in memory and in the row.

    Only the one key is set, so a concurrent collector update of the same row
    keeps its other changes.
    """
    event["evidence"]["type_check"] = block
    if number(event.get("id")) is not None:
        db.execute(
            "UPDATE events SET evidence=json_set(evidence,'$.type_check',json(?)) WHERE id=? AND json_valid(evidence)",
            (json.dumps(block), event["id"]),
        )
    return block


def _attach_all(db, events, row, now):
    """Write one address's retained check into its events; returns how many."""
    for event in events:
        _write(db, event, check_evidence(event["evidence"].get("aircraft"), row, now))
    return len(events)


def attach(db, event, now):
    """Write evidence.type_check into a type-dependent event, in memory and in its row.

    Returns the block, or None when nothing is saved for the address or the
    kind does not depend on the type.
    """
    if event.get("kind") not in TYPE_DEPENDENT_KINDS or not isinstance(event.get("evidence"), dict):
        return None
    row = saved(db, str(event.get("hex", "")).lower(), now)
    if row is None:
        return None
    return _write(db, event, check_evidence(event["evidence"].get("aircraft"), row, now))


def fill(event, row, now=None):
    """Attach a retained check (a type_checks row, or None) to an event in memory, without writing.

    The journal and a frozen corpus both keep the type_checks table, so
    selection sees the same saved check the worker would write. now=None reads a
    frozen corpus's row, which never expires. Returns the block or None; an
    existing block is left alone.
    """
    if event.get("kind") not in TYPE_DEPENDENT_KINDS or not isinstance(event.get("evidence"), dict):
        return None
    if isinstance(event["evidence"].get("type_check"), dict):
        return event["evidence"]["type_check"]
    row = _current(row, now)
    if row is None:
        return None
    block = check_evidence(event["evidence"].get("aircraft"), row, row["fetched_at"])
    event["evidence"]["type_check"] = block
    return block


def pending(db, since, address=None, *, now=None):
    """Live type-dependent events last seen since `since` whose check is missing, unsettled
    (type_check_state's pending or failed) or compared with a type the aircraft no longer
    lists. Then live RECHECK_KINDS events first seen within RECHECK_WINDOW of `now` whose
    saved check is older than RECHECK_SECONDS. Each part lists the highest scores first,
    then the newest first sightings, so the worker asks about the strongest candidates
    first; the journal keeps no selection result to order by. One call reads at most
    PENDING_LIMIT events, the first query's before the recheck's."""
    from .app import LIVE_ORIGIN

    now = time.time() if now is None else now

    kind_slots = ",".join("?" * len(TYPE_DEPENDENT_KINDS))
    settled = ",".join("?" * len(SETTLED))
    where = f"""origin=? AND last_seen>=? AND kind IN ({kind_slots}) AND hex NOT LIKE '~%'
        AND json_valid(evidence) AND (json_type(evidence,'$.type_check') IS NULL
          OR COALESCE(json_extract(evidence,'$.type_check.state'),'') NOT IN ({settled})
          OR COALESCE(json_extract(evidence,'$.type_check.listed_type'),'')
             IS NOT COALESCE(upper(trim(json_extract(evidence,'$.aircraft.t'))),''))"""
    args = [LIVE_ORIGIN, since, *sorted(TYPE_DEPENDENT_KINDS), *sorted(SETTLED)]
    if address is not None:
        where += " AND hex=?"
        args.append(address)
    rows = db.execute(
        f"SELECT id,hex,kind,evidence FROM events WHERE {where} ORDER BY score DESC, first_seen DESC, id LIMIT ?",
        (*args, PENDING_LIMIT),
    ).fetchall()
    if _has_table(db) and len(rows) < PENDING_LIMIT:
        recheck = f"""e.origin=? AND e.first_seen>=? AND e.kind IN ({",".join("?" * len(RECHECK_KINDS))})
            AND e.hex NOT LIKE '~%' AND json_valid(e.evidence) AND t.fetched_at<?"""
        recheck_args = [LIVE_ORIGIN, now - RECHECK_WINDOW, *RECHECK_KINDS, now - RECHECK_SECONDS]
        if address is not None:
            recheck += " AND e.hex=?"
            recheck_args.append(address)
        rows += db.execute(
            f"""SELECT e.id,e.hex,e.kind,e.evidence FROM events e JOIN type_checks t ON t.hex=e.hex
                WHERE {recheck} ORDER BY e.score DESC, e.first_seen DESC, e.id LIMIT ?""",
            (*recheck_args, PENDING_LIMIT - len(rows)),
        ).fetchall()
    events, seen = [], set()
    for row in rows:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        event = dict(row)
        event["evidence"] = json.loads(event["evidence"])
        events.append(event)
    return events


def _due(row, events, now):
    """Whether an address needs a lookup: it has no retained check, or one of its
    RECHECK_KINDS events rests on a check older than RECHECK_SECONDS."""
    return row is None or (
        now - row["fetched_at"] > RECHECK_SECONDS and any(event["kind"] in RECHECK_KINDS for event in events)
    )


def run_once(path, now=None, opener=None, *, enabled_override=None):
    """Attach every retained check that is due, then check one more address with a lookup.

    Returns the pass's state: "idle", "cached" (only retained checks were
    attached), "checked", "unavailable" (the lookup failed), "backoff",
    "rate_limited" or "disabled", with "attached" counting the events updated.
    """
    from .app import connect, meta, put_meta

    now = time.time() if now is None else now
    if not (enabled() if enabled_override is None else enabled_override):
        return {"state": "disabled"}
    with connect(path) as db:
        since = max(now - LOOKBACK, number(meta(db, SCANNED)) or 0)
        events = pending(db, since, now=now)
        if not events:
            # Nothing waits: the next pass reads only events updated from here on.
            put_meta(db, SCANNED, now - SCAN_OVERLAP)
            return {"state": "idle"}
        addresses = {}
        for event in events:
            addresses.setdefault(event["hex"], []).append(event)
        checks = {address: saved(db, address, now) for address in addresses}
        due = next((address for address, row in checks.items() if _due(row, addresses[address], now)), None)
        held = registry_http.waiting(db, NAME, now, INTERVAL) if due else None
        # Writes come last: the retained checks, then the claim on the next request. An
        # address due for a recheck keeps its block until its own lookup answers.
        attached = sum(
            _attach_all(db, addresses[address], row, now)
            for address, row in checks.items()
            if row is not None and not _due(row, addresses[address], now)
        )
        if due and not held:
            registry_http.requested(db, NAME, now)
    if due is None or held:
        return {"state": held or "cached", "attached": attached}
    # No database lock is held over the network request.
    try:
        result = lookup(due, opener=opener, now=now)
    except registry_http.Unavailable as error:
        with connect(path) as db:
            events = pending(db, now - LOOKBACK, due, now=now)
            registry_http.failed(db, NAME, now, error.status, "Type registry")
            # The events record the attempt; they are retried after the backoff. A settled
            # block awaiting its recheck stays: only a fresh answer replaces it.
            for event in events:
                if type_check_state(event["evidence"]) not in SETTLED:
                    _write(db, event, failure_evidence(now, error.status))
        return {"state": "unavailable", "http_status": error.status, "attached": attached}
    with connect(path) as db:
        events = pending(db, now - LOOKBACK, due, now=now)
        row = remember(db, due, result, now)
        attached += _attach_all(db, events, row, now)
        registry_http.succeeded(
            db,
            NAME,
            now,
            "Type lookup: " + (result["source"] if result else "no registry lists this address"),
        )
    return {"state": "checked", "hex": due, "result": result, "attached": attached}


def status(db):
    return registry_http.status(db, NAME, enabled(), "Waiting for type-dependent findings")


def _pause(result):
    state = result.get("state")
    if state == "cached":
        # An address that needs a lookup may be next; take it soon (a short pause, so a
        # row the worker cannot settle never spins the loop).
        return 0.2
    return INTERVAL if state in {"checked", "unavailable"} else IDLE


def enrichment_loop(path):
    registry_http.loop(NAME, "Type check worker", run_once, path, _pause)
