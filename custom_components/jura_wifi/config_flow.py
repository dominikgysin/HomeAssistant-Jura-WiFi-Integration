"""Config flow for the JURA Wi-Fi Connect integration."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
from typing import Any
import uuid

from jura_connect import (
    known_machine_names,
    list_profile_codes,
    lookup_by_article_number,
)
import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    AreaSelector,
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    ACTIVITY_PROGRAMMING,
    JuraWifiAuthError,
    JuraWifiBusy,
    JuraWifiClient,
    JuraWifiConnectionError,
    JuraWifiError,
    JuraWifiPairingTimeout,
    MachineIdentity,
    discover_machine,
)
from .const import (
    CONF_AREA,
    CONF_ARTICLE_NUMBER,
    CONF_AUTH_HASH,
    CONF_CONN_ID,
    CONF_ENABLE_BREWING,
    CONF_ENABLE_MAINTENANCE,
    CONF_ENABLE_SETTINGS,
    CONF_FIRMWARE,
    CONF_MACHINE_TYPE,
    CONF_MODEL,
    CONF_MODEL_NAME,
    CONF_MODEL_SOURCE,
    CONF_PIN,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL_NUMBER,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    MODEL_SOURCE_ARTICLE,
    MODEL_SOURCE_DISCOVERY,
    MODEL_SOURCE_MANUAL,
)
from .coordinator import JuraWifiConfigEntry

_LOGGER = logging.getLogger(__name__)


def _new_conn_id() -> str:
    """Return the identifier under which this installation pairs."""
    return f"homeassistant-{uuid.uuid4().hex[:8]}"


def _option_fields() -> dict[Any, Any]:
    """Return the form fields of the options, as asked at the setup and later."""
    return {
        vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): NumberSelector(
            NumberSelectorConfig(
                min=MIN_SCAN_INTERVAL,
                max=MAX_SCAN_INTERVAL,
                step=5,
                unit_of_measurement="s",
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Required(CONF_ENABLE_BREWING, default=False): BooleanSelector(),
        vol.Required(CONF_ENABLE_MAINTENANCE, default=False): BooleanSelector(),
        vol.Required(CONF_ENABLE_SETTINGS, default=False): BooleanSelector(),
    }


def _options_from_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Return the options that a submitted form holds."""
    return {
        CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
        CONF_ENABLE_BREWING: user_input[CONF_ENABLE_BREWING],
        CONF_ENABLE_MAINTENANCE: user_input[CONF_ENABLE_MAINTENANCE],
        CONF_ENABLE_SETTINGS: user_input[CONF_ENABLE_SETTINGS],
    }


def _machine_options() -> list[SelectOptionDict]:
    """List the machine models for which a profile is available (blocking)."""
    supported = set(list_profile_codes())
    return [
        SelectOptionDict(value=f"{code}|{name}", label=f"{name} [{code}]")
        for name, code in known_machine_names()
        if code in supported
    ]


def _resolve_article(raw: str) -> tuple[int, str, str] | None:
    """Look up an article number as ``(number, model name, EF code)`` (blocking).

    Only models the library has a profile for count as known.
    """
    text = raw.strip()
    if not text.isdigit():
        return None
    entry = lookup_by_article_number(int(text))
    if entry is None or entry.ef_code not in set(list_profile_codes()):
        return None
    return entry.article_number, entry.friendly_name, entry.ef_code


class JuraWifiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a JURA machine with a Wi-Fi Connect dongle."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._host = ""
        self._port = DEFAULT_PORT
        self._pin = ""
        self._machine_type = ""
        self._model_name = ""
        self._article_number: int | None = None
        self._firmware: str | None = None
        self._serial_number: int | None = None
        self._model_source = MODEL_SOURCE_MANUAL
        self._conn_id = ""
        self._auth_hash = ""
        self._pair_task: asyncio.Task[str] | None = None
        self._pair_client: JuraWifiClient | None = None
        self._pair_error = "unknown"
        self._pair_reason = ""
        self._identify_task: asyncio.Task[MachineIdentity | None] | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: JuraWifiConfigEntry,
    ) -> JuraWifiOptionsFlow:
        """Return the options flow."""
        return JuraWifiOptionsFlow()

    @callback
    def async_remove(self) -> None:
        """Release the dongle when the flow is closed while pairing."""
        if self._pair_client is not None:
            self._pair_client.cancel()

    def _host_in_use(self, host: str, exclude: ConfigEntry | None = None) -> bool:
        """Say whether another entry talks to the dongle at this address.

        Entries are identified by the serial number of the machine, not by the
        address, so the address has to be compared separately.
        """
        return any(
            str(entry.data.get(CONF_HOST, "")).lower() == host.lower()
            for entry in self._async_current_entries()
            if exclude is None or entry.entry_id != exclude.entry_id
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the address of the dongle; the model is read from the machine."""
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            if self._host_in_use(host):
                return self.async_abort(reason="already_configured")
            # Entries of earlier versions are identified by the address. The serial
            # number replaces it once the machine has told it.
            await self.async_set_unique_id(host.lower())
            self._abort_if_unique_id_configured()
            self._host = host
            self._conn_id = _new_conn_id()
            return await self.async_step_pair()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_HOST): TextSelector()}),
        )

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pair with the machine while the user confirms on its display."""
        if self._pair_task is None:
            self._pair_client = JuraWifiClient(
                self._host, self._port, self._conn_id, "", self._pin, self._machine_type
            )
            self._pair_task = self.hass.async_create_task(
                self._async_pair(self._pair_client)
            )

        if not self._pair_task.done():
            return self.async_show_progress(
                step_id="pair",
                progress_action="pair",
                progress_task=self._pair_task,
                description_placeholders={"host": self._host},
            )

        task, self._pair_task = self._pair_task, None
        self._pair_client = None
        self._pair_reason = ""
        try:
            self._auth_hash = task.result()
        except JuraWifiPairingTimeout:
            self._pair_error = "pairing_timeout"
            self._pair_reason = "timeout"
        except JuraWifiAuthError as err:
            _LOGGER.warning(
                "The machine at %s refused the connection request: %s",
                self._host,
                err.reason,
            )
            self._pair_error = (
                "wrong_pin" if err.reason == "WRONG_PIN" else "pairing_rejected"
            )
            self._pair_reason = err.reason
        except JuraWifiConnectionError as err:
            self._pair_error = "cannot_connect"
            self._pair_reason = str(err)
        except JuraWifiBusy as err:
            _LOGGER.warning(
                "The machine at %s is busy and cannot pair: %s (%s)",
                self._host,
                err.activity.kind,
                err.activity.detail,
            )
            self._pair_error = (
                "machine_in_menu"
                if err.activity.kind == ACTIVITY_PROGRAMMING
                else "machine_busy"
            )
            self._pair_reason = err.activity.detail or err.activity.kind
        except JuraWifiError as err:
            _LOGGER.exception("Pairing with %s failed", self._host)
            self._pair_error = "unknown"
            self._pair_reason = str(err)
        else:
            # A re-pairing keeps the model of the existing entry.
            return self.async_show_progress_done(
                next_step_id="pair_done" if self.source == SOURCE_REAUTH else "identify"
            )
        return self.async_show_progress_done(next_step_id="pair_failed")

    async def _async_pair(self, client: JuraWifiClient) -> str:
        return await self.hass.async_add_executor_job(client.pair)

    async def async_step_pair_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show why pairing failed and offer another attempt."""
        if user_input is not None:
            self._pin = user_input.get(CONF_PIN, self._pin)
            # The dongle remembers identifiers of earlier attempts, so a retry
            # pairs under a new one.
            self._conn_id = _new_conn_id()
            return await self.async_step_pair()
        schema = None
        if self._pair_error == "wrong_pin":
            schema = self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Optional(CONF_PIN, default=""): TextSelector(
                            TextSelectorConfig(type=TextSelectorType.PASSWORD)
                        )
                    }
                ),
                {CONF_PIN: self._pin},
            )
        return self.async_show_form(
            step_id="pair_failed",
            data_schema=schema,
            errors={"base": self._pair_error},
            description_placeholders={
                "host": self._host,
                "reason": self._pair_reason or "-",
            },
        )

    async def async_step_identify(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Read the exact model from the machine, like the J.O.E. app does."""
        if self._identify_task is None:
            self._identify_task = self.hass.async_create_task(self._async_identify())

        if not self._identify_task.done():
            return self.async_show_progress(
                step_id="identify",
                progress_action="identify",
                progress_task=self._identify_task,
                description_placeholders={"host": self._host},
            )

        task, self._identify_task = self._identify_task, None
        identity = None
        try:
            identity = task.result()
        except Exception:
            _LOGGER.exception("Reading the model of %s failed", self._host)
        if identity is not None:
            self._article_number = identity.article_number
            self._firmware = identity.firmware or None
            self._serial_number = identity.serial_number
            if identity.ef_code and identity.model_name:
                self._machine_type = identity.ef_code
                self._model_name = identity.model_name
                self._model_source = MODEL_SOURCE_DISCOVERY
                return self.async_show_progress_done(next_step_id="pair_done")
        return self.async_show_progress_done(next_step_id="article")

    async def _async_identify(self) -> MachineIdentity | None:
        return await self.hass.async_add_executor_job(discover_machine, self._host)

    async def async_step_article(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the article number when the machine did not announce it."""
        errors: dict[str, str] = {}
        if user_input is not None:
            raw = user_input.get(CONF_ARTICLE_NUMBER, "").strip()
            if not raw:
                return await self.async_step_model()
            resolved = await self.hass.async_add_executor_job(_resolve_article, raw)
            if resolved is not None:
                (
                    self._article_number,
                    self._model_name,
                    self._machine_type,
                ) = resolved
                self._model_source = MODEL_SOURCE_ARTICLE
                return await self.async_step_pair_done()
            errors["base"] = "unknown_article"

        return self.async_show_form(
            step_id="article",
            data_schema=vol.Schema(
                {vol.Optional(CONF_ARTICLE_NUMBER, default=""): TextSelector()}
            ),
            errors=errors,
        )

    async def async_step_model(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Last resort: pick the model from the list of all known machines."""
        if user_input is not None:
            self._machine_type, _, self._model_name = user_input[CONF_MODEL].partition(
                "|"
            )
            self._model_source = MODEL_SOURCE_MANUAL
            return await self.async_step_pair_done()

        options = await self.hass.async_add_executor_job(_machine_options)
        return self.async_show_form(
            step_id="model",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MODEL): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.DROPDOWN
                        )
                    )
                }
            ),
        )

    async def async_step_pair_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Store the credentials issued by the machine, or ask for the options."""
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data_updates={
                    CONF_CONN_ID: self._conn_id,
                    CONF_AUTH_HASH: self._auth_hash,
                    CONF_PIN: self._pin,
                },
            )
        if self._serial_number:
            await self.async_set_unique_id(str(self._serial_number))
            self._abort_if_unique_id_configured()
        return await self.async_step_options()

    async def async_step_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the area and the options, then create the entry.

        The area is only used when the device of the machine is created, so that
        all of its entities get the name of the area in their entity IDs.
        """
        if user_input is not None:
            data = {
                CONF_HOST: self._host,
                CONF_PORT: self._port,
                CONF_PIN: self._pin,
                CONF_MACHINE_TYPE: self._machine_type,
                CONF_MODEL_NAME: self._model_name,
                CONF_ARTICLE_NUMBER: self._article_number,
                CONF_FIRMWARE: self._firmware,
                CONF_MODEL_SOURCE: self._model_source,
                CONF_CONN_ID: self._conn_id,
                CONF_AUTH_HASH: self._auth_hash,
            }
            if self._serial_number:
                data[CONF_SERIAL_NUMBER] = self._serial_number
            if area := user_input.get(CONF_AREA):
                data[CONF_AREA] = area
            return self.async_create_entry(
                title=f"JURA {self._model_name}",
                data=data,
                options=_options_from_input(user_input),
            )
        return self.async_show_form(
            step_id="options",
            data_schema=vol.Schema(
                {vol.Optional(CONF_AREA): AreaSelector(), **_option_fields()}
            ),
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start pairing again after the machine rejected the credentials."""
        entry = self._get_reauth_entry()
        self._host = entry.data[CONF_HOST]
        self._port = entry.data.get(CONF_PORT, DEFAULT_PORT)
        self._pin = entry.data.get(CONF_PIN, "")
        self._machine_type = entry.data[CONF_MACHINE_TYPE]
        self._model_name = entry.data.get(CONF_MODEL_NAME, "")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explain that the machine has to confirm a new connection."""
        if user_input is not None:
            self._conn_id = _new_conn_id()
            return await self.async_step_pair()
        return self.async_show_form(
            step_id="reauth_confirm",
            description_placeholders={"host": self._host},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the address of the machine, e.g. after a new DHCP lease.

        Where the dongle cannot be reached by UDP, which is how the article number
        is read, it can be entered here instead. It must belong to the model that is
        set up: the machine type decides which profile is used and is not changed.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            host_changed = host.lower() != str(entry.data[CONF_HOST]).lower()
            if host_changed and self._host_in_use(host, exclude=entry):
                return self.async_abort(reason="already_configured")
            data_updates: dict[str, Any] = {CONF_HOST: host}
            article = (user_input.get(CONF_ARTICLE_NUMBER) or "").strip()
            if article and article != str(entry.data.get(CONF_ARTICLE_NUMBER) or ""):
                resolved = await self.hass.async_add_executor_job(
                    _resolve_article, article
                )
                if resolved is None:
                    errors["base"] = "unknown_article"
                elif resolved[2] != entry.data[CONF_MACHINE_TYPE]:
                    errors["base"] = "article_other_model"
                else:
                    data_updates.update(
                        {
                            CONF_ARTICLE_NUMBER: resolved[0],
                            CONF_MODEL_NAME: resolved[1],
                            CONF_MODEL_SOURCE: MODEL_SOURCE_ARTICLE,
                        }
                    )
            if host_changed and not errors:
                client = JuraWifiClient(
                    host,
                    entry.data.get(CONF_PORT, DEFAULT_PORT),
                    entry.data[CONF_CONN_ID],
                    entry.data[CONF_AUTH_HASH],
                    entry.data.get(CONF_PIN, ""),
                    entry.data[CONF_MACHINE_TYPE],
                )
                try:
                    await self.hass.async_add_executor_job(client.check)
                except JuraWifiAuthError:
                    errors["base"] = "invalid_auth"
                except JuraWifiError:
                    errors["base"] = "cannot_connect"
            if not errors:
                # The serial number identifies an entry that has one, the address
                # one that has not.
                unique_id = (
                    entry.unique_id
                    if entry.data.get(CONF_SERIAL_NUMBER)
                    else host.lower()
                )
                return self.async_update_reload_and_abort(
                    entry, unique_id=unique_id, data_updates=data_updates
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_HOST): TextSelector(),
                        vol.Optional(CONF_ARTICLE_NUMBER): TextSelector(),
                    }
                ),
                {
                    CONF_HOST: entry.data[CONF_HOST],
                    CONF_ARTICLE_NUMBER: str(entry.data.get(CONF_ARTICLE_NUMBER) or ""),
                },
            ),
            errors=errors,
        )


class JuraWifiOptionsFlow(OptionsFlowWithReload):
    """Polling interval and the opt-ins for brewing, maintenance and settings."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=_options_from_input(user_input))
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(_option_fields()), self.config_entry.options
            ),
        )
