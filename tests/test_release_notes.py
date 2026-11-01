"""The release workflow takes its notes from the matching CHANGELOG section."""

import importlib.util
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "release_notes.py"
SPEC = importlib.util.spec_from_file_location("release_notes", SCRIPT)
release_notes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_notes)

CHANGELOG = """# Changelog

## Unreleased

## 0.1.01 (2026-12-01)

Not this one.

## 0.1.0 (2026-11-01)

First release.

### Added

- A thing.

## 0.0.9

Older.
"""


class ReleaseNotesTests(unittest.TestCase):
    def test_prints_only_the_requested_section_without_its_heading(self):
        notes = release_notes.section(CHANGELOG, "0.1.0")
        self.assertEqual(notes, "First release.\n\n### Added\n\n- A thing.\n")

    def test_accepts_a_tag_name(self):
        self.assertEqual(release_notes.section(CHANGELOG, "v0.0.9"), "Older.\n")

    def test_does_not_match_a_longer_version(self):
        with self.assertRaisesRegex(LookupError, "no section for 0.1"):
            release_notes.section(CHANGELOG, "0.1")

    def test_refuses_an_empty_section(self):
        with self.assertRaisesRegex(LookupError, "Unreleased is empty"):
            release_notes.section(CHANGELOG, "Unreleased")

    def test_command_line_exits_nonzero_for_a_missing_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "CHANGELOG.md"
            path.write_text(CHANGELOG, encoding="utf-8")
            command = [sys.executable, "-I", str(SCRIPT), "--changelog", str(path)]
            found = subprocess.run([*command, "v0.1.0"], capture_output=True, text=True, check=True)
            self.assertTrue(found.stdout.startswith("First release."))
            missing = subprocess.run([*command, "v9.9.9"], capture_output=True, text=True)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("no section for 9.9.9", missing.stderr)

    def test_the_changelog_has_notes_for_the_project_version(self):
        version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        notes = release_notes.section((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), version)
        self.assertIn("### Known limits", notes)


if __name__ == "__main__":
    unittest.main()
