"""Signed reception, privacy, subscription idempotence, and receiver lifecycle."""

import hashlib
import hmac
import json
import logging
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from homeassistant.components import webhook
from homeassistant.util.aiohttp import MockRequest

from custom_components.netatmo_indoor.api import NetatmoError
from custom_components.netatmo_indoor.const import CONF_WEBHOOK_ID
from custom_components.netatmo_indoor.webhook import (
    MAX_BODY_SIZE,
    async_setup_webhook,
    callback_url,
)

SECRET = "synthetic-client-secret"


def request_for(payload, *, signature=True, body=None, method="POST"):
    raw = body if body is not None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if signature:
        headers["X-Netatmo-secret"] = hmac.new(
            SECRET.encode(), raw, hashlib.sha256
        ).hexdigest()
    return MockRequest(content=raw, mock_source="test", headers=headers, method=method)


@pytest.fixture
async def receiver(hass, entry):
    entry.data = {
        "product": "weather_station",
        "device_id": "indoor-1",
        CONF_WEBHOOK_ID: "synthetic-hook-id",
    }
    client = AsyncMock()
    with patch(
        "custom_components.netatmo_indoor.webhook.callback_url",
        return_value="https://ha.example/api/webhook/synthetic-hook-id",
    ):
        await async_setup_webhook(hass, entry, SECRET, client)
    yield hass, entry, client
    webhook.async_unregister(hass, entry.data[CONF_WEBHOOK_ID])


def event_payload():
    return {
        "push_type": "device_event",
        "device_id": "indoor-1",
        "user_id": "private-account",
        "home_name": "private-home",
        "extra_params": {
            "modules": [
                {
                    "id": "indoor-1",
                    "type": "NAMain",
                    "dashboard_data": {"Humidity": 58, "Noise": 42, "time_utc": 1234},
                    "access_token": "private-token",
                    "name": "private-room",
                },
                {"id": "outdoor-1", "noise": 9999, "humidity": 9999},
            ]
        },
    }


async def deliver(receiver, request):
    hass, entry, _ = receiver
    return await webhook.async_handle_webhook(
        hass, entry.data[CONF_WEBHOOK_ID], request
    )


async def test_signed_event_logs_only_selected_indoor_numeric_fields(receiver, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.netatmo_indoor")
    response = await deliver(receiver, request_for(event_payload()))
    assert response.status == 200
    text = caplog.text
    assert '"Humidity": 58' in text
    assert '"Noise": 42' in text
    for private in (
        "private-account",
        "private-home",
        "private-token",
        "private-room",
        "outdoor-1",
        "9999",
        "indoor-1",
        SECRET,
        "synthetic-hook-id",
    ):
        assert private not in text
    _, _, client = receiver
    client.async_get_units.assert_not_called()


@pytest.mark.parametrize("variant", ["missing", "wrong", "tampered"])
async def test_rejects_unauthenticated_requests(receiver, variant, caplog):
    request = request_for(event_payload(), signature=variant != "missing")
    if variant == "wrong":
        request.headers["X-Netatmo-secret"] = "0" * 64
    elif variant == "tampered":
        request._content += b" "
    response = await deliver(receiver, request)
    assert response.status == 403
    assert "Indoor webhook telemetry" not in caplog.text


@pytest.mark.parametrize("body", [b"invalid", b"[]", b'{"push_type": {}}'])
async def test_signed_malformed_payload_returns_explicit_error(receiver, body):
    assert (await deliver(receiver, request_for(None, body=body))).status == 400


async def test_oversized_body_is_rejected(receiver):
    body = b" " * (MAX_BODY_SIZE + 1)
    assert (await deliver(receiver, request_for(None, body=body))).status == 413


async def test_exact_body_limit_and_original_whitespace_signature(receiver):
    raw = b'{ "push_type": "webhook_activation" }'
    raw += b" " * (MAX_BODY_SIZE - len(raw))
    assert (await deliver(receiver, request_for(None, body=raw))).status == 200


async def test_real_http_post_to_registered_home_assistant_handler(receiver, caplog):
    hass, entry, _ = receiver
    caplog.set_level(logging.DEBUG, logger="custom_components.netatmo_indoor")

    async def handle(request):
        return await webhook.async_handle_webhook(
            hass, entry.data[CONF_WEBHOOK_ID], request
        )

    app = web.Application()
    app.router.add_post("/callback", handle)
    raw = json.dumps(event_payload()).encode()
    signature = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/callback", data=raw, headers={"X-Netatmo-secret": signature}
        )
        assert response.status == 200
        response = await client.post(
            "/callback", data=raw, headers={"X-Netatmo-secret": "0" * 64}
        )
        assert response.status == 403
    assert '"Humidity": 58' in caplog.text


async def test_events_for_other_units_are_acknowledged_without_telemetry(
    receiver, caplog
):
    caplog.set_level(logging.DEBUG, logger="custom_components.netatmo_indoor")
    payload = event_payload()
    payload["device_id"] = "other-unit"
    payload["extra_params"]["modules"] = [{"id": "outdoor-1", "humidity": 99}]
    assert (await deliver(receiver, request_for(payload))).status == 200
    assert "Indoor webhook telemetry" not in caplog.text


@pytest.mark.parametrize("event", ["webhook_activation", "webhook_deactivation"])
async def test_signed_lifecycle_event_is_logged(receiver, caplog, event):
    caplog.set_level(logging.INFO, logger="custom_components.netatmo_indoor")
    assert (await deliver(receiver, request_for({"push_type": event}))).status == 200
    assert event in caplog.text


async def test_only_post_is_allowed(receiver):
    assert (
        await deliver(receiver, request_for(event_payload(), method="PUT"))
    ).status == 405


async def test_receiver_registered_before_subscription_and_unregisters_on_unload(
    hass, entry
):
    entry.data = {
        "product": "weather_station",
        "device_id": "indoor-1",
        CONF_WEBHOOK_ID: "stable-hook",
    }
    callbacks = []
    entry.async_on_unload.side_effect = callbacks.append
    client = AsyncMock()

    async def subscribe(url):
        assert "stable-hook" in hass.data["webhook"]

    client.async_ensure_webhook.side_effect = subscribe
    with patch(
        "custom_components.netatmo_indoor.webhook.callback_url",
        return_value="https://ha.example/api/webhook/stable-hook",
    ):
        await async_setup_webhook(hass, entry, SECRET, client)
    callbacks[0]()
    assert "stable-hook" not in hass.data["webhook"]
    assert entry.data[CONF_WEBHOOK_ID] == "stable-hook"


async def test_subscription_failure_keeps_receiver_and_logs_warning(receiver, caplog):
    hass, entry, client = receiver
    webhook.async_unregister(hass, entry.data[CONF_WEBHOOK_ID])
    client.async_ensure_webhook.side_effect = NetatmoError("sensitive server response")
    with patch(
        "custom_components.netatmo_indoor.webhook.callback_url",
        return_value="https://ha.example/api/webhook/synthetic-hook-id",
    ):
        await async_setup_webhook(hass, entry, SECRET, client)
    assert entry.data[CONF_WEBHOOK_ID] in hass.data["webhook"]
    assert "Webhook subscription failed" in caplog.text
    assert "sensitive server response" not in caplog.text


async def test_missing_external_https_keeps_receiver_without_remote_request(
    receiver, caplog
):
    hass, entry, client = receiver
    webhook.async_unregister(hass, entry.data[CONF_WEBHOOK_ID])
    client.reset_mock()
    with patch(
        "custom_components.netatmo_indoor.webhook.callback_url", return_value=None
    ):
        await async_setup_webhook(hass, entry, SECRET, client)
    assert entry.data[CONF_WEBHOOK_ID] in hass.data["webhook"]
    client.async_ensure_webhook.assert_not_called()
    assert "subscription requires an external" in caplog.text


@pytest.mark.parametrize(
    ("url", "expected"),
    [("https://ha.example/api/webhook/hook", True), ("http://ha.example/hook", False)],
)
def test_only_https_callback_is_advertised(hass, url, expected):
    with patch(
        "custom_components.netatmo_indoor.webhook.webhook.async_generate_url",
        return_value=url,
    ) as generate:
        assert bool(callback_url(hass, "hook")) is expected
    generate.assert_called_once_with(hass, "hook", allow_internal=False)
