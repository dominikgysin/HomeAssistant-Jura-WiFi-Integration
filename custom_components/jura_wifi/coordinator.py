"""DataUpdateCoordinator for the JURA Wi-Fi Connect integration."""

from __future__ import annotations

import asyncio
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
    JuraWifiAuthError,
    JuraWifiClient,
    JuraWifiConnectionError,
    JuraWifiError,
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
    """What the entities see: reachability plus the last known snapshot."""

    online: bool
    snapshot: MachineSnapshot | None


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

    async def async_brew(self, product: str, label: str) -> None:
        """Start a product with its factory-default recipe."""
        async with self._lock:
            # Check under the lock: a poll that was running while the button was
            # pressed may have changed what the machine reports.
            data = self.data
            if not data.online:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="machine_offline"
                )
            if data.snapshot is not None and product in data.snapshot.blocked_products:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="product_blocked",
                    translation_placeholders={"product": label},
                )
            try:
                await self.hass.async_add_executor_job(self.client.brew, product)
            except JuraWifiAuthError as err:
                self.config_entry.async_start_reauth(self.hass)
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="auth_failed"
                ) from err
            except JuraWifiConnectionError as err:
                self.async_set_updated_data(self._handle_unreachable(err))
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="brew_failed",
                    translation_placeholders={"product": label, "error": str(err)},
                ) from err
            except JuraWifiError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="brew_failed",
                    translation_placeholders={"product": label, "error": str(err)},
                ) from err
            self._failures = 0
