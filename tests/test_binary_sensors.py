"""The binary sensors that come from the alerts and from the maintenance percent."""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock

from jura_connect import load_profile
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jura_wifi.api import JuraWifiConnectionError
from custom_components.jura_wifi.binary_sensor import (
    ALERT_DESCRIPTIONS,
    RECOMMENDATION_DESCRIPTIONS,
)
from custom_components.jura_wifi.const import DOMAIN
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .common import enable_in_registry, entity_id, poll, registered_keys, state
from .conftest import SNAPSHOT, setup_entry

# The sensors that are on for everybody, and those that wait to be enabled.
ENABLED = {
    "water_tank_empty",
    "grounds_container_full",
    "drip_tray_full",
    "drip_tray_missing",
    "grounds_container_missing",
    "beans_empty",
    "cleaning_due",
    "descaling_due",
    "filter_change_due",
    "milk_rinse_due",
    "milk_clean_due",
    "system_fill_needed",
    "tap_open",
    "front_cover_open",
    "machine_error",
}
DISABLED = {
    "outlet_missing",
    "rear_cover_missing",
    "water_tank_removal_requested",
    "ventilation_closed",
    "powder_cover_open",
    "filter_detected",
    "keys_locked",
    "remote_screen_active",
}
RECOMMENDED = {
    "cleaning_recommended",
    "descaling_recommended",
    "filter_change_recommended",
}


async def test_every_alert_the_e8_declares_has_a_sensor(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Also the alerts that were not exposed before."""
    await setup_entry(hass, mock_config_entry)

    keys = registered_keys(hass, mock_config_entry, "binary_sensor")
    assert keys == ENABLED | DISABLED | RECOMMENDED | {"connectivity"}
    assert {description.key for description in ALERT_DESCRIPTIONS} == ENABLED | DISABLED


async def test_the_less_relevant_ones_are_disabled_by_default(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The blocking alerts are on, the parts of the machine and its states wait."""
    await setup_entry(hass, mock_config_entry)
    registry = er.async_get(hass)

    for key in ENABLED | RECOMMENDED | {"connectivity"}:
        entry = registry.async_get(
            entity_id(hass, "binary_sensor", mock_config_entry, key)
        )
        assert entry is not None
        assert entry.disabled_by is None, key
        assert hass.states.get(entry.entity_id) is not None, key
    for key in DISABLED:
        entry = registry.async_get(
            entity_id(hass, "binary_sensor", mock_config_entry, key)
        )
        assert entry is not None
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION, key
        assert hass.states.get(entry.entity_id) is None, key


async def test_the_states_of_the_machine_are_diagnostic(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The lock and the filter are no problems, so they are no warnings either."""
    await setup_entry(hass, mock_config_entry)
    registry = er.async_get(hass)

    for key in ("filter_detected", "keys_locked", "remote_screen_active"):
        entry = registry.async_get(
            entity_id(hass, "binary_sensor", mock_config_entry, key)
        )
        assert entry is not None
        assert entry.entity_category is EntityCategory.DIAGNOSTIC
        assert entry.original_device_class is None
    for key in ("water_tank_empty", "system_fill_needed", "outlet_missing"):
        entry = registry.async_get(
            entity_id(hass, "binary_sensor", mock_config_entry, key)
        )
        assert entry is not None
        assert entry.original_device_class == "problem"


async def test_each_alert_turns_on_its_own_sensor(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """The alert names are those of the profile of the E8."""
    mock_config_entry.add_to_hass(hass)
    enable_in_registry(hass, mock_config_entry, "binary_sensor", *DISABLED)
    await setup_entry(hass, mock_config_entry)

    for description in ALERT_DESCRIPTIONS:
        mock_client.fetch.return_value = dataclasses.replace(
            SNAPSHOT, active_alerts=frozenset({description.alert})
        )
        await poll(hass, freezer)
        for other in ALERT_DESCRIPTIONS:
            expected = STATE_ON if other is description else STATE_OFF
            assert (
                state(hass, "binary_sensor", mock_config_entry, other.key).state
                == expected
            ), (description.alert, other.key)


@pytest.mark.parametrize(
    ("percent", "expected"),
    [(0, STATE_OFF), (79, STATE_OFF), (80, STATE_ON), (81, STATE_ON), (100, STATE_ON)],
)
async def test_the_maintenance_is_recommended_at_the_threshold_of_the_profile(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    percent: int,
    expected: str,
) -> None:
    """The J.O.E. app recommends it at 80 percent on the E8, well before the alert."""
    mock_client.fetch.return_value = dataclasses.replace(
        SNAPSHOT,
        maintenance_percent={
            "cleaning": percent,
            "descale": percent,
            "filter_change": percent,
        },
    )
    await setup_entry(hass, mock_config_entry)

    for key in RECOMMENDED:
        sensor = state(hass, "binary_sensor", mock_config_entry, key)
        assert sensor.state == expected, key
        assert sensor.attributes["threshold"] == 80
        assert sensor.attributes["device_class"] == "problem"
    # the alert of the machine itself is something else and stays off
    assert (
        state(hass, "binary_sensor", mock_config_entry, "cleaning_due").state
        == STATE_OFF
    )


async def test_each_maintenance_has_its_own_percent(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Cleaning at 85 percent is no reason to descale."""
    mock_client.fetch.return_value = dataclasses.replace(
        SNAPSHOT,
        maintenance_percent={"cleaning": 85, "descale": 10, "filter_change": 80},
    )
    await setup_entry(hass, mock_config_entry)

    assert (
        state(hass, "binary_sensor", mock_config_entry, "cleaning_recommended").state
        == STATE_ON
    )
    assert (
        state(hass, "binary_sensor", mock_config_entry, "descaling_recommended").state
        == STATE_OFF
    )
    assert (
        state(
            hass, "binary_sensor", mock_config_entry, "filter_change_recommended"
        ).state
        == STATE_ON
    )


async def test_no_recommendation_for_a_filter_the_machine_does_not_report(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The E8 without a filter reports 0xFF; then there is nothing to recommend."""
    await setup_entry(hass, mock_config_entry)

    assert (
        state(
            hass, "binary_sensor", mock_config_entry, "filter_change_recommended"
        ).state
        == STATE_UNAVAILABLE
    )
    assert (
        state(hass, "binary_sensor", mock_config_entry, "cleaning_recommended").state
        == STATE_OFF
    )


async def test_the_recommendation_is_known_while_the_machine_is_off(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """The percent is a last known value, like the counters."""
    mock_client.fetch.return_value = dataclasses.replace(
        SNAPSHOT, maintenance_percent={"cleaning": 90, "descale": 5}
    )
    await setup_entry(hass, mock_config_entry)

    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    await poll(hass, freezer)
    await poll(hass, freezer)

    assert (
        state(hass, "binary_sensor", mock_config_entry, "connectivity").state
        == STATE_OFF
    )
    assert (
        state(hass, "binary_sensor", mock_config_entry, "cleaning_recommended").state
        == STATE_ON
    )


async def test_a_machine_gets_only_the_alerts_its_profile_declares(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The E4 has no front cover to close."""
    mock_client.profile = load_profile("EF1089")
    await setup_entry(hass, mock_config_entry)

    keys = registered_keys(hass, mock_config_entry, "binary_sensor")
    assert "front_cover_open" not in keys
    assert (ENABLED - {"front_cover_open"}) | DISABLED | {"connectivity"} <= keys


@pytest.mark.parametrize(
    ("machine_type", "recommended"),
    [
        ("EF1120", RECOMMENDED),
        # the profile gives a threshold for the cleaning only
        ("EF565_C", {"cleaning_recommended"}),
        # and for nothing
        ("EF557", set()),
    ],
)
async def test_a_recommendation_needs_a_threshold_in_the_profile(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    machine_type: str,
    recommended: set[str],
) -> None:
    """Where the profile has no PREDICTIVEMAINTENANCE, nothing is recommended."""
    mock_client.profile = load_profile(machine_type)
    await setup_entry(hass, mock_config_entry)

    keys = registered_keys(hass, mock_config_entry, "binary_sensor")
    assert keys & RECOMMENDED == recommended
    assert {
        description.key for description in RECOMMENDATION_DESCRIPTIONS
    } == RECOMMENDED


async def test_the_unique_ids_of_the_known_sensors_did_not_change(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Existing installs keep their entities, with their history."""
    await setup_entry(hass, mock_config_entry)

    registry = er.async_get(hass)
    for key in (
        "water_tank_empty",
        "grounds_container_full",
        "drip_tray_full",
        "drip_tray_missing",
        "grounds_container_missing",
        "beans_empty",
        "cleaning_due",
        "descaling_due",
        "filter_change_due",
        "milk_rinse_due",
        "milk_clean_due",
        "connectivity",
    ):
        assert registry.async_get_entity_id(
            "binary_sensor", DOMAIN, f"{mock_config_entry.entry_id}_{key}"
        ), key
