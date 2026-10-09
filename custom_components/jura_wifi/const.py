"""Constants for the JURA Wi-Fi Connect integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "jura_wifi"

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR]

CONF_AUTH_HASH = "auth_hash"
CONF_CONN_ID = "conn_id"
CONF_MACHINE_TYPE = "machine_type"
CONF_MODEL = "model"
CONF_MODEL_NAME = "model_name"
CONF_PIN = "pin"
CONF_ENABLE_BREWING = "enable_brewing"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_PORT = 51515
DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 30
MAX_SCAN_INTERVAL = 900

# The dongle serves one TCP session at a time and has been seen to reset a
# connection that is opened right after the previous one was closed.
SESSION_GAP_SECONDS = 10.0

# Consecutive failed polls before the machine is reported as offline. A single
# failure is usually a collision with another client (e.g. the J.O.E. app).
OFFLINE_AFTER_FAILURES = 2
