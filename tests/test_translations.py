"""Consistency of the translations and icons."""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
import re
from typing import Any

from jura_connect import iter_profiles, load_profile
import yaml

from custom_components.jura_wifi.api import BREW_OPTIONS
from custom_components.jura_wifi.button import MAINTENANCE_PROGRAMS
from custom_components.jura_wifi.settings import (
    KNOWN_SETTINGS,
    setting_platform,
    setting_translation,
)
from custom_components.jura_wifi.status import STATUSES

COMPONENT = Path(__file__).parents[1] / "custom_components" / "jura_wifi"
LANGUAGES = ("en", "de")


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
    for language in LANGUAGES:
        states = _load(f"translations/{language}.json")["entity"]["sensor"]["status"][
            "state"
        ]
        assert set(states) == set(STATUSES), language
    icons = _load("icons.json")["entity"]["sensor"]["status"]["state"]
    assert set(icons) == set(STATUSES)


def _literal_translation_keys() -> set[str]:
    """Return the keys of the messages that the code raises its errors with."""
    keys: set[str] = set()
    for name in ("__init__.py", "api.py", "coordinator.py"):
        source = (COMPONENT / name).read_text(encoding="utf-8")
        keys.update(re.findall(r'translation_key="(\w+)"', source))
        keys.update(re.findall(r'BrewOptionError\(\s*"(\w+)"', source))
    return keys


def test_every_error_the_code_raises_is_translated() -> None:
    """Exceptions with a translation key need a message in every language."""
    used = _literal_translation_keys()
    assert {
        "brew_failed",
        "maintenance_failed",
        "cancel_failed",
        "setting_failed",
        "lock_failed",
        "not_a_brew_button",
        "brew_option_unsupported",
        "brew_option_range",
        "brew_option_step",
        "brew_option_choice",
    } <= used
    for language in LANGUAGES:
        assert used <= set(_load(f"translations/{language}.json")["exceptions"])


def test_every_message_has_the_placeholders_that_the_code_fills() -> None:
    """A message that names a placeholder the code does not give shows it raw."""
    given = {
        "brew_option_unsupported": {"product", "option"},
        "brew_option_range": {"product", "option", "minimum", "maximum"},
        "brew_option_step": {"product", "option", "minimum", "step"},
        "brew_option_choice": {"product", "option", "allowed"},
        "setting_failed": {"setting", "error"},
        "lock_failed": {"error"},
        "not_a_brew_button": set(),
    }
    for language in LANGUAGES:
        messages = _load(f"translations/{language}.json")["exceptions"]
        for key, names in given.items():
            used = set(re.findall(r"{(\w+)}", messages[key]["message"]))
            assert used <= names, (language, key)


def test_every_control_button_has_a_name_and_an_icon() -> None:
    """The buttons are named by translation key, in every language."""
    keys = {"brew", "cancel", *(f"start_{name}" for name in MAINTENANCE_PROGRAMS)}
    for language in LANGUAGES:
        buttons = _load(f"translations/{language}.json")["entity"]["button"]
        assert keys <= set(buttons), language
    assert keys <= set(_load("icons.json")["entity"]["button"])


def test_every_option_is_labelled_and_described() -> None:
    """The options form explains each switch in every language."""
    for language in LANGUAGES:
        translations = _load(f"translations/{language}.json")
        for form in (
            translations["options"]["step"]["init"],
            translations["config"]["step"]["options"],
        ):
            for key in (
                "scan_interval",
                "enable_brewing",
                "enable_maintenance",
                "enable_settings",
            ):
                assert form["data"][key], (language, key)
                assert form["data_description"][key], (language, key)
        setup = translations["config"]["step"]["options"]
        assert setup["data"]["area"], language
        assert setup["data_description"]["area"], language


def test_the_reconfiguration_explains_the_article_number() -> None:
    """The optional field has a label and a hint in every language."""
    for language in LANGUAGES:
        form = _load(f"translations/{language}.json")["config"]["step"]["reconfigure"]
        assert form["data"]["article_number"], language
        assert form["data_description"]["article_number"], language


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
    "article_other_model",
    "unknown",
]


def test_every_error_of_the_config_flow_is_translated_and_used() -> None:
    """A reason that the flow gives needs a text, and a text needs a reason."""
    source = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")
    for language in LANGUAGES:
        errors = _load(f"translations/{language}.json")["config"]["error"]
        assert set(errors) == set(FLOW_ERRORS), language
    for error in FLOW_ERRORS:
        assert f'"{error}"' in source, error


def test_every_setting_of_the_e8_is_named_and_its_values_are_translated() -> None:
    """The entities of the machine settings read as words, not as keys."""
    profile = load_profile("EF1120")
    for language in LANGUAGES:
        entity = _load(f"translations/{language}.json")["entity"]
        for definition in profile.settings:
            platform = setting_platform(definition)
            assert platform is not None, definition.name
            assert definition.name in KNOWN_SETTINGS[platform]
            texts = entity[platform][f"setting_{definition.name}"]
            assert texts["name"], (language, definition.name)
            if platform == "select":
                assert {item.name for item in definition.items} <= set(
                    texts["state"]
                ), (language, definition.name)
        # the generic names that any other setting of any other machine gets
        for platform in ("number", "select", "switch"):
            assert entity[platform]["setting_other"]["name"] == "{setting}"


def test_every_setting_of_every_bundled_profile_is_named_and_has_an_icon() -> None:
    """A machine that declares the hardness as a list, not as a range, is named too.

    The name of an entity depends on the kind of entity it becomes. Without the
    translation of that kind it would be named after the device alone.
    """
    icons = _load("icons.json")["entity"]
    for language in LANGUAGES:
        entity = _load(f"translations/{language}.json")["entity"]
        for profile in iter_profiles():
            for definition in profile.settings:
                platform = setting_platform(definition)
                if platform is None:
                    continue
                where = (language, profile.code, definition.name, platform)
                key, placeholders = setting_translation(definition)
                texts = entity[platform][key]
                assert texts["name"], where
                assert set(re.findall(r"{(\w+)}", texts["name"])) == set(
                    placeholders
                ), where
                assert icons[platform][key]["default"], where
                if platform == "select" and key != "setting_other":
                    assert {item.name for item in definition.items} <= set(
                        texts["state"]
                    ), where


def test_the_hardness_of_a_machine_with_steps_is_a_list() -> None:
    """The ENA 4, E4 and D4 know four (three) hardness steps, not 1 to 30."""
    for code, steps in (("EF1013", 4), ("EF1031", 4), ("EF529", 3)):
        hardness = next(s for s in load_profile(code).settings if s.name == "hardness")
        assert setting_platform(hardness) == "select", code
        assert len(hardness.items) == steps, code
        assert setting_translation(hardness) == ("setting_hardness", {}), code
    # the E8 sets it as a number
    e8 = next(s for s in load_profile("EF1120").settings if s.name == "hardness")
    assert setting_platform(e8) == "number"
    assert setting_translation(e8) == ("setting_hardness", {})


def test_a_setting_without_a_translation_is_named_by_its_profile() -> None:
    """The settings that only some machines have show the name the profile gives."""
    milk_rinsing = next(
        setting
        for profile in iter_profiles()
        for setting in profile.settings
        if setting.name == "milk_rinsing"
    )
    key, placeholders = setting_translation(milk_rinsing)
    assert key == "setting_other"
    assert placeholders == {"setting": milk_rinsing.raw_name}


def test_every_new_entity_has_a_name_in_every_language() -> None:
    """The entities of the machine settings and the new alerts are named."""
    for language in LANGUAGES:
        entity = _load(f"translations/{language}.json")["entity"]
        assert entity["switch"]["front_panel_lock"]["name"]
        for key in (
            "system_fill_needed",
            "tap_open",
            "front_cover_open",
            "machine_error",
            "outlet_missing",
            "rear_cover_missing",
            "water_tank_removal_requested",
            "ventilation_closed",
            "powder_cover_open",
            "filter_detected",
            "keys_locked",
            "remote_screen_active",
            "cleaning_recommended",
            "descaling_recommended",
            "filter_change_recommended",
        ):
            assert entity["binary_sensor"][key]["name"], (language, key)


def test_the_brew_action_is_documented_in_every_language() -> None:
    """The action has a name and a description, and so has each of its fields."""
    definition = yaml.safe_load((COMPONENT / "services.yaml").read_text("utf-8"))
    fields = set(definition["brew"]["fields"])
    assert fields == {option.key for option in BREW_OPTIONS}
    assert definition["brew"]["target"]["entity"] == {
        "integration": "jura_wifi",
        "domain": "button",
    }
    for language in LANGUAGES:
        translations = _load(f"translations/{language}.json")
        brew = translations["services"]["brew"]
        assert brew["name"]
        assert brew["description"]
        assert set(brew["fields"]) == fields, language
        for field in brew["fields"].values():
            assert field["name"]
            assert field["description"]
        # the options of the temperature selector are translated
        key = definition["brew"]["fields"]["temperature"]["selector"]["select"][
            "translation_key"
        ]
        assert set(translations["selector"][key]["options"]) == {
            "low",
            "normal",
            "high",
        }
    assert "brew" in _load("icons.json")["services"]


def test_the_icons_of_the_new_entities() -> None:
    """Entities that no device class gives an icon to have one."""
    icons = _load("icons.json")["entity"]
    for key in ("filter_detected", "keys_locked", "remote_screen_active"):
        assert icons["binary_sensor"][key]["default"], key
    assert icons["switch"]["front_panel_lock"]["state"]["on"]
    assert icons["number"]["setting_hardness"]["default"]
    assert {
        "setting_auto_off",
        "setting_units",
        "setting_language",
        "setting_brewing_mode",
    } <= set(icons["select"])
    assert "setting_quality_assistant" in icons["switch"]


def test_the_last_seen_sensor_left_no_name_and_no_icon_behind() -> None:
    """The time is an attribute of the status sensor now, not an entity."""
    for language in LANGUAGES:
        sensors = _load(f"translations/{language}.json")["entity"]["sensor"]
        assert "last_seen" not in sensors, language
    assert "last_seen" not in _load("icons.json")["entity"]["sensor"]


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
