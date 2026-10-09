"""Base entity for the JURA Wi-Fi Connect integration."""

from __future__ import annotations

from collections.abc import Collection

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import MachineSnapshot
from .const import DOMAIN
from .coordinator import JuraWifiCoordinator
from .identity import device_attributes


def async_remove_stale_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    domain: str,
    wanted: Collection[str | None],
) -> None:
    """Remove the entities of a platform that the options no longer ask for.

    They would stay behind as unavailable entities.
    """
    registry = er.async_get(hass)
    for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registered.domain == domain and registered.unique_id not in wanted:
            registry.async_remove(registered.entity_id)


class JuraWifiEntity(CoordinatorEntity[JuraWifiCoordinator]):
    """Common behaviour of all entities of a machine."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: JuraWifiCoordinator, key: str) -> None:
        """Initialize the entity and attach it to the machine's device."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="JURA",
            name=entry.title,
            **device_attributes(entry.data),
        )
        if coordinator.suggested_area:
            info["suggested_area"] = coordinator.suggested_area
        self._attr_device_info = info

    @property
    def snapshot(self) -> MachineSnapshot | None:
        """Return the last known machine snapshot, which may come from the cache."""
        return self.coordinator.data.snapshot

    @property
    def live_snapshot(self) -> MachineSnapshot | None:
        """Return the last snapshot that the machine reported in this run.

        What a restart restored from the cache holds the counters but no alerts.
        """
        snapshot = self.coordinator.data.snapshot
        return None if snapshot is None or snapshot.restored else snapshot

    @property
    def online(self) -> bool:
        """Return whether the machine answered the last poll."""
        return self.coordinator.data.online
