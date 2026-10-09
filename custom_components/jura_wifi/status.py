"""Derive a coarse machine status from the alert bits of the status frame."""

from __future__ import annotations

from .api import (
    ACTIVITY_BREWING,
    ACTIVITY_MAINTENANCE,
    ACTIVITY_PROGRAMMING,
)
from .coordinator import JuraWifiData

STATUS_OFFLINE = "offline"
STATUS_ATTENTION = "attention"
STATUS_RINSING = "rinsing"
STATUS_HEATING_UP = "heating_up"
STATUS_BREWING = "brewing"
STATUS_MAINTENANCE = "maintenance"
STATUS_PROGRAMMING = "programming"
STATUS_BUSY = "busy"
STATUS_SWITCHING_OFF = "switching_off"
STATUS_ENERGY_SAVING = "energy_saving"
STATUS_READY = "ready"

STATUSES = [
    STATUS_OFFLINE,
    STATUS_ATTENTION,
    STATUS_RINSING,
    STATUS_HEATING_UP,
    STATUS_BREWING,
    STATUS_MAINTENANCE,
    STATUS_PROGRAMMING,
    STATUS_BUSY,
    STATUS_SWITCHING_OFF,
    STATUS_ENERGY_SAVING,
    STATUS_READY,
]

# Blocking alerts that describe a transient machine state, not something the
# user has to fix.
TRANSIENT_ALERTS = frozenset(
    {"please_wait", "switch_off_delay_active", "program_mode_status"}
)
RINSING_ALERTS = frozenset({"coffee_rinsing", "system_filling", "system_emptying"})
# The machine counts down before it switches off, for about a quarter of an hour on
# the E8, and the dongle is gone when it is over.
SWITCHING_OFF_ALERTS = frozenset({"switch_off_delay_active"})

# What the machine is doing while it pushes progress frames; anything else it
# reports that way is just "busy".
ACTIVITY_STATUSES = {
    ACTIVITY_BREWING: STATUS_BREWING,
    ACTIVITY_MAINTENANCE: STATUS_MAINTENANCE,
    ACTIVITY_PROGRAMMING: STATUS_PROGRAMMING,
}


def machine_status(data: JuraWifiData) -> str:
    """Return the status shown by the status sensor."""
    if not data.online:
        return STATUS_OFFLINE
    # The alerts of the snapshot are outdated while the machine is busy.
    if data.activity is not None:
        return ACTIVITY_STATUSES.get(data.activity.kind, STATUS_BUSY)
    snapshot = data.snapshot
    # A snapshot from the cache has no alerts to tell the status from.
    if snapshot is None or snapshot.restored:
        return STATUS_OFFLINE
    if snapshot.errors - TRANSIENT_ALERTS:
        return STATUS_ATTENTION
    alerts = snapshot.active_alerts
    if alerts & RINSING_ALERTS:
        return STATUS_RINSING
    if "heating_up" in alerts:
        return STATUS_HEATING_UP
    if alerts & SWITCHING_OFF_ALERTS:
        return STATUS_SWITCHING_OFF
    if snapshot.errors & TRANSIENT_ALERTS:
        return STATUS_BUSY
    if "energy_safe" in alerts:
        return STATUS_ENERGY_SAVING
    return STATUS_READY
