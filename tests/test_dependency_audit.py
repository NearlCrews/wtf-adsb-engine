"""Dependency audit cases use synthetic registry responses and never access the network."""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "check_dependencies.py"
SPEC = importlib.util.spec_from_file_location("dependency_audit", SCRIPT)
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


class DependencyAuditTests(unittest.TestCase):
    def test_repeated_pins_share_a_check_without_hiding_manifest_version_differences(self):
        pins = [
            audit.Pin("pypi", "example", "1.0.0", "pyproject.toml"),
            audit.Pin("pypi", "example", "1.0.0", "requirements.txt"),
            audit.Pin("pypi", "example", "2.0.0", "requirements-dev.txt"),
        ]

        def check(pin, node_major):
            return {"pinned": pin.version, "status": "current" if pin.version == "2.0.0" else "outdated"}

        with patch.object(audit, "check_pin", side_effect=check) as checked:
            results = audit.check_pins(pins)
        self.assertEqual(checked.call_count, 2)
        self.assertEqual([result["manifest"] for result in results], [pin.source for pin in pins])
        self.assertEqual([result["status"] for result in results], ["outdated", "outdated", "current"])

    def test_skips_prerelease_yanked_and_empty_python_releases(self):
        data = {
            "releases": {
                "1.2.0": [{"yanked": False}],
                "1.3.0": [{"yanked": True}],
                "2.0.0rc1": [{"yanked": False}],
                "1.4.0": [],
            }
        }
        self.assertEqual(audit.newest_python_release(data), "1.2.0")

    def test_rejects_unpinned_or_url_requirements(self):
        for value in ("example>=1.0", "example==1.*", "example @ https://example.org/a.whl"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                audit.python_pin(value, "requirements.txt")

    def test_network_failure_cannot_report_current(self):
        def unavailable(url):
            raise OSError("offline")

        result = audit.check_pin(audit.Pin("npm", "example", "1.0.0", "package.json"), fetch=unavailable)
        self.assertEqual(result["status"], "error")

    def test_node_exception_reports_both_versions(self):
        def registry(url):
            if "nodejs.org" in url:
                return [{"version": "v26.2.0"}, {"version": "v24.3.0"}, {"version": "v24.2.0"}]
            return {"version": "26.2.0" if url.endswith("latest") else "24.3.0"}

        pin = audit.Pin("npm", "node", "24.3.0", "package.json")
        result = audit.check_pin(pin, node_major=24, fetch=registry)
        self.assertEqual(result["status"], "current-compatible")
        self.assertEqual(result["latest"], "26.2.0")
        self.assertEqual(result["expected"], "24.3.0")

    def test_npm_latest_prerelease_is_an_error(self):
        result = audit.check_pin(
            audit.Pin("npm", "example", "1.0.0", "package.json"),
            fetch=lambda url: {"version": "2.0.0-beta.1"},
        )
        self.assertEqual(result["status"], "error")


if __name__ == "__main__":
    unittest.main()
