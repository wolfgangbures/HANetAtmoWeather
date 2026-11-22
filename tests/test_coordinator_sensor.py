"""Verify poll cadence, error mapping, and the actual exposed sensor values."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import ClientConnectionError
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.netatmo_indoor.api import (
    IndoorReading,
    NetatmoAuthError,
    NetatmoRateLimitError,
)
from custom_components.netatmo_indoor.const import MAX_MEASUREMENT_AGE
from custom_components.netatmo_indoor.coordinator import IndoorCoordinator
from custom_components.netatmo_indoor.sensor import SENSORS, IndoorSensor

NOW = datetime(2026, 1, 1, tzinfo=UTC)
READING = IndoorReading("indoor-1", "Office", "NHC", 58, 42, NOW, True)


async def test_shared_poll_at_default_and_configured_interval(hass, entry):
    client = AsyncMock()
    client.async_get_units.return_value = {"indoor-1": READING}
    coordinator = IndoorCoordinator(hass, entry, client)
    assert coordinator.update_interval == timedelta(seconds=60)
    coordinator.data = await coordinator._async_update_data()
    sensors = [IndoorSensor(coordinator, description) for description in SENSORS]
    assert [sensor.native_value for sensor in sensors] == [58, 42, NOW]
    client.async_get_units.assert_awaited_once_with("indoor-1")
    entry.options = {"poll_interval": 30}
    assert IndoorCoordinator(hass, entry, client).update_interval == timedelta(
        seconds=30
    )


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (NetatmoAuthError("revoked"), ConfigEntryAuthFailed),
        (ClientConnectionError(), UpdateFailed),
        (TimeoutError(), UpdateFailed),
    ],
)
async def test_failures_surface_to_home_assistant(hass, entry, failure, expected):
    client = AsyncMock()
    client.async_get_units.side_effect = failure
    with pytest.raises(expected):
        await IndoorCoordinator(hass, entry, client)._async_update_data()


async def test_backoff_reaches_home_assistant_scheduler(hass, entry):
    client = AsyncMock()
    client.async_get_units.side_effect = NetatmoRateLimitError(120)
    with pytest.raises(UpdateFailed) as error:
        await IndoorCoordinator(hass, entry, client)._async_update_data()
    assert error.value.retry_after == 120


async def test_disappearing_unit_does_not_reuse_cached_success(hass, entry):
    client = AsyncMock()
    client.async_get_units.return_value = {}
    with pytest.raises(UpdateFailed, match="no longer returned"):
        await IndoorCoordinator(hass, entry, client)._async_update_data()


@pytest.mark.parametrize(
    ("age", "reachable", "expected"),
    [
        (0, True, True),
        (MAX_MEASUREMENT_AGE, True, True),
        (MAX_MEASUREMENT_AGE + 1, True, False),
        (-1, True, False),
        (0, False, False),
    ],
)
async def test_exact_freshness_boundary(hass, entry, age, reachable, expected):
    coordinator = IndoorCoordinator(hass, entry, AsyncMock())
    coordinator.data = replace(
        READING, measured_at=NOW - timedelta(seconds=age), reachable=reachable
    )
    with patch(
        "custom_components.netatmo_indoor.sensor.dt_util.utcnow", return_value=NOW
    ):
        for description in SENSORS[:2]:
            assert IndoorSensor(coordinator, description).available is expected
        assert IndoorSensor(coordinator, SENSORS[2]).available


async def test_missing_timestamp_and_failed_poll_make_readings_unavailable(hass, entry):
    coordinator = IndoorCoordinator(hass, entry, AsyncMock())
    coordinator.data = replace(READING, measured_at=None)
    assert not IndoorSensor(coordinator, SENSORS[0]).available
    assert not IndoorSensor(coordinator, SENSORS[2]).available
    coordinator.data = READING
    coordinator.last_update_success = False
    assert not IndoorSensor(coordinator, SENSORS[0]).available
    assert not IndoorSensor(coordinator, SENSORS[2]).available
