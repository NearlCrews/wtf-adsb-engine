"""wire.privacy refuses exactly what its plain recursive form refused, with the same error.

The reference below is privacy() as it was before the inline scalar checks and the accepted
key and text caches; the test compares verdicts, error text included, over deterministic and
seeded random mutations of a feed-like observation.
"""

import copy
import json
import math
import random
import unittest
from collections import OrderedDict
from pathlib import Path

from test_wire_validator import fixture_observation

from wtf_adsb_engine import wire

ROOT = Path(__file__).resolve().parents[1]


def reference(value, depth=0):
    if depth > 32:
        raise ValueError("Evidence nesting exceeds the limit")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or key.lower() in wire.PRIVATE_KEYS:
                raise ValueError("Private or invalid evidence field")
            reference(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 50000:
            raise ValueError("Evidence array exceeds the limit")
        for item in value:
            reference(item, depth + 1)
    elif isinstance(value, str):
        if len(value) > 20000 or wire.PRIVATE_TEXT.search(value):
            raise ValueError("Private or oversized evidence text")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Nonfinite evidence")
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        raise ValueError("Evidence must be JSON")


def verdict(check, value):
    try:
        check(value)
    except ValueError as error:
        return str(error)
    return "accepted"


class Text(str):
    pass


def nested(depth, leaf):
    for index in range(depth):
        leaf = [leaf] if index % 2 else {"x": leaf}
    return leaf


VALUES = (
    None, True, 0, -1, 1.5, math.nan, math.inf, -math.inf, "", "adsb_icao", "AB\n",
    "see /home/user/x", "https://10.0.0.1/a", "http://receiver.local/", "x.ts.net", "y" * 20001,
    "z" * 65, (1, 2), b"bytes", object(), {1: "x"}, {"RSSI": 1}, {"notes": "x"}, {"Receiver_Lat": 1},
    [math.nan], [None] * 50001, Text("plain"), OrderedDict(a=1), OrderedDict(rssi=1), nested(31, 1),
    nested(32, 1), nested(33, 1), nested(32, {}), nested(32, []), nested(40, "x"),
)  # fmt: skip
KEYS = ("extra", "rssi", "Token", "EXPLANATION", "r_dst", 7, Text("ts"), "ts")


def paths(node, prefix=()):
    if prefix:
        yield prefix
    if isinstance(node, dict):
        for key, item in node.items():
            yield from paths(item, (*prefix, key))
    elif isinstance(node, list):
        for index, item in enumerate(node[:2]):
            yield from paths(item, (*prefix, index))


def mutated(base, path, value, mode):
    result = copy.deepcopy(base)
    parent = result
    for key in path[:-1]:
        parent = parent[key]
    if mode == "key" and isinstance(parent[path[-1]], dict):
        parent[path[-1]][value] = 1
    else:
        parent[path[-1]] = value
    return result


class PrivacyEquivalenceTests(unittest.TestCase):
    def setUp(self):
        example = json.loads((ROOT / "examples/change-batch.example.json").read_text())["changes"][0][
            "observation"
        ]
        self.base = fixture_observation(example, 7)

    def assertSame(self, value, label):
        expected = verdict(reference, value)
        # Twice: the second walk may answer from the accepted key and text caches.
        for _ in range(2):
            self.assertEqual(verdict(wire.privacy, value), expected, label)
        return expected

    def test_every_value_and_key_at_every_path(self):
        refused = 0
        for path in paths(self.base):
            for value in VALUES:
                refused += self.assertSame(mutated(self.base, path, value, "value"), path) != "accepted"
            for key in KEYS:
                refused += self.assertSame(mutated(self.base, path, key, "key"), path) != "accepted"
        self.assertGreater(refused, 1000)

    def test_seeded_random_mutations_with_several_problems(self):
        rng = random.Random(3)
        candidates = list(paths(self.base))
        refused = 0
        for _ in range(3000):
            value = self.base
            for _ in range(rng.randrange(1, 4)):
                path = rng.choice(candidates)
                mode = "key" if rng.random() < 0.3 else "value"
                choice = rng.choice(KEYS if mode == "key" else VALUES)
                try:
                    value = mutated(value, path, choice, mode)
                except KeyError, IndexError, TypeError:
                    continue
            refused += self.assertSame(value, "random") != "accepted"
        self.assertGreater(refused, 1000)

    def test_top_level_values(self):
        for value in (*VALUES, *KEYS, self.base, [self.base], {"a": self.base}):
            self.assertSame(value, repr(value)[:40])


if __name__ == "__main__":
    unittest.main()
