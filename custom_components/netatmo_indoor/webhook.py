"""Signed NAMain webhook reception, with privacy-filtered diagnostic logging."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import re
from functools import partial

from aiohttp import ClientError, web
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError

from .api import NetatmoAuthError, NetatmoClient, NetatmoError
from .const import CONF_DEVICE_ID, CONF_WEBHOOK_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)
MAX_BODY_SIZE = 64 * 1024
_EVENT_TYPES = {
    "device_event",
    "topology_changed",
    "home_event",
    "webhook_activation",
    "webhook_deactivation",
}
_MEASUREMENTS = {
    "humidity",
    "noise",
    "temperature",
    "co2",
    "pressure",
    "time_utc",
    "last_seen",
}
_STRUCTURES = {"dashboard_data", "data", "measures", "measurements"}


def callback_url(hass: HomeAssistant, webhook_id: str) -> str | None:
    """Only advertise a configured external HTTPS URL, never a LAN address."""
    try:
        url = webhook.async_generate_url(hass, webhook_id, allow_internal=False)
    except NoURLAvailableError:
        return None
    return url if url.startswith("https://") else None


def _telemetry(module: dict) -> dict:
    """Only log known numeric climate fields; never raw payloads or identities."""
    values: dict = {}
    for key, value in module.items():
        if not isinstance(key, str):
            continue
        if key.lower() in _MEASUREMENTS and (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        ):
            values[key] = value
        elif key in _STRUCTURES and isinstance(value, dict):
            values[key] = {
                name: measurement
                for name, measurement in value.items()
                if isinstance(name, str)
                and name.lower() in _MEASUREMENTS
                and isinstance(measurement, (int, float))
                and not isinstance(measurement, bool)
                and math.isfinite(measurement)
            }
    return values


async def _handle(
    entry: ConfigEntry,
    secret: bytes,
    hass: HomeAssistant,
    webhook_id: str,
    request: web.Request,
) -> web.Response:
    """Authenticate the exact raw bytes before parsing or logging the event."""
    signature = request.headers.get("X-Netatmo-secret", "")
    if re.fullmatch("[0-9a-f]{64}", signature) is None:
        _LOGGER.warning("Rejected indoor webhook: missing or invalid signature")
        return web.Response(status=403)
    body = bytearray()
    stream = request.content
    try:
        async with asyncio.timeout(5):
            while chunk := await stream.read(min(8192, MAX_BODY_SIZE + 1 - len(body))):
                body.extend(chunk)
                if len(body) > MAX_BODY_SIZE:
                    _LOGGER.warning("Rejected indoor webhook: payload too large")
                    return web.Response(status=413)
    except (TimeoutError, ClientError, ConnectionError):
        _LOGGER.warning("Rejected indoor webhook: unable to read request")
        return web.Response(status=400)
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        _LOGGER.warning("Rejected indoor webhook: signature mismatch")
        return web.Response(status=403)
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        _LOGGER.warning("Rejected indoor webhook: invalid JSON")
        return web.Response(status=400)
    if not isinstance(payload, dict) or not isinstance(payload.get("push_type"), str):
        _LOGGER.warning("Rejected indoor webhook: invalid event")
        return web.Response(status=400)
    event_type = payload["push_type"]
    if event_type not in _EVENT_TYPES:
        _LOGGER.debug("Ignored indoor webhook: unsupported event type")
        return web.Response(status=200)
    if event_type in {"webhook_activation", "webhook_deactivation"}:
        _LOGGER.info("Netatmo indoor webhook: %s", event_type)
        return web.Response(status=200)
    extra = payload.get("extra_params", {})
    if not isinstance(extra, dict):
        _LOGGER.warning("Rejected indoor webhook: invalid event parameters")
        return web.Response(status=400)
    modules = extra.get("modules", [])
    if not isinstance(modules, list) or any(
        not isinstance(module, dict) for module in modules
    ):
        _LOGGER.warning("Rejected indoor webhook: invalid module list")
        return web.Response(status=400)
    selected = [
        module for module in modules if module.get("id") == entry.data[CONF_DEVICE_ID]
    ]
    if payload.get("device_id") != entry.data[CONF_DEVICE_ID] and not selected:
        _LOGGER.debug("Ignored indoor webhook for another unit")
        return web.Response(status=200)
    _LOGGER.info("Netatmo indoor webhook: %s", event_type)
    # No webhook-driven refresh yet: a burst must not consume extra API quota.
    _LOGGER.debug(
        "Indoor webhook telemetry (selected unit only): %s",
        json.dumps([_telemetry(module) for module in selected[:8]], sort_keys=True),
    )
    return web.Response(status=200)


async def async_setup_webhook(
    hass: HomeAssistant, entry: ConfigEntry, secret: str, client: NetatmoClient
) -> None:
    """Install the receiver before attempting a non-destructive subscription."""
    webhook_id = entry.data.get(CONF_WEBHOOK_ID)
    if webhook_id is None:
        webhook_id = webhook.async_generate_id()
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_WEBHOOK_ID: webhook_id}
        )
    webhook.async_register(
        hass,
        DOMAIN,
        "Netatmo indoor events",
        webhook_id,
        partial(_handle, entry, secret.encode()),
        local_only=False,
        allowed_methods=["POST"],
    )
    entry.async_on_unload(partial(webhook.async_unregister, hass, webhook_id))
    url = callback_url(hass, webhook_id)
    if url is None:
        _LOGGER.warning(
            "Indoor webhook receiver ready, but subscription requires an external "
            "HTTPS Home Assistant URL. Configure it, then reload the integration. "
            "Polling remains active"
        )
        return
    try:
        await client.async_ensure_webhook(url)
    except NetatmoAuthError:
        # The coordinator handles reauth for the same OAuth session on its next poll.
        _LOGGER.warning(
            "Webhook subscription authorization failed. Check the app's Webhooks "
            "API permission and OAuth authorization; reload to retry. Polling "
            "will check sensor authorization again"
        )
    except (NetatmoError, ClientError, TimeoutError):
        _LOGGER.warning(
            "Webhook subscription failed. Check Netatmo Webhooks API permission, "
            "quota, and external HTTPS access; reload to retry. Polling remains active"
        )
    else:
        _LOGGER.info("Netatmo indoor webhook subscription confirmed")
