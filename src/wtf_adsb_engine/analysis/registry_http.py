"""The enrichment workers' shared HTTP client, backoff gate and loop.

typecheck.py asks adsbdb and hexdb about an address; wind.py asks Open-Meteo
for upper winds. Each request is one bounded GET for a
small JSON answer that never follows a redirect. A failure backs the worker
off: six hours after a refusal (HTTP 403 or 429), 30 minutes after anything
else. The live workers keep their state in the journal's meta table under
their own name ("<name>_backoff_until", "<name>_last_request",
"<name>_last_success" and "<name>_status") and log under it. flightaware.py
keeps its own authenticated client but shares the redirect guard, the backoff
rule and the loop.
"""

import json
import logging
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .numeric import number

USER_AGENT = "wtf-adsb-engine/0.1.0"
TIMEOUT = 10
NETWORK_BACKOFF = 30 * 60
REFUSAL_BACKOFF = 6 * 3600
REFUSALS = frozenset({403, 429})


class Unavailable(Exception):
    """A service could not answer: network trouble, a server error, a refusal, or a body
    that was oversized or not JSON. ``status`` is the HTTP status, when there was one."""

    def __init__(self, status=None):
        super().__init__("Service unavailable")
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        # A redirect target is not the service's answer, and never receives a credential.
        return None


def get_json(url, opener=None, *, max_bytes, absent=frozenset(), timeout=TIMEOUT):
    """The JSON value a GET returns, or None for an empty body or an HTTP status in `absent`.

    Raises Unavailable for any other failure, a body over max_bytes, or one that
    is not JSON. The error text of a failed response is never read.
    """
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, method="GET")
    opener = opener or build_opener(NoRedirect()).open
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
    except HTTPError as error:
        error.close()
        if error.code in absent:
            return None
        raise Unavailable(error.code) from None
    except (URLError, OSError, ValueError) as error:
        raise Unavailable() from error
    if len(raw) > max_bytes:
        raise Unavailable()
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError, UnicodeError:
        raise Unavailable() from None


def backoff_delay(status, *, refusals=REFUSALS, refusal=REFUSAL_BACKOFF, network=NETWORK_BACKOFF):
    """Seconds to wait after a failure: `refusal` after a status in `refusals`, else `network`."""
    return refusal if status in refusals else network


def waiting(db, name, now, interval):
    """Why the worker may not send a request now ("backoff" or "rate_limited"), or None.

    Only reads, so a pass can decide before it writes anything.
    """
    from .app import meta

    if now < (number(meta(db, f"{name}_backoff_until")) or 0):
        return "backoff"
    if now - (number(meta(db, f"{name}_last_request")) or 0) < interval:
        return "rate_limited"
    return None


def requested(db, name, now):
    """Record a request about to go out; waiting() rate-limits the next one from it."""
    from .app import put_meta

    put_meta(db, f"{name}_last_request", now)


def failed(db, name, now, status, service):
    """Back the worker off after a failed request and say so in its status and log."""
    from .app import put_meta

    put_meta(db, f"{name}_backoff_until", now + backoff_delay(status))
    put_meta(db, f"{name}_status", f"{service} unavailable (HTTP {status or 'none'})")
    logging.getLogger(name).warning("%s unavailable (HTTP %s)", service, status or "unknown")


def succeeded(db, name, now, status):
    from .app import put_meta

    put_meta(db, f"{name}_last_success", now)
    put_meta(db, f"{name}_status", status)


def status(db, name, enabled, waiting_status):
    """The collector status block of a worker: on or off, its latest status and success."""
    from .app import meta

    return {
        "enabled": enabled,
        "status": meta(db, f"{name}_status", waiting_status) if enabled else "Disabled",
        "last_success": meta(db, f"{name}_last_success"),
    }


def loop(name, label, run_once, path, pause):
    """Run a worker's passes forever, sleeping pause(result) seconds after each.

    A pass that raises is logged under the worker's name and counts as
    {"state": "error"}: the worker must outlive one bad row.
    """
    log = logging.getLogger(name)
    while True:
        try:
            result = run_once(path)
        except Exception as error:  # noqa: BLE001 - the worker must outlive one bad row
            log.warning("%s unavailable (%s)", label, type(error).__name__)
            result = {"state": "error"}
        time.sleep(pause(result))
