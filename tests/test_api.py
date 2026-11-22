"""Exercise indoor filtering, source timestamps, failures, and backoff."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from json import JSONDecodeError
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientResponse

from custom_components.netatmo_indoor.api import (
    NetatmoAuthError,
    NetatmoClient,
    NetatmoError,
    NetatmoRateLimitError,
    RateLimit,
)
from custom_components.netatmo_indoor.const import WEBHOOK_API_URL


def dashboard_payload(model="NHC"):
    return {
        "body": {
            "devices": [
                {
                    "_id": "indoor-1",
                    "type": model,
                    "station_name": "Office",
                    "reachable": True,
                    "dashboard_data": {
                        "Humidity": 58,
                        "Noise": 42,
                        "time_utc": 1767225600,
                    },
                    "modules": [
                        {
                            "_id": "outdoor-1",
                            "type": "NAModule1",
                            "dashboard_data": {"Humidity": 99},
                        }
                    ],
                },
                {"_id": "outdoor-2", "type": "NAModule1"},
                {"_id": "extra-indoor", "type": "NAModule4"},
            ]
        }
    }


def make_client(payload=None, *, status=200, headers=None, product="home_coach"):
    response = MagicMock(spec=ClientResponse)
    response.status = status
    response.headers = headers or {}
    response.__aenter__.return_value = response
    response.json = AsyncMock(return_value=payload)
    request = AsyncMock(return_value=response)
    return NetatmoClient(request, product), request, response


@pytest.mark.parametrize(
    ("product", "model", "endpoint"),
    [
        ("home_coach", "NHC", "gethomecoachsdata"),
        ("weather_station", "NAMain", "getstationsdata"),
    ],
)
async def test_only_main_indoor_device(product, model, endpoint):
    client, request, _ = make_client(dashboard_payload(model), product=product)
    units = await client.async_get_units("indoor-1")
    assert list(units) == ["indoor-1"]
    reading = units["indoor-1"]
    assert (reading.humidity, reading.noise) == (58, 42)
    assert reading.measured_at == datetime(2026, 1, 1, tzinfo=UTC)
    request.assert_awaited_once_with(
        "GET",
        f"https://api.netatmo.com/api/{endpoint}",
        params={"device_id": "indoor-1"},
    )


async def test_discovery_ignores_all_attached_modules():
    client, _, _ = make_client(dashboard_payload("NAMain"), product="weather_station")
    assert list(await client.async_get_units()) == ["indoor-1"]


async def test_missing_values_stay_missing_and_zero_is_preserved():
    payload = dashboard_payload()
    payload["body"]["devices"][0]["dashboard_data"] = {"Noise": 0}
    client, _, _ = make_client(payload)
    unit = (await client.async_get_units())["indoor-1"]
    assert unit.humidity is None
    assert unit.noise == 0
    assert unit.measured_at is None


@pytest.mark.parametrize("value", [True, "55", float("nan"), 101, -1])
async def test_invalid_humidity_is_an_explicit_error(value):
    payload = dashboard_payload()
    payload["body"]["devices"][0]["dashboard_data"]["Humidity"] = value
    client, _, _ = make_client(payload)
    with pytest.raises(NetatmoError, match="Invalid humidity"):
        await client.async_get_units()


@pytest.mark.parametrize("payload", [None, {}, {"body": {}}, {"body": {"devices": {}}}])
async def test_malformed_payload_is_not_an_empty_success(payload):
    client, _, _ = make_client(payload)
    with pytest.raises(NetatmoError):
        await client.async_get_units()


async def test_invalid_json_is_an_explicit_error():
    client, _, response = make_client()
    response.json.side_effect = JSONDecodeError("bad json", "", 0)
    with pytest.raises(NetatmoError, match="Invalid Netatmo JSON"):
        await client.async_get_units()


@pytest.mark.parametrize("status", [401, 403])
async def test_http_authorization_errors(status):
    client, _, _ = make_client(status=status)
    with pytest.raises(NetatmoAuthError):
        await client.async_get_units()


async def test_api_authorization_error_even_with_http_200():
    client, _, _ = make_client({"error": {"code": 3}})
    with pytest.raises(NetatmoAuthError):
        await client.async_get_units()


@pytest.mark.parametrize(
    ("header", "expected"),
    [("120", 120), (None, 600), ("invalid", 600), ("nan", 600), ("1", 60)],
)
async def test_rate_limit_blocks_manual_requests_until_retry_after(header, expected):
    headers = {"Retry-After": header} if header is not None else {}
    client, request, response = make_client(status=429, headers=headers)
    with patch("custom_components.netatmo_indoor.api.monotonic", return_value=100):
        with pytest.raises(NetatmoRateLimitError) as error:
            await client.async_get_units()
        assert error.value.retry_after == expected
        with pytest.raises(NetatmoRateLimitError):
            await client.async_get_units()
    assert request.await_count == 1
    response.status = 200
    response.json.return_value = dashboard_payload()
    with patch(
        "custom_components.netatmo_indoor.api.monotonic",
        return_value=100 + expected + 1,
    ):
        assert "indoor-1" in await client.async_get_units()
    assert request.await_count == 2


async def test_retry_after_http_date():
    retry_at = datetime.now(UTC) + timedelta(minutes=5)
    client, _, _ = make_client(
        status=429, headers={"Retry-After": format_datetime(retry_at, usegmt=True)}
    )
    with pytest.raises(NetatmoRateLimitError) as error:
        await client.async_get_units()
    assert 295 <= error.value.retry_after <= 300


async def test_api_quota_error_blocks_requests():
    client, request, _ = make_client({"error": {"code": 26}})
    with pytest.raises(NetatmoRateLimitError):
        await client.async_get_units()
    with pytest.raises(NetatmoRateLimitError):
        await client.async_get_units()
    assert request.await_count == 1


async def test_shared_cooldown_survives_client_recreation():
    original, request, _ = make_client(status=429)
    cooldown = RateLimit()
    original = NetatmoClient(request, "home_coach", cooldown)
    with pytest.raises(NetatmoRateLimitError):
        await original.async_get_units()
    recreated = NetatmoClient(request, "weather_station", cooldown)
    with pytest.raises(NetatmoRateLimitError):
        await recreated.async_get_units()
    assert request.await_count == 1


async def test_subscription_already_present_does_not_post_or_replace_other_hooks():
    url = "https://ha.example/api/webhook/ours"
    client, request, _ = make_client(
        {"status": "ok", "body": [{"url": url}, {"url": "https://other.example/hook"}]}
    )
    await client.async_ensure_webhook(url)
    request.assert_awaited_once_with("GET", WEBHOOK_API_URL)


async def test_missing_subscription_is_appended_once_and_confirmed():
    url = "https://ha.example/api/webhook/ours"
    client, request, response = make_client()
    response.json.side_effect = [
        {"status": "ok", "body": [{"url": "https://other.example/hook"}]},
        {"status": "ok", "body": [{"url": "https://other.example/hook"}, {"url": url}]},
    ]
    await client.async_ensure_webhook(url)
    assert request.await_count == 2
    assert request.await_args_list[0].args == ("GET", WEBHOOK_API_URL)
    assert request.await_args_list[1].args == ("POST", WEBHOOK_API_URL)
    assert request.await_args_list[1].kwargs == {"json": {"url": url}}


@pytest.mark.parametrize(
    "payload", [{}, {"body": {}}, {"body": [{}]}, {"body": [{"url": 42}]}]
)
async def test_invalid_subscription_list_is_not_treated_as_absent(payload):
    client, request, _ = make_client(payload)
    with pytest.raises(NetatmoError, match="Invalid Netatmo webhook list"):
        await client.async_ensure_webhook("https://ha.example/hook")
    assert request.await_count == 1


async def test_subscription_post_without_url_confirmation_is_an_explicit_error():
    client, _, response = make_client()
    response.json.side_effect = [
        {"status": "ok", "body": []},
        {"status": "ok", "body": []},
    ]
    with pytest.raises(NetatmoError, match="did not confirm"):
        await client.async_ensure_webhook("https://ha.example/hook")


async def test_subscription_rate_limit_also_blocks_polling():
    client, request, _ = make_client(status=429)
    with pytest.raises(NetatmoRateLimitError):
        await client.async_ensure_webhook("https://ha.example/hook")
    with pytest.raises(NetatmoRateLimitError):
        await client.async_get_units("indoor-1")
    assert request.await_count == 1
