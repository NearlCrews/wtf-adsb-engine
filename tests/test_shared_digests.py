"""The files shared with the other repository match the digests recorded in sync-state.json.

The live project's tools/sync_split.py records the sha256 of each shared copy when it syncs, or
alone with --record-digests; the engine and the publisher must carry identical copies.
"""

import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ("change-batch-v1.schema.json", "manifest-v1.schema.json", "observation-v1.schema.json")
# Each shared file, and the resync task that adds it when it is not here yet.
SHARED = {
    "src/wtf_adsb_engine/wire.py": None,
    "src/wtf_adsb_engine/analysis/kinds.py": "Task 2 of the 2026-10-31 split resync (engine)",
    "src/wtf_adsb_engine/analysis/enrichment_rules.py": "Task 2 of the 2026-10-31 split resync (engine)",
    **{f"src/wtf_adsb_engine/contracts/{name}": None for name in SCHEMAS},
    **{f"contracts/{name}": None for name in SCHEMAS},
}


class SharedDigestTests(unittest.TestCase):
    def test_shared_files_match_the_recorded_digests(self):
        recorded = json.loads((ROOT / "sync-state.json").read_text(encoding="utf-8"))["shared_digests"]
        for path, task in SHARED.items():
            with self.subTest(path=path):
                if task and not (ROOT / path).is_file():
                    self.skipTest(f"{path} arrives with {task}")
                self.assertIn(path, recorded, "record it with tools/sync_split.py --record-digests")
                digest = hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                self.assertEqual(digest, recorded[path], f"{path} differs from its recorded digest")


if __name__ == "__main__":
    unittest.main()
