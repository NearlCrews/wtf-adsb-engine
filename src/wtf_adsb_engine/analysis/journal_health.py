"""Validate collector health within the journal snapshot being read."""

import json
import math


def watermark(database, mode):
    """Return a recorded observation time only when the requested analysis is complete."""
    if mode not in {"live", "replay"}:
        raise ValueError("Unknown journal mode")
    health = {
        key: json.loads(value)
        for key, value in database.execute(
            "SELECT key,value FROM meta WHERE key IN "
            "('last_receiver_timestamp','detector_errors','last_error','archive_scan')"
        )
    }
    errors = health.get("detector_errors")
    if errors is not None and (not isinstance(errors, dict) or errors):
        raise ValueError("Journal analysis is incomplete")
    if mode == "live" and health.get("last_error"):
        raise ValueError("Journal receiver collection is unavailable")
    stamps = [health.get("last_receiver_timestamp")]
    archive_complete = False
    if mode == "replay" and "archive_scan" in health:
        scan = health["archive_scan"]
        if not isinstance(scan, dict) or scan.get("complete") is not True:
            raise ValueError("Journal archive analysis is incomplete")
        archive_complete = True
        stamps.append(database.execute("SELECT MAX(last_seen) FROM aircraft").fetchone()[0])
    if errors is None and not archive_complete:
        raise ValueError("Journal analysis health is missing")
    stamps = [stamp for stamp in stamps if stamp is not None]
    if not stamps or any(
        type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0 for stamp in stamps
    ):
        raise ValueError("Journal has no valid recorded observation timestamp")
    return max(stamps)
