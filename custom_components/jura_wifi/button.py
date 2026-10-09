"""Buttons to control the machine: brew, maintenance programs and cancel."""

from __future__ import annotations

from jura_connect import ProductDef

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import UNDECLARED_PROGRAMS
from .const import CONF_ENABLE_BREWING, CONF_ENABLE_MAINTENANCE
from .coordinator import JuraWifiConfigEntry, JuraWifiCoordinator
from .entity import JuraWifiEntity
from .sensor import product_label

# Product kinds that can be started remotely (everything but powder products).
BREWABLE_KINDS = frozenset({"C", "CM", "M", "T"})

# The maintenance programs the integration can start, with the name used in
# messages. The profile of the machine decides which are offered, except for the
# programs that are started even if it does not list them.
MAINTENANCE_PROGRAMS = {
    "cleaning": "Cleaning",
    "descale": "Descaling",
    "filter_change": "Filter change",
    "cappu_rinse": "Milk system rinse",
    "cappu_clean": "Milk system cleaning",
    "coffee_rinse": "Coffee system rinse",
}

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JuraWifiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the control buttons that are enabled in the options."""
    coordinator = entry.runtime_data
    brewing = entry.options.get(CONF_ENABLE_BREWING, False)
    maintenance = entry.options.get(CONF_ENABLE_MAINTENANCE, False)

    entities: list[ButtonEntity] = []
    if brewing:
        entities.extend(
            JuraWifiBrewButton(coordinator, product)
            for product in coordinator.profile.products
            if product.active and product.kind in BREWABLE_KINDS
        )
    if maintenance:
        offered = {
            process.name for process in coordinator.profile.processes
        } | UNDECLARED_PROGRAMS
        entities.extend(
            JuraWifiMaintenanceButton(coordinator, process)
            for process in MAINTENANCE_PROGRAMS
            if process in offered
        )
    if brewing or maintenance:
        entities.append(JuraWifiCancelButton(coordinator))

    # Buttons of an option that was switched off again would stay behind as
    # unavailable entities.
    registry = er.async_get(hass)
    wanted = {entity.unique_id for entity in entities}
    for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registered.domain == "button" and registered.unique_id not in wanted:
            registry.async_remove(registered.entity_id)

    async_add_entities(entities)


class JuraWifiBrewButton(JuraWifiEntity, ButtonEntity):
    """Starts a drink with the recipe stored on the machine."""

    _attr_translation_key = "brew"

    def __init__(self, coordinator: JuraWifiCoordinator, product: ProductDef) -> None:
        """Initialize the button."""
        super().__init__(coordinator, f"brew_{product.name}")
        self._product = product.name
        self._label = product_label(product)
        self._attr_translation_placeholders = {"product": self._label}

    @property
    def available(self) -> bool:
        """Brewing is only possible while the machine is reachable."""
        return super().available and self.online

    async def async_press(self) -> None:
        """Start the drink."""
        await self.coordinator.async_brew(self._product, self._label)


class JuraWifiMaintenanceButton(JuraWifiEntity, ButtonEntity):
    """Starts a maintenance program; the machine continues on its display."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: JuraWifiCoordinator, process: str) -> None:
        """Initialize the button."""
        super().__init__(coordinator, f"start_{process}")
        self._process = process
        self.entity_description = ButtonEntityDescription(
            key=f"start_{process}", translation_key=f"start_{process}"
        )

    @property
    def available(self) -> bool:
        """Programs can only be started while the machine is reachable."""
        return super().available and self.online

    async def async_press(self) -> None:
        """Start the program."""
        await self.coordinator.async_start_process(
            self._process, MAINTENANCE_PROGRAMS[self._process]
        )


class JuraWifiCancelButton(JuraWifiEntity, ButtonEntity):
    """Cancels the running drink or maintenance step."""

    entity_description = ButtonEntityDescription(key="cancel", translation_key="cancel")

    def __init__(self, coordinator: JuraWifiCoordinator) -> None:
        """Initialize the button."""
        super().__init__(coordinator, "cancel")

    @property
    def available(self) -> bool:
        """Cancelling is possible while the machine is reachable, busy or not."""
        return super().available and self.online

    async def async_press(self) -> None:
        """Cancel the current step."""
        await self.coordinator.async_cancel_step()
