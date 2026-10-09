"""Diagnostics for the JURA Wi-Fi Connect integration."""

from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from .const import CONF_AUTH_HASH, CONF_CONN_ID, CONF_PIN
from .coordinator import JuraWifiConfigEntry

TO_REDACT = {CONF_HOST, CONF_AUTH_HASH, CONF_CONN_ID, CONF_PIN}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: JuraWifiConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = entry.runtime_data.data
    snapshot = data.snapshot
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "online": data.online,
        "snapshot": (
            {
                key: sorted(value) if isinstance(value, frozenset) else value
                for key, value in dataclasses.asdict(snapshot).items()
            }
            if snapshot is not None
            else None
        ),
    }
