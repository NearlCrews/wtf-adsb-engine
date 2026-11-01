#!/usr/bin/env python3
"""Record the sha256 of every shared file in sync-state.json.

The files listed under shared_digests are byte-identical with the publisher repository's
copies. After changing one, change the other repository the same way and run this tool
in both. It rewrites the shared_digests values and the dest_sha256 of every module whose
dest is a shared path, and adds the provenance note when it is missing; every other byte
of the file stays as it was. Running it again changes nothing.

The tool only refreshes paths already listed under shared_digests. Add a new shared path
to that object by hand, then run the tool to record its digest.
"""

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "sync-state.json"
KEY = "shared_digests"
NOTE = "Shared files were extracted from a private predecessor project; the commit ids refer to its history."


def digest(root, path):
    try:
        return hashlib.sha256((root / path).read_bytes()).hexdigest()
    except OSError as error:
        raise SystemExit(f"Cannot read shared file {path}: {error.strerror or error}") from None


def replace_once(text, span, pattern, new, what, name):
    """Replace the single match of pattern's value inside text[span] with the JSON string new."""
    start, end = span
    part, count = re.subn(pattern, lambda match: match[1] + json.dumps(new), text[start:end])
    if count != 1:
        raise SystemExit(f"{name}: expected one {what}, found {count}")
    return text[:start] + part + text[end:]


def module_span(text, module, name):
    """The character span of a flat module object in the original text."""
    match = re.search(f"{re.escape(json.dumps(module))}:\\s*\\{{[^{{}}]*\\}}", text)
    if not match:
        raise SystemExit(f"{name}: cannot find module {module}")
    return match.span()


def record(state_path=STATE, root=ROOT):
    """Rewrite the recorded digests in place and return the paths whose digest changed."""
    name = state_path.name
    try:
        text = state_path.read_text(encoding="utf-8")
    except OSError as error:
        raise SystemExit(f"Cannot read {name}: {error.strerror or error}") from None
    state = json.loads(text)
    if not isinstance(state.get(KEY), dict):
        raise SystemExit(f"{name}: missing the {KEY} object; add the shared paths by hand")
    recorded = state[KEY]
    current = {path: digest(root, path) for path in recorded}
    modules = state.get("modules") or {}
    changed = []
    for path, old in recorded.items():
        new = current[path]
        if new != old:
            pattern = f"({re.escape(json.dumps(path))}:\\s*){re.escape(json.dumps(old))}"
            text = replace_once(text, (0, len(text)), pattern, new, f"digest entry for {path}", name)
            changed.append(path)
    for module, entry in modules.items():
        if entry.get("dest") not in recorded:
            continue
        new, old = current[entry["dest"]], entry.get("dest_sha256")
        if new == old:
            continue
        pattern = f"({re.escape(json.dumps('dest_sha256'))}:\\s*){re.escape(json.dumps(old))}"
        text = replace_once(
            text, module_span(text, module, name), pattern, new, f"dest_sha256 in {module}", name
        )
        if entry["dest"] not in changed:
            changed.append(entry["dest"])
    if "note" not in state:
        opening = re.match(r"\{(\s*)", text)
        if not opening:
            raise SystemExit(f"{name}: expected a JSON object")
        indent = opening[1]
        text = f"{{{indent}{json.dumps('note')}: {json.dumps(NOTE)},{indent}" + text[opening.end() :]
    expected = dict(state, **({} if "note" in state else {"note": NOTE}))
    expected[KEY] = current
    if modules:
        expected["modules"] = {
            module: dict(entry, dest_sha256=current[entry["dest"]])
            if entry.get("dest") in recorded
            else entry
            for module, entry in modules.items()
        }
    if json.loads(text) != expected:
        raise SystemExit(f"{name}: the rewrite changed more than the digests")
    state_path.write_text(text, encoding="utf-8")
    return changed


def main():
    changed = record()
    for path in changed:
        print(f"recorded {path}")
    if not changed:
        print("shared digests already current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
