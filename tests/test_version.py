"""The version is written down in three places and has to be the same everywhere."""

from __future__ import annotations

from datetime import date
import importlib.util
import json
from pathlib import Path
import re
import tomllib
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[1]
VERSION = re.compile(r"\d+\.\d+\.\d+")
HEADING = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - (\S+)$", re.MULTILINE)
LINK = re.compile(r"^\[([^\]]+)\]: (\S+)$", re.MULTILINE)


def _manifest_version() -> str:
    manifest = ROOT / "custom_components" / "jura_wifi" / "manifest.json"
    return json.loads(manifest.read_text(encoding="utf-8"))["version"]


def _project_version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return project["project"]["version"]


def _changelog() -> str:
    return (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def _released() -> list[tuple[str, str]]:
    """Return the released versions of the changelog with their dates, as listed."""
    return HEADING.findall(_changelog())


def _as_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _release_notes_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "release_notes", ROOT / "scripts" / "release_notes.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_project_and_changelog_name_the_same_version() -> None:
    """A release is made from one version, written in three places."""
    released = _released()
    assert released, "the changelog lists no released version"
    assert _manifest_version() == _project_version() == released[0][0]


def test_the_version_is_a_semantic_one() -> None:
    """Home Assistant and HACS both read the version of the manifest."""
    assert VERSION.fullmatch(_manifest_version())


def test_the_changelog_lists_the_newest_version_first() -> None:
    """Each version appears once, with a valid date, and none is older than the next."""
    released = _released()
    versions = [_as_tuple(version) for version, _ in released]
    assert versions == sorted(set(versions), reverse=True)
    dates = [date.fromisoformat(day) for _, day in released]
    assert dates == sorted(dates, reverse=True)


def test_every_version_of_the_changelog_is_linked_to_its_tag() -> None:
    """The link of a version leads to the tag that the release is made from."""
    links = dict(LINK.findall(_changelog()))
    assert "Unreleased" in links
    for version, _ in _released():
        assert version in links, version
        # a comparison with the version before, or the tag itself for the first one
        assert links[version].endswith(f"v{version}"), version


def test_the_release_notes_are_one_section_of_the_changelog() -> None:
    """The text of a GitHub release is the section of its version, nothing else."""
    script = _release_notes_script()
    released = [version for version, _ in _released()]

    sections = {version: script.section(version) for version in released}
    for version, notes in sections.items():
        assert notes.startswith("### "), version
        assert notes.endswith("\n"), version
        assert "## [" not in notes, version
        assert "]: https://" not in notes, version
    assert len(set(sections.values())) == len(released)


def test_release_notes_of_an_unknown_version() -> None:
    """Asking for a version that was never released is an error, not an empty text."""
    with pytest.raises(SystemExit):
        _release_notes_script().section("99.0.0")
