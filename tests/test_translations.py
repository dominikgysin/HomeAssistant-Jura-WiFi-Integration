"""Consistency of the translations and icons."""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
import re
from typing import Any

from custom_components.jura_wifi.status import STATUSES

COMPONENT = Path(__file__).parents[1] / "custom_components" / "jura_wifi"


def _load(name: str) -> dict[str, Any]:
    return json.loads((COMPONENT / name).read_text(encoding="utf-8"))


def _keys(data: dict[str, Any], prefix: str = "") -> Iterator[str]:
    for key, value in data.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            yield from _keys(value, name)
        else:
            yield name


def test_german_has_every_key_of_the_english_translation() -> None:
    """Neither language misses a string of the other."""
    english = set(_keys(_load("translations/en.json")))
    german = set(_keys(_load("translations/de.json")))
    assert english == german


def test_every_status_is_translated_and_has_an_icon() -> None:
    """A new status needs its label in every language and its icon."""
    for language in ("en", "de"):
        states = _load(f"translations/{language}.json")["entity"]["sensor"]["status"][
            "state"
        ]
        assert set(states) == set(STATUSES), language
    icons = _load("icons.json")["entity"]["sensor"]["status"]["state"]
    assert set(icons) == set(STATUSES)


def test_every_error_the_coordinator_raises_is_translated() -> None:
    """Exceptions with a translation key need a message."""
    used = set(
        re.findall(
            r'translation_key="(\w+)"',
            (COMPONENT / "coordinator.py").read_text(encoding="utf-8"),
        )
    )
    assert used
    for language in ("en", "de"):
        assert used <= set(_load(f"translations/{language}.json")["exceptions"])
