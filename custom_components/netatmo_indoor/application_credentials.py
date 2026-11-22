"""Netatmo developer-app authorization endpoints."""

from homeassistant.components.application_credentials import AuthorizationServer
from homeassistant.core import HomeAssistant

from .const import AUTHORIZE_URL, TOKEN_URL


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    """Describe the OAuth2 server for Home Assistant."""
    return AuthorizationServer(authorize_url=AUTHORIZE_URL, token_url=TOKEN_URL)
