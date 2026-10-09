"""Select entities for the machine settings that offer a list of values."""

from __future__ import annotations

from jura_connect import SettingDef, SettingItem

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_ENABLE_SETTINGS
from .coordinator import JuraWifiConfigEntry, JuraWifiCoordinator
from .entity import JuraWifiEntity, async_remove_stale_entities
from .settings import (
    PLATFORM_SELECT,
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
    """Set up the settings of the machine that are lists, if they are enabled."""
    coordinator = entry.runtime_data
    entities: list[JuraWifiSettingSelect] = []
    if entry.options.get(CONF_ENABLE_SETTINGS, False):
        entities = [
            JuraWifiSettingSelect(coordinator, definition)
            for definition in coordinator.profile.settings
            if setting_platform(definition) == PLATFORM_SELECT
        ]
    async_remove_stale_entities(
        hass, entry, Platform.SELECT, {entity.unique_id for entity in entities}
    )
    async_add_entities(entities)


class JuraWifiSettingSelect(JuraWifiEntity, SelectEntity):
    """A machine setting with a list of values, e.g. the switch-off time."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coordinator: JuraWifiCoordinator, definition: SettingDef
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, setting_key(definition))
        self._definition = definition
        self._attr_translation_key, self._attr_translation_placeholders = (
            setting_translation(definition)
        )
        items: dict[str, SettingItem] = {}
        for item in definition.items:
            items.setdefault(item.name, item)
        self._items = items
        self._attr_options = list(items)

    @property
    def available(self) -> bool:
        """The setting can be shown and changed while the machine answers."""
        return super().available and self.online and self.current_option is not None

    @property
    def current_option(self) -> str | None:
        """Return the value the machine reported the last time it was read."""
        raw = self.coordinator.data.settings.get(self._definition.p_argument.upper())
        item = stored_item(self._definition, raw)
        if item is None or item.name not in self._items:
            return None
        return item.name

    async def async_select_option(self, option: str) -> None:
        """Write the value to the machine."""
        await self.coordinator.async_set_setting(
            self._definition, self._items[option].value, self._definition.raw_name
        )
