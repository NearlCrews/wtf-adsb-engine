"""Optional dated fleet references for private context and lookup budgeting."""

import json
import logging
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

FRESH_FOR = 30 * 86400
MAX_BYTES = 1024 * 1024
LOG = logging.getLogger(__name__)


@lru_cache(maxsize=8)
def _read(path, modified, size):
    if size > MAX_BYTES:
        raise ValueError("Fleet reference is too large")
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Fleet reference is too large")
    value = json.loads(raw)
    checked = datetime.strptime(value["checked_on"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    fleets = value["aircraft"]
    if not isinstance(fleets, dict):
        raise ValueError("Fleet references require an aircraft mapping")
    for registration, entry in fleets.items():
        if not isinstance(registration, str) or not isinstance(entry, dict):
            raise ValueError("Invalid fleet entry")
        for key in ("aircraft_type", "organization", "association", "source_url"):
            if not isinstance(entry.get(key), str) or not 0 < len(entry[key]) <= 1024:
                raise ValueError("Invalid fleet reference field")
        url = urlsplit(entry["source_url"])
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("Fleet sources require public HTTPS URLs")
    return value["checked_on"], checked.timestamp(), fleets


def fleet_context(aircraft, now, path=None):
    if path is None:
        return None
    path = Path(path)
    try:
        info = path.stat()
        checked, stamp, fleets = _read(str(path.resolve()), info.st_mtime_ns, info.st_size)
    except OSError, ValueError, TypeError, KeyError, AttributeError:
        return None
    registration = str(aircraft.get("r") or "").strip().upper()
    entry = fleets.get(registration)
    if entry is None or str(aircraft.get("t") or "").strip().upper() != entry["aircraft_type"]:
        return None
    return {
        **entry,
        "registration": registration,
        "checked_on": checked,
        "fresh": 0 <= now - stamp < FRESH_FOR,
        "caveat": "Listed in this organization's fleet when checked. This does not establish who operated an earlier flight or whether it was training.",
    }
