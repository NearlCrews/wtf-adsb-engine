"""Pinned reference loading enforces byte bounds before parsing or hashing."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wtf_adsb_engine.analysis import reference


class PinnedReferenceTests(unittest.TestCase):
    def test_exact_size_limit_accepts_valid_pinned_content(self):
        content = {"schema_version": 1, "label": "synthetic"}
        data = {**content, "content_sha256": reference.canonical_hash(content)}
        raw = json.dumps(data).encode()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reference.json"
            path.write_bytes(raw)
            self.assertEqual(reference.pinned_json(path, max_bytes=len(raw)), data)
            with self.assertRaisesRegex(ValueError, "size bound"):
                reference.pinned_json(path, max_bytes=len(raw) - 1)

    def test_oversized_reference_is_bounded_and_its_file_is_closed(self):
        limit = 64

        class BoundedReader(io.BytesIO):
            def read(self, size=-1):
                if not 0 <= size <= limit + 1:
                    raise AssertionError("Reference read exceeded its memory bound")
                return super().read(size)

        source = BoundedReader(b"x" * (limit * 100))
        with patch.object(Path, "open", return_value=source):
            with self.assertRaisesRegex(ValueError, "size bound"):
                reference.pinned_json("synthetic.json", max_bytes=limit)
        self.assertTrue(source.closed)


if __name__ == "__main__":
    unittest.main()
