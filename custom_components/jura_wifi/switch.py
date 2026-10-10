"""Switches: on/off machine settings and the lock of the front panel."""

from __future__ import annotations

from typing import Any

from jura_connect import SettingDef

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_ENABLE_SETTINGS, LOCK_ALERTS
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
        known_alerts = {alert.name for alert in coordinator.profile.alerts}
        # The state comes from the alerts, so the machine has to declare one of them.
        if coordinator.profile_extras.front_panel_lock and LOCK_ALERTS & known_alerts:
            entities.append(JuraWifiFrontPanelLock(coordinator))
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


class JuraWifiFrontPanelLock(JuraWifiEntity, SwitchEntity):
    """Locks the keys of the machine, or releases them again.

    The state is the one the machine reports with its alerts. Right after a command
    the switch shows what was asked for, until a poll reads the alerts again. While
    the machine is busy, the alerts are those of the poll before, so what was asked
    is shown until the machine is idle again.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "front_panel_lock"

    def __init__(self, coordinator: JuraWifiCoordinator) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, "front_panel_lock")
        self._asked: bool | None = None

    @property
    def available(self) -> bool:
        """The panel can be locked or released while the machine answers."""
        return super().available and self.online and self.live_snapshot is not None

    @property
    def is_on(self) -> bool | None:
        """Return True while the machine reports its keys or screen as locked."""
        if self._asked is not None:
            return self._asked
        snapshot = self.live_snapshot
        if snapshot is None:
            return None
        return bool(LOCK_ALERTS & snapshot.active_alerts)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Show what the machine reports again, once a poll has read its alerts."""
        data = self.coordinator.data
        if not data.online or data.activity is None:
            self._asked = None
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Lock the front panel."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Release the front panel."""
        await self._async_set(False)

    async def _async_set(self, locked: bool) -> None:
        await self.coordinator.async_set_front_panel_lock(locked)
        self._asked = locked
        self.async_write_ha_state()
