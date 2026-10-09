"""Sensors for the JURA Wi-Fi Connect integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jura_connect import ProductDef

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ARTICLE_NUMBER,
    CONF_FIRMWARE,
    CONF_MACHINE_TYPE,
    CONF_MODEL_NAME,
    CONF_MODEL_SOURCE,
    DOMAIN,
)
from .coordinator import JuraWifiConfigEntry, JuraWifiCoordinator, JuraWifiData
from .entity import JuraWifiEntity
from .status import STATUSES, machine_status

PARALLEL_UPDATES = 0

# Sensors of earlier versions that do not exist any more. Their entities are removed
# from the entity registry by their unique ID, enabled or not, and nothing else is.
# ``last_seen`` (0.5.0) wrote a new state at every poll; the time is an attribute of
# the status sensor now, see ``JuraWifiStatusSensor``.
RETIRED_SENSORS = ("last_seen",)

# Friendlier labels than the raw names of the machine profile.
PRODUCT_LABELS = {
    "hotwater_portion_normal": "Hot water",
    "milk_foam": "Milk foam",
    "powderproduct": "Powder product",
}


@dataclass(frozen=True, kw_only=True)
class JuraWifiSensorDescription(SensorEntityDescription):
    """Describes a JURA sensor.

    ``available_fn`` tells from the data whether the machine reports the value at
    all; without it the sensor is available whenever the coordinator is.
    """

    value_fn: Callable[[JuraWifiData], StateType]
    available_fn: Callable[[JuraWifiData], bool] | None = None


def _total_brews(data: JuraWifiData) -> int | None:
    return data.snapshot.total_brews if data.snapshot else None


def _product_brews(data: JuraWifiData, product: str) -> int | None:
    return data.snapshot.product_counts.get(product) if data.snapshot else None


def _percent(data: JuraWifiData, field: str) -> int | None:
    return data.snapshot.maintenance_percent.get(field) if data.snapshot else None


def _reports_percent(data: JuraWifiData, field: str) -> bool:
    """Whether the machine reports the indicator; not knowing it yet counts as yes."""
    return data.snapshot is None or field in data.snapshot.maintenance_percent


def _cycles(data: JuraWifiData, field: str) -> int | None:
    return data.snapshot.maintenance_counters.get(field) if data.snapshot else None


STATUS_DESCRIPTION = JuraWifiSensorDescription(
    key="status",
    translation_key="status",
    device_class=SensorDeviceClass.ENUM,
    options=STATUSES,
    value_fn=machine_status,
)

TOTAL_BREWS_DESCRIPTION = JuraWifiSensorDescription(
    key="total_brews",
    translation_key="total_brews",
    state_class=SensorStateClass.TOTAL_INCREASING,
    value_fn=_total_brews,
)

# Maintenance indicators: field name in the machine profile -> entity key. A
# machine reports 0xFF for an indicator it does not have (the E8 does that for the
# filter without a filter), and the sensor is not available then.
PERCENT_DESCRIPTIONS: dict[str, JuraWifiSensorDescription] = {
    field: JuraWifiSensorDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=enabled,
        value_fn=lambda data, field=field: _percent(data, field),
        available_fn=lambda data, field=field: _reports_percent(data, field),
    )
    for field, key, enabled in (
        ("cleaning", "cleaning_need", True),
        ("descale", "descaling_need", True),
        ("filter_change", "filter_wear", False),
    )
}

CYCLE_DESCRIPTIONS: dict[str, JuraWifiSensorDescription] = {
    field: JuraWifiSensorDescription(
        key=key,
        translation_key=key,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data, field=field: _cycles(data, field),
    )
    for field, key in (
        ("cleaning", "cycles_cleaning"),
        ("descale", "cycles_descale"),
        ("filter_change", "cycles_filter_change"),
        ("cappu_rinse", "cycles_milk_rinse"),
        ("coffee_rinse", "cycles_coffee_rinse"),
        ("cappu_clean", "cycles_milk_clean"),
    )
}


def product_label(product: ProductDef) -> str:
    """Return the display name of a product."""
    return PRODUCT_LABELS.get(product.name, product.raw_name)


def _is_powder(product: ProductDef) -> bool:
    """Whether the product is ground coffee from the powder compartment.

    The profile of the E8 gives its powder product the kind of a coffee, so the name
    counts as well.
    """
    return product.kind == "P" or "powder" in product.name


def _product_description(product: ProductDef) -> JuraWifiSensorDescription:
    name = product.name
    return JuraWifiSensorDescription(
        key=f"brews_{name}",
        translation_key="product_brews",
        translation_placeholders={"product": product_label(product)},
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=not _is_powder(product),
        value_fn=lambda data: _product_brews(data, name),
    )


@callback
def async_remove_retired_sensors(
    hass: HomeAssistant, entry: JuraWifiConfigEntry
) -> None:
    """Remove the entities of the sensors that were dropped, enabled or disabled.

    Only the exact unique IDs of ``RETIRED_SENSORS`` are looked up, so no other
    entity that is registered for the entry can be removed by accident.
    """
    registry = er.async_get(hass)
    for key in RETIRED_SENSORS:
        entity_id = registry.async_get_entity_id(
            Platform.SENSOR, DOMAIN, f"{entry.entry_id}_{key}"
        )
        if entity_id is not None:
            registry.async_remove(entity_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JuraWifiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors of a machine."""
    coordinator = entry.runtime_data
    profile = coordinator.profile

    async_remove_retired_sensors(hass, entry)

    entities: list[SensorEntity] = [
        JuraWifiStatusSensor(coordinator, STATUS_DESCRIPTION),
        JuraWifiModelSensor(coordinator),
        JuraWifiSensor(coordinator, TOTAL_BREWS_DESCRIPTION),
    ]
    # Every product gets its counter, also those that the profile does not offer as
    # a drink: the machine counts the doubles (and a double counts twice in the
    # total) and the powder product as well.
    entities.extend(
        JuraWifiSensor(coordinator, _product_description(product))
        for product in profile.products
    )
    entities.extend(
        JuraWifiSensor(coordinator, PERCENT_DESCRIPTIONS[field])
        for field in profile.maintenance_percent_fields
        if field in PERCENT_DESCRIPTIONS
    )
    entities.extend(
        JuraWifiSensor(coordinator, CYCLE_DESCRIPTIONS[field])
        for field in profile.maintenance_counter_fields
        if field in CYCLE_DESCRIPTIONS
    )
    async_add_entities(entities)


class JuraWifiSensor(JuraWifiEntity, SensorEntity):
    """A sensor backed by the coordinator data."""

    entity_description: JuraWifiSensorDescription

    def __init__(
        self,
        coordinator: JuraWifiCoordinator,
        description: JuraWifiSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Return False if the machine does not report the value at all."""
        if not super().available:
            return False
        available_fn = self.entity_description.available_fn
        return available_fn is None or available_fn(self.coordinator.data)

    @property
    def native_value(self) -> StateType:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.coordinator.data)


class JuraWifiModelSensor(JuraWifiEntity, SensorEntity):
    """The exact model, as read from the machine when it was set up."""

    entity_description = SensorEntityDescription(
        key="model",
        translation_key="model",
        entity_category=EntityCategory.DIAGNOSTIC,
    )

    def __init__(self, coordinator: JuraWifiCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, "model")

    @property
    def available(self) -> bool:
        """The model is known even while the machine is switched off."""
        return True

    @property
    def native_value(self) -> str:
        """Return the model designation, e.g. ``E8 (SDS)``."""
        data = self.coordinator.config_entry.data
        return data.get(CONF_MODEL_NAME) or data[CONF_MACHINE_TYPE]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the article number, the profile code and the firmware."""
        data = self.coordinator.config_entry.data
        attributes: dict[str, Any] = {"machine_type": data[CONF_MACHINE_TYPE]}
        for attribute, key in (
            ("article_number", CONF_ARTICLE_NUMBER),
            ("firmware", CONF_FIRMWARE),
            ("source", CONF_MODEL_SOURCE),
        ):
            if data.get(key):
                attributes[attribute] = data[key]
        return attributes


class JuraWifiStatusSensor(JuraWifiSensor):
    """The machine status, with the raw alerts as attributes."""

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return what the machine is doing, or else its active alerts.

        While the machine is offline the attribute ``last_seen`` holds the time of the
        last poll in which it answered (ISO 8601, UTC), also from before a restart of
        Home Assistant. That time does not move while the machine stays offline, so the
        attribute changes together with the status and never on its own.
        """
        data = self.coordinator.data
        if not data.online:
            last_seen = self.coordinator.last_seen
            if last_seen is None:
                return None
            return {"last_seen": dt_util.as_utc(last_seen).isoformat()}
        if data.activity is not None:
            attributes: dict[str, Any] = {"activity": data.activity.kind}
            if data.activity.detail:
                attributes["activity_detail"] = data.activity.detail
            return attributes
        snapshot = data.snapshot
        if snapshot is None or snapshot.restored:
            return None
        return {
            "active_alerts": sorted(snapshot.active_alerts),
            "errors": sorted(snapshot.errors),
            "blocked_products": sorted(snapshot.blocked_products),
        }
