"""DataUpdateCoordinator for the JURA Wi-Fi Connect integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
import dataclasses
from datetime import datetime, timedelta
import logging
from typing import Any

from jura_connect import MachineProfile, SettingDef

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import area_registry as ar, device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    ACTIVITY_BREWING,
    ACTIVITY_MAINTENANCE,
    BrewOptionError,
    JuraWifiAuthError,
    JuraWifiBusy,
    JuraWifiClient,
    JuraWifiConnectionError,
    JuraWifiError,
    MachineActivity,
    MachineIdentity,
    MachineSnapshot,
    ProfileExtras,
    brew_arguments,
    discover_machine,
    load_profile_extras,
)
from .const import (
    ACTIVE_SCAN_INTERVAL,
    CACHE_SAVE_DELAY,
    CONF_AREA,
    CONF_ENABLE_SETTINGS,
    CONF_MACHINE_TYPE,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL_NUMBER,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    OFFLINE_AFTER_FAILURES,
    SETTINGS_REFRESH_SECONDS,
    STORAGE_VERSION,
)
from .identity import device_attributes, identity_updates, machine_device

_LOGGER = logging.getLogger(__name__)

type JuraWifiConfigEntry = ConfigEntry[JuraWifiCoordinator]


@dataclasses.dataclass(frozen=True, slots=True)
class JuraWifiData:
    """What the entities see: reachability, activity and the last known snapshot.

    ``activity`` is set while the machine answers but only reports what it is
    doing; the snapshot then still holds the values of the last full poll.
    ``settings`` holds the raw values of the machine settings, by the argument that
    reads them, as of the last time they were read.
    """

    online: bool
    snapshot: MachineSnapshot | None
    activity: MachineActivity | None = None
    settings: Mapping[str, str] = dataclasses.field(default_factory=dict)


def _suggested_area(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """Return the name of the area for a device that does not exist yet.

    The area chosen in the setup is only for the moment the device is created, so
    that all of its entities get the name of the area in their entity IDs. A device
    that exists keeps the area it has, also if the user removed it.
    """
    area_id = entry.data.get(CONF_AREA)
    if not area_id or machine_device(hass, entry) is not None:
        return None
    area = ar.async_get(hass).async_get_area(area_id)
    return area.name if area is not None else None


def cache_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """Return the store of the cache that belongs to a config entry."""
    return Store[dict[str, Any]](hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")


def _count_map(raw: Any) -> dict[str, int]:
    """Return the integer values of a mapping that was read from the cache."""
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): value
        for key, value in raw.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }


def _snapshot_from_cache(raw: Any) -> MachineSnapshot | None:
    """Rebuild the counters and maintenance values that the cache holds."""
    if not isinstance(raw, dict):
        return None
    total = raw.get("total_brews")
    return MachineSnapshot(
        active_alerts=frozenset(),
        errors=frozenset(),
        blocked_products=frozenset(),
        maintenance_counters=_count_map(raw.get("maintenance_counters")),
        maintenance_percent=_count_map(raw.get("maintenance_percent")),
        total_brews=total
        if isinstance(total, int) and not isinstance(total, bool)
        else None,
        product_counts=_count_map(raw.get("product_counts")),
        restored=True,
    )


class JuraWifiCoordinator(DataUpdateCoordinator[JuraWifiData]):
    """Poll one machine and serialize every session with it."""

    config_entry: JuraWifiConfigEntry
    profile: MachineProfile
    profile_extras: ProfileExtras

    def __init__(
        self,
        hass: HomeAssistant,
        entry: JuraWifiConfigEntry,
        client: JuraWifiClient,
    ) -> None:
        """Initialize the coordinator."""
        scan_interval = timedelta(
            seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=scan_interval,
        )
        self.client = client
        self.profile_extras = ProfileExtras()
        # The area for a device that has not been created yet, see _suggested_area.
        self.suggested_area = _suggested_area(hass, entry)
        # When the machine last answered a poll, also in an earlier run.
        self.last_seen: datetime | None = None
        self._scan_interval = scan_interval
        # The dongle accepts a single TCP session at a time.
        self._lock = asyncio.Lock()
        self._failures = 0
        self._online: bool | None = None
        self._online_since: datetime | None = None
        # The last values read from the machine, or restored from the cache.
        self._snapshot: MachineSnapshot | None = None
        self._settings: dict[str, str] = {}
        self._settings_read: datetime | None = None
        self._settings_due = False
        self._store = cache_store(hass, entry.entry_id)
        self._save_pending = False
        self._identity_pending = not entry.data.get(CONF_SERIAL_NUMBER)
        self._identity_running = False

    @property
    def consecutive_failures(self) -> int:
        """Return how many polls in a row did not reach the machine."""
        return self._failures

    @property
    def online_since(self) -> datetime | None:
        """Return when the machine became reachable or unreachable the last time."""
        return self._online_since

    async def _async_setup(self) -> None:
        """Load the machine profile and the cache outside the event loop."""
        try:
            self.profile = await self.hass.async_add_executor_job(
                lambda: self.client.profile
            )
        except JuraWifiError as err:
            raise ConfigEntryError(
                translation_domain=DOMAIN,
                translation_key="unknown_machine_type",
                translation_placeholders={
                    "machine_type": str(self.config_entry.data.get(CONF_MACHINE_TYPE))
                },
            ) from err
        self.profile_extras = await self.hass.async_add_executor_job(
            load_profile_extras, self.profile
        )
        await self._async_restore_cache()

    async def _async_restore_cache(self) -> None:
        """Load the values that the machine reported before Home Assistant stopped.

        They are shown until the machine answers again. Nothing is counted here: the
        machine stays the only source of every value.
        """
        try:
            stored = await self._store.async_load()
        except (HomeAssistantError, OSError, ValueError) as err:
            _LOGGER.debug("The cache could not be read: %s", err)
            return
        if not isinstance(stored, dict):
            return
        if stored.get("machine_type") != self.config_entry.data.get(CONF_MACHINE_TYPE):
            return
        if isinstance(last_seen := stored.get("last_seen"), str):
            self.last_seen = dt_util.parse_datetime(last_seen)
        self._snapshot = _snapshot_from_cache(stored.get("snapshot"))

    @callback
    def _cache_to_save(self) -> dict[str, Any]:
        """Return what the cache holds. Called when the write is due."""
        self._save_pending = False
        snapshot = self._snapshot
        return {
            "machine_type": self.config_entry.data.get(CONF_MACHINE_TYPE),
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "snapshot": (
                None
                if snapshot is None
                else {
                    "total_brews": snapshot.total_brews,
                    "product_counts": dict(snapshot.product_counts),
                    "maintenance_counters": dict(snapshot.maintenance_counters),
                    "maintenance_percent": dict(snapshot.maintenance_percent),
                }
            ),
        }

    @callback
    def _schedule_cache_save(self) -> None:
        """Have the cache written soon, unless a write is already waiting."""
        if not self._save_pending:
            self._save_pending = True
            self._store.async_delay_save(self._cache_to_save, CACHE_SAVE_DELAY)

    async def async_save_cache(self) -> None:
        """Write the cache now, for example before the entry is unloaded."""
        await self._store.async_save(self._cache_to_save())

    async def async_remove_cache(self) -> None:
        """Delete the cache of a config entry that was removed."""
        await self._store.async_remove()

    def _settings_wanted(self) -> bool:
        """Say whether this poll reads the machine settings as well."""
        if (
            not self.config_entry.options.get(CONF_ENABLE_SETTINGS, False)
            or not self.profile.settings
        ):
            return False
        if self._settings_due or self._settings_read is None:
            return True
        age = dt_util.utcnow() - self._settings_read
        return age.total_seconds() >= SETTINGS_REFRESH_SECONDS

    async def _async_update_data(self) -> JuraWifiData:
        """Fetch a snapshot; an unreachable machine is data, not an error."""
        with_settings = self._settings_wanted()
        try:
            async with self._lock:
                snapshot = await self.hass.async_add_executor_job(
                    self.client.fetch, with_settings
                )
        except JuraWifiAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except JuraWifiBusy as err:
            data = self._handle_busy(err)
        except JuraWifiConnectionError as err:
            data = self._handle_unreachable(err)
        except JuraWifiError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        else:
            data = self._handle_snapshot(snapshot, with_settings)
        self._track(data)
        return data

    def _handle_snapshot(
        self, snapshot: MachineSnapshot, with_settings: bool
    ) -> JuraWifiData:
        """Take over what a poll read from the machine.

        When the counters could not be read this time, the values that the machine
        reported before are kept, so that the counters do not drop to unknown.
        """
        self._failures = 0
        if with_settings:
            self._settings.update(snapshot.settings or {})
            self._settings_read = dt_util.utcnow()
            self._settings_due = False
        previous = self._snapshot
        if (
            snapshot.total_brews is None
            and previous is not None
            and previous.total_brews is not None
        ):
            snapshot = dataclasses.replace(
                snapshot,
                total_brews=previous.total_brews,
                product_counts=previous.product_counts,
            )
        self._snapshot = dataclasses.replace(snapshot, settings=None)
        self._mark_seen()
        return JuraWifiData(
            online=True, snapshot=self._snapshot, settings=dict(self._settings)
        )

    def _handle_busy(self, err: JuraWifiBusy) -> JuraWifiData:
        """Report what the machine is doing and keep the values of the last poll."""
        _LOGGER.debug(
            "Machine is busy: %s (%s)", err.activity.kind, err.activity.detail
        )
        self._failures = 0
        self._mark_seen()
        return JuraWifiData(
            online=True,
            snapshot=self._snapshot,
            activity=err.activity,
            settings=dict(self._settings),
        )

    def _handle_unreachable(self, err: JuraWifiConnectionError) -> JuraWifiData:
        """Tolerate a single failed poll, then report the machine as offline."""
        self._failures += 1
        previous = self.data
        if (
            previous is not None
            and previous.online
            and self._failures < OFFLINE_AFTER_FAILURES
        ):
            _LOGGER.debug(
                "Poll failed (%s/%s), keeping last data: %s",
                self._failures,
                OFFLINE_AFTER_FAILURES,
                err,
            )
            return previous
        _LOGGER.debug("Machine is unreachable: %s", err)
        self._note_reachability(False, str(err))
        return JuraWifiData(
            online=False, snapshot=self._snapshot, settings=dict(self._settings)
        )

    @callback
    def _mark_seen(self) -> None:
        """Remember that the machine answered, and have the cache written."""
        self.last_seen = dt_util.utcnow()
        self._schedule_cache_save()

    @callback
    def _note_reachability(self, online: bool, reason: str | None = None) -> None:
        """Log once when the machine becomes unreachable and when it is back."""
        if online == self._online:
            return
        previous, self._online = self._online, online
        self._online_since = dt_util.utcnow()
        title = self.config_entry.title
        if not online:
            _LOGGER.info("%s is not reachable: %s", title, reason or "no answer")
            return
        if previous is not None:
            _LOGGER.info("%s is reachable again", title)
        if self._identity_pending and not self._identity_running:
            self._identity_running = True
            self.config_entry.async_create_background_task(
                self.hass,
                self._async_backfill_identity(),
                f"{DOMAIN} identity of {title}",
            )

    @callback
    def _track(self, data: JuraWifiData) -> None:
        """Follow the data that is about to be published.

        An activity is polled at a short interval, so that the status does not
        linger after a drink is done; the interval of the options applies otherwise.
        """
        if data.online:
            self._note_reachability(True)
        if data.online and data.activity is not None:
            interval = min(timedelta(seconds=ACTIVE_SCAN_INTERVAL), self._scan_interval)
        else:
            interval = self._scan_interval
        self.update_interval = interval

    @callback
    def async_set_updated_data(self, data: JuraWifiData) -> None:
        """Publish data that no poll produced, e.g. the guess after a command."""
        self._track(data)
        super().async_set_updated_data(data)

    async def _async_backfill_identity(self) -> None:
        """Ask the dongle who the machine is and fill in what the entry lacks.

        Entries of the first versions have no article number and no firmware, and no
        entry before 0.5.2 has the serial number of the type plate. The discovery
        works when Home Assistant is in the network of the dongle. It runs while the
        machine answers, in the background, so that it never holds up the setup.
        """
        try:
            identity = await self.hass.async_add_executor_job(
                discover_machine, self.config_entry.data[CONF_HOST]
            )
            if identity is None:
                _LOGGER.debug("The dongle did not answer the discovery")
                return
            self._apply_identity(identity)
        except Exception:
            _LOGGER.exception("Reading the identity of the machine failed")
        finally:
            self._identity_running = False

    def _apply_identity(self, identity: MachineIdentity) -> None:
        """Store what the discovery reply adds to the entry and show it."""
        entry = self.config_entry
        if identity.ef_code is not None and identity.ef_code != entry.data.get(
            CONF_MACHINE_TYPE
        ):
            _LOGGER.warning(
                "The machine reports article number %s, a %s [%s], but %s is set up "
                "as machine type %s. The machine type is kept; to change it, set the "
                "integration up again",
                identity.article_number,
                identity.model_name,
                identity.ef_code,
                entry.title,
                entry.data.get(CONF_MACHINE_TYPE),
            )
        updates = identity_updates(entry.data, identity)
        data = {**entry.data, **updates}
        unique_id = entry.unique_id
        serial = data.get(CONF_SERIAL_NUMBER)
        if serial and unique_id != str(serial):
            other = self.hass.config_entries.async_entry_for_domain_unique_id(
                DOMAIN, str(serial)
            )
            if other is None or other.entry_id == entry.entry_id:
                unique_id = str(serial)
            else:
                _LOGGER.warning(
                    "%s has the serial number %s, which %s is set up with as well",
                    entry.title,
                    serial,
                    other.title,
                )
        if updates or unique_id != entry.unique_id:
            self.hass.config_entries.async_update_entry(
                entry, data=data, unique_id=unique_id
            )
            self._update_device(data)
        self._identity_pending = not data.get(CONF_SERIAL_NUMBER)
        self.async_update_listeners()

    def _update_device(self, data: Mapping[str, Any]) -> None:
        """Show what is known about the machine on the device that exists already."""
        device = machine_device(self.hass, self.config_entry)
        if device is not None:
            dr.async_get(self.hass).async_update_device(
                device.id, **device_attributes(data)
            )

    def _ensure_online(self) -> None:
        """Refuse to send anything unless the machine is reachable."""
        if not self.data.online:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="machine_offline"
            )

    def _ensure_idle(self) -> None:
        """Refuse to start anything unless the machine is reachable and idle."""
        self._ensure_online()
        if self.data.activity is not None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="machine_busy"
            )

    async def _async_command(
        self,
        command: Callable[..., Any],
        *args: Any,
        translation_key: str,
        placeholders: dict[str, str],
    ) -> Any:
        """Run one command session and turn its errors into Home Assistant errors.

        The caller holds the lock. Returns what the command returns.
        """
        try:
            result = await self.hass.async_add_executor_job(command, *args)
        except JuraWifiAuthError as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except JuraWifiConnectionError as err:
            self.async_set_updated_data(self._handle_unreachable(err))
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=translation_key,
                translation_placeholders={**placeholders, "error": str(err)},
            ) from err
        except JuraWifiError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=translation_key,
                translation_placeholders={**placeholders, "error": str(err)},
            ) from err
        self._failures = 0
        return result

    def _look_again_soon(self, activity: MachineActivity | None = None) -> None:
        """Show what a command started right away and check on the machine.

        The dongle needs a pause between two sessions, so the check follows a few
        seconds later in the background instead of holding up the caller.
        """
        if activity is not None:
            self.async_set_updated_data(
                JuraWifiData(
                    online=True,
                    snapshot=self.data.snapshot,
                    activity=activity,
                    settings=self.data.settings,
                )
            )
        self.config_entry.async_create_background_task(
            self.hass, self.async_request_refresh(), f"{DOMAIN} refresh after command"
        )

    async def async_brew(
        self,
        product: str,
        label: str,
        options: Mapping[str, Any] | None = None,
    ) -> None:
        """Start a drink with the recipe stored on the machine.

        ``options`` change single parameters of that recipe for this drink only. They
        are checked against the profile before anything is sent.
        """
        arguments: dict[str, int | str] = {}
        if options:
            definition = next(p for p in self.profile.products if p.name == product)
            try:
                arguments = brew_arguments(definition, options)
            except BrewOptionError as err:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key=err.translation_key,
                    translation_placeholders={"product": label, **err.placeholders},
                ) from err
        async with self._lock:
            # Check under the lock: a poll that was running while the button was
            # pressed may have changed what the machine reports.
            self._ensure_idle()
            snapshot = self.data.snapshot
            if snapshot is not None and product in snapshot.blocked_products:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="product_blocked",
                    translation_placeholders={"product": label},
                )
            await self._async_command(
                self.client.brew,
                product,
                *([arguments] if arguments else []),
                translation_key="brew_failed",
                placeholders={"product": label},
            )
        self._look_again_soon(MachineActivity(ACTIVITY_BREWING, product))

    async def async_start_process(self, process: str, label: str) -> None:
        """Start a maintenance program; the machine continues on its display."""
        async with self._lock:
            self._ensure_idle()
            await self._async_command(
                self.client.start_process,
                process,
                translation_key="maintenance_failed",
                placeholders={"program": label},
            )
        self._look_again_soon(MachineActivity(ACTIVITY_MAINTENANCE, process))

    async def async_cancel_step(self) -> None:
        """Cancel the running drink or maintenance step.

        Allowed while the machine is busy, which is when it is needed.
        """
        async with self._lock:
            self._ensure_online()
            await self._async_command(
                self.client.cancel_step,
                translation_key="cancel_failed",
                placeholders={},
            )
        self._look_again_soon()

    async def async_set_setting(
        self, definition: SettingDef, value: str, label: str
    ) -> None:
        """Write a machine setting, given in the wire format of the machine.

        The machine has to be reachable and idle. The value it stores is read back
        and published, and the next poll reads all settings again.
        """
        async with self._lock:
            self._ensure_idle()
            stored = await self._async_command(
                self.client.write_setting,
                definition.p_argument,
                value,
                translation_key="setting_failed",
                placeholders={"setting": label},
            )
        self._settings[definition.p_argument.upper()] = stored
        self._settings_due = True
        self.async_set_updated_data(
            dataclasses.replace(self.data, settings=dict(self._settings))
        )
        self._look_again_soon()
