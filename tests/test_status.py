"""Tests for the derived machine status."""

from __future__ import annotations

import dataclasses

import pytest

from custom_components.jura_wifi.coordinator import JuraWifiData
from custom_components.jura_wifi.status import machine_status

from .conftest import SNAPSHOT


def _data(*, alerts: set[str], errors: set[str] | None = None, online: bool = True):
    snapshot = dataclasses.replace(
        SNAPSHOT,
        active_alerts=frozenset(alerts | (errors or set())),
        errors=frozenset(errors or ()),
    )
    return JuraWifiData(online=online, snapshot=snapshot)


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
    ],
)
def test_machine_status(data: JuraWifiData, expected: str) -> None:
    """Map alert bits to the status shown to the user."""
    assert machine_status(data) == expected
