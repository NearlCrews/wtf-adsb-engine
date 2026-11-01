"""Compare wire.validate_observation with the same checks over the complete published schema.

The compiled observation validator leaves three definitions to Python checks (wire._validator).
complete_schema() restores the schema walk over every definition and turns those checks off,
which is how validate_observation behaved before; this tool mutates observations at random and
reports any observation the two verdicts disagree on. It is offline and reads only the feed
batches it is given (by default the example batch). Exit status 1 means a disagreement.

    .venv/bin/python tools/fuzz_wire.py [--feed FEED_DIRECTORY] [--cases 9000] [--seed 1]
"""

import argparse
import copy
import json
import random
import sys
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from jsonschema import Draft202012Validator  # noqa: E402

from wtf_adsb_engine import wire  # noqa: E402

VALUES = (
    None, True, False, 0, -1, 1, 1.0, 1.5, -0.5, 91, -181, 200.0, 10**20, "", "x", "AB", "AB\n",
    "ABCDE", "a" * 161, "y" * 20001, "Barometric", "barometric", "00000000-0000-4000-8000-00000000000Z",
    [], [1], [[[[1]]]], {}, {"x": 1}, {"a": {"b": [None, True]}},
)  # fmt: skip
KEYS = ("extra", "AB\n", "ab", "speed", "parts", "by_finding", "0000")


@contextmanager
def complete_schema():
    """validate_observation as it was before: every definition walked, no Python subtree checks."""

    def validator(name):
        schema = json.loads(files("wtf_adsb_engine").joinpath("contracts", name).read_text())
        if name == "change-batch-v1.schema.json":
            schema["$defs"]["observation"] = {"type": "object"}
        return Draft202012Validator(schema)

    with (
        patch.object(wire, "_validator", validator),
        patch.object(wire, "_novelty", lambda value: None),
        patch.object(wire, "_media", lambda value: None),
    ):
        yield


def verdict(observation):
    """The error validate_observation raises, or "accepted"; never answered from its cache."""
    wire._VALIDATED_OBSERVATIONS.clear()
    try:
        wire.validate_observation(observation)
    except ValueError as error:
        return str(error)
    return "accepted"


def verdicts(observation):
    """(compiled, complete) verdicts for one observation."""
    compiled = verdict(observation)
    with complete_schema():
        return compiled, verdict(observation)


def paths(node, prefix=()):
    if prefix:
        yield prefix
    if isinstance(node, dict):
        for key, item in node.items():
            yield from paths(item, (*prefix, key))
    elif isinstance(node, list):
        for index, item in enumerate(node[:3]):
            yield from paths(item, (*prefix, index))


def mutate(observation, rng):
    """A copy with one value replaced or removed, or an extra field added, anywhere in the tree."""
    mutated = copy.deepcopy(observation)
    path = rng.choice(list(paths(mutated)))
    parent = mutated
    for key in path[:-1]:
        parent = parent[key]
    action = rng.random()
    if action < 0.2 and isinstance(parent, dict):
        del parent[path[-1]]
    elif action < 0.4 and isinstance(parent[path[-1]], dict):
        parent[path[-1]][rng.choice(KEYS)] = copy.deepcopy(rng.choice(VALUES))
    else:
        parent[path[-1]] = copy.deepcopy(rng.choice(VALUES))
    return path, mutated


def observations(feed):
    if feed is None:
        batch = json.loads((ROOT / "examples/change-batch.example.json").read_text())
        return [change["observation"] for change in batch["changes"]]
    found = []
    for path in sorted((Path(feed) / "batches").glob("*.json"))[:5]:
        batch = wire.loads(path.read_bytes())
        found += [change["observation"] for change in batch["changes"] if "observation" in change]
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--feed", help="an engine feed directory with batches/ (default: the example batch)")
    parser.add_argument("--cases", type=int, default=9000)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)
    rng, bases = random.Random(args.seed), observations(args.feed)
    if not bases:
        parser.error("no observations found")
    refused, disagreements = 0, []
    for _ in range(args.cases):
        path, mutated = mutate(rng.choice(bases), rng)
        compiled, complete = verdicts(mutated)
        refused += complete != "accepted"
        if compiled != complete:
            disagreements.append((path, compiled, complete))
    for path, compiled, complete in disagreements[:10]:
        print(f"{'/'.join(map(str, path))}: compiled {compiled!r}, complete {complete!r}")
    print(json.dumps({"cases": args.cases, "refused": refused, "disagreements": len(disagreements)}))
    return 1 if disagreements else 0


if __name__ == "__main__":
    raise SystemExit(main())
