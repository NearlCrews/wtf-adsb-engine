"""Number parsing shared below rules.py.

rules.py imports limits.py, and limits.py needs the same parsing, so the one
definition lives here; rules re-exports both functions for every other module.
"""

import math


def number(value):
    """A finite float from a number or numeric text, else None (never from a bool)."""
    if type(value) is float:
        # The common case on every per-report path.
        return value if math.isfinite(value) else None
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except TypeError, ValueError, OverflowError:
        return None


def finite(value):
    """A real, finite number: never a bool or numeric text (compare number())."""
    return isinstance(value, (int, float)) and number(value) is not None
