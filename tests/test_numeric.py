"""Invalid numeric measurements remain validation failures, never arithmetic errors."""

import unittest

from wtf_adsb_engine.analysis import airport_context, numeric


class NumericTests(unittest.TestCase):
    def test_finite_rejects_unrepresentable_or_non_numeric_values(self):
        for value in (10**1000, -(10**1000), float("nan"), float("inf"), True, False, "2", None):
            with self.subTest(value=value):
                self.assertFalse(numeric.finite(value))
        for value in (0, -4, 2.5, 1e308):
            with self.subTest(value=value):
                self.assertTrue(numeric.finite(value))

    def test_oversized_loop_measurement_is_rejected(self):
        event = {"measurements": [{"key": "closed_loops", "scope": "analysis-window", "value": 10**1000}]}
        self.assertIsNone(airport_context.analysis_loops(event, []))


if __name__ == "__main__":
    unittest.main()
