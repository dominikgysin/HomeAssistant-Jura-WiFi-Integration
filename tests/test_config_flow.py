"""Tests for the config flow."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jura_wifi.api import (
    JuraWifiAuthError,
    JuraWifiBusy,
    JuraWifiConnectionError,
    JuraWifiError,
    JuraWifiPairingTimeout,
    MachineActivity,
    MachineIdentity,
)
from custom_components.jura_wifi.const import (
    CONF_ARTICLE_NUMBER,
    CONF_AUTH_HASH,
    CONF_CONN_ID,
    CONF_ENABLE_BREWING,
    CONF_ENABLE_MAINTENANCE,
    CONF_FIRMWARE,
    CONF_MACHINE_TYPE,
    CONF_MODEL,
    CONF_MODEL_NAME,
    CONF_MODEL_SOURCE,
    CONF_PIN,
    CONF_SCAN_INTERVAL,
    DOMAIN,
    MODEL_SOURCE_ARTICLE,
    MODEL_SOURCE_DISCOVERY,
    MODEL_SOURCE_MANUAL,
)
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from .conftest import AUTH_HASH, HOST, IDENTITY, setup_entry

USER_INPUT = {CONF_HOST: HOST}


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


async def _pair(hass: HomeAssistant, release: threading.Event) -> dict:
    """Submit the address, let the pairing end while it is shown and settle.

    The pairing must still be running when the address is submitted, otherwise
    Home Assistant would hand the submitted address on to the following step.
    """
    result = await _submit_user_input(hass)
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    release.set()
    return await _settle(hass, result)


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


async def test_user_flow(
    hass: HomeAssistant, mock_client: MagicMock, mock_discover: MagicMock
) -> None:
    """Only the address is asked; the model is read from the machine."""
    calls, release = _pair_outcomes(mock_client, AUTH_HASH)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert [str(key) for key in result["data_schema"].schema] == [CONF_HOST]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "pair"
    assert result["progress_action"] == "pair"
    assert result["description_placeholders"] == {"host": HOST}

    release.set()
    result = await _settle(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "JURA E8 (SDS)"
    assert result["result"].unique_id == HOST
    assert result["data"][CONF_HOST] == HOST
    assert result["data"][CONF_PORT] == 51515
    assert result["data"][CONF_MACHINE_TYPE] == "EF1120"
    assert result["data"][CONF_MODEL_NAME] == "E8 (SDS)"
    assert result["data"][CONF_ARTICLE_NUMBER] == 15833
    assert result["data"][CONF_FIRMWARE] == "TT237W V06.11"
    assert result["data"][CONF_MODEL_SOURCE] == MODEL_SOURCE_DISCOVERY
    assert result["data"][CONF_AUTH_HASH] == AUTH_HASH
    assert result["data"][CONF_CONN_ID].startswith("homeassistant-")
    assert result["options"] == {
        CONF_SCAN_INTERVAL: 60,
        CONF_ENABLE_BREWING: False,
        CONF_ENABLE_MAINTENANCE: False,
    }
    assert len(calls) == 1
    mock_discover.assert_called_once_with(HOST)


async def test_discovery_runs_after_pairing(
    hass: HomeAssistant, mock_client: MagicMock, mock_discover: MagicMock
) -> None:
    """The model is read only once the machine accepted the pairing."""
    _, release = _pair_outcomes(mock_client, JuraWifiPairingTimeout("no"))

    result = await _pair(hass, release)
    assert result["step_id"] == "pair_failed"
    mock_discover.assert_not_called()


async def test_identify_shows_progress_while_searching(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    """The search of the model is visible as its own progress step."""
    found = threading.Event()

    def slow_discovery(host: str) -> MachineIdentity:
        found.wait(5)
        return IDENTITY

    _, release = _pair_outcomes(mock_client, AUTH_HASH)

    # A plain function, because the harness runs executor jobs of mocks inline.
    with patch(
        "custom_components.jura_wifi.config_flow.discover_machine", slow_discovery
    ):
        result = await _submit_user_input(hass)
        assert result["step_id"] == "pair"
        release.set()
        while result["type"] is FlowResultType.SHOW_PROGRESS and (
            result["step_id"] != "identify"
        ):
            await hass.async_block_till_done()
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        assert result["step_id"] == "identify"
        assert result["progress_action"] == "identify"
        assert result["description_placeholders"] == {"host": HOST}

        found.set()
        result = await _settle(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MODEL_NAME] == "E8 (SDS)"


async def test_article_number_when_the_machine_does_not_announce_itself(
    hass: HomeAssistant, mock_client: MagicMock, mock_discover: MagicMock
) -> None:
    """Without a discovery reply the article number from the app is asked for."""
    mock_discover.return_value = None
    _, release = _pair_outcomes(mock_client, AUTH_HASH)

    result = await _pair(hass, release)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "article"
    assert [str(key) for key in result["data_schema"].schema] == [CONF_ARTICLE_NUMBER]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ARTICLE_NUMBER: " 15833 "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "JURA E8 (SDS)"
    assert result["data"][CONF_MACHINE_TYPE] == "EF1120"
    assert result["data"][CONF_MODEL_NAME] == "E8 (SDS)"
    assert result["data"][CONF_ARTICLE_NUMBER] == 15833
    assert result["data"][CONF_FIRMWARE] is None
    assert result["data"][CONF_MODEL_SOURCE] == MODEL_SOURCE_ARTICLE


@pytest.mark.parametrize("typed", ["99999", "abc", "15,833", "E8"])
async def test_unknown_article_number_can_be_corrected(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_discover: MagicMock,
    typed: str,
) -> None:
    """An article number that is not in the catalogue is rejected."""
    mock_discover.return_value = None
    _, release = _pair_outcomes(mock_client, AUTH_HASH)

    result = await _pair(hass, release)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ARTICLE_NUMBER: typed}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "article"
    assert result["errors"] == {"base": "unknown_article"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ARTICLE_NUMBER: "15713"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MODEL_NAME] == "E8 (SD)"


async def test_empty_article_number_opens_the_model_list(
    hass: HomeAssistant, mock_client: MagicMock, mock_discover: MagicMock
) -> None:
    """The list of all models is the last resort."""
    mock_discover.return_value = None
    _, release = _pair_outcomes(mock_client, AUTH_HASH)

    result = await _pair(hass, release)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ARTICLE_NUMBER: ""}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "model"
    selector = result["data_schema"].schema[CONF_MODEL]
    values = {option["value"] for option in selector.config["options"]}
    assert "EF1120|E8 (SDS)" in values
    assert "EF1091|S8 (EB)" in values

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODEL: "EF1120|E8 (SD)"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MACHINE_TYPE] == "EF1120"
    assert result["data"][CONF_MODEL_NAME] == "E8 (SD)"
    assert result["data"][CONF_ARTICLE_NUMBER] is None
    assert result["data"][CONF_MODEL_SOURCE] == MODEL_SOURCE_MANUAL


async def test_article_missing_from_the_catalogue_keeps_what_was_learned(
    hass: HomeAssistant, mock_client: MagicMock, mock_discover: MagicMock
) -> None:
    """A reply with an unknown article still tells the firmware and the article."""
    mock_discover.return_value = MachineIdentity(
        article_number=19999, firmware="TT237W V09.99", ef_code=None, model_name=None
    )
    _, release = _pair_outcomes(mock_client, AUTH_HASH)

    result = await _pair(hass, release)
    assert result["step_id"] == "article"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ARTICLE_NUMBER: ""}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODEL: "EF1120|E8 (SDS)"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ARTICLE_NUMBER] == 19999
    assert result["data"][CONF_FIRMWARE] == "TT237W V09.99"
    assert result["data"][CONF_MODEL_SOURCE] == MODEL_SOURCE_MANUAL


async def test_discovery_errors_fall_back_to_the_article_number(
    hass: HomeAssistant, mock_client: MagicMock, mock_discover: MagicMock
) -> None:
    """A crash while reading the model never blocks the setup."""
    mock_discover.side_effect = RuntimeError("boom")
    _, release = _pair_outcomes(mock_client, AUTH_HASH)

    result = await _pair(hass, release)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "article"


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
        # A machine in its settings menu cannot show the prompt: tell the user so.
        (JuraWifiBusy(MachineActivity("programming")), "machine_in_menu"),
        (JuraWifiBusy(MachineActivity("brewing", "espresso")), "machine_busy"),
        (JuraWifiBusy(MachineActivity("busy", "hotwater_volume")), "machine_busy"),
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
    # The reason reported by the machine is shown to make refusals diagnosable.
    assert result["description_placeholders"]["host"] == HOST
    assert result["description_placeholders"]["reason"] != "-"

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
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_discover: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Pairing again replaces the stored credentials and keeps the model."""
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
    assert mock_config_entry.data[CONF_MODEL_NAME] == "E8 (SDS)"
    mock_discover.assert_not_called()
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


async def test_options_flow_enables_the_controls(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Changing the options reloads the entry and adds the control buttons."""
    await setup_entry(hass, mock_config_entry)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert {str(key) for key in result["data_schema"].schema} == {
        CONF_SCAN_INTERVAL,
        CONF_ENABLE_BREWING,
        CONF_ENABLE_MAINTENANCE,
    }

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL: 120,
            CONF_ENABLE_BREWING: True,
            CONF_ENABLE_MAINTENANCE: True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert mock_config_entry.options == {
        CONF_SCAN_INTERVAL: 120,
        CONF_ENABLE_BREWING: True,
        CONF_ENABLE_MAINTENANCE: True,
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


async def test_options_of_an_older_entry_have_no_maintenance_key(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """An entry from before the maintenance buttons gets the new switch, off."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_SCAN_INTERVAL: 60, CONF_ENABLE_BREWING: True}
    )

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    # Submitting the form untouched fills in the default of the new switch.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 60, CONF_ENABLE_BREWING: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_SCAN_INTERVAL: 60,
        CONF_ENABLE_BREWING: True,
        CONF_ENABLE_MAINTENANCE: False,
    }
