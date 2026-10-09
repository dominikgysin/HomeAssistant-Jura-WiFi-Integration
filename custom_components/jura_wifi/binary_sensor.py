"""Binary sensors for the JURA Wi-Fi Connect integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import JuraWifiConfigEntry, JuraWifiCoordinator
from .entity import JuraWifiEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class JuraWifiAlertDescription(BinarySensorEntityDescription):
    """Describes a binary sensor driven by one alert bit of the status frame."""

    alert: str


def _problem(key: str, alert: str) -> JuraWifiAlertDescription:
    return JuraWifiAlertDescription(
        key=key,
        translation_key=key,
        device_class=BinarySensorDeviceClass.PROBLEM,
        alert=alert,
    )


# Entity key -> alert name used by the machine profiles.
ALERT_DESCRIPTIONS: tuple[JuraWifiAlertDescription, ...] = (
    _problem("water_tank_empty", "fill_water"),
    _problem("grounds_container_full", "empty_grounds"),
    _problem("drip_tray_full", "empty_tray"),
    _problem("drip_tray_missing", "insert_tray"),
    _problem("grounds_container_missing", "insert_coffee_bin"),
    _problem("beans_empty", "no_beans"),
    _problem("cleaning_due", "cleaning_alert"),
    _problem("descaling_due", "descale_alert"),
    _problem("filter_change_due", "filter_alert"),
    _problem("milk_rinse_due", "cappu_rinse_alert"),
    _problem("milk_clean_due", "cappu_clean_alert"),
)

CONNECTIVITY_DESCRIPTION = BinarySensorEntityDescription(
    key="connectivity",
    translation_key="connectivity",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JuraWifiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the binary sensors of a machine."""
    coordinator = entry.runtime_data
    known_alerts = {alert.name for alert in coordinator.profile.alerts}

    entities: list[BinarySensorEntity] = [JuraWifiConnectivity(coordinator)]
    entities.extend(
        JuraWifiAlertSensor(coordinator, description)
        for description in ALERT_DESCRIPTIONS
        if description.alert in known_alerts
    )
    async_add_entities(entities)


class JuraWifiConnectivity(JuraWifiEntity, BinarySensorEntity):
    """Whether the machine answered the last poll."""

    entity_description = CONNECTIVITY_DESCRIPTION

    def __init__(self, coordinator: JuraWifiCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, CONNECTIVITY_DESCRIPTION.key)

    @property
    def is_on(self) -> bool:
        """Return True while the machine is reachable."""
        return self.online


class JuraWifiAlertSensor(JuraWifiEntity, BinarySensorEntity):
    """A problem reported by the machine."""

    entity_description: JuraWifiAlertDescription

    def __init__(
        self,
        coordinator: JuraWifiCoordinator,
        description: JuraWifiAlertDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Alerts are only meaningful while the machine is reachable."""
        return super().available and self.online and self.snapshot is not None

    @property
    def is_on(self) -> bool | None:
        """Return True while the alert is active."""
        snapshot = self.snapshot
        if snapshot is None:
            return None
        return self.entity_description.alert in snapshot.active_alerts
