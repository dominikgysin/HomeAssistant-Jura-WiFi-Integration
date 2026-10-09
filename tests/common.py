"""Helpers shared by the tests of the entities."""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from typing import Any

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.jura_wifi.const import DOMAIN
from custom_components.jura_wifi.identity import machine_device
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .conftest import SNAPSHOT

# The raw values of the six settings that the E8 declares, as the machine reads them
# back: hardness 16, switch-off after 30 minutes, millilitres, English, brewing mode
# "ask" and the quality assistant active.
E8_SETTINGS = {"02": "10", "13": "1E", "08": "00", "09": "02", "65": "00", "7E": "01"}
SETTINGS_SNAPSHOT = dataclasses.replace(SNAPSHOT, settings=E8_SETTINGS)


def entity_id(hass: HomeAssistant, platform: str, entry: ConfigEntry, key: str) -> str:
    """Return the entity ID of the entity that an entry has for a key."""
    found = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{entry.entry_id}_{key}"
    )
    assert found is not None, f"no {platform} entity for {key}"
    return found


def state(hass: HomeAssistant, platform: str, entry: ConfigEntry, key: str) -> State:
    """Return the state of the entity that an entry has for a key."""
    found = hass.states.get(entity_id(hass, platform, entry, key))
    assert found is not None, f"{platform} {key} has no state"
    return found


def registered_keys(hass: HomeAssistant, entry: ConfigEntry, platform: str) -> set[str]:
    """Return the keys of the entities of a platform that an entry has registered."""
    return {
        registered.unique_id.removeprefix(f"{entry.entry_id}_")
        for registered in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
        if registered.domain == platform
    }


def enable_in_registry(
    hass: HomeAssistant, entry: ConfigEntry, platform: str, *keys: str
) -> None:
    """Register entities as enabled before the setup, as a user can enable them.

    The entry has to be added to Home Assistant already.
    """
    registry = er.async_get(hass)
    for key in keys:
        registry.async_get_or_create(
            platform,
            DOMAIN,
            f"{entry.entry_id}_{key}",
            config_entry=entry,
            suggested_object_id=key,
        )


def device_of(hass: HomeAssistant, entry: ConfigEntry) -> dr.DeviceEntry:
    """Return the device of an entry."""
    found = machine_device(hass, entry)
    assert found is not None
    return found


async def poll(hass: HomeAssistant, freezer: Any, seconds: float = 61) -> None:
    """Let time pass and the coordinator poll once."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def setup_with_options(
    hass: HomeAssistant, entry: MockConfigEntry, **options: Any
) -> None:
    """Set an entry up with some of its options changed."""
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={**entry.options, **options})
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def call(
    hass: HomeAssistant, domain: str, service: str, entity: str, **data: Any
) -> None:
    """Call a service on one entity and wait for it."""
    await hass.services.async_call(
        domain, service, {"entity_id": entity, **data}, blocking=True
    )
