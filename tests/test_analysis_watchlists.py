"""The runtime watchlist module reads a pinned cache and has no downloader.

Ported from the changed test in the live project's tests/test_watchlists.py, with its cache
fixture: only the import tool, which belongs to the publisher, downloads the list.
"""

import csv
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wtf_adsb_engine.analysis import watchlists

COMMIT = "f" * 40
URL = f"https://raw.githubusercontent.com/sdr-enthusiasts/plane-alert-db/{COMMIT}/plane-alert-db.csv"
ROW = [
    "A12345",
    "N123-AB",
    "Example Foundation",
    "Example aircraft",
    "R44",
    "Civ",
    "Interesting",
    "",
    "",
    "Don't you know who I am?",
    "https://example.org/source",
]


def cache(folder):
    """A parsed one-entry catalog for A12345 / N123-AB, written as the runtime reads it."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(watchlists.CSV_HEADER)
    row = dict(zip(watchlists.CSV_HEADER, ROW, strict=False))
    writer.writerow([row.get(key, "") for key in watchlists.CSV_HEADER])
    data = output.getvalue().encode()
    catalog = watchlists.parse_catalog(
        data,
        URL,
        hashlib.sha256(data).hexdigest(),
        fetched_at="2026-10-04T12:00:00+00:00",
        source_date="2026-09-26T14:04:45Z",
    )
    path = Path(folder) / "current.json"
    path.write_text(json.dumps(catalog))
    return path


class RuntimeWatchlists(unittest.TestCase):
    def tearDown(self):
        watchlists._read_catalog.cache_clear()

    def test_invalid_address_or_absent_cache_returns_no_match_without_network(self):
        self.assertFalse(hasattr(watchlists, "fetch_pinned"))
        never = AssertionError("Runtime must never fetch")
        with (
            tempfile.TemporaryDirectory() as folder,
            patch("urllib.request.urlopen", side_effect=never),
            patch("urllib.request.build_opener", side_effect=never),
        ):
            cached = cache(folder)
            self.assertIsNone(
                watchlists.lookup({"hex": "a12345", "r": "N123AB"}, Path(folder) / "absent.json")
            )
            self.assertIsNone(watchlists.lookup({"hex": "~a12345", "r": "N123AB"}, cached))
            # The same cache matches the valid address, so the refusal above is the address's.
            self.assertIsNotNone(watchlists.lookup({"hex": "a12345", "r": "N123AB"}, cached))


if __name__ == "__main__":
    unittest.main()
