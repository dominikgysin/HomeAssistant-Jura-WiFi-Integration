"""Tests for setup, entities, offline handling and brewing."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.jura_wifi.api import (
    JuraWifiAuthError,
    JuraWifiBusy,
    JuraWifiConnectionError,
    JuraWifiError,
    MachineActivity,
)
from custom_components.jura_wifi.const import (
    CONF_ARTICLE_NUMBER,
    CONF_ENABLE_BREWING,
    CONF_ENABLE_MAINTENANCE,
    CONF_FIRMWARE,
    CONF_MODEL_SOURCE,
    DOMAIN,
    MODEL_SOURCE_MANUAL,
)
from custom_components.jura_wifi.coordinator import JuraWifiData
from custom_components.jura_wifi.diagnostics import async_get_config_entry_diagnostics
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .conftest import AUTH_HASH, HOST, SNAPSHOT, setup_entry


def _entity_id(hass: HomeAssistant, platform: str, entry: ConfigEntry, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{entry.entry_id}_{key}"
    )
    assert entity_id is not None, f"no {platform} entity for {key}"
    return entity_id


def _state(hass: HomeAssistant, platform: str, entry: ConfigEntry, key: str) -> State:
    state = hass.states.get(_entity_id(hass, platform, entry, key))
    assert state is not None
    return state


async def _poll(hass: HomeAssistant, freezer) -> None:
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def _poll_now(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


def _device(hass: HomeAssistant, entry: ConfigEntry) -> dr.DeviceEntry:
    """Return the device of the entry, found through its model sensor."""
    entity = er.async_get(hass).async_get(_entity_id(hass, "sensor", entry, "model"))
    assert entity is not None
    assert entity.device_id is not None
    device = dr.async_get(hass).async_get(entity.device_id)
    assert device is not None
    return device


async def test_setup_and_unload(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The entry loads and unloads cleanly."""
    await setup_entry(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED


async def test_sensor_states(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Counters and maintenance values come from the snapshot."""
    await setup_entry(hass, mock_config_entry)

    assert _state(hass, "sensor", mock_config_entry, "status").state == "ready"
    assert _state(hass, "sensor", mock_config_entry, "total_brews").state == "20"
    assert _state(hass, "sensor", mock_config_entry, "brews_espresso").state == "7"
    assert _state(hass, "sensor", mock_config_entry, "brews_coffee").state == "8"
    assert _state(hass, "sensor", mock_config_entry, "brews_flat_white").state == (
        STATE_UNKNOWN
    )
    assert _state(hass, "sensor", mock_config_entry, "cleaning_need").state == "10"
    assert _state(hass, "sensor", mock_config_entry, "descaling_need").state == "0"


async def test_model_sensor(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The exact model read from the machine is exposed as a diagnostic sensor."""
    await setup_entry(hass, mock_config_entry)

    state = _state(hass, "sensor", mock_config_entry, "model")
    assert state.state == "E8 (SDS)"
    assert state.attributes["article_number"] == 15833
    assert state.attributes["machine_type"] == "EF1120"
    assert state.attributes["firmware"] == "TT237W V06.11"
    assert state.attributes["source"] == "discovery"

    entry = er.async_get(hass).async_get(
        _entity_id(hass, "sensor", mock_config_entry, "model")
    )
    assert entry is not None
    assert entry.entity_category is er.EntityCategory.DIAGNOSTIC
    assert entry.disabled_by is None


async def test_model_sensor_is_available_while_the_machine_is_off(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The model does not depend on the machine answering."""
    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    await setup_entry(hass, mock_config_entry)

    assert _state(hass, "sensor", mock_config_entry, "status").state == "offline"
    assert _state(hass, "sensor", mock_config_entry, "model").state == "E8 (SDS)"


async def test_device_describes_the_model(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The device shows the model, the article number and the firmware."""
    await setup_entry(hass, mock_config_entry)

    device = _device(hass, mock_config_entry)
    assert device.identifiers == {(DOMAIN, mock_config_entry.entry_id)}
    assert device.manufacturer == "JURA"
    assert device.model == "E8 (SDS)"
    assert device.model_id == "15833"
    assert device.sw_version == "TT237W V06.11"


async def test_manually_chosen_model_has_no_article_number(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Without an article number the profile code identifies the model."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={
            **mock_config_entry.data,
            CONF_ARTICLE_NUMBER: None,
            CONF_FIRMWARE: None,
            CONF_MODEL_SOURCE: MODEL_SOURCE_MANUAL,
        },
    )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    state = _state(hass, "sensor", mock_config_entry, "model")
    assert state.attributes == {
        "machine_type": "EF1120",
        "source": "manual",
        "friendly_name": "JURA E8 (SDS) Model",
    }
    device = _device(hass, mock_config_entry)
    assert device.model_id == "EF1120"
    assert device.sw_version is None


async def test_entries_from_older_versions_still_load(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Entries created before the model detection have none of the new keys."""
    data = {
        key: value
        for key, value in mock_config_entry.data.items()
        if key not in (CONF_ARTICLE_NUMBER, CONF_FIRMWARE, CONF_MODEL_SOURCE)
    }
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(mock_config_entry, data=data)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert _state(hass, "sensor", mock_config_entry, "model").state == "E8 (SDS)"


async def test_diagnostic_sensors_are_disabled_by_default(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Rarely needed sensors exist in the registry but are not enabled."""
    await setup_entry(hass, mock_config_entry)
    registry = er.async_get(hass)
    for key in ("filter_wear", "cycles_cleaning", "cycles_milk_rinse"):
        entity_id = _entity_id(hass, "sensor", mock_config_entry, key)
        assert hass.states.get(entity_id) is None
        entry = registry.async_get(entity_id)
        assert entry is not None
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_alerts_and_status(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """An empty water tank raises its problem sensor and the status."""
    mock_client.fetch.return_value = dataclasses.replace(
        SNAPSHOT,
        active_alerts=frozenset({"fill_water"}),
        errors=frozenset({"fill_water"}),
    )
    await setup_entry(hass, mock_config_entry)

    assert (
        _state(hass, "binary_sensor", mock_config_entry, "water_tank_empty").state
        == STATE_ON
    )
    assert (
        _state(hass, "binary_sensor", mock_config_entry, "drip_tray_full").state
        == STATE_OFF
    )
    status = _state(hass, "sensor", mock_config_entry, "status")
    assert status.state == "attention"
    assert status.attributes["errors"] == ["fill_water"]


async def test_connectivity_is_on_when_reachable(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The connectivity sensor follows the reachability."""
    await setup_entry(hass, mock_config_entry)
    state = _state(hass, "binary_sensor", mock_config_entry, "connectivity")
    assert state.state == STATE_ON


async def test_offline_after_repeated_failures(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """One failed poll is tolerated, two flip the machine to offline."""
    await setup_entry(hass, mock_config_entry)
    mock_client.fetch.side_effect = JuraWifiConnectionError("down")

    await _poll(hass, freezer)
    assert (
        _state(hass, "binary_sensor", mock_config_entry, "connectivity").state
        == STATE_ON
    )
    assert _state(hass, "sensor", mock_config_entry, "status").state == "ready"

    await _poll(hass, freezer)
    assert (
        _state(hass, "binary_sensor", mock_config_entry, "connectivity").state
        == STATE_OFF
    )
    assert _state(hass, "sensor", mock_config_entry, "status").state == "offline"
    # Counters keep their last value, alerts become unavailable.
    assert _state(hass, "sensor", mock_config_entry, "total_brews").state == "20"
    assert (
        _state(hass, "binary_sensor", mock_config_entry, "water_tank_empty").state
        == STATE_UNAVAILABLE
    )

    mock_client.fetch.side_effect = None
    await _poll(hass, freezer)
    assert (
        _state(hass, "binary_sensor", mock_config_entry, "connectivity").state
        == STATE_ON
    )
    assert _state(hass, "sensor", mock_config_entry, "status").state == "ready"


async def test_offline_at_startup(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """A switched-off machine does not stop the entry from loading."""
    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert _state(hass, "sensor", mock_config_entry, "status").state == "offline"
    assert (
        _state(hass, "sensor", mock_config_entry, "total_brews").state == STATE_UNKNOWN
    )
    assert (
        _state(hass, "binary_sensor", mock_config_entry, "connectivity").state
        == STATE_OFF
    )


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Rejected credentials put the entry into reauth."""
    mock_client.fetch.side_effect = JuraWifiAuthError("WRONG_HASH")
    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_unexpected_error_retries_setup(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """A garbled reply is not an outage and retries the setup."""
    mock_client.fetch.side_effect = JuraWifiError("garbled")
    await setup_entry(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_no_brew_buttons_by_default(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Controlling the machine is an opt-in."""
    await setup_entry(hass, mock_config_entry)
    assert not _button_keys(hass, mock_config_entry)


def _button_keys(hass: HomeAssistant, entry: ConfigEntry) -> set[str]:
    """Return the keys of the buttons that exist for the entry."""
    return {
        registered.unique_id.removeprefix(f"{entry.entry_id}_")
        for registered in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
        if registered.domain == "button"
    }


async def _enable_controls(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    brewing: bool = False,
    maintenance: bool = False,
) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_ENABLE_BREWING: brewing,
            CONF_ENABLE_MAINTENANCE: maintenance,
        },
    )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _enable_brewing(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await _enable_controls(hass, entry, brewing=True)


async def _press(hass: HomeAssistant, entry: ConfigEntry, key: str) -> None:
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": _entity_id(hass, "button", entry, key)},
        blocking=True,
    )


async def test_brew_button(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Pressing a button brews the product."""
    await _enable_brewing(hass, mock_config_entry)

    keys = _button_keys(hass, mock_config_entry)
    brewable = {key.removeprefix("brew_") for key in keys if key.startswith("brew_")}
    assert {
        "espresso",
        "cappuccino",
        "latte_macchiato",
        "hotwater_portion_normal",
    } <= brewable
    assert "powderproduct" not in brewable
    assert "2x_espresso" not in brewable
    # brewing alone does not bring the maintenance programs
    assert not {key for key in keys if key.startswith("start_")}

    await _press(hass, mock_config_entry, "brew_espresso")
    mock_client.brew.assert_called_once_with("espresso")


async def test_brewing_shows_up_in_the_status_right_away(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The status does not wait for the next poll to say what was started."""
    await _enable_brewing(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data
    # Hold back the check that follows, to see what the press itself shows.
    coordinator.async_request_refresh = AsyncMock()

    await _press(hass, mock_config_entry, "brew_cappuccino")

    status = _state(hass, "sensor", mock_config_entry, "status")
    assert status.state == "brewing"
    assert status.attributes["activity_detail"] == "cappuccino"
    # The values of the last poll stay.
    assert _state(hass, "sensor", mock_config_entry, "total_brews").state == "20"
    # A second press while the first drink runs is refused.
    with pytest.raises(ServiceValidationError):
        await _press(hass, mock_config_entry, "brew_espresso")
    mock_client.brew.assert_called_once_with("cappuccino")


async def test_the_machine_is_checked_after_a_command(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """A poll follows the press, so the status does not stay on the guess."""
    await _enable_brewing(hass, mock_config_entry)
    polls = mock_client.fetch.call_count

    await _press(hass, mock_config_entry, "brew_espresso")
    await hass.async_block_till_done()

    assert mock_client.fetch.call_count == polls + 1
    # the mock machine reports normally again
    assert _state(hass, "sensor", mock_config_entry, "status").state == "ready"


async def test_the_status_follows_the_machine_after_a_command(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """While the drink runs the machine reports what it does, and that is shown."""
    await _enable_brewing(hass, mock_config_entry)
    mock_client.fetch.side_effect = JuraWifiBusy(MachineActivity("brewing", "espresso"))

    await _press(hass, mock_config_entry, "brew_espresso")
    await hass.async_block_till_done()

    status = _state(hass, "sensor", mock_config_entry, "status")
    assert status.state == "brewing"
    assert status.attributes["activity_detail"] == "espresso"


async def test_maintenance_buttons(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The programs of the profile get a button, each in the config section."""
    await _enable_controls(hass, mock_config_entry, maintenance=True)

    # The E8 declares these five; it has no coffee system rinse program.
    assert _button_keys(hass, mock_config_entry) == {
        "start_cleaning",
        "start_descale",
        "start_filter_change",
        "start_cappu_rinse",
        "start_cappu_clean",
        "cancel",
    }
    registry = er.async_get(hass)
    for key in ("start_cleaning", "start_cappu_rinse"):
        entry = registry.async_get(_entity_id(hass, "button", mock_config_entry, key))
        assert entry is not None
        assert entry.entity_category is er.EntityCategory.CONFIG
    cancel = registry.async_get(_entity_id(hass, "button", mock_config_entry, "cancel"))
    assert cancel is not None
    assert cancel.entity_category is None


async def test_brew_and_maintenance_buttons_share_one_cancel_button(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Both options together give both sets and a single cancel button."""
    await _enable_controls(hass, mock_config_entry, brewing=True, maintenance=True)

    keys = _button_keys(hass, mock_config_entry)
    assert "brew_espresso" in keys
    assert "start_descale" in keys
    assert [key for key in keys if "cancel" in key] == ["cancel"]


async def test_start_a_maintenance_program(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The press starts the program; the machine continues on its display."""
    await _enable_controls(hass, mock_config_entry, maintenance=True)
    mock_client.fetch.side_effect = JuraWifiBusy(
        MachineActivity("maintenance", "cappu_rinse")
    )

    await _press(hass, mock_config_entry, "start_cappu_rinse")
    await hass.async_block_till_done()

    mock_client.start_process.assert_called_once_with("cappu_rinse")
    status = _state(hass, "sensor", mock_config_entry, "status")
    assert status.state == "maintenance"
    assert status.attributes["activity_detail"] == "cappu_rinse"


async def test_no_second_program_while_the_machine_is_busy(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Whatever the machine does has to end before a program starts."""
    mock_client.fetch.side_effect = JuraWifiBusy(MachineActivity("brewing", "coffee"))
    await _enable_controls(hass, mock_config_entry, maintenance=True)

    with pytest.raises(ServiceValidationError):
        await mock_config_entry.runtime_data.async_start_process("cleaning", "Cleaning")
    mock_client.start_process.assert_not_called()


async def test_a_program_needs_a_reachable_machine(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """An offline machine cannot be told to start anything."""
    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    await _enable_controls(hass, mock_config_entry, maintenance=True)

    with pytest.raises(HomeAssistantError):
        await mock_config_entry.runtime_data.async_start_process("cleaning", "Cleaning")
    mock_client.start_process.assert_not_called()
    assert (
        _state(hass, "button", mock_config_entry, "start_cleaning").state
        == STATE_UNAVAILABLE
    )


async def test_a_refused_program_is_reported(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The reason the machine gave is part of the error."""
    mock_client.start_process.side_effect = JuraWifiError("machine refused to start")
    await _enable_controls(hass, mock_config_entry, maintenance=True)

    with pytest.raises(HomeAssistantError, match="machine refused to start"):
        await _press(hass, mock_config_entry, "start_cleaning")
    # a refusal is no sign of the machine being away
    assert mock_config_entry.runtime_data.data.online
    assert mock_config_entry.runtime_data.data.activity is None


async def test_a_lost_connection_while_starting_counts_towards_offline(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The same two-strike rule as for brewing."""
    mock_client.start_process.side_effect = JuraWifiConnectionError("peer closed")
    await _enable_controls(hass, mock_config_entry, maintenance=True)
    coordinator = mock_config_entry.runtime_data

    for _ in range(2):
        with pytest.raises(HomeAssistantError, match="peer closed"):
            await coordinator.async_start_process("descale", "Descaling")
    await hass.async_block_till_done()
    assert not coordinator.data.online


async def test_rejected_credentials_while_starting_a_program_ask_for_pairing(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The machine forgot the pairing: the entry asks to pair again."""
    mock_client.start_process.side_effect = JuraWifiAuthError("WRONG_HASH")
    await _enable_controls(hass, mock_config_entry, maintenance=True)

    with pytest.raises(HomeAssistantError):
        await _press(hass, mock_config_entry, "start_cleaning")
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_cancel_button(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The cancel button ends what the machine is doing, also while it is busy."""
    await _enable_brewing(hass, mock_config_entry)
    mock_client.fetch.side_effect = JuraWifiBusy(MachineActivity("brewing", "coffee"))
    await _poll_now(hass, mock_config_entry)
    assert _state(hass, "sensor", mock_config_entry, "status").state == "brewing"

    await _press(hass, mock_config_entry, "cancel")

    mock_client.cancel_step.assert_called_once_with()


async def test_cancel_needs_a_reachable_machine(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Nothing to cancel on a machine that is off."""
    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    await _enable_brewing(hass, mock_config_entry)

    with pytest.raises(HomeAssistantError):
        await mock_config_entry.runtime_data.async_cancel_step()
    mock_client.cancel_step.assert_not_called()
    assert (
        _state(hass, "button", mock_config_entry, "cancel").state == STATE_UNAVAILABLE
    )


async def test_cancel_failure_is_reported(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """A refused cancel surfaces as an error."""
    mock_client.cancel_step.side_effect = JuraWifiError("machine refused the request")
    await _enable_brewing(hass, mock_config_entry)

    with pytest.raises(HomeAssistantError, match="machine refused the request"):
        await _press(hass, mock_config_entry, "cancel")


async def test_switching_an_option_off_removes_its_buttons(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Buttons of a disabled option do not linger as unavailable entities."""
    await _enable_controls(hass, mock_config_entry, brewing=True, maintenance=True)
    assert "brew_espresso" in _button_keys(hass, mock_config_entry)

    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={
            **mock_config_entry.options,
            CONF_ENABLE_BREWING: False,
            CONF_ENABLE_MAINTENANCE: True,
        },
    )
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    keys = _button_keys(hass, mock_config_entry)
    assert not {key for key in keys if key.startswith("brew_")}
    assert {"start_cleaning", "cancel"} <= keys

    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={
            **mock_config_entry.options,
            CONF_ENABLE_BREWING: False,
            CONF_ENABLE_MAINTENANCE: False,
        },
    )
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert not _button_keys(hass, mock_config_entry)


async def test_brew_blocked_product(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """A product blocked by an active alert is refused."""
    mock_client.fetch.return_value = dataclasses.replace(
        SNAPSHOT, blocked_products=frozenset({"espresso"})
    )
    await _enable_brewing(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "button",
            "press",
            {
                "entity_id": _entity_id(
                    hass, "button", mock_config_entry, "brew_espresso"
                )
            },
            blocking=True,
        )
    mock_client.brew.assert_not_called()


@pytest.mark.parametrize(
    ("newer_data", "expected"),
    [
        (
            JuraWifiData(
                online=True,
                snapshot=dataclasses.replace(
                    SNAPSHOT, blocked_products=frozenset({"espresso"})
                ),
            ),
            ServiceValidationError,
        ),
        (JuraWifiData(online=False, snapshot=SNAPSHOT), HomeAssistantError),
    ],
)
async def test_brew_checks_the_state_a_running_poll_produces(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    newer_data: JuraWifiData,
    expected: type[Exception],
) -> None:
    """A press that waited for a running poll must not use the old state."""
    await _enable_brewing(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data

    await coordinator._lock.acquire()  # a poll is in flight
    task = hass.async_create_task(coordinator.async_brew("espresso", "Espresso"))
    await asyncio.sleep(0)
    coordinator.async_set_updated_data(newer_data)  # the poll came back
    coordinator._lock.release()

    with pytest.raises(expected):
        await task
    mock_client.brew.assert_not_called()


async def test_brew_connection_errors_count_towards_offline(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Failing to reach the machine for a brew uses the same two-strike rule."""
    mock_client.brew.side_effect = JuraWifiConnectionError("peer closed")
    await _enable_brewing(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data

    with pytest.raises(HomeAssistantError, match="peer closed"):
        await coordinator.async_brew("espresso", "Espresso")
    assert coordinator.data.online

    with pytest.raises(HomeAssistantError):
        await coordinator.async_brew("espresso", "Espresso")
    await hass.async_block_till_done()
    assert not coordinator.data.online
    assert (
        _state(hass, "binary_sensor", mock_config_entry, "connectivity").state
        == STATE_OFF
    )


async def test_brew_failure_is_reported(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """A rejected brew request surfaces as an error."""
    mock_client.brew.side_effect = JuraWifiError("machine rejected the request")
    await _enable_brewing(hass, mock_config_entry)

    with pytest.raises(HomeAssistantError, match="machine rejected the request"):
        await hass.services.async_call(
            "button",
            "press",
            {
                "entity_id": _entity_id(
                    hass, "button", mock_config_entry, "brew_espresso"
                )
            },
            blocking=True,
        )


async def test_brew_while_offline(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Brewing needs a reachable machine."""
    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    await _enable_brewing(hass, mock_config_entry)

    with pytest.raises(HomeAssistantError):
        await mock_config_entry.runtime_data.async_brew("espresso", "Espresso")
    mock_client.brew.assert_not_called()


async def test_busy_machine_at_startup(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """A machine in its menu is online; its values are just not known yet."""
    mock_client.fetch.side_effect = JuraWifiBusy(MachineActivity("programming"))
    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    status = _state(hass, "sensor", mock_config_entry, "status")
    assert status.state == "programming"
    assert status.attributes["activity"] == "programming"
    assert "activity_detail" not in status.attributes
    assert (
        _state(hass, "binary_sensor", mock_config_entry, "connectivity").state
        == STATE_ON
    )
    assert (
        _state(hass, "binary_sensor", mock_config_entry, "water_tank_empty").state
        == STATE_UNAVAILABLE
    )
    assert (
        _state(hass, "sensor", mock_config_entry, "total_brews").state == STATE_UNKNOWN
    )
    assert _state(hass, "sensor", mock_config_entry, "model").state == "E8 (SDS)"

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    assert diagnostics["activity"] == {"kind": "programming", "detail": None}
    assert diagnostics["snapshot"] is None


async def test_busy_machine_keeps_the_last_values(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """While the machine brews, the values of the last full poll stay."""
    await setup_entry(hass, mock_config_entry)
    mock_client.fetch.side_effect = JuraWifiBusy(MachineActivity("brewing", "espresso"))

    # A busy machine is reachable, however long it stays busy.
    for _ in range(3):
        await _poll(hass, freezer)

    status = _state(hass, "sensor", mock_config_entry, "status")
    assert status.state == "brewing"
    assert status.attributes["activity"] == "brewing"
    assert status.attributes["activity_detail"] == "espresso"
    assert "active_alerts" not in status.attributes
    assert (
        _state(hass, "binary_sensor", mock_config_entry, "connectivity").state
        == STATE_ON
    )
    assert _state(hass, "sensor", mock_config_entry, "total_brews").state == "20"
    assert (
        _state(hass, "binary_sensor", mock_config_entry, "water_tank_empty").state
        == STATE_OFF
    )

    mock_client.fetch.side_effect = None
    await _poll(hass, freezer)
    status = _state(hass, "sensor", mock_config_entry, "status")
    assert status.state == "ready"
    assert "activity" not in status.attributes
    assert status.attributes["active_alerts"] == ["coffee_ready"]


async def test_brew_while_the_machine_is_busy(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """A machine that is brewing, in its menu or in a program is not started."""
    mock_client.fetch.side_effect = JuraWifiBusy(MachineActivity("maintenance"))
    await _enable_brewing(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError):
        await mock_config_entry.runtime_data.async_brew("espresso", "Espresso")
    mock_client.brew.assert_not_called()


async def test_diagnostics_redacts_credentials(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The diagnostics do not leak the address or the credentials."""
    await setup_entry(hass, mock_config_entry)
    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert AUTH_HASH not in str(diagnostics)
    assert HOST not in str(diagnostics)
    assert diagnostics["online"] is True
    assert diagnostics["activity"] is None
    assert diagnostics["snapshot"]["total_brews"] == 20
    assert diagnostics["snapshot"]["active_alerts"] == ["coffee_ready"]
