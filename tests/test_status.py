"""Tests for the derived machine status."""

from __future__ import annotations

import dataclasses

import pytest

from custom_components.jura_wifi.api import MachineActivity
from custom_components.jura_wifi.coordinator import JuraWifiData
from custom_components.jura_wifi.status import STATUSES, machine_status

from .conftest import SNAPSHOT


def _data(*, alerts: set[str], errors: set[str] | None = None, online: bool = True):
    snapshot = dataclasses.replace(
        SNAPSHOT,
        active_alerts=frozenset(alerts | (errors or set())),
        errors=frozenset(errors or ()),
    )
    return JuraWifiData(online=online, snapshot=snapshot)


def _busy(kind: str, detail: str | None = None, *, online: bool = True):
    return JuraWifiData(
        online=online, snapshot=SNAPSHOT, activity=MachineActivity(kind, detail)
    )


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (JuraWifiData(online=False, snapshot=None), "offline"),
        (_data(alerts={"coffee_ready"}, online=False), "offline"),
        (_data(alerts={"coffee_ready"}), "ready"),
        (_data(alerts=set()), "ready"),
        (_data(alerts=set(), errors={"fill_water"}), "attention"),
        (_data(alerts={"heating_up"}), "heating_up"),
        (_data(alerts={"coffee_rinsing"}, errors={"please_wait"}), "rinsing"),
        (_data(alerts=set(), errors={"please_wait"}), "busy"),
        (_data(alerts={"energy_safe"}), "energy_saving"),
        (_data(alerts={"heating_up"}, errors={"empty_tray"}), "attention"),
        (_data(alerts={"cleaning_alert"}), "ready"),
        # What a real E8 reported while it switched off: busy for a short while, then
        # a quarter of an hour of the switch-off delay, then the dongle was gone.
        (_data(alerts={"energy_safe"}, errors={"please_wait"}), "busy"),
        (_data(alerts=set(), errors={"switch_off_delay_active"}), "switching_off"),
        (
            _data(alerts={"energy_safe"}, errors={"switch_off_delay_active"}),
            "switching_off",
        ),
        (
            _data(alerts=set(), errors={"switch_off_delay_active", "please_wait"}),
            "switching_off",
        ),
        # Something the user has to fix is still shown first.
        (
            _data(alerts=set(), errors={"switch_off_delay_active", "empty_tray"}),
            "attention",
        ),
        (_data(alerts={"switch_off_delay_active"}, online=False), "offline"),
        (_busy("brewing", "espresso"), "brewing"),
        (_busy("maintenance", "cleaning"), "maintenance"),
        (_busy("programming"), "programming"),
        (_busy("busy", "warning"), "busy"),
        (_busy("something_new"), "busy"),
        (_busy("brewing", online=False), "offline"),
        (
            JuraWifiData(
                online=True, snapshot=None, activity=MachineActivity("programming")
            ),
            "programming",
        ),
        # What a restart restored from the cache has no alerts to tell a status from.
        (
            JuraWifiData(
                online=True, snapshot=dataclasses.replace(SNAPSHOT, restored=True)
            ),
            "offline",
        ),
        (
            JuraWifiData(
                online=False, snapshot=dataclasses.replace(SNAPSHOT, restored=True)
            ),
            "offline",
        ),
        (
            JuraWifiData(
                online=True,
                snapshot=dataclasses.replace(SNAPSHOT, restored=True),
                activity=MachineActivity("brewing", "espresso"),
            ),
            "brewing",
        ),
    ],
)
def test_machine_status(data: JuraWifiData, expected: str) -> None:
    """Map alert bits and the activity to the status shown to the user."""
    assert machine_status(data) == expected


def test_switching_off_is_a_status() -> None:
    """The status sensor offers it as one of its states."""
    assert "switching_off" in STATUSES


def test_activity_wins_over_outdated_alerts() -> None:
    """The alerts of the last full poll say nothing while the machine is busy."""
    data = dataclasses.replace(
        _data(alerts=set(), errors={"fill_water"}),
        activity=MachineActivity("brewing", "espresso"),
    )
    assert machine_status(data) == "brewing"
