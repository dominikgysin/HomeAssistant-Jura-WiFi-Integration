"""The machine settings, and the front panel lock that 0.5.3 removed."""

from __future__ import annotations

import dataclasses
from typing import Any
from unittest.mock import MagicMock

from jura_connect import load_profile
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jura_wifi.api import (
    JuraWifiAuthError,
    JuraWifiBusy,
    JuraWifiConnectionError,
    JuraWifiError,
    MachineActivity,
    MachineSnapshot,
)
from custom_components.jura_wifi.const import (
    CONF_ENABLE_SETTINGS,
    DOMAIN,
    SETTINGS_REFRESH_SECONDS,
)
from custom_components.jura_wifi.settings import (
    number_to_wire,
    setting_platform,
    stored_item,
    stored_number,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from .common import (
    E8_SETTINGS,
    call,
    entity_id,
    poll,
    registered_keys,
    setup_with_options,
    state,
)
from .conftest import SNAPSHOT, setup_entry

SETTING_KEYS = {
    "number": {"setting_hardness"},
    "select": {
        "setting_auto_off",
        "setting_units",
        "setting_language",
        "setting_brewing_mode",
    },
    "switch": {"setting_quality_assistant"},
}


class FakeMachine:
    """The settings of a machine, read and written like the client does."""

    def __init__(self, settings: dict[str, str]) -> None:
        """Start with the settings that the machine has stored."""
        self.settings = dict(settings)
        self.writes: list[tuple[str, str]] = []

    def fetch(self, with_settings: bool = False) -> MachineSnapshot:
        """Answer a poll; the settings only when they are asked for."""
        return dataclasses.replace(
            SNAPSHOT,
            active_alerts=frozenset({"coffee_ready"}),
            settings=dict(self.settings) if with_settings else None,
        )

    def write_setting(self, p_argument: str, value: str) -> str:
        """Store a value; the machine reads it back without its marker bytes."""
        self.writes.append((p_argument, value))
        self.settings[p_argument] = value[2:] if len(value) > 2 else value
        return self.settings[p_argument]


@pytest.fixture
def machine(mock_client: MagicMock) -> FakeMachine:
    """Let the mocked client behave like a machine with the settings of an E8."""
    fake = FakeMachine(E8_SETTINGS)
    mock_client.fetch.side_effect = fake.fetch
    mock_client.write_setting.side_effect = fake.write_setting
    return fake


async def _with_settings(
    hass: HomeAssistant, entry: MockConfigEntry, **options: Any
) -> None:
    await setup_with_options(hass, entry, **{CONF_ENABLE_SETTINGS: True, **options})


async def _settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)


def _reads(mock_client: MagicMock) -> list[bool]:
    """Return for every poll whether it read the settings."""
    return [bool(poll_call.args[0]) for poll_call in mock_client.fetch.call_args_list]


async def test_no_settings_without_the_option(
    hass: HomeAssistant,
    mock_client: MagicMock,
    machine: FakeMachine,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """Writing to the machine is an opt-in, and nothing is read for it either."""
    await setup_entry(hass, mock_config_entry)
    await poll(hass, freezer)

    for platform in ("number", "select", "switch"):
        assert registered_keys(hass, mock_config_entry, platform) == set()
    assert _reads(mock_client) == [False, False]


async def test_the_settings_of_the_e8_become_entities(
    hass: HomeAssistant,
    mock_client: MagicMock,
    machine: FakeMachine,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Only what the profile declares: the six settings of the E8."""
    await _with_settings(hass, mock_config_entry)

    registry = er.async_get(hass)
    for platform, keys in SETTING_KEYS.items():
        assert registered_keys(hass, mock_config_entry, platform) == keys
        for key in keys:
            entry = registry.async_get(
                entity_id(hass, platform, mock_config_entry, key)
            )
            assert entry is not None
            assert entry.entity_category is EntityCategory.CONFIG
            assert entry.disabled_by is None


async def test_the_states_are_what_the_machine_stores(
    hass: HomeAssistant,
    mock_client: MagicMock,
    machine: FakeMachine,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Hardness 16, switch off after 30 minutes, ml, English, ask, assistant on."""
    await _with_settings(hass, mock_config_entry)

    hardness = state(hass, "number", mock_config_entry, "setting_hardness")
    assert float(hardness.state) == 16
    assert hardness.attributes["min"] == 1
    assert hardness.attributes["max"] == 30
    assert hardness.attributes["step"] == 1
    assert state(hass, "select", mock_config_entry, "setting_auto_off").state == "30min"
    assert state(hass, "select", mock_config_entry, "setting_units").state == "ml"
    assert state(hass, "select", mock_config_entry, "setting_language").state == (
        "english"
    )
    assert state(hass, "select", mock_config_entry, "setting_brewing_mode").state == (
        "ask"
    )
    assert state(
        hass, "switch", mock_config_entry, "setting_quality_assistant"
    ).state == (STATE_ON)

    auto_off = state(hass, "select", mock_config_entry, "setting_auto_off")
    assert auto_off.attributes["options"] == [
        "15min",
        "30min",
        "1h",
        "2h",
        "3h",
        "4h",
        "5h",
        "6h",
        "7h",
        "8h",
        "9h",
    ]
    assert state(hass, "select", mock_config_entry, "setting_units").attributes[
        "options"
    ] == ["ml", "oz"]
    languages = state(hass, "select", mock_config_entry, "setting_language")
    assert len(languages.attributes["options"]) == 11


@pytest.mark.parametrize(
    ("raw", "option"),
    [("0F", "15min"), ("3C", "1h"), ("B4", "3h"), ("012C", "5h"), ("021C", "9h")],
)
async def test_the_switch_off_time_is_read_back_without_its_marker(
    hass: HomeAssistant,
    mock_client: MagicMock,
    machine: FakeMachine,
    mock_config_entry: MockConfigEntry,
    raw: str,
    option: str,
) -> None:
    """The machine stores 3C where the write said 213C."""
    machine.settings["13"] = raw
    await _with_settings(hass, mock_config_entry)

    assert state(hass, "select", mock_config_entry, "setting_auto_off").state == option


async def test_the_settings_are_read_at_the_first_poll_and_every_ten_minutes(
    hass: HomeAssistant,
    mock_client: MagicMock,
    machine: FakeMachine,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """Settings rarely change, so a poll does not ask for them each time."""
    await _with_settings(hass, mock_config_entry)
    assert _reads(mock_client) == [True]

    polls = SETTINGS_REFRESH_SECONDS // 61
    for _ in range(polls):
        await poll(hass, freezer)
    assert _reads(mock_client) == [True] + [False] * polls

    await poll(hass, freezer)
    assert _reads(mock_client)[-1] is True
    await poll(hass, freezer)
    assert _reads(mock_client)[-1] is False
    # the values stay between the polls that do not read them
    assert (
        float(state(hass, "number", mock_config_entry, "setting_hardness").state) == 16
    )


async def test_a_setting_that_changed_on_the_machine_is_found_at_the_next_read(
    hass: HomeAssistant,
    mock_client: MagicMock,
    machine: FakeMachine,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """Someone used the display of the machine, or the app."""
    await _with_settings(hass, mock_config_entry)

    machine.settings["02"] = "14"
    await poll(hass, freezer)
    assert (
        float(state(hass, "number", mock_config_entry, "setting_hardness").state) == 16
    )

    await poll(hass, freezer, seconds=SETTINGS_REFRESH_SECONDS)
    assert (
        float(state(hass, "number", mock_config_entry, "setting_hardness").state) == 20
    )


async def test_writing_a_number(
    hass: HomeAssistant,
    mock_client: MagicMock,
    machine: FakeMachine,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The hardness is written in hex, and read again afterwards."""
    await _with_settings(hass, mock_config_entry)
    reads = len(mock_client.fetch.call_args_list)
    number = entity_id(hass, "number", mock_config_entry, "setting_hardness")

    await call(hass, "number", "set_value", number, value=12)
    await _settle(hass)

    assert machine.writes == [("02", "0C")]
    assert (
        float(state(hass, "number", mock_config_entry, "setting_hardness").state) == 12
    )
    # the poll that follows reads the settings again
    assert len(mock_client.fetch.call_args_list) == reads + 1
    assert mock_client.fetch.call_args_list[-1].args == (True,)


@pytest.mark.parametrize(
    ("key", "option", "written", "shown"),
    [
        ("setting_auto_off", "1h", ("13", "213C"), "1h"),
        ("setting_auto_off", "15min", ("13", "0F"), "15min"),
        ("setting_auto_off", "5h", ("13", "22012C"), "5h"),
        ("setting_units", "oz", ("08", "01"), "oz"),
        ("setting_language", "german", ("09", "01"), "german"),
        ("setting_brewing_mode", "sweet", ("65", "10"), "sweet"),
    ],
)
async def test_writing_a_list_setting(
    hass: HomeAssistant,
    mock_client: MagicMock,
    machine: FakeMachine,
    mock_config_entry: MockConfigEntry,
    key: str,
    option: str,
    written: tuple[str, str],
    shown: str,
) -> None:
    """The value of the item goes to the machine, and the machine's answer is shown."""
    await _with_settings(hass, mock_config_entry)

    await call(
        hass,
        "select",
        "select_option",
        entity_id(hass, "select", mock_config_entry, key),
        option=option,
    )
    await _settle(hass)

    assert machine.writes == [written]
    assert state(hass, "select", mock_config_entry, key).state == shown


async def test_writing_a_switch(
    hass: HomeAssistant,
    mock_client: MagicMock,
    machine: FakeMachine,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The quality assistant is turned off and on again."""
    await _with_settings(hass, mock_config_entry)
    assistant = entity_id(
        hass, "switch", mock_config_entry, "setting_quality_assistant"
    )

    await call(hass, "switch", "turn_off", assistant)
    await _settle(hass)
    assert state(
        hass, "switch", mock_config_entry, "setting_quality_assistant"
    ).state == (STATE_OFF)

    await call(hass, "switch", "turn_on", assistant)
    await _settle(hass)
    assert state(
        hass, "switch", mock_config_entry, "setting_quality_assistant"
    ).state == (STATE_ON)
    assert machine.writes == [("7E", "00"), ("7E", "01")]


async def test_the_machine_has_to_be_online_for_a_write(
    hass: HomeAssistant,
    mock_client: MagicMock,
    machine: FakeMachine,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A machine that is switched off cannot take a setting."""
    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    await _with_settings(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data
    definition = next(s for s in coordinator.profile.settings if s.name == "hardness")

    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_setting(definition, "0C", "Hardness")

    mock_client.write_setting.assert_not_called()
    assert (
        state(hass, "number", mock_config_entry, "setting_hardness").state
        == STATE_UNAVAILABLE
    )


@pytest.mark.parametrize("activity", ["brewing", "maintenance", "programming", "busy"])
async def test_the_machine_has_to_be_idle_for_a_write(
    hass: HomeAssistant,
    mock_client: MagicMock,
    machine: FakeMachine,
    mock_config_entry: MockConfigEntry,
    freezer,
    activity: str,
) -> None:
    """A machine that is brewing or in its menu is not asked to change a setting."""
    await _with_settings(hass, mock_config_entry)
    mock_client.fetch.side_effect = JuraWifiBusy(MachineActivity(activity))
    await poll(hass, freezer)

    with pytest.raises(ServiceValidationError):
        await call(
            hass,
            "number",
            "set_value",
            entity_id(hass, "number", mock_config_entry, "setting_hardness"),
            value=12,
        )

    mock_client.write_setting.assert_not_called()


async def test_a_refused_write_is_reported_and_the_value_stays(
    hass: HomeAssistant,
    mock_client: MagicMock,
    machine: FakeMachine,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The machine answers an error; the entity does not pretend otherwise."""
    await _with_settings(hass, mock_config_entry)
    mock_client.write_setting.side_effect = JuraWifiError("the machine refused 02")

    with pytest.raises(HomeAssistantError, match="the machine refused 02"):
        await call(
            hass,
            "number",
            "set_value",
            entity_id(hass, "number", mock_config_entry, "setting_hardness"),
            value=12,
        )

    assert (
        float(state(hass, "number", mock_config_entry, "setting_hardness").state) == 16
    )


async def test_a_lost_connection_while_writing_counts_towards_offline(
    hass: HomeAssistant,
    mock_client: MagicMock,
    machine: FakeMachine,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The same two-strike rule as for the other commands."""
    await _with_settings(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data
    mock_client.write_setting.side_effect = JuraWifiConnectionError("peer closed")
    definition = next(s for s in coordinator.profile.settings if s.name == "hardness")

    with pytest.raises(HomeAssistantError, match="peer closed"):
        await coordinator.async_set_setting(definition, "0C", "Hardness")
    assert coordinator.data.online

    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_setting(definition, "0C", "Hardness")
    assert not coordinator.data.online


async def test_rejected_credentials_while_writing_ask_for_pairing(
    hass: HomeAssistant,
    mock_client: MagicMock,
    machine: FakeMachine,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A write is a session like any other: wrong credentials start the repair."""
    await _with_settings(hass, mock_config_entry)
    mock_client.write_setting.side_effect = JuraWifiAuthError("WRONG_HASH")

    with pytest.raises(HomeAssistantError):
        await call(
            hass,
            "number",
            "set_value",
            entity_id(hass, "number", mock_config_entry, "setting_hardness"),
            value=12,
        )

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_nothing_is_shown_while_the_machine_is_off(
    hass: HomeAssistant,
    mock_client: MagicMock,
    machine: FakeMachine,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """The settings are not part of what is kept for a machine that is off."""
    await _with_settings(hass, mock_config_entry)
    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    await poll(hass, freezer)
    await poll(hass, freezer)

    for platform, keys in SETTING_KEYS.items():
        for key in keys:
            assert state(hass, platform, mock_config_entry, key).state == (
                STATE_UNAVAILABLE
            ), key


async def test_a_setting_that_the_machine_does_not_report_is_unavailable(
    hass: HomeAssistant,
    mock_client: MagicMock,
    machine: FakeMachine,
    mock_config_entry: MockConfigEntry,
) -> None:
    """One setting without a value does not take the others with it."""
    del machine.settings["65"]
    machine.settings["08"] = "FF"
    machine.settings["02"] = ""
    machine.settings["7E"] = "05"
    await _with_settings(hass, mock_config_entry)

    for platform, key in (
        ("select", "setting_brewing_mode"),
        ("select", "setting_units"),
        ("number", "setting_hardness"),
        ("switch", "setting_quality_assistant"),
    ):
        assert state(hass, platform, mock_config_entry, key).state == STATE_UNAVAILABLE
    assert (
        state(hass, "select", mock_config_entry, "setting_language").state == "english"
    )
    assert state(hass, "select", mock_config_entry, "setting_auto_off").state == "30min"


async def test_a_number_outside_its_range_is_not_a_value(
    hass: HomeAssistant,
    mock_client: MagicMock,
    machine: FakeMachine,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The hardness is 1 to 30."""
    machine.settings["02"] = "FF"
    await _with_settings(hass, mock_config_entry)

    assert (
        state(hass, "number", mock_config_entry, "setting_hardness").state
        == STATE_UNAVAILABLE
    )


async def test_a_machine_gets_only_the_settings_its_profile_declares(
    hass: HomeAssistant,
    mock_client: MagicMock,
    machine: FakeMachine,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The E4 declares four settings: no brewing mode, no quality assistant."""
    mock_client.profile = load_profile("EF1089")
    await _with_settings(hass, mock_config_entry)

    assert registered_keys(hass, mock_config_entry, "number") == {"setting_hardness"}
    assert registered_keys(hass, mock_config_entry, "select") == {
        "setting_units",
        "setting_language",
        "setting_auto_off",
    }
    assert registered_keys(hass, mock_config_entry, "switch") == set()


async def test_a_hardness_with_steps_is_a_named_select(
    hass: HomeAssistant,
    mock_client: MagicMock,
    machine: FakeMachine,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The E4 knows four steps instead of 1 to 30; it is still the water hardness."""
    profile = load_profile("EF1031")
    mock_client.profile = profile
    hardness = next(s for s in profile.settings if s.name == "hardness")
    machine.settings = {hardness.p_argument.upper(): "08"}
    await _with_settings(hass, mock_config_entry)

    assert registered_keys(hass, mock_config_entry, "number") == set()
    assert "setting_hardness" in registered_keys(hass, mock_config_entry, "select")
    select = state(hass, "select", mock_config_entry, "setting_hardness")
    assert select.state == "step_2"
    assert select.attributes["options"] == ["step_1", "step_2", "step_3", "step_4"]
    entry = er.async_get(hass).async_get(select.entity_id)
    assert entry is not None
    assert entry.original_name == "Water hardness"

    await call(
        hass,
        "select",
        "select_option",
        select.entity_id,
        option="step_4",
    )
    await _settle(hass)
    assert machine.writes == [(hardness.p_argument.upper(), "18")]
    assert state(hass, "select", mock_config_entry, "setting_hardness").state == (
        "step_4"
    )


async def test_a_machine_without_settings_reads_none(
    hass: HomeAssistant,
    mock_client: MagicMock,
    machine: FakeMachine,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """Old machines declare none; the polls do not ask for them either."""
    mock_client.profile = load_profile("EF532")
    await _with_settings(hass, mock_config_entry)
    await poll(hass, freezer)

    for platform in ("number", "select", "switch"):
        assert registered_keys(hass, mock_config_entry, platform) == set()
    assert _reads(mock_client) == [False, False]


async def test_switching_the_option_off_removes_the_entities(
    hass: HomeAssistant,
    mock_client: MagicMock,
    machine: FakeMachine,
    mock_config_entry: MockConfigEntry,
) -> None:
    """They would stay behind as unavailable entities."""
    await _with_settings(hass, mock_config_entry)
    assert registered_keys(hass, mock_config_entry, "number")

    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={**mock_config_entry.options, CONF_ENABLE_SETTINGS: False},
    )
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    for platform in ("number", "select", "switch"):
        assert registered_keys(hass, mock_config_entry, platform) == set()
    assert _reads(mock_client)[-1] is False


async def test_switching_the_option_on_adds_the_entities(
    hass: HomeAssistant,
    mock_client: MagicMock,
    machine: FakeMachine,
    mock_config_entry: MockConfigEntry,
) -> None:
    """An existing install gets the entities when the owner asks for them."""
    await setup_entry(hass, mock_config_entry)
    assert registered_keys(hass, mock_config_entry, "number") == set()

    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={**mock_config_entry.options, CONF_ENABLE_SETTINGS: True},
    )
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    for platform, keys in SETTING_KEYS.items():
        assert registered_keys(hass, mock_config_entry, platform) == keys
    assert (
        float(state(hass, "number", mock_config_entry, "setting_hardness").state) == 16
    )


# The front panel lock of 0.5.0 to 0.5.2. On a real E8 the keys stayed usable and the
# machine never reported them as locked, so 0.5.3 removed the switch.


@pytest.mark.parametrize("reachable", [True, False])
@pytest.mark.parametrize("settings", [True, False])
@pytest.mark.parametrize("disabled_by", [None, er.RegistryEntryDisabler.USER])
async def test_the_front_panel_lock_of_0_5_2_is_removed_at_setup(
    hass: HomeAssistant,
    mock_client: MagicMock,
    machine: FakeMachine,
    mock_config_entry: MockConfigEntry,
    reachable: bool,
    settings: bool,
    disabled_by: er.RegistryEntryDisabler | None,
) -> None:
    """The switch leaves the registry with the machine settings on or off.

    Enabled or disabled, and also while the machine is switched off; the switches of
    the settings stay.
    """
    entry = mock_config_entry
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "switch",
        DOMAIN,
        f"{entry.entry_id}_front_panel_lock",
        config_entry=entry,
        disabled_by=disabled_by,
        suggested_object_id="jura_e8_sds_front_panel_lock",
    )
    if disabled_by is None:
        # what Home Assistant shows for a registered entity that nothing provides
        hass.states.async_set(old.entity_id, STATE_UNAVAILABLE, {"restored": True})
    if not reachable:
        mock_client.fetch.side_effect = JuraWifiConnectionError("down")

    await setup_with_options(hass, entry, **{CONF_ENABLE_SETTINGS: settings})

    assert entry.state is ConfigEntryState.LOADED
    assert registry.async_get(old.entity_id) is None
    assert hass.states.get(old.entity_id) is None
    for platform, keys in SETTING_KEYS.items():
        assert registered_keys(hass, entry, platform) == (keys if settings else set())
    # the alerts that the switch showed are still there as binary sensors
    assert {"keys_locked", "remote_screen_active"} <= registered_keys(
        hass, entry, "binary_sensor"
    )

    # the next setup does not bring it back
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert "front_panel_lock" not in registered_keys(hass, entry, "switch")


# How the values of a setting are read and written


def test_what_a_setting_becomes() -> None:
    """A range is a number, two on and off values a switch, a list a select."""
    kinds = {
        setting.name: setting_platform(setting)
        for setting in load_profile("EF1120").settings
    }
    assert kinds == {
        "hardness": "number",
        "auto_off": "select",
        "units": "select",
        "language": "select",
        "brewing_mode": "select",
        "quality_assistant": "switch",
    }


def test_numbers_are_written_with_the_width_of_the_setting() -> None:
    """The hardness takes two hex digits."""
    hardness = next(s for s in load_profile("EF1120").settings if s.name == "hardness")
    assert number_to_wire(hardness, 12) == "0C"
    assert number_to_wire(hardness, 30) == "1E"
    assert stored_number(hardness, "0C") == 12
    assert stored_number(hardness, "") is None
    assert stored_number(hardness, "00") is None
    assert stored_number(hardness, "1F") is None
    assert stored_number(hardness, "zz") is None
    assert stored_number(hardness, None) is None


def test_an_empty_answer_is_not_the_first_item() -> None:
    """The library would take the first item for an empty value."""
    language = next(s for s in load_profile("EF1120").settings if s.name == "language")
    assert language.item_from_hex("").name == "german"
    assert stored_item(language, "") is None
    assert stored_item(language, None) is None
    assert stored_item(language, "02").name == "english"
    assert stored_item(language, "FF") is None
