"""Print one version's section of CHANGELOG.md, for use as GitHub release notes."""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def section(text, version):
    """Return the body under `## <version>`, without its heading; a leading `v` is ignored."""
    version = version.removeprefix("v")
    heading = re.compile(rf"^## {re.escape(version)}(?:\s|$)")
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if heading.match(line):
            body = []
            for following in lines[index + 1 :]:
                if following.startswith("## "):
                    break
                body.append(following)
            notes = "\n".join(body).strip()
            if not notes:
                raise LookupError(f"CHANGELOG section {version} is empty")
            return notes + "\n"
    raise LookupError(f"CHANGELOG has no section for {version}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="release version, with or without a leading v")
    parser.add_argument("--changelog", type=Path, default=ROOT / "CHANGELOG.md")
    arguments = parser.parse_args(argv)
    try:
        sys.stdout.write(section(arguments.changelog.read_text(encoding="utf-8"), arguments.version))
    except LookupError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
