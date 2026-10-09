"""Tests for the config flow."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jura_wifi.api import (
    JuraWifiAuthError,
    JuraWifiConnectionError,
    JuraWifiError,
    JuraWifiPairingTimeout,
)
from custom_components.jura_wifi.const import (
    CONF_AUTH_HASH,
    CONF_CONN_ID,
    CONF_ENABLE_BREWING,
    CONF_MACHINE_TYPE,
    CONF_MODEL,
    CONF_MODEL_NAME,
    CONF_PIN,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from .conftest import AUTH_HASH, HOST, setup_entry

USER_INPUT = {CONF_HOST: HOST, CONF_MODEL: "EF1120|E8 (SD)", CONF_PIN: ""}


async def _submit_user_input(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)


async def _settle(hass: HomeAssistant, result: dict) -> dict:
    """Wait for a running progress step and return the result that follows it."""
    while result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    return result


def _pair_outcomes(
    mock_client: MagicMock, *outcomes: str | Exception
) -> tuple[list[int], threading.Event]:
    """Make pairing return/raise the given outcomes in order.

    Pairing blocks until the returned event is set, like the real client waits
    for the user to confirm on the machine. A plain function is used because
    the test harness runs executor jobs inline when the target is a mock.
    """
    pending = list(outcomes)
    calls: list[int] = []
    release = threading.Event()

    def pair(*args, **kwargs) -> str:
        calls.append(1)
        release.wait(5)
        outcome = pending.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    mock_client.pair = pair
    return calls, release


async def test_user_flow(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """Pairing shows a progress step until confirmed, then creates the entry."""
    calls, release = _pair_outcomes(mock_client, AUTH_HASH)

    result = await _submit_user_input(hass)
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "pair"
    assert result["progress_action"] == "pair"
    assert result["description_placeholders"] == {"host": HOST}

    release.set()
    result = await _settle(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "JURA E8 (SD)"
    assert result["result"].unique_id == HOST
    assert result["data"][CONF_HOST] == HOST
    assert result["data"][CONF_PORT] == 51515
    assert result["data"][CONF_MACHINE_TYPE] == "EF1120"
    assert result["data"][CONF_MODEL_NAME] == "E8 (SD)"
    assert result["data"][CONF_AUTH_HASH] == AUTH_HASH
    assert result["data"][CONF_CONN_ID].startswith("homeassistant-")
    assert result["options"] == {CONF_SCAN_INTERVAL: 60, CONF_ENABLE_BREWING: False}
    assert len(calls) == 1


async def test_model_list_contains_the_e8(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    """The selector offers the models that have a bundled profile."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    schema = result["data_schema"].schema
    selector = next(v for k, v in schema.items() if k == CONF_MODEL)
    values = {option["value"] for option in selector.config["options"]}
    assert "EF1120|E8 (SD)" in values
    assert "EF1091|S8 (EB)" in values


async def test_duplicate_host(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The same machine cannot be added twice."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (JuraWifiPairingTimeout("no confirmation"), "pairing_timeout"),
        (JuraWifiAuthError("WRONG_PIN"), "wrong_pin"),
        (JuraWifiAuthError("ABORTED"), "pairing_rejected"),
        (JuraWifiConnectionError("refused"), "cannot_connect"),
        (JuraWifiError("garbled"), "unknown"),
    ],
)
async def test_pairing_errors_can_be_retried(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_client_cls: MagicMock,
    error: Exception,
    expected: str,
) -> None:
    """A failed pairing shows the reason and a retry succeeds."""
    calls, release = _pair_outcomes(mock_client, error, AUTH_HASH)

    result = await _submit_user_input(hass)
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    release.set()
    result = await _settle(hass, result)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair_failed"
    assert result["errors"] == {"base": expected}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await _settle(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(calls) == 2
    # The dongle remembers earlier attempts, so the retry uses a new identifier.
    first, second = (
        call.args[2] for call in mock_client_cls.call_args_list if call.args
    )
    assert first != second
    assert result["data"][CONF_CONN_ID] == second


async def test_wrong_pin_can_be_corrected(
    hass: HomeAssistant, mock_client: MagicMock, mock_client_cls: MagicMock
) -> None:
    """After a wrong PIN the form offers a PIN field and the new PIN is used."""
    _, release = _pair_outcomes(mock_client, JuraWifiAuthError("WRONG_PIN"), AUTH_HASH)

    result = await _submit_user_input(hass)
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    release.set()
    result = await _settle(hass, result)
    assert result["step_id"] == "pair_failed"
    assert result["errors"] == {"base": "wrong_pin"}
    assert CONF_PIN in {str(key) for key in result["data_schema"].schema}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PIN: "4711"}
    )
    result = await _settle(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PIN] == "4711"
    pairing_calls = [call for call in mock_client_cls.call_args_list if call.args]
    assert pairing_calls[-1].args[4] == "4711"


async def test_other_errors_do_not_ask_for_a_pin(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    """The PIN field only shows up when the machine rejected the PIN."""
    _, release = _pair_outcomes(mock_client, JuraWifiPairingTimeout("no"))
    result = await _submit_user_input(hass)
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    release.set()
    result = await _settle(hass, result)
    assert result["step_id"] == "pair_failed"
    assert result["data_schema"] is None


async def test_closing_the_flow_releases_the_dongle(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    """Aborting while the machine waits for confirmation cancels the pairing."""
    _, release = _pair_outcomes(mock_client, AUTH_HASH)
    result = await _submit_user_input(hass)
    assert result["type"] is FlowResultType.SHOW_PROGRESS

    hass.config_entries.flow.async_abort(result["flow_id"])
    mock_client.cancel.assert_called_once()
    release.set()
    await hass.async_block_till_done()


async def test_reauth(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Pairing again replaces the stored credentials."""
    mock_client.pair.return_value = "b" * 64
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await _settle(hass, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"

    assert mock_config_entry.data[CONF_AUTH_HASH] == "b" * 64
    assert mock_config_entry.data[CONF_CONN_ID] != "homeassistant-12345678"
    assert mock_config_entry.data[CONF_HOST] == HOST
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_reauth_with_a_changed_pin(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """A PIN that was set on the machine later can be entered during reauth."""
    _, release = _pair_outcomes(mock_client, JuraWifiAuthError("WRONG_PIN"), "c" * 64)
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    release.set()
    result = await _settle(hass, result)
    assert result["step_id"] == "pair_failed"
    assert result["errors"] == {"base": "wrong_pin"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PIN: "4711"}
    )
    result = await _settle(hass, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PIN] == "4711"
    assert mock_config_entry.data[CONF_AUTH_HASH] == "c" * 64


NEW_HOST = "192.0.2.20"


async def test_reconfigure(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The address can be changed if the machine answers there."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    mock_client.check.side_effect = JuraWifiConnectionError("refused")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: NEW_HOST}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}

    mock_client.check.side_effect = JuraWifiAuthError("WRONG_HASH")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: NEW_HOST}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    mock_client.check.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: NEW_HOST}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data[CONF_HOST] == NEW_HOST
    assert mock_config_entry.unique_id == NEW_HOST


async def test_options_flow_enables_brewing(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Changing the options reloads the entry and adds the brew buttons."""
    await setup_entry(hass, mock_config_entry)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 120, CONF_ENABLE_BREWING: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert mock_config_entry.options == {
        CONF_SCAN_INTERVAL: 120,
        CONF_ENABLE_BREWING: True,
    }
    assert mock_config_entry.state is ConfigEntryState.LOADED
    registry = er.async_get(hass)
    assert any(
        entry.domain == "button"
        for entry in er.async_entries_for_config_entry(
            registry, mock_config_entry.entry_id
        )
    )
    assert mock_config_entry.runtime_data.update_interval.total_seconds() == 120
