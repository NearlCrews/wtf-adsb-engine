"""Every kind-keyed engine table equals the value, container type, and order the live project recorded.

Ported from the live project's tests/test_kinds_golden.py with the engine's share of its fixture.
"""

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "kinds_golden.py"
FIXTURE = ROOT / "tests" / "fixtures" / "kinds-golden.json"
_spec = importlib.util.spec_from_file_location("kinds_golden", TOOL)
kinds_golden = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kinds_golden)


def seeded_dump(seed):
    """The golden tool's dump in a fresh interpreter under the given string-hashing seed."""
    run = subprocess.run(
        [sys.executable, "-B", str(TOOL)],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONHASHSEED": seed},
        timeout=60,
    )
    return json.loads(run.stdout)


class KindTablesMatchTheRecording(unittest.TestCase):
    def test_every_kind_table_equals_its_recorded_value_under_any_hash_seed(self):
        # This process's own seed and two fixed ones: no table may take its order from string hashing.
        recorded = json.loads(FIXTURE.read_text())
        for label, dumped in (
            ("this process", kinds_golden.dump()),
            ("seed 1", seeded_dump("1")),
            ("seed 2", seeded_dump("2")),
        ):
            for name in sorted(set(recorded) | set(dumped)):
                with self.subTest(name=name, dump=label):
                    self.assertEqual(dumped.get(name), recorded.get(name))

    def test_the_fixture_is_the_tools_own_serialisation(self):
        recorded = json.loads(FIXTURE.read_text())
        self.assertEqual(FIXTURE.read_text(), kinds_golden.serialise(recorded))


if __name__ == "__main__":
    unittest.main()
