"""Fixtures for the JURA Wi-Fi Connect tests."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock, patch

from jura_connect import load_profile
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jura_wifi.api import MachineIdentity, MachineSnapshot
from custom_components.jura_wifi.const import (
    CONF_ARTICLE_NUMBER,
    CONF_AUTH_HASH,
    CONF_CONN_ID,
    CONF_ENABLE_BREWING,
    CONF_FIRMWARE,
    CONF_MACHINE_TYPE,
    CONF_MODEL_NAME,
    CONF_MODEL_SOURCE,
    CONF_PIN,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL_NUMBER,
    DOMAIN,
    MODEL_SOURCE_DISCOVERY,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

HOST = "192.0.2.10"
AUTH_HASH = "f" * 64

# The serial number is a 16 bit field of the discovery reply of the dongle.
SERIAL = 4711

# What the J.O.E. app shows for the real machine: article 15833, E8 (SDS).
IDENTITY = MachineIdentity(
    article_number=15833,
    firmware="TT237W V06.11",
    ef_code="EF1120",
    model_name="E8 (SDS)",
    serial_number=SERIAL,
)

# A progress frame captured from a real E8 whose display sat in its menu.
P_MODE_FRAME = "@TV:FF510000149305B9057F05AC058E"

# Recipes as stored on a real E8 (SDS), read with @TM:41, and what they mean for
# the brew command. Several drinks were adjusted at the display: for example the
# factory recipe of the cappuccino has 14 s of foam, the latte macchiato 22 s with a
# 30 s pause, the americano 70 ml of coffee plus 50 ml of water. The profile marks
# the cortado and the americano as not programmable, yet the machine answers for
# them. The last byte is not a parameter: it differs from read to read.
E8_STORED_RECIPES = {
    "espresso": "0200080900000200000000000066",
    "coffee": "0300051400000100000000000077",
    "cortado": "2B00080500040200000000000000",
    "americano": "2800060C00000100000800000000",
    "lungo": "2900071800000100001400000000",
    "cappuccino": "0400080C00140100000000000000",
    "latte_macchiato": "0700080900210200000014000000",
    "espresso_macchiato": "06000805000402000000000000DD",
    "flat_white": "2E00050C001201000000000000DD",
    "espresso_doppio": "30000812000002000000000000AA",
    "milk_foam": "08000100000F00000000000000AA",
    "hotwater_portion_normal": "0D00012C000001000000000000AA",
}
E8_RECIPE_ARGUMENTS = {
    "espresso": {"strength": 8, "ml": 45, "temperature": 2},
    "coffee": {"strength": 5, "ml": 100, "temperature": 1},
    "cortado": {"strength": 8, "ml": 25, "temperature": 2, "milk_foam": 4},
    "americano": {"strength": 6, "ml": 60, "temperature": 1, "bypass": 40},
    "lungo": {"strength": 7, "ml": 120, "temperature": 1, "bypass": 100},
    "cappuccino": {"strength": 8, "ml": 60, "temperature": 1, "milk_foam": 20},
    "latte_macchiato": {
        "strength": 8,
        "ml": 45,
        "temperature": 2,
        "milk_foam": 33,
        "milk_break": 20,
    },
    "espresso_macchiato": {"strength": 8, "ml": 25, "temperature": 2, "milk_foam": 4},
    "flat_white": {"strength": 5, "ml": 60, "temperature": 1, "milk_foam": 18},
    "espresso_doppio": {"strength": 8, "ml": 90, "temperature": 2},
    "milk_foam": {"milk_foam": 15},
    "hotwater_portion_normal": {"ml": 220, "temperature": 1},
}
# The drinks whose stored recipe is not the factory recipe.
E8_ADJUSTED_DRINKS = {
    "cortado",
    "americano",
    "cappuccino",
    "latte_macchiato",
    "espresso_macchiato",
    "flat_white",
    "milk_foam",
}

# Values read from a real E8 (SDS).
SNAPSHOT = MachineSnapshot(
    active_alerts=frozenset({"coffee_ready"}),
    errors=frozenset(),
    blocked_products=frozenset(),
    maintenance_counters={
        "cleaning": 0,
        "filter_change": 1,
        "descale": 0,
        "cappu_rinse": 9,
        "coffee_rinse": 23,
        "cappu_clean": 2,
    },
    maintenance_percent={"cleaning": 10, "descale": 0},
    total_brews=20,
    product_counts={"espresso": 7, "coffee": 8, "cappuccino": 4, "latte_macchiato": 1},
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Allow Home Assistant to load the custom integration."""


@pytest.fixture(autouse=True)
def cache_storage(hass_storage: dict[str, Any]) -> dict[str, Any]:
    """Keep the cache of the integration out of the file system.

    What the integration wrote is found in the returned dictionary, by storage key.
    """
    return hass_storage


@pytest.fixture
def mock_client_cls() -> Generator[MagicMock]:
    """Replace the blocking client class so that no socket is ever opened."""
    with (
        patch("custom_components.jura_wifi.JuraWifiClient") as client_cls,
        patch("custom_components.jura_wifi.config_flow.JuraWifiClient", client_cls),
    ):
        yield client_cls


@pytest.fixture
def mock_discover() -> Generator[MagicMock]:
    """Replace the UDP discovery; by default the machine announces its model.

    The setup flow and the background fill-in of an existing entry use the same
    replacement, so that their calls are counted together.
    """
    with (
        patch(
            "custom_components.jura_wifi.config_flow.discover_machine",
            return_value=IDENTITY,
        ) as discover,
        patch("custom_components.jura_wifi.coordinator.discover_machine", discover),
    ):
        yield discover


@pytest.fixture
def mock_client(mock_client_cls: MagicMock, mock_discover: MagicMock) -> MagicMock:
    """Return the client instance used by the integration and the config flow."""
    client = mock_client_cls.return_value
    client.profile = load_profile("EF1120")
    client.fetch.return_value = SNAPSHOT
    client.pair.return_value = AUTH_HASH
    return client


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a configured entry for an E8 (SDS), as the setup creates it now."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="JURA E8 (SDS)",
        unique_id=str(SERIAL),
        data={
            CONF_HOST: HOST,
            CONF_PORT: 51515,
            CONF_PIN: "",
            CONF_MACHINE_TYPE: "EF1120",
            CONF_MODEL_NAME: "E8 (SDS)",
            CONF_ARTICLE_NUMBER: 15833,
            CONF_FIRMWARE: "TT237W V06.11",
            CONF_SERIAL_NUMBER: SERIAL,
            CONF_MODEL_SOURCE: MODEL_SOURCE_DISCOVERY,
            CONF_CONN_ID: "homeassistant-12345678",
            CONF_AUTH_HASH: AUTH_HASH,
        },
        options={CONF_SCAN_INTERVAL: 60, CONF_ENABLE_BREWING: False},
    )


@pytest.fixture
def legacy_config_entry() -> MockConfigEntry:
    """Return an entry as 0.1.0 created it: no article, firmware or serial number."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="JURA E8 (SDS)",
        unique_id=HOST,
        data={
            CONF_HOST: HOST,
            CONF_PORT: 51515,
            CONF_PIN: "",
            CONF_MACHINE_TYPE: "EF1120",
            CONF_MODEL_NAME: "E8 (SDS)",
            CONF_CONN_ID: "homeassistant-12345678",
            CONF_AUTH_HASH: AUTH_HASH,
        },
        options={CONF_SCAN_INTERVAL: 60, CONF_ENABLE_BREWING: False},
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add the entry and set it up."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
