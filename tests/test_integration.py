"""Tests for setup, entities, offline handling and brewing."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.jura_wifi.api import (
    JuraWifiAuthError,
    JuraWifiConnectionError,
    JuraWifiError,
)
from custom_components.jura_wifi.const import CONF_ENABLE_BREWING, DOMAIN
from custom_components.jura_wifi.coordinator import JuraWifiData
from custom_components.jura_wifi.diagnostics import async_get_config_entry_diagnostics
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

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
    """Brewing is an opt-in."""
    await setup_entry(hass, mock_config_entry)
    registry = er.async_get(hass)
    assert not [
        entry
        for entry in er.async_entries_for_config_entry(
            registry, mock_config_entry.entry_id
        )
        if entry.domain == "button"
    ]


async def _enable_brewing(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_ENABLE_BREWING: True}
    )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_brew_button(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Pressing a button brews the product."""
    await _enable_brewing(hass, mock_config_entry)

    brewable = {
        entry.unique_id.removeprefix(f"{mock_config_entry.entry_id}_brew_")
        for entry in er.async_entries_for_config_entry(
            er.async_get(hass), mock_config_entry.entry_id
        )
        if entry.domain == "button"
    }
    assert {
        "espresso",
        "cappuccino",
        "latte_macchiato",
        "hotwater_portion_normal",
    } <= brewable
    assert "powderproduct" not in brewable
    assert "2x_espresso" not in brewable

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": _entity_id(hass, "button", mock_config_entry, "brew_espresso")},
        blocking=True,
    )
    mock_client.brew.assert_called_once_with("espresso")


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


async def test_diagnostics_redacts_credentials(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The diagnostics do not leak the address or the credentials."""
    await setup_entry(hass, mock_config_entry)
    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert AUTH_HASH not in str(diagnostics)
    assert HOST not in str(diagnostics)
    assert diagnostics["online"] is True
    assert diagnostics["snapshot"]["total_brews"] == 20
    assert diagnostics["snapshot"]["active_alerts"] == ["coffee_ready"]
