"""Nothing writes a state while nothing changes, and the sensor of 0.5.0 is gone.

The sensor *Last seen* of 0.5.0 wrote a new state at every poll, about 62 an hour and
1,500 a day. It was the only entity that wrote continuously, and it filled the logbook
and the activity of the device. The time is an attribute of the status sensor now, and
only while the machine is offline.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.jura_wifi.api import (
    JuraWifiBusy,
    JuraWifiConnectionError,
    MachineActivity,
)
from custom_components.jura_wifi.const import (
    CONF_ENABLE_BREWING,
    CONF_ENABLE_MAINTENANCE,
    CONF_ENABLE_SETTINGS,
    DOMAIN,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import entity_registry as er

from .common import (
    SETTINGS_SNAPSHOT,
    entity_id,
    poll,
    registered_keys,
    setup_with_options,
    state,
)

# Every platform of the integration takes part.
ALL_OPTIONS = {
    CONF_ENABLE_BREWING: True,
    CONF_ENABLE_MAINTENANCE: True,
    CONF_ENABLE_SETTINGS: True,
}
PLATFORMS = ("binary_sensor", "button", "number", "select", "sensor", "switch")
BREWING = JuraWifiBusy(MachineActivity("brewing", "coffee"))


def _entity_ids(hass: HomeAssistant, entry: MockConfigEntry) -> set[str]:
    """Return the entity IDs that are registered for an entry."""
    return {
        registered.entity_id
        for registered in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }


def _written(changes: list[Event], ids: set[str]) -> list[str]:
    """Return the entities of the entry that wrote a state, in that order."""
    return [
        event.data["entity_id"] for event in changes if event.data["entity_id"] in ids
    ]


async def _set_up(
    hass: HomeAssistant, mock_client: MagicMock, entry: MockConfigEntry
) -> set[str]:
    """Set the entry up with every option on, and return its entity IDs."""
    mock_client.fetch.return_value = SETTINGS_SNAPSHOT
    await setup_with_options(hass, entry, **ALL_OPTIONS)
    ids = _entity_ids(hass, entry)
    # the test is only worth something if all kinds of entities take part
    assert {entity.split(".")[0] for entity in ids} == set(PLATFORMS)
    return ids


async def test_polls_that_find_nothing_new_write_no_state(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """Every entity changes only when something about the machine changes."""
    ids = await _set_up(hass, mock_client, mock_config_entry)
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)

    # long enough for the machine settings to be read a second time
    for _ in range(12):
        await poll(hass, freezer)

    reads = [bool(call.args[0]) for call in mock_client.fetch.call_args_list]
    assert len(reads) > 12
    assert sum(reads) >= 2
    assert _written(changes, ids) == []


async def test_a_machine_that_stays_off_writes_no_state(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """The time it was last seen does not move, so it is no reason to write."""
    ids = await _set_up(hass, mock_client, mock_config_entry)
    status_id = entity_id(hass, "sensor", mock_config_entry, "status")
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)

    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    for _ in range(2):
        await poll(hass, freezer)
    # the machine is offline now, and the status says since when
    assert status_id in _written(changes, ids)
    assert state(hass, "sensor", mock_config_entry, "status").state == "offline"
    assert "last_seen" in state(hass, "sensor", mock_config_entry, "status").attributes

    changes.clear()
    polls = mock_client.fetch.call_count
    for _ in range(8):
        await poll(hass, freezer)
    assert mock_client.fetch.call_count == polls + 8
    assert _written(changes, ids) == []

    # the entities do write when the machine is back, which the capture shows
    mock_client.fetch.side_effect = None
    await poll(hass, freezer)
    assert status_id in _written(changes, ids)
    status = state(hass, "sensor", mock_config_entry, "status")
    assert status.state == "ready"
    assert "last_seen" not in status.attributes


async def test_a_busy_machine_writes_no_state_while_it_stays_busy(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """A machine that brews is polled every 15 seconds, and nothing changes meanwhile."""
    ids = await _set_up(hass, mock_client, mock_config_entry)

    mock_client.fetch.side_effect = BREWING
    await poll(hass, freezer)
    assert state(hass, "sensor", mock_config_entry, "status").state == "brewing"
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)

    polls = mock_client.fetch.call_count
    for _ in range(5):
        await poll(hass, freezer, seconds=16)

    assert mock_client.fetch.call_count == polls + 5
    assert _written(changes, ids) == []


async def test_there_is_no_last_seen_entity(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Not as a sensor and not as anything else."""
    await _set_up(hass, mock_client, mock_config_entry)

    for platform in PLATFORMS:
        assert "last_seen" not in registered_keys(hass, mock_config_entry, platform)
    assert not [name for name in hass.states.async_entity_ids() if "last_seen" in name]


@pytest.mark.parametrize(
    "disabled_by",
    [None, er.RegistryEntryDisabler.USER, er.RegistryEntryDisabler.INTEGRATION],
)
async def test_the_last_seen_entity_of_0_5_0_is_removed_at_setup(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    disabled_by: er.RegistryEntryDisabler | None,
) -> None:
    """Enabled or disabled, it goes, and nothing else goes with it."""
    entry = mock_config_entry
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_last_seen",
        config_entry=entry,
        disabled_by=disabled_by,
        suggested_object_id="jura_e8_sds_last_seen",
    )
    if disabled_by is None:
        # what Home Assistant shows for a registered entity that nothing provides
        hass.states.async_set(old.entity_id, STATE_UNAVAILABLE, {"restored": True})
    # entities that look alike, but are not the one that was dropped
    others = [
        ("sensor", DOMAIN, f"{entry.entry_id}_last_seen_2"),
        ("sensor", DOMAIN, f"{entry.entry_id}_no_sensor_of_this_version"),
        ("sensor", DOMAIN, "another_entry_last_seen"),
        ("binary_sensor", DOMAIN, f"{entry.entry_id}_last_seen"),
        ("sensor", "another_integration", f"{entry.entry_id}_last_seen"),
    ]
    kept = {
        identity: registry.async_get_or_create(*identity).entity_id
        for identity in others
    }

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert registry.async_get(old.entity_id) is None
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_last_seen")
        is None
    )
    assert hass.states.get(old.entity_id) is None
    for identity, kept_id in kept.items():
        assert registry.async_get_entity_id(*identity) == kept_id, identity
    # the entities of the machine itself are there
    assert state(hass, "sensor", entry, "status").state == "ready"
    assert state(hass, "sensor", entry, "total_brews").state == "20"

    # the next setup has nothing left to remove
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    for identity, kept_id in kept.items():
        assert registry.async_get_entity_id(*identity) == kept_id, identity
