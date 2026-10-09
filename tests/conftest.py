"""Fixtures for the JURA Wi-Fi Connect tests."""

from __future__ import annotations

from collections.abc import Generator
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
    DOMAIN,
    MODEL_SOURCE_DISCOVERY,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

HOST = "192.0.2.10"
AUTH_HASH = "f" * 64

# What the J.O.E. app shows for the real machine: article 15833, E8 (SDS).
IDENTITY = MachineIdentity(
    article_number=15833,
    firmware="TT237W V06.11",
    ef_code="EF1120",
    model_name="E8 (SDS)",
)

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
    """Replace the UDP discovery; by default the machine announces its model."""
    with patch(
        "custom_components.jura_wifi.config_flow.discover_machine",
        return_value=IDENTITY,
    ) as discover:
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
    """Return a configured entry for an E8 (SDS)."""
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
            CONF_ARTICLE_NUMBER: 15833,
            CONF_FIRMWARE: "TT237W V06.11",
            CONF_MODEL_SOURCE: MODEL_SOURCE_DISCOVERY,
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
