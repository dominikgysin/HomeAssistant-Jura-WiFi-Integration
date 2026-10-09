"""Config flow for the JURA Wi-Fi Connect integration."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
from typing import Any
import uuid

from jura_connect import known_machine_names, list_profile_codes
import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.selector import (
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
    JuraWifiAuthError,
    JuraWifiClient,
    JuraWifiConnectionError,
    JuraWifiError,
    JuraWifiPairingTimeout,
)
from .const import (
    CONF_AUTH_HASH,
    CONF_CONN_ID,
    CONF_ENABLE_BREWING,
    CONF_MACHINE_TYPE,
    CONF_MODEL,
    CONF_MODEL_NAME,
    CONF_PIN,
    CONF_SCAN_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .coordinator import JuraWifiConfigEntry

_LOGGER = logging.getLogger(__name__)


def _new_conn_id() -> str:
    """Return the identifier under which this installation pairs."""
    return f"homeassistant-{uuid.uuid4().hex[:8]}"


def _machine_options() -> list[SelectOptionDict]:
    """List the machine models for which a profile is available (blocking)."""
    supported = set(list_profile_codes())
    return [
        SelectOptionDict(value=f"{code}|{name}", label=f"{name} [{code}]")
        for name, code in known_machine_names()
        if code in supported
    ]


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
        self._conn_id = ""
        self._auth_hash = ""
        self._pair_task: asyncio.Task[str] | None = None
        self._pair_client: JuraWifiClient | None = None
        self._pair_error = "unknown"

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

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the address and the model of the machine."""
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            await self.async_set_unique_id(host.lower())
            self._abort_if_unique_id_configured()
            self._host = host
            self._pin = user_input.get(CONF_PIN, "")
            self._machine_type, _, self._model_name = user_input[CONF_MODEL].partition(
                "|"
            )
            self._conn_id = _new_conn_id()
            return await self.async_step_pair()

        options = await self.hass.async_add_executor_job(_machine_options)
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): TextSelector(),
                vol.Required(CONF_MODEL): SelectSelector(
                    SelectSelectorConfig(
                        options=options, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Optional(CONF_PIN, default=""): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

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
        try:
            self._auth_hash = task.result()
        except JuraWifiPairingTimeout:
            self._pair_error = "pairing_timeout"
        except JuraWifiAuthError as err:
            self._pair_error = (
                "wrong_pin" if err.reason == "WRONG_PIN" else "pairing_rejected"
            )
        except JuraWifiConnectionError:
            self._pair_error = "cannot_connect"
        except JuraWifiError:
            _LOGGER.exception("Pairing with %s failed", self._host)
            self._pair_error = "unknown"
        else:
            return self.async_show_progress_done(next_step_id="pair_done")
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
            description_placeholders={"host": self._host},
        )

    async def async_step_pair_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Store the credentials issued by the machine."""
        credentials = {CONF_CONN_ID: self._conn_id, CONF_AUTH_HASH: self._auth_hash}
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data_updates={**credentials, CONF_PIN: self._pin},
            )
        return self.async_create_entry(
            title=f"JURA {self._model_name}",
            data={
                CONF_HOST: self._host,
                CONF_PORT: self._port,
                CONF_PIN: self._pin,
                CONF_MACHINE_TYPE: self._machine_type,
                CONF_MODEL_NAME: self._model_name,
                **credentials,
            },
            options={
                CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                CONF_ENABLE_BREWING: False,
            },
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
        """Change the address of the machine, e.g. after a new DHCP lease."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            if host.lower() != entry.unique_id:
                await self.async_set_unique_id(host.lower())
                self._abort_if_unique_id_configured()
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
            else:
                return self.async_update_reload_and_abort(
                    entry, unique_id=host.lower(), data_updates={CONF_HOST: host}
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({vol.Required(CONF_HOST): TextSelector()}),
                {CONF_HOST: entry.data[CONF_HOST]},
            ),
            errors=errors,
        )


class JuraWifiOptionsFlow(OptionsFlowWithReload):
    """Polling interval and the opt-in for brewing."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_ENABLE_BREWING: user_input[CONF_ENABLE_BREWING],
                }
            )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        step=5,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(CONF_ENABLE_BREWING, default=False): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema, self.config_entry.options
            ),
        )
