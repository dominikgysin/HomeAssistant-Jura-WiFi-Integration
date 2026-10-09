"""Brew buttons for the JURA Wi-Fi Connect integration."""

from __future__ import annotations

from jura_connect import ProductDef

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_ENABLE_BREWING
from .coordinator import JuraWifiConfigEntry, JuraWifiCoordinator
from .entity import JuraWifiEntity
from .sensor import product_label

# Product kinds that can be started remotely (everything but powder products).
BREWABLE_KINDS = frozenset({"C", "CM", "M", "T"})

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JuraWifiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up brew buttons when brewing is enabled in the options."""
    if not entry.options.get(CONF_ENABLE_BREWING, False):
        return
    coordinator = entry.runtime_data
    async_add_entities(
        JuraWifiBrewButton(coordinator, product)
        for product in coordinator.profile.products
        if product.active and product.kind in BREWABLE_KINDS
    )


class JuraWifiBrewButton(JuraWifiEntity, ButtonEntity):
    """Starts a product with its factory-default recipe."""

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
        """Start the product."""
        await self.coordinator.async_brew(self._product, self._label)
