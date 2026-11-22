"""Polling and failure handling for a single indoor unit."""

import logging
from datetime import timedelta

from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    IndoorReading,
    NetatmoAuthError,
    NetatmoClient,
    NetatmoError,
    NetatmoRateLimitError,
)
from .const import CONF_DEVICE_ID, CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class IndoorCoordinator(DataUpdateCoordinator[IndoorReading]):
    """Fetch humidity and noise together, with server-directed backoff."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: NetatmoClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
            ),
        )
        self._client = client
        self._device_id: str = entry.data[CONF_DEVICE_ID]

    async def _async_update_data(self) -> IndoorReading:
        try:
            units = await self._client.async_get_units(self._device_id)
        except NetatmoAuthError as err:
            raise ConfigEntryAuthFailed("Please authorize Netatmo again") from err
        except NetatmoRateLimitError as err:
            raise UpdateFailed(
                "Netatmo rate limit reached; polling paused",
                retry_after=err.retry_after,
            ) from err
        except NetatmoError as err:
            raise UpdateFailed(str(err)) from err
        except (ClientError, TimeoutError) as err:
            raise UpdateFailed("Unable to contact Netatmo") from err
        if self._device_id not in units:
            raise UpdateFailed(
                "The selected indoor unit is no longer returned by Netatmo"
            )
        return units[self._device_id]
