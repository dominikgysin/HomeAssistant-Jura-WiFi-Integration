"""Constants for the JURA Wi-Fi Connect integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "jura_wifi"

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

SERVICE_BREW = "brew"

CONF_AREA = "area"
CONF_ARTICLE_NUMBER = "article_number"
CONF_AUTH_HASH = "auth_hash"
CONF_CONN_ID = "conn_id"
CONF_FIRMWARE = "firmware"
CONF_MACHINE_TYPE = "machine_type"
CONF_MODEL = "model"
CONF_MODEL_NAME = "model_name"
CONF_MODEL_SOURCE = "model_source"
CONF_PIN = "pin"
CONF_SERIAL_NUMBER = "serial_number"
CONF_ENABLE_BREWING = "enable_brewing"
CONF_ENABLE_MAINTENANCE = "enable_maintenance"
CONF_ENABLE_SETTINGS = "enable_settings"
CONF_SCAN_INTERVAL = "scan_interval"

# How the model of an entry was determined.
MODEL_SOURCE_DISCOVERY = "discovery"
MODEL_SOURCE_ARTICLE = "article_number"
MODEL_SOURCE_MANUAL = "manual"

DEFAULT_PORT = 51515
DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 30
MAX_SCAN_INTERVAL = 900

# Seconds to wait for the UDP discovery reply of the dongle.
DISCOVERY_TIMEOUT = 4.0

# The dongle serves one TCP session at a time and has been seen to reset a
# connection that is opened right after the previous one was closed.
SESSION_GAP_SECONDS = 10.0

# Consecutive failed polls before the machine is reported as offline. A single
# failure is usually a collision with another client (e.g. the J.O.E. app).
OFFLINE_AFTER_FAILURES = 2

# Seconds between two polls while the machine reports an activity (brewing, a
# maintenance program, its menu), so that the status does not linger after the
# drink is done. The pause between two sessions still applies.
ACTIVE_SCAN_INTERVAL = 15

# Machine settings rarely change: they are read at the first poll, then once in
# this many seconds, and after every write.
SETTINGS_REFRESH_SECONDS = 600

# The last values read from the machine are kept for the time it is switched off.
STORAGE_VERSION = 1
# Seconds to wait before the cache is written after a poll; polls that come in
# meanwhile do not move the write.
CACHE_SAVE_DELAY = 300

# The alerts that tell the front panel of the machine is locked: its keys are
# locked, or the display is under remote control.
LOCK_ALERTS = frozenset({"locked_keys", "remote_screen"})
