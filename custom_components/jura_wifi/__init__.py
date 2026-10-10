"""The JURA Wi-Fi Connect integration."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.service import async_register_platform_entity_service
from homeassistant.helpers.typing import ConfigType

from .api import BREW_OPTIONS, JuraWifiClient
from .button import JuraWifiBrewButton
from .const import (
    CONF_AUTH_HASH,
    CONF_CONN_ID,
    CONF_MACHINE_TYPE,
    CONF_PIN,
    CONF_SERIAL_NUMBER,
    DEFAULT_PORT,
    DOMAIN,
    PLATFORMS,
    SERVICE_BREW,
)
from .coordinator import JuraWifiConfigEntry, JuraWifiCoordinator, cache_store
from .identity import machine_device

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# The fields of the action are the recipe parameters of the brew options. Whether a
# value fits the drink is checked against its profile when the action runs.
BREW_SCHEMA = {
    vol.Optional(option.key): (
        vol.All(cv.string, vol.Lower) if option.named else vol.Coerce(int)
    )
    for option in BREW_OPTIONS
}


async def _async_brew(entity: Entity, call: ServiceCall) -> None:
    """Brew the drink of a brew button with single parameters of its recipe changed."""
    if not isinstance(entity, JuraWifiBrewButton):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_a_brew_button"
        )
    options = {
        option.key: call.data[option.key]
        for option in BREW_OPTIONS
        if option.key in call.data
    }
    await entity.async_brew_with_options(options)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions of the integration."""
    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_BREW,
        entity_domain=Platform.BUTTON,
        schema=BREW_SCHEMA,
        func=_async_brew,
    )
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: JuraWifiConfigEntry) -> bool:
    """Bring an entry of an earlier version up to date.

    Version 1.2: 0.5.0 and 0.5.1 stored a field of the discovery reply as the serial
    number, which is not the number on the type plate. It is dropped, and so are the
    entry ID and the serial number of the device that came from it. The serial
    number of the type plate is filled in the next time the machine answers. The
    identifiers of the device and of the entities do not depend on it.
    """
    if entry.version > 1:
        return False
    if entry.minor_version < 2:
        data = dict(entry.data)
        unique_id = entry.unique_id
        if (wrong := data.pop(CONF_SERIAL_NUMBER, None)) is not None:
            # The address identifies an entry without a serial number, as before.
            host = str(data[CONF_HOST]).lower()
            if (
                unique_id == str(wrong)
                and hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, host)
                is None
            ):
                unique_id = host
            if (device := machine_device(hass, entry)) is not None:
                dr.async_get(hass).async_update_device(device.id, serial_number=None)
        hass.config_entries.async_update_entry(
            entry, data=data, unique_id=unique_id, minor_version=2
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: JuraWifiConfigEntry) -> bool:
    """Set up a JURA machine from a config entry."""
    client = JuraWifiClient(
        host=entry.data[CONF_HOST],
        port=entry.data.get(CONF_PORT, DEFAULT_PORT),
        conn_id=entry.data[CONF_CONN_ID],
        auth_hash=entry.data[CONF_AUTH_HASH],
        pin=entry.data.get(CONF_PIN, ""),
        machine_type=entry.data[CONF_MACHINE_TYPE],
    )
    coordinator = JuraWifiCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: JuraWifiConfigEntry) -> bool:
    """Unload a config entry and keep what the machine reported last."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_save_cache()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: JuraWifiConfigEntry) -> None:
    """Delete the cache of a config entry that was removed."""
    await cache_store(hass, entry.entry_id).async_remove()
