"""Consistency of the translations and icons."""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
import re
from typing import Any

from custom_components.jura_wifi.button import MAINTENANCE_PROGRAMS
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
    assert {"brew_failed", "maintenance_failed", "cancel_failed"} <= used
    for language in ("en", "de"):
        assert used <= set(_load(f"translations/{language}.json")["exceptions"])


def test_every_control_button_has_a_name_and_an_icon() -> None:
    """The buttons are named by translation key, in every language."""
    keys = {"brew", "cancel", *(f"start_{name}" for name in MAINTENANCE_PROGRAMS)}
    for language in ("en", "de"):
        buttons = _load(f"translations/{language}.json")["entity"]["button"]
        assert keys <= set(buttons), language
    assert keys <= set(_load("icons.json")["entity"]["button"])


def test_every_option_is_labelled_and_described() -> None:
    """The options form explains each switch in every language."""
    for language in ("en", "de"):
        form = _load(f"translations/{language}.json")["options"]["step"]["init"]
        for key in ("scan_interval", "enable_brewing", "enable_maintenance"):
            assert form["data"][key], (language, key)
            assert form["data_description"][key], (language, key)


# The reasons the config flow can give for a failed pairing or address.
FLOW_ERRORS = [
    "cannot_connect",
    "invalid_auth",
    "pairing_timeout",
    "pairing_rejected",
    "machine_in_menu",
    "machine_busy",
    "wrong_pin",
    "unknown_article",
    "unknown",
]


def test_every_error_of_the_config_flow_is_translated_and_used() -> None:
    """A reason that the flow gives needs a text, and a text needs a reason."""
    source = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")
    for language in ("en", "de"):
        errors = _load(f"translations/{language}.json")["config"]["error"]
        assert set(errors) == set(FLOW_ERRORS), language
    for error in FLOW_ERRORS:
        assert f'"{error}"' in source, error


def test_everything_that_asks_to_pair_says_to_leave_the_settings_menu() -> None:
    """Pairing fails while the machine is in its menu, so the hint has to be there.

    It is the one mistake that is easy to make: the machine cannot show the prompt
    in the menu and refuses the connection.
    """
    hints = {"en": "settings menu", "de": "Einstellungsmenü"}
    for language, hint in hints.items():
        config = _load(f"translations/{language}.json")["config"]
        texts = {
            "first form": config["step"]["user"]["description"],
            "while waiting": config["progress"]["pair"],
            "after a failure": config["step"]["pair_failed"]["description"],
            "pairing again": config["step"]["reauth_confirm"]["description"],
            "refused": config["error"]["pairing_rejected"],
            "not confirmed": config["error"]["pairing_timeout"],
            "detected": config["error"]["machine_in_menu"],
        }
        for where, text in texts.items():
            assert hint in text, (language, where)
