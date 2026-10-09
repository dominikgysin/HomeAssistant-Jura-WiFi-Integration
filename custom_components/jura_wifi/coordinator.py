"""DataUpdateCoordinator for the JURA Wi-Fi Connect integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import dataclasses
from datetime import timedelta
import logging

from jura_connect import MachineProfile

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    ACTIVITY_BREWING,
    ACTIVITY_MAINTENANCE,
    JuraWifiAuthError,
    JuraWifiBusy,
    JuraWifiClient,
    JuraWifiConnectionError,
    JuraWifiError,
    MachineActivity,
    MachineSnapshot,
)
from .const import (
    CONF_MACHINE_TYPE,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    OFFLINE_AFTER_FAILURES,
)

_LOGGER = logging.getLogger(__name__)

type JuraWifiConfigEntry = ConfigEntry[JuraWifiCoordinator]


@dataclasses.dataclass(frozen=True, slots=True)
class JuraWifiData:
    """What the entities see: reachability, activity and the last known snapshot.

    ``activity`` is set while the machine answers but only reports what it is
    doing; the snapshot then still holds the values of the last full poll.
    """

    online: bool
    snapshot: MachineSnapshot | None
    activity: MachineActivity | None = None


class JuraWifiCoordinator(DataUpdateCoordinator[JuraWifiData]):
    """Poll one machine and serialize every session with it."""

    config_entry: JuraWifiConfigEntry
    profile: MachineProfile

    def __init__(
        self,
        hass: HomeAssistant,
        entry: JuraWifiConfigEntry,
        client: JuraWifiClient,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.client = client
        # The dongle accepts a single TCP session at a time.
        self._lock = asyncio.Lock()
        self._failures = 0

    async def _async_setup(self) -> None:
        """Load the machine profile (blocking file read) outside the event loop."""
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

    async def _async_update_data(self) -> JuraWifiData:
        """Fetch a snapshot; an unreachable machine is data, not an error."""
        try:
            async with self._lock:
                snapshot = await self.hass.async_add_executor_job(self.client.fetch)
        except JuraWifiAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except JuraWifiBusy as err:
            return self._handle_busy(err)
        except JuraWifiConnectionError as err:
            return self._handle_unreachable(err)
        except JuraWifiError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self._failures = 0
        return JuraWifiData(online=True, snapshot=snapshot)

    def _handle_busy(self, err: JuraWifiBusy) -> JuraWifiData:
        """Report what the machine is doing and keep the values of the last poll."""
        _LOGGER.debug(
            "Machine is busy: %s (%s)", err.activity.kind, err.activity.detail
        )
        self._failures = 0
        previous = self.data
        return JuraWifiData(
            online=True,
            snapshot=previous.snapshot if previous is not None else None,
            activity=err.activity,
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
        return JuraWifiData(
            online=False, snapshot=previous.snapshot if previous is not None else None
        )

    def _ensure_idle(self) -> None:
        """Refuse to start anything unless the machine is reachable and idle."""
        data = self.data
        if not data.online:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="machine_offline"
            )
        if data.activity is not None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="machine_busy"
            )

    async def _async_command(
        self,
        command: Callable[..., None],
        *args: str,
        translation_key: str,
        placeholders: dict[str, str],
    ) -> None:
        """Run one command session and turn its errors into Home Assistant errors.

        The caller holds the lock.
        """
        try:
            await self.hass.async_add_executor_job(command, *args)
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

    def _look_again_soon(self, activity: MachineActivity | None = None) -> None:
        """Show what a command started right away and check on the machine.

        The dongle needs a pause between two sessions, so the check follows a few
        seconds later in the background instead of holding up the caller.
        """
        if activity is not None:
            self.async_set_updated_data(
                JuraWifiData(
                    online=True, snapshot=self.data.snapshot, activity=activity
                )
            )
        self.config_entry.async_create_background_task(
            self.hass, self.async_request_refresh(), f"{DOMAIN} refresh after command"
        )

    async def async_brew(self, product: str, label: str) -> None:
        """Start a drink with the recipe stored on the machine."""
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
            if not self.data.online:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="machine_offline"
                )
            await self._async_command(
                self.client.cancel_step,
                translation_key="cancel_failed",
                placeholders={},
            )
        self._look_again_soon()
