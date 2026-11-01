"""The compiled observation validator decides as the complete published schema does.

Ported from the publisher's tests/test_ingest.py (PublishedSchemaTests). wire._validator leaves
jsonValue, context.novelty, and media to Python checks; these tests compare its verdicts with the
complete schema walk (tools/fuzz_wire.complete_schema) over deterministic mutations.
tools/fuzz_wire.py runs the same comparison over seeded random mutations of any feed, offline.
"""

import copy
import importlib.util
import json
import sys
import unittest
from importlib.resources import files
from pathlib import Path

from jsonschema import Draft202012Validator

from wtf_adsb_engine import wire

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("fuzz_wire", ROOT / "tools" / "fuzz_wire.py")
fuzz_wire = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fuzz_wire
SPEC.loader.exec_module(fuzz_wire)
NOW = 1791150000


def _uuid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def fixture_observation(example, number, revision=1):
    """The example observation grown to a feed-like one: several track and media points,
    a day's type counts, and a type norm, all deterministic in `number`."""
    value = copy.deepcopy(example)
    group, finding = value["group"], _uuid(1000 + number)
    start = NOW - 86400 + number * 600
    group.update(id=_uuid(number), first_seen=start, last_seen=start + 300, start=start)
    event = group["events"][0]
    event.update(id=finding, first_seen=start, last_seen=start + 300, hits=2 + revision)
    track = [
        {
            "ts": start + 15 * index,
            "lat": 42 + index / 100,
            "lon": -84 + index / 100,
            "gs": 600 + index,
            "tas": 450,
            "alt_baro": 35000,
            "source": "adsb_icao",
        }
        for index in range(3 + number % 5)
    ]
    event["evidence"].update(track=track)
    event["evidence"]["confirmation"]["samples"] = track[:2]
    value["context"] = {
        "policy_profile": "legacy-collection-v1",
        "novelty": {
            "coverage_days": 30,
            "type_days": {f"T{index:02d}": (index * number) % 31 for index in range(1 + number % 20)},
        },
        "type_norms": {
            finding: None if number % 3 else next(iter(example["context"]["type_norms"].values()))
        },
    }
    value["media"] = {
        "by_finding": {
            finding: {
                "parts": [
                    [
                        {
                            "ts": point["ts"],
                            "lat": point["lat"],
                            "lon": point["lon"],
                            "source": "adsb_icao",
                            "speed": point["gs"],
                            "altitude": None if index % 2 else point["alt_baro"],
                            "altitude_reference": "barometric",
                        }
                        for index, point in enumerate(track)
                    ]
                ]
            }
        }
    }
    return value


class PublishedSchemaTests(unittest.TestCase):
    def setUp(self):
        schema = json.loads(
            files("wtf_adsb_engine").joinpath("contracts/observation-v1.schema.json").read_text()
        )
        self.published = Draft202012Validator(schema)
        example = json.loads((ROOT / "examples/change-batch.example.json").read_text())["changes"][0][
            "observation"
        ]
        self.base = fixture_observation(example, 7)

    @staticmethod
    def accepted(observation):
        try:
            wire.validate_observation(observation)
        except ValueError:
            return False
        return True

    def mutations(self):
        """Each value replaced, removed, or joined by an extra field, through the whole tree."""
        values = (None, True, -1, 1.5, 91, -181, "", "AB\n", "y" * 20001, [], {"x": 1})

        def paths(node, prefix=()):
            if prefix:
                yield prefix
            if isinstance(node, dict):
                for key, item in node.items():
                    yield from paths(item, (*prefix, key))
            elif isinstance(node, list):
                for index, item in enumerate(node[:1]):
                    yield from paths(item, (*prefix, index))

        for path in paths(self.base):
            for value in (*values, "remove", "extra"):
                mutated = copy.deepcopy(self.base)
                parent = mutated
                for key in path[:-1]:
                    parent = parent[key]
                if value == "remove":
                    if not isinstance(parent, dict):
                        continue
                    del parent[path[-1]]
                elif value == "extra":
                    target = parent[path[-1]]
                    if not isinstance(target, dict):
                        continue
                    target["AB\n" if path[-1] == "type_days" else "extra"] = 1
                else:
                    parent[path[-1]] = copy.deepcopy(value)
                yield path, mutated

    def test_no_mutation_the_published_schema_rejects_is_accepted(self):
        self.assertTrue(self.published.is_valid(self.base))
        self.assertTrue(self.accepted(self.base))
        refused = 0
        for path, mutated in self.mutations():
            if not self.published.is_valid(mutated):
                refused += 1
                with self.subTest(path=path):
                    self.assertFalse(self.accepted(mutated))
        # The mutations reach the evidence, novelty, norm, and media rules.
        self.assertGreater(refused, 500)

    def test_the_compiled_validator_agrees_with_the_complete_schema(self):
        for path, mutated in self.mutations():
            compiled, complete = fuzz_wire.verdicts(mutated)
            with self.subTest(path=path):
                self.assertEqual(compiled, complete)


if __name__ == "__main__":
    unittest.main()
