"""Switches: the machine settings that are either on or off."""

from __future__ import annotations

from typing import Any

from jura_connect import SettingDef

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_ENABLE_SETTINGS
from .coordinator import JuraWifiConfigEntry, JuraWifiCoordinator
from .entity import JuraWifiEntity, async_remove_stale_entities
from .settings import (
    PLATFORM_SWITCH,
    on_off_items,
    setting_key,
    setting_platform,
    setting_translation,
    stored_item,
)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JuraWifiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the switches of the machine, if the machine settings are enabled."""
    coordinator = entry.runtime_data
    entities: list[JuraWifiEntity] = []
    if entry.options.get(CONF_ENABLE_SETTINGS, False):
        entities.extend(
            JuraWifiSettingSwitch(coordinator, definition)
            for definition in coordinator.profile.settings
            if setting_platform(definition) == PLATFORM_SWITCH
        )
    # Also removes the front panel lock that 0.5.0 to 0.5.2 created.
    async_remove_stale_entities(
        hass, entry, Platform.SWITCH, {entity.unique_id for entity in entities}
    )
    async_add_entities(entities)


class JuraWifiSettingSwitch(JuraWifiEntity, SwitchEntity):
    """A machine setting with two values, on and off, e.g. the quality assistant."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coordinator: JuraWifiCoordinator, definition: SettingDef
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, setting_key(definition))
        self._definition = definition
        items = on_off_items(definition)
        assert items is not None
        self._on, self._off = items
        self._attr_translation_key, self._attr_translation_placeholders = (
            setting_translation(definition)
        )

    @property
    def available(self) -> bool:
        """The setting can be shown and changed while the machine answers."""
        return super().available and self.online and self.is_on is not None

    @property
    def is_on(self) -> bool | None:
        """Return the value the machine reported the last time it was read."""
        raw = self.coordinator.data.settings.get(self._definition.p_argument.upper())
        item = stored_item(self._definition, raw)
        return None if item is None else item == self._on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Write the on value to the machine."""
        await self.coordinator.async_set_setting(
            self._definition, self._on.value, self._definition.raw_name
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Write the off value to the machine."""
        await self.coordinator.async_set_setting(
            self._definition, self._off.value, self._definition.raw_name
        )
