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


@dataclass(frozen=True, kw_only=True)
class JuraWifiRecommendationDescription(BinarySensorEntityDescription):
    """Describes a binary sensor that turns on when a maintenance percent is reached.

    ``field`` is the field of the maintenance percent bank that tells how far the
    machine is; the sensor turns on at the threshold that the profile declares.
    """

    field: str


def _problem(key: str, alert: str, *, enabled: bool = True) -> JuraWifiAlertDescription:
    return JuraWifiAlertDescription(
        key=key,
        translation_key=key,
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_registry_enabled_default=enabled,
        alert=alert,
    )


def _state(key: str, alert: str) -> JuraWifiAlertDescription:
    """A state that the machine reports and that is no problem. Disabled by default."""
    return JuraWifiAlertDescription(
        key=key,
        translation_key=key,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
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
    # Alerts that block the machine as well.
    _problem("system_fill_needed", "fill_system"),
    _problem("tap_open", "close_tab"),
    _problem("front_cover_open", "close_front_cover"),
    _problem("machine_error", "error_status"),
    # Parts of the machine that are not where they belong; these come and go with
    # what the machine asks for on its display, so they are off until wanted.
    _problem("outlet_missing", "outlet_missing", enabled=False),
    _problem("rear_cover_missing", "rear_cover_missing", enabled=False),
    _problem("water_tank_removal_requested", "remove_water_tank", enabled=False),
    _problem("ventilation_closed", "ventilation_closed", enabled=False),
    _problem("powder_cover_open", "close_powder_cover", enabled=False),
    _state("filter_detected", "active_rf_filter"),
    _state("keys_locked", "locked_keys"),
    _state("remote_screen_active", "remote_screen"),
)

# Maintenance percent field -> entity key of the sensor that recommends the
# maintenance once the profile's threshold is reached, like the J.O.E. app does.
RECOMMENDATION_DESCRIPTIONS: tuple[JuraWifiRecommendationDescription, ...] = tuple(
    JuraWifiRecommendationDescription(
        key=key,
        translation_key=key,
        device_class=BinarySensorDeviceClass.PROBLEM,
        field=field,
    )
    for field, key in (
        ("cleaning", "cleaning_recommended"),
        ("descale", "descaling_recommended"),
        ("filter_change", "filter_change_recommended"),
    )
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
    thresholds = coordinator.profile_extras.predictive_thresholds
    percent_fields = set(coordinator.profile.maintenance_percent_fields)

    entities: list[BinarySensorEntity] = [JuraWifiConnectivity(coordinator)]
    entities.extend(
        JuraWifiAlertSensor(coordinator, description)
        for description in ALERT_DESCRIPTIONS
        if description.alert in known_alerts
    )
    entities.extend(
        JuraWifiMaintenanceRecommended(coordinator, description, thresholds[field])
        for description in RECOMMENDATION_DESCRIPTIONS
        if (field := description.field) in thresholds and field in percent_fields
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
        return super().available and self.online and self.live_snapshot is not None

    @property
    def is_on(self) -> bool | None:
        """Return True while the alert is active."""
        snapshot = self.live_snapshot
        if snapshot is None:
            return None
        return self.entity_description.alert in snapshot.active_alerts


class JuraWifiMaintenanceRecommended(JuraWifiEntity, BinarySensorEntity):
    """The maintenance percent of a machine reached the threshold of its profile.

    The J.O.E. app recommends the maintenance at that point, well before the machine
    itself asks for it with an alert.
    """

    entity_description: JuraWifiRecommendationDescription

    def __init__(
        self,
        coordinator: JuraWifiCoordinator,
        description: JuraWifiRecommendationDescription,
        threshold: int,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._threshold = threshold

    def _percent(self) -> int | None:
        snapshot = self.snapshot
        if snapshot is None:
            return None
        return snapshot.maintenance_percent.get(self.entity_description.field)

    @property
    def available(self) -> bool:
        """The percent stays known while the machine is off, if it reports it."""
        return super().available and self._percent() is not None

    @property
    def is_on(self) -> bool | None:
        """Return True once the machine is at or past the threshold."""
        percent = self._percent()
        return None if percent is None else percent >= self._threshold

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        """Return the percent at which the maintenance is recommended."""
        return {"threshold": self._threshold}
