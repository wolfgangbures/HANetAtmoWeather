"""OAuth2 setup, indoor device selection, and polling options."""

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import IndoorReading, NetatmoAuthError, NetatmoClient, NetatmoError
from .const import (
    CONF_DEVICE_ID,
    CONF_POLL_INTERVAL,
    CONF_PRODUCT,
    CONF_WEBHOOK_ID,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    PRODUCTS,
)
from .webhook import callback_url

_LOGGER = logging.getLogger(__name__)
PRODUCT_NAMES = {
    "home_coach": "Healthy Home Coach / Smart Indoor Air Quality Monitor",
    "weather_station": "Weather station indoor base unit",
}


class IndoorConfigFlow(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Only ask for permission to read the selected product family."""

    DOMAIN = DOMAIN
    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._product = "weather_station"
        self._oauth_data: dict[str, Any] = {}
        self._units: dict[str, IndoorReading] | None = None
        self._discovery_client: NetatmoClient | None = None

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, str]:
        return {"scope": PRODUCTS[self._product][1]}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return IndoorOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._product = user_input[CONF_PRODUCT]
            return await super().async_step_user()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PRODUCT, default="weather_station"): vol.In(
                        PRODUCT_NAMES
                    )
                }
            ),
        )

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        if self.source == "reauth":
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates=data
            )
        self._oauth_data = data
        return await self.async_step_device()

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if self._units is None:
            if self._discovery_client is None:
                self._discovery_client = NetatmoClient.for_access_token(
                    async_get_clientsession(self.hass),
                    self._product,
                    self._oauth_data["token"]["access_token"],
                )
            try:
                self._units = await self._discovery_client.async_get_units()
            except NetatmoAuthError:
                return self.async_abort(reason="invalid_auth")
            except (ClientError, TimeoutError, NetatmoError):
                _LOGGER.warning("Unable to discover Netatmo indoor units")
                errors["base"] = "cannot_connect"
            else:
                if not self._units:
                    return self.async_abort(reason="no_devices")
        if user_input is not None and self._units is not None:
            device_id = user_input.get(CONF_DEVICE_ID)
            if device_id not in self._units:
                errors["base"] = "invalid_device"
            else:
                await self.async_set_unique_id(f"{self._product}_{device_id}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=self._units[device_id].name,
                    data={
                        **self._oauth_data,
                        CONF_PRODUCT: self._product,
                        CONF_DEVICE_ID: device_id,
                    },
                )
        schema = (
            vol.Schema(
                {
                    vol.Required(CONF_DEVICE_ID): vol.In(
                        {key: unit.name for key, unit in self._units.items()}
                    )
                }
            )
            if self._units
            else vol.Schema({})
        )
        return self.async_show_form(step_id="device", data_schema=schema, errors=errors)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        self._product = entry_data[CONF_PRODUCT]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return await super().async_step_user()
        return self.async_show_form(step_id="reauth_confirm")


class IndoorOptionsFlow(OptionsFlow):
    """Bound the polling interval to avoid accidental high request rates."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=self.config_entry.options.get(
                        CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
                    ),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL),
                )
            }
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                options = schema(user_input)
            except vol.Invalid:
                errors["base"] = "invalid_interval"
            else:
                if not isinstance(options, dict):
                    raise TypeError(
                        "Polling options validation did not return a mapping"
                    )
                return self.async_create_entry(title="", data=options)
        placeholders = {}
        if self.config_entry.data[CONF_PRODUCT] == "weather_station":
            webhook_id = self.config_entry.data.get(CONF_WEBHOOK_ID)
            url = callback_url(self.hass, webhook_id) if webhook_id else None
            placeholders["webhook_url"] = url or (
                "Configure an external HTTPS Home Assistant URL and reload "
                "the integration to enable webhook registration."
            )
            step_id = "indoor_webhook"
        else:
            step_id = "init"
        return self.async_show_form(
            step_id=step_id,
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_indoor_webhook(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_init(user_input)
