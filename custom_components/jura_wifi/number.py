"""Number entities for the machine settings that are sliders."""

from __future__ import annotations

from jura_connect import SettingDef

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_ENABLE_SETTINGS
from .coordinator import JuraWifiConfigEntry, JuraWifiCoordinator
from .entity import JuraWifiEntity, async_remove_stale_entities
from .settings import (
    PLATFORM_NUMBER,
    number_to_wire,
    setting_key,
    setting_platform,
    setting_translation,
    stored_number,
)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JuraWifiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the settings of the machine that are numbers, if they are enabled."""
    coordinator = entry.runtime_data
    entities: list[JuraWifiSettingNumber] = []
    if entry.options.get(CONF_ENABLE_SETTINGS, False):
        entities = [
            JuraWifiSettingNumber(coordinator, definition)
            for definition in coordinator.profile.settings
            if setting_platform(definition) == PLATFORM_NUMBER
        ]
    async_remove_stale_entities(
        hass, entry, Platform.NUMBER, {entity.unique_id for entity in entities}
    )
    async_add_entities(entities)


class JuraWifiSettingNumber(JuraWifiEntity, NumberEntity):
    """A machine setting that is a number within a range, e.g. the water hardness."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(
        self, coordinator: JuraWifiCoordinator, definition: SettingDef
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, setting_key(definition))
        self._definition = definition
        self._attr_translation_key, self._attr_translation_placeholders = (
            setting_translation(definition)
        )
        self._attr_native_min_value = float(definition.minimum or 0)
        self._attr_native_max_value = float(definition.maximum or 0)
        self._attr_native_step = float(definition.step or 1)

    @property
    def available(self) -> bool:
        """The setting can be shown and changed while the machine answers."""
        return super().available and self.online and self.native_value is not None

    @property
    def native_value(self) -> float | None:
        """Return the value the machine reported the last time it was read."""
        raw = self.coordinator.data.settings.get(self._definition.p_argument.upper())
        number = stored_number(self._definition, raw)
        return None if number is None else float(number)

    async def async_set_native_value(self, value: float) -> None:
        """Write the value to the machine."""
        await self.coordinator.async_set_setting(
            self._definition,
            number_to_wire(self._definition, round(value)),
            self._definition.raw_name,
        )
