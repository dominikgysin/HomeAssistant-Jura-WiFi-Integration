"""Tests for the config flow."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from jura_connect.profile import _catalogue
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
    CONF_AREA,
    CONF_ARTICLE_NUMBER,
    CONF_AUTH_HASH,
    CONF_CONN_ID,
    CONF_ENABLE_BREWING,
    CONF_ENABLE_MAINTENANCE,
    CONF_ENABLE_SETTINGS,
    CONF_FIRMWARE,
    CONF_MACHINE_TYPE,
    CONF_MODEL,
    CONF_MODEL_NAME,
    CONF_MODEL_SOURCE,
    CONF_PIN,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL_NUMBER,
    DOMAIN,
    MODEL_SOURCE_ARTICLE,
    MODEL_SOURCE_DISCOVERY,
    MODEL_SOURCE_MANUAL,
)
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar, entity_registry as er

from .conftest import AUTH_HASH, HOST, IDENTITY, SERIAL, setup_entry

USER_INPUT = {CONF_HOST: HOST}

# What the form of the options asks for, as answered without any change.
OPTIONS_INPUT = {
    CONF_SCAN_INTERVAL: 60,
    CONF_ENABLE_BREWING: False,
    CONF_ENABLE_MAINTENANCE: False,
    CONF_ENABLE_SETTINGS: False,
}


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


async def _finish(hass: HomeAssistant, result: dict, **answers) -> dict:
    """Answer the step that asks for the area and the options, then create the entry."""
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "options"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {**OPTIONS_INPUT, **answers}
    )


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
    """Only the address is asked first; the model is read from the machine."""
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

    # After the pairing and the identification the area and the options are asked.
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "options"
    assert [str(key) for key in result["data_schema"].schema] == [
        CONF_AREA,
        CONF_SCAN_INTERVAL,
        CONF_ENABLE_BREWING,
        CONF_ENABLE_MAINTENANCE,
        CONF_ENABLE_SETTINGS,
    ]
    result = await _finish(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "JURA E8 (SDS)"
    # The serial number of the machine identifies the entry, not the address.
    assert result["result"].unique_id == SERIAL
    # A new entry is up to date, there is nothing to migrate.
    assert (result["result"].version, result["result"].minor_version) == (1, 2)
    assert result["data"][CONF_HOST] == HOST
    assert result["data"][CONF_PORT] == 51515
    assert result["data"][CONF_MACHINE_TYPE] == "EF1120"
    assert result["data"][CONF_MODEL_NAME] == "E8 (SDS)"
    assert result["data"][CONF_ARTICLE_NUMBER] == 15833
    assert result["data"][CONF_FIRMWARE] == "TT237W V06.11"
    assert result["data"][CONF_SERIAL_NUMBER] == SERIAL
    assert result["data"][CONF_MODEL_SOURCE] == MODEL_SOURCE_DISCOVERY
    assert result["data"][CONF_AUTH_HASH] == AUTH_HASH
    assert result["data"][CONF_CONN_ID].startswith("homeassistant-")
    assert CONF_AREA not in result["data"]
    assert result["options"] == {
        CONF_SCAN_INTERVAL: 60,
        CONF_ENABLE_BREWING: False,
        CONF_ENABLE_MAINTENANCE: False,
        CONF_ENABLE_SETTINGS: False,
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
        result = await _finish(hass, result)
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
    result = await _finish(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "JURA E8 (SDS)"
    # No serial number is known without the discovery: the address identifies it.
    assert result["result"].unique_id == HOST
    assert CONF_SERIAL_NUMBER not in result["data"]
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
    result = await _finish(hass, result)
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
    result = await _finish(hass, result)
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
    result = await _finish(hass, result)
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
    result = await _finish(hass, result)
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
    result = await _finish(hass, result)
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
    # The serial number identifies the entry, so the new address does not change it.
    assert mock_config_entry.unique_id == str(SERIAL)


async def test_reconfigure_of_an_entry_identified_by_its_address(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_discover: MagicMock,
    legacy_config_entry: MockConfigEntry,
) -> None:
    """An entry that has no serial number yet follows the address."""
    mock_discover.return_value = None  # the dongle cannot be reached by UDP
    legacy_config_entry.add_to_hass(hass)
    result = await legacy_config_entry.start_reconfigure_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: NEW_HOST}
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert result["reason"] == "reconfigure_successful"
    assert legacy_config_entry.data[CONF_HOST] == NEW_HOST
    assert legacy_config_entry.unique_id == NEW_HOST


async def test_reconfigure_refuses_an_address_that_another_entry_uses(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The address is compared with that of the other entries, not with their IDs."""
    mock_config_entry.add_to_hass(hass)
    other = MockConfigEntry(
        domain=DOMAIN,
        title="JURA second",
        unique_id="999",
        data={**mock_config_entry.data, CONF_HOST: NEW_HOST, CONF_SERIAL_NUMBER: 999},
    )
    other.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: NEW_HOST.upper()}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert mock_config_entry.data[CONF_HOST] == HOST
    mock_client.check.assert_not_called()


def _article_of(ef_code: str) -> int:
    """Return an article number of the catalogue that belongs to a machine type."""
    return next(
        entry.article_number for entry in _catalogue() if entry.ef_code == ef_code
    )


async def test_reconfigure_offers_the_article_number(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_discover: MagicMock,
    legacy_config_entry: MockConfigEntry,
) -> None:
    """Where the dongle cannot be reached by UDP the article number is entered here."""
    mock_discover.return_value = None  # the dongle cannot be reached by UDP
    legacy_config_entry.add_to_hass(hass)
    result = await legacy_config_entry.start_reconfigure_flow(hass)
    assert {str(key) for key in result["data_schema"].schema} == {
        CONF_HOST,
        CONF_ARTICLE_NUMBER,
    }

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST, CONF_ARTICLE_NUMBER: " 15713 "}
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert result["reason"] == "reconfigure_successful"
    data = legacy_config_entry.data
    assert data[CONF_ARTICLE_NUMBER] == 15713
    assert data[CONF_MODEL_NAME] == "E8 (SD)"
    assert data[CONF_MODEL_SOURCE] == MODEL_SOURCE_ARTICLE
    # The machine type decides which profile is used. It stays what it was.
    assert data[CONF_MACHINE_TYPE] == "EF1120"
    # The address did not change, so there was nothing to check on the machine,
    # which may well be switched off.
    mock_client.check.assert_not_called()


async def test_reconfigure_prefills_the_article_number_that_is_known(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """A stored article number is shown, and entering it again changes nothing."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    suggested = {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description
    }
    assert suggested == {CONF_HOST: HOST, CONF_ARTICLE_NUMBER: "15833"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST, CONF_ARTICLE_NUMBER: "15833"}
    )

    assert result["reason"] == "reconfigure_successful"
    # The model of the entry came from the machine itself and stays so.
    assert mock_config_entry.data[CONF_MODEL_SOURCE] == MODEL_SOURCE_DISCOVERY


async def test_reconfigure_keeps_the_article_number_when_the_field_is_empty(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Leaving the field empty does not clear what is stored."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST, CONF_ARTICLE_NUMBER: ""}
    )

    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data[CONF_ARTICLE_NUMBER] == 15833


@pytest.mark.parametrize(
    ("typed", "error"),
    [
        ("99999", "unknown_article"),
        ("abc", "unknown_article"),
        # the article number of another machine type, here a machine without milk system
        (str(_article_of("EF1089")), "article_other_model"),
    ],
)
async def test_reconfigure_never_changes_the_machine_type(
    hass: HomeAssistant,
    mock_client: MagicMock,
    legacy_config_entry: MockConfigEntry,
    typed: str,
    error: str,
) -> None:
    """An article number of another model is refused, not silently taken over."""
    legacy_config_entry.add_to_hass(hass)
    result = await legacy_config_entry.start_reconfigure_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST, CONF_ARTICLE_NUMBER: typed}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    assert legacy_config_entry.data[CONF_MACHINE_TYPE] == "EF1120"
    assert CONF_ARTICLE_NUMBER not in legacy_config_entry.data
    assert legacy_config_entry.data[CONF_MODEL_NAME] == "E8 (SDS)"


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
        CONF_ENABLE_SETTINGS,
    }

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL: 120,
            CONF_ENABLE_BREWING: True,
            CONF_ENABLE_MAINTENANCE: True,
            CONF_ENABLE_SETTINGS: True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert mock_config_entry.options == {
        CONF_SCAN_INTERVAL: 120,
        CONF_ENABLE_BREWING: True,
        CONF_ENABLE_MAINTENANCE: True,
        CONF_ENABLE_SETTINGS: True,
    }
    assert mock_config_entry.state is ConfigEntryState.LOADED
    registry = er.async_get(hass)
    domains = {
        entry.domain
        for entry in er.async_entries_for_config_entry(
            registry, mock_config_entry.entry_id
        )
    }
    assert {"button", "number", "select", "switch"} <= domains
    assert mock_config_entry.runtime_data.update_interval.total_seconds() == 120


async def test_options_of_an_older_entry_have_no_maintenance_or_settings_key(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """An entry from before the newer buttons gets their switches, off."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_SCAN_INTERVAL: 60, CONF_ENABLE_BREWING: True}
    )

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    # Submitting the form untouched fills in the defaults of the new switches.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 60, CONF_ENABLE_BREWING: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_SCAN_INTERVAL: 60,
        CONF_ENABLE_BREWING: True,
        CONF_ENABLE_MAINTENANCE: False,
        CONF_ENABLE_SETTINGS: False,
    }


async def test_the_setup_asks_for_the_area_and_the_options(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    """The area is optional data of the entry; the rest become its options."""
    area = ar.async_get(hass).async_create("Kitchen")
    _, release = _pair_outcomes(mock_client, AUTH_HASH)

    result = await _pair(hass, release)
    result = await _finish(
        hass,
        result,
        **{
            CONF_AREA: area.id,
            CONF_SCAN_INTERVAL: 120,
            CONF_ENABLE_BREWING: True,
            CONF_ENABLE_MAINTENANCE: True,
            CONF_ENABLE_SETTINGS: True,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_AREA] == area.id
    assert result["options"] == {
        CONF_SCAN_INTERVAL: 120,
        CONF_ENABLE_BREWING: True,
        CONF_ENABLE_MAINTENANCE: True,
        CONF_ENABLE_SETTINGS: True,
    }


async def test_the_area_can_be_left_out(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    """Without an area the entry holds none, and the entry is set up all the same."""
    _, release = _pair_outcomes(mock_client, AUTH_HASH)

    result = await _pair(hass, release)
    result = await _finish(hass, result)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_AREA not in result["data"]
    assert result["result"].state is ConfigEntryState.LOADED


async def test_the_options_are_asked_after_the_model_is_known(
    hass: HomeAssistant, mock_client: MagicMock, mock_discover: MagicMock
) -> None:
    """Nothing is created before the options were answered."""
    _, release = _pair_outcomes(mock_client, AUTH_HASH)

    result = await _pair(hass, release)

    assert result["step_id"] == "options"
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_a_machine_that_is_set_up_already_is_found_by_its_serial_number(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The dongle may have got another address; the machine is still the same."""
    mock_config_entry.add_to_hass(hass)
    _, release = _pair_outcomes(mock_client, AUTH_HASH)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: NEW_HOST}
    )
    release.set()

    result = await _settle(hass, result)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_a_second_machine_is_set_up_next_to_the_first(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_discover: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Another serial number is another machine."""
    mock_config_entry.add_to_hass(hass)
    mock_discover.return_value = MachineIdentity(
        article_number=15833,
        firmware="TT237W V06.11",
        ef_code="EF1120",
        model_name="E8 (SDS)",
        serial_number="20240117001235",
    )
    _, release = _pair_outcomes(mock_client, AUTH_HASH)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: NEW_HOST}
    )
    release.set()

    result = await _settle(hass, result)
    result = await _finish(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "20240117001235"
    assert result["data"][CONF_HOST] == NEW_HOST
