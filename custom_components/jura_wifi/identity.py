"""What the config entry knows about its machine, and what a discovery reply adds."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .api import MachineIdentity
from .const import (
    CONF_ARTICLE_NUMBER,
    CONF_FIRMWARE,
    CONF_MACHINE_TYPE,
    CONF_MODEL_NAME,
    CONF_MODEL_SOURCE,
    CONF_SERIAL_NUMBER,
    DOMAIN,
    MODEL_SOURCE_DISCOVERY,
)


@callback
def machine_device(hass: HomeAssistant, entry: ConfigEntry) -> dr.DeviceEntry | None:
    """Return the device of the machine of an entry, or ``None`` if it has none yet.

    The devices of the entry are listed, because the lookup by identifier was
    replaced in Home Assistant 2026.
    """
    registry = dr.async_get(hass)
    return next(
        (
            device
            for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
            if (DOMAIN, entry.entry_id) in device.identifiers
        ),
        None,
    )


def device_attributes(data: Mapping[str, Any]) -> dict[str, str | None]:
    """Describe the machine for the device registry, as far as the entry knows it."""
    article = data.get(CONF_ARTICLE_NUMBER)
    serial = data.get(CONF_SERIAL_NUMBER)
    return {
        "model": data.get(CONF_MODEL_NAME) or data[CONF_MACHINE_TYPE],
        "model_id": str(article) if article else data[CONF_MACHINE_TYPE],
        "serial_number": str(serial) if serial else None,
        "sw_version": data.get(CONF_FIRMWARE) or None,
    }


def identity_updates(
    data: Mapping[str, Any], identity: MachineIdentity
) -> dict[str, Any]:
    """Return the entry data that a discovery reply can fill in.

    Entries of the first versions know neither the article number nor the firmware,
    and no entry before 0.5.0 knows the serial number. Those are added where they
    are missing. The machine type decides which profile is used and is never touched.
    A reply for that very type confirms the model: article number, model name and the
    source of the model then come from the machine, as in the setup.
    """
    updates: dict[str, Any] = {}
    if not data.get(CONF_FIRMWARE) and identity.firmware:
        updates[CONF_FIRMWARE] = identity.firmware
    if not data.get(CONF_SERIAL_NUMBER) and identity.serial_number:
        updates[CONF_SERIAL_NUMBER] = identity.serial_number
    if (
        identity.ef_code is not None
        and identity.ef_code == data.get(CONF_MACHINE_TYPE)
        and identity.model_name
    ):
        updates[CONF_ARTICLE_NUMBER] = identity.article_number
        updates[CONF_MODEL_NAME] = identity.model_name
        updates[CONF_MODEL_SOURCE] = MODEL_SOURCE_DISCOVERY
    elif not data.get(CONF_ARTICLE_NUMBER) and identity.article_number:
        updates[CONF_ARTICLE_NUMBER] = identity.article_number
    return {key: value for key, value in updates.items() if data.get(key) != value}
