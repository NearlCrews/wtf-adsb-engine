"""The runtime watchlist module reads a pinned cache and has no downloader.

Ported from the changed test in the live project's tests/test_watchlists.py: only the import
tool, which belongs to the publisher, downloads the list.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wtf_adsb_engine.analysis import watchlists


class RuntimeWatchlists(unittest.TestCase):
    def test_invalid_address_or_absent_cache_returns_no_match_without_network(self):
        self.assertFalse(hasattr(watchlists, "fetch_pinned"))
        never = AssertionError("Runtime must never fetch")
        with (
            tempfile.TemporaryDirectory() as folder,
            patch("urllib.request.urlopen", side_effect=never),
            patch("urllib.request.build_opener", side_effect=never),
        ):
            absent = Path(folder) / "absent.json"
            self.assertIsNone(watchlists.lookup({"hex": "a12345", "r": "N123AB"}, absent))
            self.assertIsNone(watchlists.lookup({"hex": "~a12345", "r": "N123AB"}, absent))


if __name__ == "__main__":
    unittest.main()
