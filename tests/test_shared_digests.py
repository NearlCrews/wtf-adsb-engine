"""The files shared with the publisher repository match the digests recorded in sync-state.json.

tools/record_shared_digests.py records the sha256 of each shared copy; the engine and the
publisher must carry identical copies.
"""

import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ("change-batch-v1.schema.json", "manifest-v1.schema.json", "observation-v1.schema.json")
SHARED = (
    "src/wtf_adsb_engine/wire.py",
    "src/wtf_adsb_engine/analysis/kinds.py",
    "src/wtf_adsb_engine/analysis/enrichment_rules.py",
    *(f"src/wtf_adsb_engine/contracts/{name}" for name in SCHEMAS),
    *(f"contracts/{name}" for name in SCHEMAS),
)


class SharedDigestTests(unittest.TestCase):
    def test_shared_files_match_the_recorded_digests(self):
        recorded = json.loads((ROOT / "sync-state.json").read_text(encoding="utf-8"))["shared_digests"]
        self.assertEqual(set(recorded), set(SHARED), "every shared file has a recorded digest")
        for path in SHARED:
            with self.subTest(path=path):
                digest = hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                self.assertEqual(digest, recorded[path], f"{path} differs from its recorded digest")


if __name__ == "__main__":
    unittest.main()
