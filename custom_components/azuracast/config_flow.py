"""Config flow for AzuraCast."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from homeassistant.config_entries import (
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_API_KEY, CONF_SCAN_INTERVAL, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
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
import probatio

from .api.client import AzuraCastClient, normalize_url
from .api.errors import AzuraCastAuthError, AzuraCastError, AzuraCastPermissionError
from .const import (
    CONF_ALLOW_UNAVAILABLE_STATIONS,
    CONF_EXCLUDED_STATIONS,
    CONF_IMMEDIATE_CHANGE_STATIONS,
    CONF_KEEP_QUEUE_STATIONS,
    CONF_RELOAD_AFTER_SONG_STATIONS,
    CONF_RELOAD_CHANGE_STATIONS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOGGER,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .runtime import AzuraCastConfigEntry

URL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
KEY_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

USER_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_URL): URL_SELECTOR,
        probatio.Optional(CONF_API_KEY): KEY_SELECTOR,
        probatio.Required(CONF_VERIFY_SSL, default=True): BooleanSelector(),
    }
)
REAUTH_SCHEMA = probatio.Schema({probatio.Required(CONF_API_KEY): KEY_SELECTOR})


def _title(url: str) -> str:
    return urlsplit(url).hostname or url


class AzuraCastConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for AzuraCast."""

    VERSION = 1

    async def _validate(self, url: str, api_key: str | None, verify_ssl: bool) -> dict[str, str]:
        client = AzuraCastClient(
            async_get_clientsession(self.hass, verify_ssl=verify_ssl), url, api_key
        )
        try:
            await client.get_status()
            if api_key:
                await client.get_account()
        except AzuraCastAuthError, AzuraCastPermissionError:
            return {"base": "invalid_auth"}
        except AzuraCastError:
            return {"base": "cannot_connect"}
        except Exception:
            LOGGER.exception("Unexpected error validating AzuraCast")
            return {"base": "unknown"}
        return {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                url = normalize_url(user_input[CONF_URL])
            except ValueError:
                errors[CONF_URL] = "invalid_url"
            else:
                await self.async_set_unique_id(url)
                self._abort_if_unique_id_configured()
                api_key = user_input.get(CONF_API_KEY) or None
                verify_ssl = user_input[CONF_VERIFY_SSL]
                errors = await self._validate(url, api_key, verify_ssl)
                if not errors:
                    return self.async_create_entry(
                        title=_title(url),
                        data={CONF_URL: url, CONF_API_KEY: api_key, CONF_VERIFY_SSL: verify_ssl},
                    )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY]
            errors = await self._validate(
                entry.data[CONF_URL], api_key, entry.data[CONF_VERIFY_SSL]
            )
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: api_key}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"name": entry.title},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                url = normalize_url(user_input[CONF_URL])
            except ValueError:
                errors[CONF_URL] = "invalid_url"
            else:
                if url != entry.unique_id:
                    await self.async_set_unique_id(url)
                    self._abort_if_unique_id_configured()
                api_key = user_input.get(CONF_API_KEY) or None
                verify_ssl = user_input[CONF_VERIFY_SSL]
                errors = await self._validate(url, api_key, verify_ssl)
                if not errors:
                    return self.async_update_reload_and_abort(
                        entry,
                        unique_id=url,
                        title=_title(url),
                        data={CONF_URL: url, CONF_API_KEY: api_key, CONF_VERIFY_SSL: verify_ssl},
                    )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input or entry.data),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: AzuraCastConfigEntry) -> AzuraCastOptionsFlow:
        return AzuraCastOptionsFlow()


class AzuraCastOptionsFlow(OptionsFlowWithReload):
    """Options for an AzuraCast instance."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry: AzuraCastConfigEntry = self.config_entry
        if user_input is not None:
            return self.async_create_entry(
                data={**user_input, CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            )
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_loaded")
        try:
            stations = await entry.runtime_data.client.get_stations()
        except AzuraCastError:
            return self.async_abort(reason="cannot_connect")
        except Exception:
            LOGGER.exception("Unexpected error listing AzuraCast stations")
            return self.async_abort(reason="unknown")

        station_selector = SelectSelector(
            SelectSelectorConfig(
                options=[SelectOptionDict(value=str(s.id), label=s.name) for s in stations],
                multiple=True,
                mode=SelectSelectorMode.LIST,
            )
        )
        options = entry.options
        known = {str(s.id) for s in stations}

        def _known(key: str) -> list[str]:
            # Drop ids of stations that no longer exist so the form still validates.
            return [sid for sid in options.get(key, []) if sid in known]

        schema = probatio.Schema(
            {
                probatio.Optional(
                    CONF_EXCLUDED_STATIONS, default=_known(CONF_EXCLUDED_STATIONS)
                ): station_selector,
                probatio.Optional(
                    CONF_KEEP_QUEUE_STATIONS, default=_known(CONF_KEEP_QUEUE_STATIONS)
                ): station_selector,
                probatio.Optional(
                    CONF_IMMEDIATE_CHANGE_STATIONS,
                    default=_known(CONF_IMMEDIATE_CHANGE_STATIONS),
                ): station_selector,
                probatio.Optional(
                    CONF_RELOAD_CHANGE_STATIONS,
                    default=_known(CONF_RELOAD_CHANGE_STATIONS),
                ): station_selector,
                probatio.Optional(
                    CONF_RELOAD_AFTER_SONG_STATIONS,
                    default=_known(CONF_RELOAD_AFTER_SONG_STATIONS),
                ): station_selector,
                probatio.Optional(
                    CONF_ALLOW_UNAVAILABLE_STATIONS,
                    default=_known(CONF_ALLOW_UNAVAILABLE_STATIONS),
                ): station_selector,
                probatio.Required(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        step=10,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
