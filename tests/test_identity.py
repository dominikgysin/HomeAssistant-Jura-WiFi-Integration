"""What an entry knows about its machine, and the area of the device."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jura_wifi.api import JuraWifiConnectionError, MachineIdentity
from custom_components.jura_wifi.const import (
    CONF_AREA,
    CONF_ARTICLE_NUMBER,
    CONF_FIRMWARE,
    CONF_MACHINE_TYPE,
    CONF_MODEL_NAME,
    CONF_MODEL_SOURCE,
    CONF_SERIAL_NUMBER,
    DOMAIN,
    MODEL_SOURCE_DISCOVERY,
)
from custom_components.jura_wifi.identity import identity_updates
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)

from .common import device_of, entity_id, poll, state
from .conftest import HOST, IDENTITY, SERIAL, setup_entry


async def _loaded(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set an entry up and wait for what it does in the background."""
    await setup_entry(hass, entry)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_an_entry_of_the_first_version_learns_what_the_dongle_knows(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_discover: MagicMock,
    legacy_config_entry: MockConfigEntry,
) -> None:
    """Article number, firmware, serial number and the source are filled in."""
    await _loaded(hass, legacy_config_entry)

    mock_discover.assert_called_once_with(HOST)
    assert legacy_config_entry.state is ConfigEntryState.LOADED
    assert dict(legacy_config_entry.data) == {
        **legacy_config_entry.data,
        CONF_ARTICLE_NUMBER: 15833,
        CONF_FIRMWARE: "TT237W V06.11",
        CONF_SERIAL_NUMBER: SERIAL,
        CONF_MODEL_NAME: "E8 (SDS)",
        CONF_MODEL_SOURCE: MODEL_SOURCE_DISCOVERY,
        CONF_MACHINE_TYPE: "EF1120",
    }
    assert legacy_config_entry.unique_id == str(SERIAL)

    model = state(hass, "sensor", legacy_config_entry, "model")
    assert model.state == "E8 (SDS)"
    assert model.attributes["article_number"] == 15833
    assert model.attributes["firmware"] == "TT237W V06.11"
    assert model.attributes["source"] == "discovery"

    device = device_of(hass, legacy_config_entry)
    assert device.sw_version == "TT237W V06.11"
    assert device.model_id == "15833"
    assert device.model == "E8 (SDS)"
    assert device.serial_number == str(SERIAL)


async def test_the_backfill_keeps_the_device_and_the_entities(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_discover: MagicMock,
    legacy_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """Nothing is lost: not the device, not an entity, not its history."""
    mock_discover.return_value = None
    await _loaded(hass, legacy_config_entry)
    entry_id = legacy_config_entry.entry_id
    registry = er.async_get(hass)
    entities_before = {
        entity.unique_id: entity.entity_id
        for entity in er.async_entries_for_config_entry(registry, entry_id)
    }
    device_before = device_of(hass, legacy_config_entry)
    assert entities_before
    assert legacy_config_entry.unique_id == HOST

    # the dongle answers the next time the machine is back
    mock_discover.return_value = IDENTITY
    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    for _ in range(2):
        await poll(hass, freezer)
    mock_client.fetch.side_effect = None
    await poll(hass, freezer)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert legacy_config_entry.unique_id == str(SERIAL)
    assert {
        entity.unique_id: entity.entity_id
        for entity in er.async_entries_for_config_entry(registry, entry_id)
    } == entities_before
    device = device_of(hass, legacy_config_entry)
    assert device.id == device_before.id
    assert device.identifiers == {(DOMAIN, entry_id)}
    assert device.serial_number == str(SERIAL)
    assert hass.states.get(
        entity_id(hass, "sensor", legacy_config_entry, "total_brews")
    )


async def test_an_entry_that_knows_its_serial_number_is_not_asked_again(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_discover: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Nothing is missing, so the dongle is left alone."""
    await _loaded(hass, mock_config_entry)

    mock_discover.assert_not_called()


async def test_a_dongle_that_does_not_answer_the_discovery_is_left_alone(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_discover: MagicMock,
    legacy_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """Nothing changes, and the discovery is not repeated with every poll."""
    mock_discover.return_value = None
    original = dict(legacy_config_entry.data)
    await _loaded(hass, legacy_config_entry)

    assert dict(legacy_config_entry.data) == original
    assert legacy_config_entry.unique_id == HOST
    assert legacy_config_entry.state is ConfigEntryState.LOADED

    for _ in range(3):
        await poll(hass, freezer)
        await hass.async_block_till_done(wait_background_tasks=True)
    mock_discover.assert_called_once_with(HOST)
    device = device_of(hass, legacy_config_entry)
    assert device.sw_version is None
    assert device.serial_number is None


async def test_the_discovery_is_tried_again_when_the_machine_is_back(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_discover: MagicMock,
    legacy_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """The dongle only answers while the machine is on, so that is the time to ask."""
    mock_discover.return_value = None
    await _loaded(hass, legacy_config_entry)
    assert mock_discover.call_count == 1

    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    for _ in range(3):
        await poll(hass, freezer)
    mock_discover.return_value = IDENTITY
    mock_client.fetch.side_effect = None
    await poll(hass, freezer)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_discover.call_count == 2
    assert legacy_config_entry.data[CONF_SERIAL_NUMBER] == SERIAL
    assert legacy_config_entry.unique_id == str(SERIAL)


async def test_a_machine_that_is_off_at_startup_is_asked_when_it_is_on(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_discover: MagicMock,
    legacy_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """Without the machine there is no dongle to ask."""
    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    await _loaded(hass, legacy_config_entry)
    await poll(hass, freezer)
    mock_discover.assert_not_called()

    mock_client.fetch.side_effect = None
    await poll(hass, freezer)
    await hass.async_block_till_done(wait_background_tasks=True)

    mock_discover.assert_called_once_with(HOST)
    assert legacy_config_entry.data[CONF_FIRMWARE] == "TT237W V06.11"


async def test_a_failing_discovery_does_not_hurt_the_entry(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_discover: MagicMock,
    legacy_config_entry: MockConfigEntry,
    caplog,
) -> None:
    """The fill-in is a favour; whatever goes wrong in it is only logged."""
    mock_discover.side_effect = OSError("no route to host")
    await _loaded(hass, legacy_config_entry)

    assert legacy_config_entry.state is ConfigEntryState.LOADED
    assert state(hass, "sensor", legacy_config_entry, "status").state == "ready"
    assert "Reading the identity of the machine failed" in caplog.text
    assert legacy_config_entry.unique_id == HOST


async def test_the_backfill_never_changes_the_machine_type(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_discover: MagicMock,
    legacy_config_entry: MockConfigEntry,
    caplog,
) -> None:
    """A machine of another model than the one set up is reported, not adopted."""
    caplog.set_level(logging.WARNING)
    mock_discover.return_value = MachineIdentity(
        article_number=15001,
        firmware="TT237W V06.11",
        ef_code="EF1089",
        model_name="Z10 (SDS)",
        serial_number=SERIAL,
    )
    await _loaded(hass, legacy_config_entry)

    data = legacy_config_entry.data
    assert data[CONF_MACHINE_TYPE] == "EF1120"
    # the model that was set up stays what it was
    assert data[CONF_MODEL_NAME] == "E8 (SDS)"
    assert CONF_MODEL_SOURCE not in data
    # what only the machine can tell is taken over
    assert data[CONF_ARTICLE_NUMBER] == 15001
    assert data[CONF_FIRMWARE] == "TT237W V06.11"
    assert data[CONF_SERIAL_NUMBER] == SERIAL
    assert "machine type EF1120" in caplog.text
    assert legacy_config_entry.state is ConfigEntryState.LOADED


async def test_the_serial_number_of_another_entry_is_not_taken(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    caplog,
) -> None:
    """Two entries cannot share an identifier; the one that was first keeps it."""
    mock_config_entry.add_to_hass(hass)
    twin = MockConfigEntry(
        domain=DOMAIN,
        title="JURA twin",
        unique_id="192.0.2.99",
        data={
            CONF_HOST: "192.0.2.99",
            CONF_MACHINE_TYPE: "EF1120",
            "conn_id": "homeassistant-87654321",
            "auth_hash": "e" * 64,
            "port": 51515,
            "pin": "",
        },
        options={"scan_interval": 60},
    )
    await _loaded(hass, twin)

    assert twin.state is ConfigEntryState.LOADED
    assert twin.unique_id == "192.0.2.99"
    assert mock_config_entry.unique_id == str(SERIAL)
    assert f"JURA twin has the serial number {SERIAL}" in caplog.text
    assert "JURA E8 (SDS) is set up with as well" in caplog.text


def test_what_a_discovery_reply_adds() -> None:
    """The fields that are missing are filled in, the others stay as they are."""
    legacy = {CONF_MACHINE_TYPE: "EF1120", CONF_MODEL_NAME: "E8 (SDS)"}
    assert identity_updates(legacy, IDENTITY) == {
        CONF_ARTICLE_NUMBER: 15833,
        CONF_FIRMWARE: "TT237W V06.11",
        CONF_SERIAL_NUMBER: SERIAL,
        CONF_MODEL_SOURCE: MODEL_SOURCE_DISCOVERY,
    }

    complete = {
        CONF_MACHINE_TYPE: "EF1120",
        CONF_MODEL_NAME: "E8 (SDS)",
        CONF_ARTICLE_NUMBER: 15833,
        CONF_FIRMWARE: "TT237W V06.11",
        CONF_SERIAL_NUMBER: SERIAL,
        CONF_MODEL_SOURCE: MODEL_SOURCE_DISCOVERY,
    }
    assert identity_updates(complete, IDENTITY) == {}


def test_a_new_firmware_is_not_taken_over_silently() -> None:
    """What the entry knows is not overwritten, except the confirmed model."""
    data = {
        CONF_MACHINE_TYPE: "EF1120",
        CONF_FIRMWARE: "TT237W V05.00",
        CONF_SERIAL_NUMBER: "20230102000001",
    }
    updates = identity_updates(data, IDENTITY)
    assert CONF_FIRMWARE not in updates
    assert CONF_SERIAL_NUMBER not in updates


def test_an_identity_without_a_serial_number_adds_none() -> None:
    """A dongle that does not report one leaves the entry as it is."""
    identity = MachineIdentity(
        article_number=15833,
        firmware="TT237W V06.11",
        ef_code="EF1120",
        model_name="E8 (SDS)",
        serial_number=None,
    )
    updates = identity_updates({CONF_MACHINE_TYPE: "EF1120"}, identity)
    assert CONF_SERIAL_NUMBER not in updates
    assert updates[CONF_FIRMWARE] == "TT237W V06.11"


# The area of the device


async def _entry_in_area(
    hass: HomeAssistant, entry: MockConfigEntry, name: str = "Kitchen"
) -> ar.AreaEntry:
    """Choose an area for an entry, as the setup does."""
    area = ar.async_get(hass).async_create(name)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_AREA: area.id}
    )
    return area


def _entity_prefixes(
    hass: HomeAssistant, entry: MockConfigEntry, area: str
) -> set[bool]:
    """Tell, for every entity, whether its entity ID starts with the area."""
    return {
        found.entity_id.split(".", 1)[1].startswith(f"{area}_")
        for found in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }


async def test_the_area_is_applied_when_the_device_is_created(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """All entities start in the area, so they get the same entity ID prefix."""
    area = await _entry_in_area(hass, mock_config_entry)
    await _loaded(hass, mock_config_entry)

    assert device_of(hass, mock_config_entry).area_id == area.id
    # Home Assistant 2026 puts the name of the area in front of the entity IDs of a
    # device that has one, older versions do not; either way it is the same for all.
    assert len(_entity_prefixes(hass, mock_config_entry, "kitchen")) == 1


async def test_no_area_without_a_choice(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """An entry that was set up without an area has a device without one."""
    await _loaded(hass, mock_config_entry)

    assert device_of(hass, mock_config_entry).area_id is None


async def test_a_manual_change_of_the_area_is_kept(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The area of the setup is a start; what the user does later stays."""
    kitchen = await _entry_in_area(hass, mock_config_entry)
    await _loaded(hass, mock_config_entry)
    assert device_of(hass, mock_config_entry).area_id == kitchen.id
    office = ar.async_get(hass).async_create("Office")
    device = device_of(hass, mock_config_entry)
    dr.async_get(hass).async_update_device(device.id, area_id=office.id)

    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert device_of(hass, mock_config_entry).area_id == office.id


async def test_an_area_that_was_taken_away_does_not_come_back(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """A device without an area is a decision as well."""
    await _entry_in_area(hass, mock_config_entry)
    await _loaded(hass, mock_config_entry)
    device = device_of(hass, mock_config_entry)
    dr.async_get(hass).async_update_device(device.id, area_id=None)

    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert device_of(hass, mock_config_entry).area_id is None


async def test_an_area_that_does_not_exist_any_more_is_ignored(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The area was deleted before the device was created."""
    area = await _entry_in_area(hass, mock_config_entry)
    ar.async_get(hass).async_delete(area.id)

    await _loaded(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert device_of(hass, mock_config_entry).area_id is None


async def test_the_device_shows_the_serial_number(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The serial number is on the device; its identifier is what it was."""
    await _loaded(hass, mock_config_entry)

    device = device_of(hass, mock_config_entry)
    assert device.serial_number == str(SERIAL)
    assert device.identifiers == {(DOMAIN, mock_config_entry.entry_id)}
    assert mock_config_entry.unique_id == str(SERIAL)
