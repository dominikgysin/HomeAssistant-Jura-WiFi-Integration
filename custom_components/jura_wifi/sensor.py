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
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import (
    CONF_ARTICLE_NUMBER,
    CONF_FIRMWARE,
    CONF_MACHINE_TYPE,
    CONF_MODEL_NAME,
    CONF_MODEL_SOURCE,
)
from .coordinator import JuraWifiConfigEntry, JuraWifiCoordinator, JuraWifiData
from .entity import JuraWifiEntity
from .status import STATUSES, machine_status

PARALLEL_UPDATES = 0

# Friendlier labels than the raw names of the machine profile.
PRODUCT_LABELS = {
    "hotwater_portion_normal": "Hot water",
    "milk_foam": "Milk foam",
}


@dataclass(frozen=True, kw_only=True)
class JuraWifiSensorDescription(SensorEntityDescription):
    """Describes a JURA sensor."""

    value_fn: Callable[[JuraWifiData], StateType]


def _total_brews(data: JuraWifiData) -> int | None:
    return data.snapshot.total_brews if data.snapshot else None


def _product_brews(data: JuraWifiData, product: str) -> int | None:
    return data.snapshot.product_counts.get(product) if data.snapshot else None


def _percent(data: JuraWifiData, field: str) -> int | None:
    return data.snapshot.maintenance_percent.get(field) if data.snapshot else None


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

# Maintenance indicators: field name in the machine profile -> entity key.
PERCENT_DESCRIPTIONS: dict[str, JuraWifiSensorDescription] = {
    field: JuraWifiSensorDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=enabled,
        value_fn=lambda data, field=field: _percent(data, field),
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


def _product_description(product: ProductDef) -> JuraWifiSensorDescription:
    name = product.name
    return JuraWifiSensorDescription(
        key=f"brews_{name}",
        translation_key="product_brews",
        translation_placeholders={"product": product_label(product)},
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: _product_brews(data, name),
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JuraWifiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors of a machine."""
    coordinator = entry.runtime_data
    profile = coordinator.profile

    entities: list[SensorEntity] = [
        JuraWifiStatusSensor(coordinator, STATUS_DESCRIPTION),
        JuraWifiModelSensor(coordinator),
        JuraWifiSensor(coordinator, TOTAL_BREWS_DESCRIPTION),
    ]
    entities.extend(
        JuraWifiSensor(coordinator, _product_description(product))
        for product in profile.products
        if product.active
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
        """Return what the machine is doing, or else its active alerts."""
        data = self.coordinator.data
        if not data.online:
            return None
        if data.activity is not None:
            attributes: dict[str, Any] = {"activity": data.activity.kind}
            if data.activity.detail:
                attributes["activity_detail"] = data.activity.detail
            return attributes
        snapshot = data.snapshot
        if snapshot is None:
            return None
        return {
            "active_alerts": sorted(snapshot.active_alerts),
            "errors": sorted(snapshot.errors),
            "blocked_products": sorted(snapshot.blocked_products),
        }
