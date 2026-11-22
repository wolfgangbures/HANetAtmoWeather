"""Netatmo indoor humidity and noise, without other Netatmo products."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_entry_oauth2_flow

from .api import NetatmoClient, RateLimit
from .const import CONF_PRODUCT, DOMAIN
from .coordinator import IndoorCoordinator
from .webhook import async_setup_webhook

type IndoorConfigEntry = ConfigEntry[IndoorCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: IndoorConfigEntry) -> bool:
    """Load the selected indoor unit after a successful initial fetch."""
    implementation = (
        await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass, entry
        )
    )
    oauth = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
    cooldown = hass.data.setdefault(DOMAIN, RateLimit())
    client = NetatmoClient(oauth.async_request, entry.data[CONF_PRODUCT], cooldown)
    coordinator = IndoorCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    if entry.data[CONF_PRODUCT] == "weather_station":
        if (
            not isinstance(
                implementation, config_entry_oauth2_flow.LocalOAuth2Implementation
            )
            or not implementation.client_secret
        ):
            raise ConfigEntryError(
                "Indoor webhooks require local Netatmo application credentials "
                "with a client secret for signature verification"
            )
        await async_setup_webhook(hass, entry, implementation.client_secret, client)
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.SENSOR])
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: IndoorConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: IndoorConfigEntry) -> bool:
    """Unload sensors; the coordinator cancels its polling on entry unload."""
    return await hass.config_entries.async_unload_platforms(entry, [Platform.SENSOR])
