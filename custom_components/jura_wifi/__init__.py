"""The JURA Wi-Fi Connect integration."""

from __future__ import annotations

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from .api import JuraWifiClient
from .const import (
    CONF_AUTH_HASH,
    CONF_CONN_ID,
    CONF_MACHINE_TYPE,
    CONF_PIN,
    DEFAULT_PORT,
    PLATFORMS,
)
from .coordinator import JuraWifiConfigEntry, JuraWifiCoordinator


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
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
