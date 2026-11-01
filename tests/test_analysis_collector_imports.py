"""The collector's analysis modules load no publisher module and import from a shallow package copy.

Adapted from the live project's tests/test_collector_imports.py: there the collector container
mounts src/discoveries alone at /app; here the analysis is the wtf_adsb_engine.analysis package,
which must import from a bare copy of the package without the repository or an installation.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import wtf_adsb_engine

PACKAGE = Path(wtf_adsb_engine.__file__).resolve().parent
# What the collector imports (the collection plugin loads native and export).
COLLECTOR = (
    "app",
    "selection",
    "curation",
    "rules",
    "maneuvers",
    "stories",
    "kinds",
    "enrichment_rules",
    "watchlists",
    "native",
    "export",
    "identity",
)
# Publisher modules, which the engine never carries or loads under any name.
HOST_ONLY = (
    "autopilot",
    "editorial",
    "explain",
    "public_events",
    "public_prose",
    "public_writer",
    "places",
    "story_facts",
    "story_writer",
    "airports",
    "entity_context",
    "ghost_client",
    "weekly_spotlight",
)
IMPORT_ALL = "\n".join(f"import wtf_adsb_engine.analysis.{name}" for name in COLLECTOR)
LOADED = """
import sys
host_only = %r
print(sorted(
    name for name in sys.modules
    if name.rpartition(".")[2] in host_only or name.split(".")[0] == "wtf_adsb_publisher"
))
"""


class CollectorImports(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        shutil.copytree(PACKAGE, cls.root / "wtf_adsb_engine", ignore=shutil.ignore_patterns("__pycache__"))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def run_copy(self, code):
        """Run code in isolated mode from the copy's parent, ahead of any installed package."""
        prelude = f"import sys\nsys.path.insert(0, {str(self.root)!r})\n"
        return subprocess.run(
            [sys.executable, "-I", "-B", "-c", prelude + code],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_the_collector_modules_load_no_publisher_module(self):
        run = self.run_copy(IMPORT_ALL + LOADED % (HOST_ONLY,))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip().splitlines()[-1], "[]")
        for name in HOST_ONLY:
            with self.subTest(module=name):
                self.assertFalse((PACKAGE / "analysis" / f"{name}.py").exists())

    def test_the_collector_modules_import_from_a_shallow_copy_of_the_package(self):
        run = self.run_copy(IMPORT_ALL + "\nimport wtf_adsb_engine\nprint(wtf_adsb_engine.__file__)")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            Path(run.stdout.strip().splitlines()[-1]).resolve(),
            (self.root / "wtf_adsb_engine" / "__init__.py").resolve(),
        )

    def test_the_facts_layer_loads_no_http_client(self):
        # enrichment_rules holds what the facts layer reads, so stories never loads the workers' client.
        run = self.run_copy(
            "import wtf_adsb_engine.analysis.stories\n"
            "print(sorted(m for m in sys.modules if m in ('http.client', 'urllib.request')))"
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip().splitlines()[-1], "[]")


if __name__ == "__main__":
    unittest.main()
