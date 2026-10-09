"""Diagnostics for the JURA Wi-Fi Connect integration."""

from __future__ import annotations

import dataclasses
from typing import Any

from jura_connect import __version__ as library_version

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import (
    CONF_AREA,
    CONF_AUTH_HASH,
    CONF_CONN_ID,
    CONF_PIN,
    CONF_SERIAL_NUMBER,
    DOMAIN,
)
from .coordinator import JuraWifiConfigEntry

TO_REDACT = {
    CONF_HOST,
    CONF_AUTH_HASH,
    CONF_CONN_ID,
    CONF_PIN,
    CONF_SERIAL_NUMBER,
    CONF_AREA,
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: JuraWifiConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data
    snapshot = data.snapshot
    integration = await async_get_integration(hass, DOMAIN)
    since = coordinator.online_since
    interval = coordinator.update_interval
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "versions": {
            "integration": str(integration.version),
            "jura_connect": library_version,
        },
        "profile": {
            "code": coordinator.profile.code,
            "version": coordinator.profile.version,
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval": interval.total_seconds() if interval else None,
            "consecutive_failures": coordinator.consecutive_failures,
            "online_since": since.isoformat() if since and data.online else None,
            "offline_since": since.isoformat() if since and not data.online else None,
            "last_seen": (
                coordinator.last_seen.isoformat() if coordinator.last_seen else None
            ),
        },
        "online": data.online,
        "activity": (
            dataclasses.asdict(data.activity) if data.activity is not None else None
        ),
        "snapshot": (
            {
                key: sorted(value) if isinstance(value, frozenset) else value
                for key, value in dataclasses.asdict(snapshot).items()
            }
            if snapshot is not None
            else None
        ),
        "settings": dict(data.settings),
    }
