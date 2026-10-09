"""How the machine is polled: the interval, the log lines and a refusing machine."""

from __future__ import annotations

import dataclasses
from datetime import timedelta
import logging
from unittest.mock import MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jura_wifi import api
from custom_components.jura_wifi.api import (
    JuraWifiBusy,
    JuraWifiConnectionError,
    MachineActivity,
)
from custom_components.jura_wifi.const import (
    ACTIVE_SCAN_INTERVAL,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SESSION_GAP_SECONDS,
)
from custom_components.jura_wifi.coordinator import JuraWifiData
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from .common import poll, setup_with_options, state
from .conftest import SNAPSHOT, setup_entry

COORDINATOR_LOGGER = "custom_components.jura_wifi.coordinator"

BREWING = JuraWifiBusy(MachineActivity("brewing", "coffee"))


def _info_lines(caplog) -> list[str]:
    """Return the messages that the coordinator logged at INFO or above."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == COORDINATOR_LOGGER and record.levelno >= logging.INFO
    ]


async def test_the_machine_is_polled_often_while_it_is_active(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """The status does not linger up to a minute after the drink is done."""
    await setup_entry(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data
    assert coordinator.update_interval == timedelta(seconds=DEFAULT_SCAN_INTERVAL)

    mock_client.fetch.side_effect = BREWING
    await poll(hass, freezer)
    assert coordinator.update_interval == timedelta(seconds=ACTIVE_SCAN_INTERVAL)
    assert state(hass, "sensor", mock_config_entry, "status").state == "brewing"

    # the next polls follow after the short interval, not after the long one
    for _ in range(2):
        polls = mock_client.fetch.call_count
        await poll(hass, freezer, seconds=ACTIVE_SCAN_INTERVAL + 1)
        assert mock_client.fetch.call_count == polls + 1

    # the drink is done: the poll that sees it is early, and ends the short interval
    mock_client.fetch.side_effect = None
    polls = mock_client.fetch.call_count
    await poll(hass, freezer, seconds=ACTIVE_SCAN_INTERVAL + 1)
    assert mock_client.fetch.call_count == polls + 1
    assert state(hass, "sensor", mock_config_entry, "status").state == "ready"
    assert coordinator.update_interval == timedelta(seconds=DEFAULT_SCAN_INTERVAL)

    # from now on a poll is not due after a few seconds
    polls = mock_client.fetch.call_count
    await poll(hass, freezer, seconds=ACTIVE_SCAN_INTERVAL + 1)
    assert mock_client.fetch.call_count == polls
    await poll(hass, freezer, seconds=DEFAULT_SCAN_INTERVAL)
    assert mock_client.fetch.call_count == polls + 1


async def test_every_kind_of_activity_is_polled_often(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """Brewing, a maintenance program, the menu and a busy machine alike."""
    await setup_entry(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data

    for kind in ("brewing", "maintenance", "programming", "busy"):
        mock_client.fetch.side_effect = JuraWifiBusy(MachineActivity(kind))
        await poll(hass, freezer)
        assert coordinator.update_interval == timedelta(seconds=ACTIVE_SCAN_INTERVAL), (
            kind
        )
        mock_client.fetch.side_effect = None
        await poll(hass, freezer)
        assert coordinator.update_interval == timedelta(
            seconds=DEFAULT_SCAN_INTERVAL
        ), kind


async def test_a_command_that_starts_an_activity_shortens_the_interval(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """What a button press publishes counts as activity too."""
    await setup_entry(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data

    coordinator.async_set_updated_data(
        JuraWifiData(
            online=True,
            snapshot=SNAPSHOT,
            activity=MachineActivity("brewing", "espresso"),
        )
    )
    assert coordinator.update_interval == timedelta(seconds=ACTIVE_SCAN_INTERVAL)

    coordinator.async_set_updated_data(JuraWifiData(online=True, snapshot=SNAPSHOT))
    assert coordinator.update_interval == timedelta(seconds=DEFAULT_SCAN_INTERVAL)


async def test_an_offline_machine_is_polled_at_the_configured_interval(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """A switched-off machine is not asked every few seconds."""
    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    await setup_entry(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data

    assert coordinator.update_interval == timedelta(seconds=DEFAULT_SCAN_INTERVAL)
    await poll(hass, freezer)
    assert coordinator.update_interval == timedelta(seconds=DEFAULT_SCAN_INTERVAL)


async def test_the_configured_interval_is_not_made_longer_for_an_activity(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """A user who polls faster than the active interval keeps what was chosen."""
    await setup_with_options(hass, mock_config_entry, **{CONF_SCAN_INTERVAL: 10})
    coordinator = mock_config_entry.runtime_data
    mock_client.fetch.side_effect = BREWING

    await poll(hass, freezer, seconds=11)

    assert coordinator.update_interval == timedelta(seconds=10)


def test_the_short_interval_leaves_the_pause_between_two_sessions() -> None:
    """The dongle resets a session that is opened right after another one."""
    assert ACTIVE_SCAN_INTERVAL >= SESSION_GAP_SECONDS
    assert api.SESSION_GAP_SECONDS == SESSION_GAP_SECONDS


async def test_a_machine_that_refuses_a_session_is_polled_again(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """A machine that aborts a handshake is not a pairing problem.

    The entry stays set up, no reauthentication starts, and the polls go on until the
    machine answers again.
    """
    mock_client.fetch.side_effect = JuraWifiConnectionError(
        "the machine refused the connection (ABORTED)"
    )
    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert state(hass, "sensor", mock_config_entry, "status").state == "offline"
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    for _ in range(3):
        polls = mock_client.fetch.call_count
        await poll(hass, freezer)
        assert mock_client.fetch.call_count == polls + 1
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    mock_client.fetch.side_effect = None
    await poll(hass, freezer)
    assert state(hass, "sensor", mock_config_entry, "status").state == "ready"
    assert state(hass, "binary_sensor", mock_config_entry, "connectivity").state == "on"


async def test_losing_the_machine_is_logged_once(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    caplog,
    freezer,
) -> None:
    """The log says when the machine went away and when it is back, not on every poll."""
    caplog.set_level(logging.INFO, logger=COORDINATOR_LOGGER)
    await setup_entry(hass, mock_config_entry)
    # the first poll that reaches the machine is no news
    assert _info_lines(caplog) == []

    mock_client.fetch.side_effect = JuraWifiConnectionError("peer closed")
    # one failed poll is tolerated and not logged
    await poll(hass, freezer)
    assert _info_lines(caplog) == []

    for _ in range(3):
        await poll(hass, freezer)
    assert _info_lines(caplog) == ["JURA E8 (SDS) is not reachable: peer closed"]

    mock_client.fetch.side_effect = None
    for _ in range(3):
        await poll(hass, freezer)
    assert _info_lines(caplog) == [
        "JURA E8 (SDS) is not reachable: peer closed",
        "JURA E8 (SDS) is reachable again",
    ]


async def test_a_machine_that_is_off_at_startup_is_logged_once(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    caplog,
    freezer,
) -> None:
    """The unreachable machine is reported at the start, and reachable only later."""
    caplog.set_level(logging.INFO, logger=COORDINATOR_LOGGER)
    mock_client.fetch.side_effect = JuraWifiConnectionError("timed out")
    await setup_entry(hass, mock_config_entry)

    for _ in range(3):
        await poll(hass, freezer)
    assert _info_lines(caplog) == ["JURA E8 (SDS) is not reachable: timed out"]

    mock_client.fetch.side_effect = None
    await poll(hass, freezer)
    assert _info_lines(caplog) == [
        "JURA E8 (SDS) is not reachable: timed out",
        "JURA E8 (SDS) is reachable again",
    ]


async def test_a_busy_machine_counts_as_reachable_in_the_log(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    caplog,
    freezer,
) -> None:
    """A machine that answers with its progress is back, too."""
    caplog.set_level(logging.INFO, logger=COORDINATOR_LOGGER)
    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    await setup_entry(hass, mock_config_entry)
    await poll(hass, freezer)

    mock_client.fetch.side_effect = BREWING
    for _ in range(3):
        await poll(hass, freezer)

    assert _info_lines(caplog) == [
        "JURA E8 (SDS) is not reachable: down",
        "JURA E8 (SDS) is reachable again",
    ]


async def test_going_offline_twice_is_logged_twice(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    caplog,
    freezer,
) -> None:
    """Every transition is logged, once."""
    caplog.set_level(logging.INFO, logger=COORDINATOR_LOGGER)
    await setup_entry(hass, mock_config_entry)

    for _ in range(2):
        mock_client.fetch.side_effect = JuraWifiConnectionError("down")
        for _ in range(3):
            await poll(hass, freezer)
        mock_client.fetch.side_effect = None
        for _ in range(2):
            await poll(hass, freezer)

    assert _info_lines(caplog) == [
        "JURA E8 (SDS) is not reachable: down",
        "JURA E8 (SDS) is reachable again",
        "JURA E8 (SDS) is not reachable: down",
        "JURA E8 (SDS) is reachable again",
    ]


async def test_the_coordinator_knows_since_when_the_machine_is_off(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """The failures in a row and the time of the last change are kept."""
    await setup_entry(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data
    assert coordinator.consecutive_failures == 0
    online_since = coordinator.online_since
    assert online_since is not None

    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    await poll(hass, freezer)
    assert coordinator.consecutive_failures == 1
    assert coordinator.online_since == online_since

    await poll(hass, freezer)
    assert coordinator.consecutive_failures == 2
    assert coordinator.online_since is not None
    assert coordinator.online_since > online_since

    mock_client.fetch.side_effect = None
    await poll(hass, freezer)
    assert coordinator.consecutive_failures == 0


async def test_the_status_while_the_machine_switches_off(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """The E8 counts down for about a quarter of an hour before it is gone."""
    await setup_entry(hass, mock_config_entry)

    # what the real machine reported on its way out, in this order
    for alerts, expected in (
        ({"energy_safe"}, "energy_saving"),
        ({"please_wait", "energy_safe"}, "busy"),
        ({"switch_off_delay_active"}, "switching_off"),
        ({"switch_off_delay_active", "energy_safe"}, "switching_off"),
    ):
        mock_client.fetch.return_value = dataclasses.replace(
            SNAPSHOT,
            active_alerts=frozenset(alerts),
            errors=frozenset(alerts & {"please_wait", "switch_off_delay_active"}),
        )
        await poll(hass, freezer)
        assert state(hass, "sensor", mock_config_entry, "status").state == expected

    mock_client.fetch.side_effect = JuraWifiConnectionError("gone")
    await poll(hass, freezer)
    await poll(hass, freezer)
    assert state(hass, "sensor", mock_config_entry, "status").state == "offline"
