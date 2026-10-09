"""The diagnostics of an entry."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from jura_connect import __version__ as library_version
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jura_wifi.api import (
    JuraWifiBusy,
    JuraWifiConnectionError,
    MachineActivity,
)
from custom_components.jura_wifi.const import (
    CONF_AREA,
    CONF_ENABLE_SETTINGS,
    CONF_PIN,
    CONF_SERIAL_NUMBER,
    DOMAIN,
)
from custom_components.jura_wifi.diagnostics import async_get_config_entry_diagnostics
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar

from .common import E8_SETTINGS, SETTINGS_SNAPSHOT, poll, setup_with_options
from .conftest import AUTH_HASH, HOST, SERIAL, setup_entry

MANIFEST = Path(__file__).parents[1] / "custom_components" / DOMAIN / "manifest.json"


async def test_diagnostics_redact_the_address_the_credentials_and_the_serial_number(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Nothing that identifies the machine, the network or the pairing is in there."""
    area = ar.async_get(hass).async_create("Kitchen")
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_AREA: area.id, CONF_PIN: "pin-secret"},
    )
    await setup_entry(hass, mock_config_entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    text = json.dumps(diagnostics)
    for secret in (
        HOST,
        AUTH_HASH,
        mock_config_entry.data["conn_id"],
        area.id,
        "pin-secret",
    ):
        assert secret not in text
    data = diagnostics["entry"]["data"]
    for key in ("host", "auth_hash", "conn_id", "pin", CONF_SERIAL_NUMBER, CONF_AREA):
        assert data[key] == "**REDACTED**", key
    # what helps to understand a problem is not hidden
    assert data["machine_type"] == "EF1120"
    assert data["model_name"] == "E8 (SDS)"
    assert data["article_number"] == 15833
    assert data["firmware"] == "TT237W V06.11"
    assert diagnostics["entry"]["options"] == dict(mock_config_entry.options)


async def test_diagnostics_name_the_versions_and_the_profile(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The integration, the library and the profile of the machine."""
    await setup_entry(hass, mock_config_entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert diagnostics["versions"] == {
        "integration": json.loads(MANIFEST.read_text(encoding="utf-8"))["version"],
        "jura_connect": library_version,
    }
    assert diagnostics["profile"]["code"] == "EF1120"
    assert diagnostics["profile"]["version"]


async def test_diagnostics_show_the_state_of_the_coordinator(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Whether the last update worked, how often, and since when the machine is here."""
    await setup_entry(hass, mock_config_entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    coordinator = diagnostics["coordinator"]
    assert coordinator["last_update_success"] is True
    assert coordinator["update_interval"] == 60.0
    assert coordinator["consecutive_failures"] == 0
    assert coordinator["online_since"] is not None
    assert coordinator["offline_since"] is None
    assert coordinator["last_seen"] is not None
    assert diagnostics["online"] is True
    assert diagnostics["activity"] is None
    assert diagnostics["snapshot"]["total_brews"] == 20
    assert diagnostics["snapshot"]["active_alerts"] == ["coffee_ready"]
    assert diagnostics["snapshot"]["restored"] is False


async def test_diagnostics_of_a_machine_that_is_off(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """The failures in a row and since when the machine is gone."""
    await setup_entry(hass, mock_config_entry)
    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    await poll(hass, freezer)
    await poll(hass, freezer)
    await poll(hass, freezer)

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    coordinator = diagnostics["coordinator"]
    # a machine that is switched off is not a failed update
    assert coordinator["last_update_success"] is True
    assert coordinator["consecutive_failures"] == 3
    assert coordinator["online_since"] is None
    assert coordinator["offline_since"] is not None
    # the last time it answered is kept
    assert coordinator["last_seen"] is not None
    assert diagnostics["online"] is False
    assert diagnostics["snapshot"]["total_brews"] == 20


async def test_diagnostics_of_a_machine_that_is_active(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """The short interval of an activity is visible."""
    await setup_entry(hass, mock_config_entry)
    mock_client.fetch.side_effect = JuraWifiBusy(MachineActivity("brewing", "coffee"))
    await poll(hass, freezer)

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert diagnostics["coordinator"]["update_interval"] == 15.0
    assert diagnostics["activity"] == {"kind": "brewing", "detail": "coffee"}


async def test_diagnostics_hold_the_settings_that_were_read(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The raw values of the machine settings, by the argument that reads them."""
    mock_client.fetch.return_value = SETTINGS_SNAPSHOT
    await setup_with_options(hass, mock_config_entry, **{CONF_ENABLE_SETTINGS: True})

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert diagnostics["settings"] == E8_SETTINGS
    # the settings are not a part of the snapshot's values
    assert diagnostics["snapshot"]["settings"] is None


async def test_diagnostics_without_settings(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Nothing is read without the option."""
    await setup_entry(hass, mock_config_entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert diagnostics["settings"] == {}


async def test_diagnostics_say_that_the_values_come_from_the_cache(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    cache_storage: dict,
) -> None:
    """After a restart with the machine off, the values are not from this run."""
    cache_storage[f"{DOMAIN}.{mock_config_entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.{mock_config_entry.entry_id}",
        "data": {
            "machine_type": "EF1120",
            "last_seen": "2026-10-08T07:30:00+00:00",
            "snapshot": {"total_brews": 27},
        },
    }
    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    await setup_entry(hass, mock_config_entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert diagnostics["online"] is False
    assert diagnostics["snapshot"]["total_brews"] == 27
    assert diagnostics["snapshot"]["restored"] is True
    assert diagnostics["coordinator"]["last_seen"] == "2026-10-08T07:30:00+00:00"
    assert SERIAL not in diagnostics["entry"]["data"].values()
