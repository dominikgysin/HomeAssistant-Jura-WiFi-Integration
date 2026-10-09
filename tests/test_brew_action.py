"""The action jura_wifi.brew: a drink with single parameters of its recipe changed."""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.jura_wifi.api import (
    BREW_OPTIONS,
    JuraWifiBusy,
    JuraWifiConnectionError,
    JuraWifiError,
    MachineActivity,
)
from custom_components.jura_wifi.const import (
    CONF_ENABLE_BREWING,
    CONF_ENABLE_MAINTENANCE,
    DOMAIN,
    SERVICE_BREW,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .common import call, entity_id, poll, setup_with_options
from .conftest import SNAPSHOT, setup_entry


async def _brew(
    hass: HomeAssistant, entry: MockConfigEntry, product: str = "espresso", **fields
) -> None:
    await call(
        hass,
        DOMAIN,
        SERVICE_BREW,
        entity_id(hass, "button", entry, f"brew_{product}"),
        **fields,
    )


@pytest.fixture
async def brewing(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> MockConfigEntry:
    """Return an entry with the brew buttons enabled, set up."""
    await setup_with_options(hass, mock_config_entry, **{CONF_ENABLE_BREWING: True})
    return mock_config_entry


async def test_the_action_exists_whatever_the_options(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """It is registered with the integration, not with the buttons."""
    await setup_entry(hass, mock_config_entry)

    assert hass.services.has_service(DOMAIN, SERVICE_BREW)


async def test_a_drink_with_changed_parameters(
    hass: HomeAssistant, mock_client: MagicMock, brewing: MockConfigEntry
) -> None:
    """The values are checked and handed to the client with its argument names."""
    await _brew(hass, brewing, strength=6, water_amount=60, temperature="high")

    mock_client.brew.assert_called_once_with(
        "espresso", {"strength": 6, "ml": 60, "temperature": "high"}
    )


async def test_a_milk_drink_takes_the_milk_parameters(
    hass: HomeAssistant, mock_client: MagicMock, brewing: MockConfigEntry
) -> None:
    """The foam time and the break are the parameters of the latte macchiato."""
    await _brew(
        hass, brewing, "latte_macchiato", milk_foam_time=30, milk_break=10, strength=4
    )

    mock_client.brew.assert_called_once_with(
        "latte_macchiato", {"strength": 4, "milk_foam": 30, "milk_break": 10}
    )


async def test_the_bypass_of_an_americano(
    hass: HomeAssistant, mock_client: MagicMock, brewing: MockConfigEntry
) -> None:
    """Coffee plus extra water."""
    await _brew(hass, brewing, "americano", water_amount=70, bypass=45)

    mock_client.brew.assert_called_once_with("americano", {"ml": 70, "bypass": 45})


async def test_without_parameters_the_stored_recipe_is_brewed(
    hass: HomeAssistant, mock_client: MagicMock, brewing: MockConfigEntry
) -> None:
    """Anything not given keeps the recipe that is stored on the machine."""
    await _brew(hass, brewing)

    mock_client.brew.assert_called_once_with("espresso")


async def test_the_names_are_not_case_sensitive(
    hass: HomeAssistant, mock_client: MagicMock, brewing: MockConfigEntry
) -> None:
    """A template that says High is as good as high."""
    await _brew(hass, brewing, temperature="High")

    mock_client.brew.assert_called_once_with("espresso", {"temperature": "high"})


async def test_numbers_that_arrive_as_text_are_accepted(
    hass: HomeAssistant, mock_client: MagicMock, brewing: MockConfigEntry
) -> None:
    """Templates in automations hand over text."""
    await _brew(hass, brewing, strength="7", water_amount="55")

    mock_client.brew.assert_called_once_with("espresso", {"strength": 7, "ml": 55})


async def test_the_status_shows_the_drink_right_away(
    hass: HomeAssistant, mock_client: MagicMock, brewing: MockConfigEntry
) -> None:
    """The same as pressing the button."""
    coordinator = brewing.runtime_data
    mock_client.fetch.side_effect = JuraWifiBusy(MachineActivity("brewing", "espresso"))

    await _brew(hass, brewing, water_amount=60)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert coordinator.data.activity == MachineActivity("brewing", "espresso")


@pytest.mark.parametrize(
    ("product", "fields", "key"),
    [
        ("espresso", {"water_amount": 85}, "brew_option_range"),
        ("espresso", {"water_amount": 10}, "brew_option_range"),
        ("espresso", {"water_amount": 17}, "brew_option_step"),
        ("espresso", {"strength": 11}, "brew_option_choice"),
        ("espresso", {"temperature": "boiling"}, "brew_option_choice"),
        ("espresso", {"bypass": 20}, "brew_option_unsupported"),
        ("espresso", {"milk_foam_time": 20}, "brew_option_unsupported"),
        ("espresso", {"strength": 5, "milk_break": 5}, "brew_option_unsupported"),
        ("hotwater_portion_normal", {"strength": 5}, "brew_option_unsupported"),
        ("latte_macchiato", {"milk_break": 61}, "brew_option_range"),
    ],
)
async def test_what_the_drink_does_not_allow_is_refused_before_anything_is_sent(
    hass: HomeAssistant,
    mock_client: MagicMock,
    brewing: MockConfigEntry,
    product: str,
    fields: dict,
    key: str,
) -> None:
    """The profile of the drink decides, and the message says why."""
    with pytest.raises(ServiceValidationError) as err:
        await _brew(hass, brewing, product, **fields)

    assert err.value.translation_key == key
    assert err.value.translation_domain == DOMAIN
    placeholders = err.value.translation_placeholders
    assert placeholders is not None
    assert placeholders["product"]
    assert placeholders["option"] in fields
    mock_client.brew.assert_not_called()


async def test_the_message_names_the_drink_and_the_allowed_values(
    hass: HomeAssistant, mock_client: MagicMock, brewing: MockConfigEntry
) -> None:
    """A message that can be read in the user interface."""
    with pytest.raises(ServiceValidationError) as err:
        await _brew(hass, brewing, "espresso", temperature="boiling")

    assert str(err.value).startswith(
        "temperature for Espresso has to be one of: low, normal, high"
    )
    with pytest.raises(ServiceValidationError) as err:
        await _brew(hass, brewing, "espresso", water_amount=85)
    assert str(err.value).startswith(
        "water_amount for Espresso has to be between 15 and 80"
    )


@pytest.mark.parametrize(
    "fields",
    [{"strength": "strong"}, {"water_amount": "a lot"}, {"unknown_field": 1}],
)
async def test_values_the_schema_cannot_read_are_refused(
    hass: HomeAssistant,
    mock_client: MagicMock,
    brewing: MockConfigEntry,
    fields: dict,
) -> None:
    """Text for a number, and fields that the action does not have."""
    with pytest.raises(vol.Invalid):
        await _brew(hass, brewing, **fields)

    mock_client.brew.assert_not_called()


async def test_the_action_works_on_brew_buttons_only(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The drink is implied by the button, so the other buttons make no sense."""
    await setup_with_options(
        hass,
        mock_config_entry,
        **{CONF_ENABLE_BREWING: True, CONF_ENABLE_MAINTENANCE: True},
    )

    for key in ("start_cleaning", "cancel"):
        with pytest.raises(ServiceValidationError) as err:
            await call(
                hass,
                DOMAIN,
                SERVICE_BREW,
                entity_id(hass, "button", mock_config_entry, key),
                strength=5,
            )
        assert err.value.translation_key == "not_a_brew_button"
    mock_client.brew.assert_not_called()
    mock_client.start_process.assert_not_called()
    mock_client.cancel_step.assert_not_called()


async def test_no_second_drink_while_the_machine_is_busy(
    hass: HomeAssistant,
    mock_client: MagicMock,
    brewing: MockConfigEntry,
    freezer,
) -> None:
    """The same guard as for the buttons."""
    mock_client.fetch.side_effect = JuraWifiBusy(MachineActivity("brewing", "coffee"))
    await poll(hass, freezer)

    with pytest.raises(ServiceValidationError) as err:
        await _brew(hass, brewing, strength=5)

    assert err.value.translation_key == "machine_busy"
    mock_client.brew.assert_not_called()


async def test_a_blocked_product_is_not_brewed(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The machine blocks the drink while an alert needs attention."""
    mock_client.fetch.return_value = dataclasses.replace(
        SNAPSHOT, blocked_products=frozenset({"espresso"})
    )
    await setup_with_options(hass, mock_config_entry, **{CONF_ENABLE_BREWING: True})

    with pytest.raises(ServiceValidationError) as err:
        await _brew(hass, mock_config_entry, strength=5)

    assert err.value.translation_key == "product_blocked"
    mock_client.brew.assert_not_called()


async def test_the_machine_has_to_be_online(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Nothing is sent to a machine that does not answer."""
    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    await setup_with_options(hass, mock_config_entry, **{CONF_ENABLE_BREWING: True})

    with pytest.raises(HomeAssistantError) as err:
        await mock_config_entry.runtime_data.async_brew(
            "espresso", "Espresso", {"strength": 5}
        )

    assert err.value.translation_key == "machine_offline"
    mock_client.brew.assert_not_called()


async def test_a_refusal_of_the_machine_is_reported(
    hass: HomeAssistant, mock_client: MagicMock, brewing: MockConfigEntry
) -> None:
    """The machine can still say no."""
    mock_client.brew.side_effect = JuraWifiError("the machine rejected the request")

    with pytest.raises(HomeAssistantError, match="the machine rejected the request"):
        await _brew(hass, brewing, strength=5)


async def test_a_lost_connection_counts_towards_offline(
    hass: HomeAssistant, mock_client: MagicMock, brewing: MockConfigEntry
) -> None:
    """The same two-strike rule as for the buttons."""
    coordinator = brewing.runtime_data
    mock_client.brew.side_effect = JuraWifiConnectionError("peer closed")

    for _ in range(2):
        with pytest.raises(HomeAssistantError, match="peer closed"):
            await _brew(hass, brewing, strength=5)

    assert not coordinator.data.online


def test_the_schema_has_a_field_for_every_option() -> None:
    """The fields of the action are the options of the client, with their names."""
    from custom_components.jura_wifi import BREW_SCHEMA

    assert {str(key) for key in BREW_SCHEMA} == {option.key for option in BREW_OPTIONS}
    assert all(isinstance(key, vol.Optional) for key in BREW_SCHEMA)
