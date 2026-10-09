"""Base entity for the JURA Wi-Fi Connect integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import MachineSnapshot
from .const import (
    CONF_ARTICLE_NUMBER,
    CONF_FIRMWARE,
    CONF_MACHINE_TYPE,
    CONF_MODEL_NAME,
    DOMAIN,
)
from .coordinator import JuraWifiCoordinator


class JuraWifiEntity(CoordinatorEntity[JuraWifiCoordinator]):
    """Common behaviour of all entities of a machine."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: JuraWifiCoordinator, key: str) -> None:
        """Initialize the entity and attach it to the machine's device."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        article = entry.data.get(CONF_ARTICLE_NUMBER)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="JURA",
            model=entry.data.get(CONF_MODEL_NAME) or entry.data[CONF_MACHINE_TYPE],
            model_id=str(article) if article else entry.data[CONF_MACHINE_TYPE],
            name=entry.title,
            sw_version=entry.data.get(CONF_FIRMWARE) or None,
        )

    @property
    def snapshot(self) -> MachineSnapshot | None:
        """Return the last known machine snapshot."""
        return self.coordinator.data.snapshot

    @property
    def online(self) -> bool:
        """Return whether the machine answered the last poll."""
        return self.coordinator.data.online
