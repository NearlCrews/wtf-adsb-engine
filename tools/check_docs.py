"""Check local documentation links and fragments without network access."""

import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"!?\[[^\]\n]*\]\(([^)\n]+)\)")
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")


def outside_fences(text):
    marker = None
    for number, line in enumerate(text.splitlines(), 1):
        fence = FENCE.match(line)
        if fence:
            token = fence[1]
            if marker is None:
                marker = token
            elif token[0] == marker[0] and len(token) >= len(marker):
                marker = None
        elif marker is None:
            yield number, line
    if marker is not None:
        raise ValueError("unclosed code fence")


def anchors(text):
    result, counts = set(), {}
    for _, line in outside_fences(text):
        heading = HEADING.match(line)
        if heading:
            title = re.sub(r"!?\[([^]]*)\]\([^)]*\)", r"\1", heading[1])
            slug = re.sub(r"[^\w -]", "", title.lower()).replace(" ", "-")
            count = counts.get(slug, 0)
            counts[slug] = count + 1
            result.add(f"{slug}-{count}" if count else slug)
    return result


def documents():
    paths = []
    excluded = {".git", ".venv", ".private", "node_modules", "dist", "artifacts", "state", "__pycache__"}
    for directory, children, files in os.walk(ROOT, followlinks=False):
        children[:] = [name for name in children if name not in excluded]
        paths.extend(
            Path(directory) / name
            for name in files
            if name.endswith(".md") and not (Path(directory) / name).is_symlink()
        )
    return sorted(paths)


def main():
    errors = []
    anchor_cache = {}
    paths = documents()
    for path in paths:
        relative = path.relative_to(ROOT)
        try:
            for number, line in outside_fences(path.read_text()):
                for match in LINK.finditer(line):
                    target = urlsplit(match[1].strip().strip("<>"))
                    if target.scheme or target.netloc:
                        continue
                    destination = (path.parent / unquote(target.path)).resolve() if target.path else path
                    if not destination.is_relative_to(ROOT) or not destination.exists():
                        errors.append(f"{relative}:{number}: missing or external local target {match[1]}")
                    elif target.fragment and destination.suffix == ".md":
                        if destination not in anchor_cache:
                            anchor_cache[destination] = anchors(destination.read_text())
                        if unquote(target.fragment) not in anchor_cache[destination]:
                            errors.append(f"{relative}:{number}: missing fragment {match[1]}")
        except ValueError as error:
            errors.append(f"{relative}: {error}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"Documentation checks passed: {len(paths)} Markdown files, local links, fragments, and fences.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
