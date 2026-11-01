#!/usr/bin/env python3
"""Dump every kind-keyed engine table with its container type and order, so a refactor can prove it changed none.

Sets are recorded sorted, dicts as their (key, value) pairs in insertion order, and tuples and
lists in their order. No table may take its order from iterating a set, whose order depends on
string hashing (PYTHONHASHSEED); tests/test_analysis_kinds_golden.py dumps under two seeds to keep
it so. The entries are the engine's share of the live project's recording, with its key names;
the publisher records the tables of its own modules.
"""

import argparse
import json
import sys
from pathlib import Path

from wtf_adsb_engine.analysis import (
    app,
    curation,
    episode_evidence,
    episode_replay,
    flightaware,
    history_backfill,
    maneuvers,
    rules,
    selection,
    stories,
    typecheck,
    wind,
)

# Kinds the probes ask about: every story kind, every collector episode kind, the two
# context kinds the collector and curation name, and one unknown kind.
CONTEXT_ONLY = ("distant", "vfr_code_above_fl180")
UNKNOWN = "no_such_kind"


def probe_kinds():
    return sorted(set(stories.STORY_KINDS) | set(maneuvers.KINDS) | set(CONTEXT_ONLY)) + [UNKNOWN]


def tables():
    return {
        "stories.KINDS": stories.KINDS,
        "stories.SPECIAL_KINDS": stories.SPECIAL_KINDS,
        "stories.EPISODE_KINDS": stories.EPISODE_KINDS,
        "stories.PATTERN_KINDS": stories.PATTERN_KINDS,
        "stories.STORY_KINDS": stories.STORY_KINDS,
        "stories.FIRST_SIGHTING_KINDS": stories.FIRST_SIGHTING_KINDS,
        "stories.RATED_FIELDS": stories.RATED_FIELDS,
        "stories.KIND_READINGS": stories.KIND_READINGS,
        "maneuvers.KINDS": maneuvers.KINDS,
        "maneuvers.PATTERN_KINDS": maneuvers.PATTERN_KINDS,
        "maneuvers.CATEGORIES": maneuvers.CATEGORIES,
        "maneuvers.FRESHNESS_ROBUST_KINDS": maneuvers.FRESHNESS_ROBUST_KINDS,
        "maneuvers.PRIORITY_STATUSES": maneuvers.PRIORITY_STATUSES,
        "maneuvers.STATUS_MEANINGS": maneuvers.STATUS_MEANINGS,
        "maneuvers.STATUS_CODES": maneuvers.STATUS_CODES,
        "maneuvers.STATUS_NAMES": maneuvers.STATUS_NAMES,
        "rules.STATUS_MEANINGS": rules.STATUS_MEANINGS,
        "rules.STATUS_CODES": rules.STATUS_CODES,
        "rules.STATUS_NAMES": rules.STATUS_NAMES,
        "rules.LIVE_SQUAWK_PREFIX": rules.LIVE_SQUAWK_PREFIX,
        "rules.ARCHIVE_SQUAWK_PREFIX": rules.ARCHIVE_SQUAWK_PREFIX,
        "rules.SQUAWK_PREFIXES": rules.SQUAWK_PREFIXES,
        "rules.NORM_KINDS": rules.NORM_KINDS,
        "rules.IDENTITY_LED_KINDS": rules.IDENTITY_LED_KINDS,
        "rules.RATED_KINDS": rules.RATED_KINDS,
        "rules.GROUND_SPEED_KINDS": rules.GROUND_SPEED_KINDS,
        "rules.MEASURED_FIELDS": rules.MEASURED_FIELDS,
        "typecheck.TYPE_DEPENDENT_KINDS": typecheck.TYPE_DEPENDENT_KINDS,
        "typecheck.REGISTRY_TYPE_KINDS": typecheck.REGISTRY_TYPE_KINDS,
        "wind.GROUND_SPEED_KINDS": wind.GROUND_SPEED_KINDS,
        "flightaware.CONTEXT_KINDS": flightaware.CONTEXT_KINDS,
        "curation.NORM_KINDS": curation.NORM_KINDS,
        "curation.ENVELOPE_KINDS": curation.ENVELOPE_KINDS,
        "curation.RECORD_KINDS": curation.RECORD_KINDS,
        "curation.SERIES_KINDS": curation.SERIES_KINDS,
        "curation.PERFORMANCE_TYPE_KINDS": curation.PERFORMANCE_TYPE_KINDS,
        "curation.CONTEXT_KINDS": curation.CONTEXT_KINDS,
        "curation.IDENTITY_KINDS": curation.IDENTITY_KINDS,
        "curation.DATA_QUALITY_KINDS": curation.DATA_QUALITY_KINDS,
        "curation.CIRCUIT_KINDS": curation.CIRCUIT_KINDS,
        "curation.BALLOON_FOLDS": curation.BALLOON_FOLDS,
        "curation.STATUS_EXCEPTIONS": curation.STATUS_EXCEPTIONS,
        "curation.INTEREST": curation.INTEREST,
        "curation.FIRST_KINDS": curation.FIRST_KINDS,
        "curation.REPEAT_RULES kinds": [
            [rule[0], None if rule[2] is None else sorted(rule[2])] for rule in curation.REPEAT_RULES
        ],
        "curation.topic probes": {name: curation.topic(name) for name in probe_kinds()},
        "curation.kind_family probes": {name: curation.kind_family(name) for name in probe_kinds()},
        "selection.NUMERIC_KINDS": selection.NUMERIC_KINDS,
        "selection.NOVELTY_KINDS": selection.NOVELTY_KINDS,
        "selection.FIXED_SCORES": selection.FIXED_SCORES,
        "app.DAILY_KINDS": app.DAILY_KINDS,
        "app.BEST_WINDOW_KINDS": app.BEST_WINDOW_KINDS,
        "app.ABSORBED_KINDS": app.ABSORBED_KINDS,
        "app.Detector.NUMERIC_FIELDS": app.Detector.NUMERIC_FIELDS,
        "app.Detector.WIDE_SPREAD_KINDS": app.Detector.WIDE_SPREAD_KINDS,
        "app.Detector.SIGNED_KINDS": app.Detector.SIGNED_KINDS,
        "episode_replay.DETAIL_KINDS": episode_replay.DETAIL_KINDS,
        "episode_replay.ORBIT_KINDS": episode_replay.ORBIT_KINDS,
        "episode_evidence.MLAT_ESTIMATE_KINDS": episode_evidence.MLAT_ESTIMATE_KINDS,
        "history_backfill.CROSS_DAY_KINDS": history_backfill.CROSS_DAY_KINDS,
    }


def plain(value):
    """Type-tagged, order-preserving form: sets sort their members; dicts keep insertion order as pairs."""
    kind = type(value).__name__
    if isinstance(value, (set, frozenset)):
        return {"type": kind, "value": sorted((plain(item) for item in value), key=json.dumps)}
    if isinstance(value, dict):
        return {"type": kind, "value": [[plain(key), plain(item)] for key, item in value.items()]}
    if isinstance(value, (list, tuple)):
        return {"type": kind, "value": [plain(item) for item in value]}
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    return {"type": kind, "value": repr(value)}


def dump():
    return {name: plain(value) for name, value in tables().items()}


def serialise(data):
    """The fixture text both golden tools write: sorted keys, one-space indent, a final newline."""
    return json.dumps(data, indent=1, sort_keys=True) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", type=Path, help="write the dump here instead of printing it")
    args = parser.parse_args()
    text = serialise(dump())
    if args.write:
        args.write.write_text(text)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
