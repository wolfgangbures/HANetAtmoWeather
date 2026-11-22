"""Isolated Home Assistant objects; no hardware or external requests."""

from unittest.mock import MagicMock

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.netatmo_indoor.const import CONF_DEVICE_ID, CONF_PRODUCT


@pytest.fixture
def entry():
    config_entry = MagicMock(spec=ConfigEntry)
    config_entry.data = {CONF_PRODUCT: "home_coach", CONF_DEVICE_ID: "indoor-1"}
    config_entry.options = {}
    return config_entry


@pytest.fixture
async def hass(tmp_path):
    instance = HomeAssistant(str(tmp_path))
    yield instance
    await instance.async_stop()
