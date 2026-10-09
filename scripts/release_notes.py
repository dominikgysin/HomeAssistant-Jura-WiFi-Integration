"""Print the changelog section of a version, as the text of a GitHub release.

Used to create a release in one go::

    python scripts/release_notes.py 0.3.0 | gh release create v0.3.0 --title v0.3.0 --notes-file -
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

CHANGELOG = Path(__file__).parents[1] / "CHANGELOG.md"


def section(version: str, text: str | None = None) -> str:
    """Return the body of the changelog section of ``version``."""
    if text is None:
        text = CHANGELOG.read_text(encoding="utf-8")
    # The section ends at the next version heading or at the link definitions.
    match = re.search(
        rf"^## \[{re.escape(version)}\] - [^\n]*\n(.*?)(?=^## \[|^\[[^\]]+\]: |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise SystemExit(f"CHANGELOG.md has no section for version {version}")
    return match.group(1).strip() + "\n"


def main() -> None:
    """Print the section of the version given on the command line."""
    if len(sys.argv) != 2:
        raise SystemExit("usage: release_notes.py <version>, for example 0.3.0")
    print(section(sys.argv[1]), end="")


if __name__ == "__main__":
    main()
