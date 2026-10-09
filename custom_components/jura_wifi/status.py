"""Derive a coarse machine status from the alert bits of the status frame."""

from __future__ import annotations

from .coordinator import JuraWifiData

STATUS_OFFLINE = "offline"
STATUS_ATTENTION = "attention"
STATUS_RINSING = "rinsing"
STATUS_HEATING_UP = "heating_up"
STATUS_BUSY = "busy"
STATUS_ENERGY_SAVING = "energy_saving"
STATUS_READY = "ready"

STATUSES = [
    STATUS_OFFLINE,
    STATUS_ATTENTION,
    STATUS_RINSING,
    STATUS_HEATING_UP,
    STATUS_BUSY,
    STATUS_ENERGY_SAVING,
    STATUS_READY,
]

# Blocking alerts that describe a transient machine state, not something the
# user has to fix.
TRANSIENT_ALERTS = frozenset(
    {"please_wait", "switch_off_delay_active", "program_mode_status"}
)
RINSING_ALERTS = frozenset({"coffee_rinsing", "system_filling", "system_emptying"})


def machine_status(data: JuraWifiData) -> str:
    """Return the status shown by the status sensor."""
    snapshot = data.snapshot
    if not data.online or snapshot is None:
        return STATUS_OFFLINE
    if snapshot.errors - TRANSIENT_ALERTS:
        return STATUS_ATTENTION
    alerts = snapshot.active_alerts
    if alerts & RINSING_ALERTS:
        return STATUS_RINSING
    if "heating_up" in alerts:
        return STATUS_HEATING_UP
    if snapshot.errors & TRANSIENT_ALERTS:
        return STATUS_BUSY
    if "energy_safe" in alerts:
        return STATUS_ENERGY_SAVING
    return STATUS_READY
