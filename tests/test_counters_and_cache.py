"""The counters of every product, the cache of the last values and the last seen time.

The time the machine was last seen is no entity: while the machine is offline it is
an attribute of the status sensor.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from jura_connect import load_profile
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jura_wifi.api import (
    JuraWifiBusy,
    JuraWifiConnectionError,
    MachineActivity,
)
from custom_components.jura_wifi.const import CACHE_SAVE_DELAY, DOMAIN
from homeassistant.const import STATE_OFF, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .common import enable_in_registry, entity_id, poll, registered_keys, state
from .conftest import SNAPSHOT, setup_entry

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

# What a real E8 (SDS) reported: the drinks add up to 25, the machine counted 27,
# because it counts a double drink twice in the total.
REAL_E8 = dataclasses.replace(
    SNAPSHOT,
    total_brews=27,
    product_counts={
        "espresso": 10,
        "coffee": 8,
        "cappuccino": 4,
        "latte_macchiato": 1,
        "flat_white": 1,
        "hotwater_portion_normal": 1,
        "2x_espresso": 1,
        "2x_coffee": 0,
        "powderproduct": 0,
    },
)


def _cache_key(entry: MockConfigEntry) -> str:
    return f"{DOMAIN}.{entry.entry_id}"


def _cached(entry: MockConfigEntry, **data) -> dict:
    """Return what the cache of an entry holds, in the format of the storage."""
    return {
        "version": 1,
        "minor_version": 1,
        "key": _cache_key(entry),
        "data": {"machine_type": "EF1120", **data},
    }


async def test_every_product_the_machine_counts_gets_a_counter(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The doubles and the powder product are counted by the machine as well."""
    mock_client.fetch.return_value = REAL_E8
    await setup_entry(hass, mock_config_entry)

    profile = load_profile("EF1120")
    keys = registered_keys(hass, mock_config_entry, "sensor")
    assert {f"brews_{product.name}" for product in profile.products} <= keys
    # the products that the profile does not offer as a drink, but the machine counts
    assert {"brews_2x_espresso", "brews_2x_coffee", "brews_powderproduct"} <= keys
    assert state(hass, "sensor", mock_config_entry, "brews_2x_espresso").state == "1"
    assert state(hass, "sensor", mock_config_entry, "brews_2x_coffee").state == "0"
    # the values come from the machine, nothing is added up here
    assert state(hass, "sensor", mock_config_entry, "total_brews").state == "27"
    assert state(hass, "sensor", mock_config_entry, "brews_espresso").state == "10"


async def test_the_counters_of_the_drinks_keep_their_entities(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Existing installs keep the unique IDs of the counters they have."""
    await setup_entry(hass, mock_config_entry)
    for product in load_profile("EF1120").products:
        if product.active:
            assert (
                er.async_get(hass).async_get_entity_id(
                    "sensor",
                    DOMAIN,
                    f"{mock_config_entry.entry_id}_brews_{product.name}",
                )
                is not None
            )


async def test_the_counter_of_the_powder_product_is_disabled_by_default(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Only the powder product is hidden; the doubles are counted for everybody."""
    await setup_entry(hass, mock_config_entry)
    registry = er.async_get(hass)

    powder = registry.async_get(
        entity_id(hass, "sensor", mock_config_entry, "brews_powderproduct")
    )
    assert powder is not None
    assert powder.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(powder.entity_id) is None
    for key in ("brews_2x_espresso", "brews_2x_coffee", "brews_espresso"):
        entry = registry.async_get(entity_id(hass, "sensor", mock_config_entry, key))
        assert entry is not None
        assert entry.disabled_by is None, key


# The machine answered, but its reply of the counters could not be read.
NO_COUNTERS = dataclasses.replace(
    REAL_E8,
    total_brews=None,
    product_counts={},
    maintenance_percent={"cleaning": 20, "descale": 0},
)


async def test_the_counters_are_kept_when_their_reply_cannot_be_read(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """The counters stay what the machine reported last, instead of unknown."""
    mock_client.fetch.return_value = REAL_E8
    await setup_entry(hass, mock_config_entry)

    mock_client.fetch.return_value = NO_COUNTERS
    await poll(hass, freezer)

    assert state(hass, "sensor", mock_config_entry, "total_brews").state == "27"
    assert state(hass, "sensor", mock_config_entry, "brews_espresso").state == "10"
    assert state(hass, "sensor", mock_config_entry, "brews_2x_espresso").state == "1"
    # what the same poll did read is taken over
    assert state(hass, "sensor", mock_config_entry, "cleaning_need").state == "20"
    assert state(hass, "sensor", mock_config_entry, "status").state == "ready"

    # the next reply that can be read counts again, nothing was added up meanwhile
    mock_client.fetch.return_value = dataclasses.replace(
        REAL_E8,
        total_brews=28,
        product_counts={**REAL_E8.product_counts, "espresso": 11},
    )
    await poll(hass, freezer)

    assert state(hass, "sensor", mock_config_entry, "total_brews").state == "28"
    assert state(hass, "sensor", mock_config_entry, "brews_espresso").state == "11"


async def test_the_cache_keeps_the_counters_when_their_reply_cannot_be_read(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    cache_storage: dict,
    freezer,
) -> None:
    """The next start shows the counters, not the gap of the last poll."""
    mock_client.fetch.return_value = REAL_E8
    await setup_entry(hass, mock_config_entry)
    mock_client.fetch.return_value = NO_COUNTERS
    await poll(hass, freezer)

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)

    stored = cache_storage[_cache_key(mock_config_entry)]["data"]["snapshot"]
    assert stored["total_brews"] == 27
    assert stored["product_counts"] == REAL_E8.product_counts
    assert stored["maintenance_percent"] == {"cleaning": 20, "descale": 0}


async def test_the_counters_of_the_cache_are_kept_when_their_reply_cannot_be_read(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    cache_storage: dict,
) -> None:
    """The first poll after a restart cannot read the counters: the cache stays."""
    cache_storage[_cache_key(mock_config_entry)] = _cached(
        mock_config_entry, snapshot={"total_brews": 7, "product_counts": {"coffee": 3}}
    )
    mock_client.fetch.return_value = NO_COUNTERS
    await setup_entry(hass, mock_config_entry)

    assert state(hass, "sensor", mock_config_entry, "status").state == "ready"
    assert state(hass, "sensor", mock_config_entry, "total_brews").state == "7"
    assert state(hass, "sensor", mock_config_entry, "brews_coffee").state == "3"


async def test_counters_that_were_never_read_stay_unknown(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Without an earlier reply there is nothing to keep, and nothing is made up."""
    mock_client.fetch.return_value = NO_COUNTERS
    await setup_entry(hass, mock_config_entry)

    assert state(hass, "sensor", mock_config_entry, "status").state == "ready"
    for key in ("total_brews", "brews_espresso"):
        assert state(hass, "sensor", mock_config_entry, key).state == STATE_UNKNOWN


async def test_filter_wear_is_unavailable_when_the_machine_does_not_report_it(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """The E8 reports 0xFF without a filter. That is no value, not an unknown one."""
    mock_config_entry.add_to_hass(hass)
    enable_in_registry(hass, mock_config_entry, "sensor", "filter_wear")
    await setup_entry(hass, mock_config_entry)

    assert (
        state(hass, "sensor", mock_config_entry, "filter_wear").state
        == STATE_UNAVAILABLE
    )
    # the indicators that the machine reports are not affected
    assert state(hass, "sensor", mock_config_entry, "cleaning_need").state == "10"

    mock_client.fetch.return_value = dataclasses.replace(
        SNAPSHOT,
        maintenance_percent={"cleaning": 10, "descale": 0, "filter_change": 42},
    )
    await poll(hass, freezer)
    assert state(hass, "sensor", mock_config_entry, "filter_wear").state == "42"

    mock_client.fetch.return_value = SNAPSHOT
    await poll(hass, freezer)
    assert (
        state(hass, "sensor", mock_config_entry, "filter_wear").state
        == STATE_UNAVAILABLE
    )


async def test_filter_wear_is_unknown_before_the_machine_has_reported(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Nothing is known yet while the machine has not answered: unknown, as before."""
    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    mock_config_entry.add_to_hass(hass)
    enable_in_registry(hass, mock_config_entry, "sensor", "filter_wear")
    await setup_entry(hass, mock_config_entry)

    for key in ("filter_wear", "cleaning_need", "total_brews"):
        assert state(hass, "sensor", mock_config_entry, key).state == STATE_UNKNOWN


async def test_the_last_values_are_written_for_the_next_start(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    cache_storage: dict,
    freezer,
) -> None:
    """Counters and maintenance values are kept, the alerts are not."""
    freezer.move_to(NOW)
    await setup_entry(hass, mock_config_entry)

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)

    stored = cache_storage[_cache_key(mock_config_entry)]["data"]
    assert stored == {
        "machine_type": "EF1120",
        "last_seen": NOW.isoformat(),
        "snapshot": {
            "total_brews": 20,
            "product_counts": SNAPSHOT.product_counts,
            "maintenance_counters": SNAPSHOT.maintenance_counters,
            "maintenance_percent": SNAPSHOT.maintenance_percent,
        },
    }


async def test_the_values_of_the_last_run_are_shown_while_the_machine_is_off(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    cache_storage: dict,
    freezer,
) -> None:
    """After a restart with the machine switched off the entities are not unknown."""
    freezer.move_to(NOW)
    await setup_entry(hass, mock_config_entry)
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)

    # Home Assistant starts again, the machine is switched off
    freezer.tick(timedelta(hours=5))
    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert state(hass, "sensor", mock_config_entry, "total_brews").state == "20"
    assert state(hass, "sensor", mock_config_entry, "brews_espresso").state == "7"
    assert state(hass, "sensor", mock_config_entry, "brews_cappuccino").state == "4"
    assert state(hass, "sensor", mock_config_entry, "cleaning_need").state == "10"
    assert state(hass, "sensor", mock_config_entry, "descaling_need").state == "0"
    # a counter the machine did not report is still not known
    assert (
        state(hass, "sensor", mock_config_entry, "brews_flat_white").state
        == STATE_UNKNOWN
    )
    # but the entities still show the machine as offline
    assert state(hass, "sensor", mock_config_entry, "status").state == "offline"
    assert (
        state(hass, "binary_sensor", mock_config_entry, "connectivity").state
        == STATE_OFF
    )
    # and what is not a counter is not made up from the cache
    assert (
        state(hass, "binary_sensor", mock_config_entry, "water_tank_empty").state
        == STATE_UNAVAILABLE
    )
    assert (
        state(hass, "binary_sensor", mock_config_entry, "cleaning_due").state
        == STATE_UNAVAILABLE
    )


async def test_the_status_attributes_do_not_show_alerts_from_the_cache(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    cache_storage: dict,
) -> None:
    """The alerts are not known after a restart, so none are listed."""
    cache_storage[_cache_key(mock_config_entry)] = _cached(
        mock_config_entry, snapshot={"total_brews": 7}
    )
    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    await setup_entry(hass, mock_config_entry)

    status = state(hass, "sensor", mock_config_entry, "status")
    assert status.state == "offline"
    assert "active_alerts" not in status.attributes


async def test_the_first_poll_replaces_the_values_of_the_cache(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    cache_storage: dict,
) -> None:
    """What the machine reports wins, and the alerts are known again."""
    cache_storage[_cache_key(mock_config_entry)] = _cached(
        mock_config_entry, snapshot={"total_brews": 7, "product_counts": {"coffee": 3}}
    )
    mock_client.fetch.return_value = dataclasses.replace(
        SNAPSHOT, total_brews=21, active_alerts=frozenset({"fill_water"})
    )
    await setup_entry(hass, mock_config_entry)

    assert state(hass, "sensor", mock_config_entry, "total_brews").state == "21"
    assert state(hass, "sensor", mock_config_entry, "brews_coffee").state == "8"
    assert (
        state(hass, "binary_sensor", mock_config_entry, "water_tank_empty").state
        == "on"
    )


async def test_a_busy_machine_at_startup_shows_the_restored_counters(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    cache_storage: dict,
) -> None:
    """A machine in its menu answers, but sends no counters: the cache fills in."""
    cache_storage[_cache_key(mock_config_entry)] = _cached(
        mock_config_entry, snapshot={"total_brews": 7}
    )
    mock_client.fetch.side_effect = JuraWifiBusy(MachineActivity("programming"))
    await setup_entry(hass, mock_config_entry)

    assert state(hass, "sensor", mock_config_entry, "status").state == "programming"
    assert state(hass, "sensor", mock_config_entry, "total_brews").state == "7"
    # the alerts are not known, so no sensor claims that there are none
    assert (
        state(hass, "binary_sensor", mock_config_entry, "water_tank_empty").state
        == STATE_UNAVAILABLE
    )


async def test_a_cache_of_another_machine_type_is_ignored(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    cache_storage: dict,
) -> None:
    """The counters of another profile would be wrong."""
    stored = _cached(mock_config_entry, snapshot={"total_brews": 7})
    stored["data"]["machine_type"] = "EF1089"
    cache_storage[_cache_key(mock_config_entry)] = stored
    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    await setup_entry(hass, mock_config_entry)

    assert (
        state(hass, "sensor", mock_config_entry, "total_brews").state == STATE_UNKNOWN
    )


@pytest.mark.parametrize(
    "data",
    [
        {"snapshot": "garbage", "last_seen": 12},
        {"snapshot": {"total_brews": "many", "product_counts": [1, 2]}},
        {"snapshot": {"total_brews": True, "maintenance_percent": {"cleaning": 1.5}}},
        {"snapshot": None, "last_seen": "yesterday"},
        {},
    ],
)
async def test_a_damaged_cache_is_ignored(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    cache_storage: dict,
    data: dict,
) -> None:
    """A cache that cannot be read costs nothing but the restored values."""
    cache_storage[_cache_key(mock_config_entry)] = _cached(mock_config_entry, **data)
    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state.value == "loaded"
    assert (
        state(hass, "sensor", mock_config_entry, "total_brews").state == STATE_UNKNOWN
    )
    assert state(hass, "sensor", mock_config_entry, "status").state == "offline"
    # a time that cannot be read is no time
    assert _last_seen(hass, mock_config_entry) is None


async def test_the_cache_is_written_after_a_delay(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    cache_storage: dict,
    freezer,
) -> None:
    """The write is delayed, and polls that come in meanwhile do not postpone it."""
    freezer.move_to(NOW)
    await setup_entry(hass, mock_config_entry)
    assert _cache_key(mock_config_entry) not in cache_storage

    mock_client.fetch.return_value = dataclasses.replace(SNAPSHOT, total_brews=21)
    # polls every minute for most of the delay
    for _ in range(CACHE_SAVE_DELAY // 61):
        await poll(hass, freezer)
    assert _cache_key(mock_config_entry) not in cache_storage

    await poll(hass, freezer, seconds=61)
    stored = cache_storage[_cache_key(mock_config_entry)]["data"]
    assert stored["snapshot"]["total_brews"] == 21
    assert datetime.fromisoformat(stored["last_seen"]) > NOW


async def test_the_cache_is_removed_with_the_entry(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    cache_storage: dict,
) -> None:
    """Nothing of a removed entry stays in the storage."""
    await setup_entry(hass, mock_config_entry)
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert _cache_key(mock_config_entry) in cache_storage

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert _cache_key(mock_config_entry) not in cache_storage


def _last_seen(hass: HomeAssistant, entry: MockConfigEntry) -> str | None:
    """Return the attribute last_seen of the status sensor, or None without one."""
    return state(hass, "sensor", entry, "status").attributes.get("last_seen")


async def test_the_status_says_when_the_machine_was_last_seen_while_it_is_off(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """The time of the last poll in which the machine answered, in ISO 8601 and UTC."""
    freezer.move_to(NOW)
    await setup_entry(hass, mock_config_entry)
    assert _last_seen(hass, mock_config_entry) is None

    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    # one failed poll is a collision with another client, not an offline machine
    await poll(hass, freezer)
    assert state(hass, "sensor", mock_config_entry, "status").state == "ready"
    assert _last_seen(hass, mock_config_entry) is None

    await poll(hass, freezer)
    status = state(hass, "sensor", mock_config_entry, "status")
    assert status.state == "offline"
    # the time of the poll that was answered, not of the polls that failed since
    assert status.attributes["last_seen"] == NOW.isoformat()
    seen = datetime.fromisoformat(status.attributes["last_seen"])
    assert seen == NOW
    assert seen.utcoffset() == timedelta(0)
    # nothing else of the status is known while the machine is off
    for attribute in ("active_alerts", "errors", "blocked_products", "activity"):
        assert attribute not in status.attributes

    # it stays what it is for as long as the machine does not answer
    for _ in range(3):
        await poll(hass, freezer)
    assert _last_seen(hass, mock_config_entry) == NOW.isoformat()


async def test_the_time_is_that_of_the_last_answer_busy_or_not(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """A machine that answers with its progress was seen, too. The attribute goes
    again as soon as the machine answers."""
    freezer.move_to(NOW)
    await setup_entry(hass, mock_config_entry)

    mock_client.fetch.side_effect = JuraWifiBusy(MachineActivity("brewing", "coffee"))
    await poll(hass, freezer)
    busy = state(hass, "sensor", mock_config_entry, "status")
    assert busy.state == "brewing"
    assert busy.attributes["activity"] == "brewing"
    assert "last_seen" not in busy.attributes

    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    await poll(hass, freezer)
    await poll(hass, freezer)
    assert state(hass, "sensor", mock_config_entry, "status").state == "offline"
    assert (
        _last_seen(hass, mock_config_entry) == (NOW + timedelta(seconds=61)).isoformat()
    )

    mock_client.fetch.side_effect = None
    await poll(hass, freezer)
    back = state(hass, "sensor", mock_config_entry, "status")
    assert back.state == "ready"
    assert "last_seen" not in back.attributes


async def test_the_time_survives_a_restart_of_home_assistant(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    cache_storage: dict,
    freezer,
) -> None:
    """The time comes from the cache after a restart with the machine switched off."""
    freezer.move_to(NOW)
    await setup_entry(hass, mock_config_entry)
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)

    freezer.tick(timedelta(days=2))
    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    status = state(hass, "sensor", mock_config_entry, "status")
    assert status.state == "offline"
    assert status.attributes["last_seen"] == NOW.isoformat()

    # the machine is switched on: what the cache knew is not shown any more
    mock_client.fetch.side_effect = None
    await poll(hass, freezer)
    assert state(hass, "sensor", mock_config_entry, "status").state == "ready"
    assert _last_seen(hass, mock_config_entry) is None


async def test_a_machine_that_was_never_seen_has_no_time(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Nothing to show before the machine answered the first time."""
    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    await setup_entry(hass, mock_config_entry)

    status = state(hass, "sensor", mock_config_entry, "status")
    assert status.state == "offline"
    assert "last_seen" not in status.attributes


async def test_the_time_is_given_in_utc_whatever_the_cache_holds(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    cache_storage: dict,
) -> None:
    """An offset in the stored time does not leak into the attribute."""
    cache_storage[_cache_key(mock_config_entry)] = _cached(
        mock_config_entry,
        last_seen="2026-10-08T09:30:00+02:00",
        snapshot={"total_brews": 7},
    )
    mock_client.fetch.side_effect = JuraWifiConnectionError("down")
    await setup_entry(hass, mock_config_entry)

    assert _last_seen(hass, mock_config_entry) == "2026-10-08T07:30:00+00:00"
