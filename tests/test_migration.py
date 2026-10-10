"""Entries of earlier versions are brought up to date when they are loaded."""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jura_wifi.api import JuraWifiConnectionError
from custom_components.jura_wifi.const import CONF_SERIAL_NUMBER, DOMAIN
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .common import device_of, entity_id, poll
from .conftest import HOST, IDENTITY, SERIAL, setup_entry

# What 0.5.0 and 0.5.1 stored as the serial number: another field of the discovery
# reply, which is not the number on the type plate.
WRONG_SERIAL = 321

OTHER_HOST = "192.0.2.11"
OTHER_SERIAL = "20240117001235"


def _entry_of_0_5_1(
    template: MockConfigEntry, *, unique_id: str = str(WRONG_SERIAL), **data: object
) -> MockConfigEntry:
    """Return an entry as 0.5.0 and 0.5.1 left it, with the wrong serial number."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=template.title,
        unique_id=unique_id,
        version=1,
        minor_version=1,
        data={**template.data, CONF_SERIAL_NUMBER: WRONG_SERIAL, **data},
        options=dict(template.options),
    )


def _device_of_0_5_1(hass: HomeAssistant, entry: MockConfigEntry) -> dr.DeviceEntry:
    """Register the device as 0.5.0 and 0.5.1 showed it."""
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="JURA",
        name=entry.title,
        serial_number=str(WRONG_SERIAL),
    )


async def _loaded(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set an entry up and wait for what it does in the background."""
    await setup_entry(hass, entry)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_the_serial_number_of_the_type_plate_replaces_the_wrong_one(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_discover: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Entry, entry ID and device get it without anything to do for the user.

    The device, the entities and their IDs stay what they were.
    """
    entry = _entry_of_0_5_1(mock_config_entry)
    entry.add_to_hass(hass)
    device = _device_of_0_5_1(hass, entry)
    registered = er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_total_brews",
        config_entry=entry,
        device_id=device.id,
        suggested_object_id="kitchen_jura_e8_sds_total_brews",
    )

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.state is ConfigEntryState.LOADED
    assert (entry.version, entry.minor_version) == (1, 2)
    mock_discover.assert_called_once_with(HOST)
    assert dict(entry.data) == dict(mock_config_entry.data)
    assert entry.data[CONF_SERIAL_NUMBER] == SERIAL
    assert entry.unique_id == SERIAL
    migrated = device_of(hass, entry)
    assert migrated.id == device.id
    assert migrated.identifiers == {(DOMAIN, entry.entry_id)}
    assert migrated.serial_number == SERIAL
    assert entity_id(hass, "sensor", entry, "total_brews") == registered.entity_id
    assert hass.states.get(registered.entity_id).state == "20"


async def test_the_wrong_serial_number_is_gone_before_the_machine_answers(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_discover: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """Until the dongle answers the discovery, the entry has no serial number.

    It is known by its address meanwhile, as the entries of the first versions.
    """
    mock_discover.return_value = None
    entry = _entry_of_0_5_1(mock_config_entry)
    entry.add_to_hass(hass)
    _device_of_0_5_1(hass, entry)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.state is ConfigEntryState.LOADED
    assert (entry.version, entry.minor_version) == (1, 2)
    assert CONF_SERIAL_NUMBER not in entry.data
    assert entry.unique_id == HOST
    assert device_of(hass, entry).serial_number is None

    # the dongle answers the next time the machine is back
    mock_discover.return_value = IDENTITY
    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    for _ in range(3):
        await poll(hass, freezer)
    mock_client.fetch.side_effect = None
    await poll(hass, freezer)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_discover.call_count == 2
    assert entry.data[CONF_SERIAL_NUMBER] == SERIAL
    assert entry.unique_id == SERIAL
    assert device_of(hass, entry).serial_number == SERIAL


async def test_an_entry_known_by_its_address_keeps_it(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_discover: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The wrong serial number goes, the entry ID did not come from it."""
    mock_discover.return_value = None
    entry = _entry_of_0_5_1(mock_config_entry, unique_id=HOST)
    await _loaded(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert CONF_SERIAL_NUMBER not in entry.data
    assert entry.unique_id == HOST


async def test_an_address_that_another_entry_is_known_by_is_not_taken(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_discover: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Two entries never share an ID; the wrong serial number goes all the same."""
    mock_discover.return_value = None
    MockConfigEntry(
        domain=DOMAIN,
        title="JURA at the same address",
        unique_id=HOST,
        version=1,
        minor_version=2,
        data=dict(mock_config_entry.data),
        disabled_by=ConfigEntryDisabler.USER,
    ).add_to_hass(hass)
    entry = _entry_of_0_5_1(mock_config_entry)
    await _loaded(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert CONF_SERIAL_NUMBER not in entry.data
    assert entry.unique_id == str(WRONG_SERIAL)


async def test_two_machines_with_the_same_wrong_serial_number(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_discover: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """0.5.0 knew the second machine by its address; each gets its own number."""
    plates = {
        HOST: IDENTITY,
        OTHER_HOST: dataclasses.replace(IDENTITY, serial_number=OTHER_SERIAL),
    }
    mock_discover.side_effect = plates.get
    first = _entry_of_0_5_1(mock_config_entry)
    second = _entry_of_0_5_1(
        mock_config_entry, unique_id=OTHER_HOST, **{CONF_HOST: OTHER_HOST}
    )
    first.add_to_hass(hass)
    second.add_to_hass(hass)
    # the setup of the integration sets up both entries
    await hass.config_entries.async_setup(first.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert first.state is second.state is ConfigEntryState.LOADED
    assert (first.unique_id, first.data[CONF_SERIAL_NUMBER]) == (SERIAL, SERIAL)
    assert (second.unique_id, second.data[CONF_SERIAL_NUMBER]) == (
        OTHER_SERIAL,
        OTHER_SERIAL,
    )
    assert device_of(hass, first).serial_number == SERIAL
    assert device_of(hass, second).serial_number == OTHER_SERIAL


async def test_an_entry_without_a_serial_number_only_gets_the_new_version(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_discover: MagicMock,
    legacy_config_entry: MockConfigEntry,
) -> None:
    """The entries of the first versions never had one."""
    mock_discover.return_value = None
    original = dict(legacy_config_entry.data)
    await _loaded(hass, legacy_config_entry)

    assert legacy_config_entry.state is ConfigEntryState.LOADED
    assert (legacy_config_entry.version, legacy_config_entry.minor_version) == (1, 2)
    assert dict(legacy_config_entry.data) == original
    assert legacy_config_entry.unique_id == HOST


async def test_an_entry_of_this_version_is_left_alone(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_discover: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Its serial number is the one on the type plate, so the dongle is not asked."""
    original = dict(mock_config_entry.data)
    await _loaded(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert dict(mock_config_entry.data) == original
    assert mock_config_entry.unique_id == SERIAL
    assert device_of(hass, mock_config_entry).serial_number == SERIAL
    mock_discover.assert_not_called()


async def test_an_entry_of_a_later_major_version_is_not_loaded(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_discover: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """What a later version stored cannot be read by this one."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=mock_config_entry.title,
        unique_id=SERIAL,
        version=2,
        minor_version=1,
        data=dict(mock_config_entry.data),
        options=dict(mock_config_entry.options),
    )
    await _loaded(hass, entry)

    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    mock_client.fetch.assert_not_called()
