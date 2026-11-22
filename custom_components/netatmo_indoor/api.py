"""Small read-only client for Netatmo's indoor dashboard endpoints."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from functools import partial
from json import JSONDecodeError
from time import monotonic
from typing import Any

from aiohttp import ClientResponse, ClientResponseError, ClientSession

from .const import API_URL, PRODUCTS, WEBHOOK_API_URL

Request = Callable[..., Awaitable[ClientResponse]]


class NetatmoError(Exception):
    """An invalid or failed Netatmo response."""


class NetatmoAuthError(NetatmoError):
    """Authorization must be renewed."""


class NetatmoRateLimitError(NetatmoError):
    """No requests may be made until the cooldown expires."""

    def __init__(self, retry_after: float) -> None:
        super().__init__("Netatmo rate limit reached")
        self.retry_after = retry_after


@dataclass
class RateLimit:
    """Keep a cooldown across config-entry retries and reloads."""

    retry_at: float = 0.0


@dataclass(frozen=True)
class IndoorReading:
    """One main indoor device, never an attached outdoor module."""

    device_id: str
    name: str
    model: str
    humidity: float | None
    noise: float | None
    measured_at: datetime | None
    reachable: bool


def _measurement(value: Any, key: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NetatmoError(f"Invalid {key} measurement")
    if not math.isfinite(value) or (key == "humidity" and not 0 <= value <= 100):
        raise NetatmoError(f"Invalid {key} measurement")
    return float(value)


def _timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NetatmoError("Invalid measurement timestamp")
    try:
        return datetime.fromtimestamp(value, UTC)
    except (ValueError, OverflowError, OSError) as err:
        raise NetatmoError("Invalid measurement timestamp") from err


def _retry_delay(header: str | None) -> float:
    """Use a conservative cooldown if the server omits Retry-After."""
    if header is None:
        return 600
    try:
        seconds = float(header)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(header)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            seconds = (retry_at - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 600
    return max(60, seconds) if math.isfinite(seconds) else 600


class NetatmoClient:
    """Share a single request across all sensors of the selected unit."""

    def __init__(
        self, request: Request, product: str, cooldown: RateLimit | None = None
    ) -> None:
        self._request = request
        self._endpoint, _, self._model = PRODUCTS[product]
        self._cooldown = cooldown if cooldown is not None else RateLimit()

    @classmethod
    def for_access_token(
        cls, session: ClientSession, product: str, token: str
    ) -> NetatmoClient:
        """Use a newly issued token during device selection."""
        return cls(
            partial(session.request, headers={"Authorization": f"Bearer {token}"}),
            product,
        )

    def _rate_limit(self, delay: float) -> NetatmoRateLimitError:
        self._cooldown.retry_at = monotonic() + delay
        return NetatmoRateLimitError(delay)

    async def _async_request_json(
        self, method: str, url: str, **kwargs: Any
    ) -> dict[str, Any]:
        if (remaining := self._cooldown.retry_at - monotonic()) > 0:
            raise NetatmoRateLimitError(remaining)
        try:
            async with asyncio.timeout(20):
                response = await self._request(method, url, **kwargs)
                async with response:
                    if response.status in (401, 403):
                        raise NetatmoAuthError("Netatmo authorization failed")
                    if response.status == 429:
                        raise self._rate_limit(
                            _retry_delay(response.headers.get("Retry-After"))
                        )
                    response.raise_for_status()
                    try:
                        payload = await response.json()
                    except (JSONDecodeError, UnicodeDecodeError) as err:
                        raise NetatmoError("Invalid Netatmo JSON response") from err
        except ClientResponseError as err:
            # OAuth2Session may fail while refreshing the token, before the GET.
            if err.status == 429:
                raise self._rate_limit(
                    _retry_delay(
                        err.headers.get("Retry-After") if err.headers else None
                    )
                ) from err
            if err.status in (401, 403) or (
                err.status == 400
                and err.request_info.real_url.path.endswith("/oauth2/token")
            ):
                raise NetatmoAuthError("Netatmo authorization failed") from err
            raise
        if not isinstance(payload, dict):
            raise NetatmoError("Invalid Netatmo response")
        if error := payload.get("error"):
            if not isinstance(error, dict):
                raise NetatmoError("Invalid Netatmo error response")
            if error.get("code") in (1, 2, 3, 13):
                raise NetatmoAuthError("Netatmo authorization failed")
            if error.get("code") == 26:
                raise self._rate_limit(600)
            raise NetatmoError("Netatmo returned an API error")
        return payload

    async def async_ensure_webhook(self, url: str) -> None:
        """Append our subscription only if missing; never replace other hooks."""
        payload = await self._async_request_json("GET", WEBHOOK_API_URL)
        hooks = payload.get("body")
        if not isinstance(hooks, list) or any(
            not isinstance(hook, dict) or not isinstance(hook.get("url"), str)
            for hook in hooks
        ):
            raise NetatmoError("Invalid Netatmo webhook list")
        if any(hook["url"] == url for hook in hooks):
            return
        payload = await self._async_request_json(
            "POST", WEBHOOK_API_URL, json={"url": url}
        )
        hooks = payload.get("body")
        if (
            payload.get("status") != "ok"
            or not isinstance(hooks, list)
            or not any(
                isinstance(hook, dict) and hook.get("url") == url for hook in hooks
            )
        ):
            raise NetatmoError("Netatmo did not confirm webhook registration")

    async def async_get_units(
        self, device_id: str | None = None
    ) -> dict[str, IndoorReading]:
        """Fetch main devices, optionally restricted to one selected unit."""
        params = {"device_id": device_id} if device_id else {}
        payload = await self._async_request_json(
            "GET", f"{API_URL}/{self._endpoint}", params=params
        )
        body = payload.get("body")
        if not isinstance(body, dict) or not isinstance(body.get("devices"), list):
            raise NetatmoError("Missing Netatmo device list")
        units: dict[str, IndoorReading] = {}
        for device in body["devices"]:
            if not isinstance(device, dict):
                raise NetatmoError("Invalid Netatmo device")
            if device.get("type") != self._model:
                continue
            unit_id = device.get("_id")
            if not isinstance(unit_id, str) or not unit_id:
                raise NetatmoError("Missing Netatmo device ID")
            if device_id is not None and unit_id != device_id:
                continue
            dashboard = device.get("dashboard_data", {})
            if not isinstance(dashboard, dict):
                raise NetatmoError("Invalid indoor dashboard")
            reachable = device.get("reachable", True)
            if not isinstance(reachable, bool):
                raise NetatmoError("Invalid device reachability")
            name = device.get("module_name") or device.get("station_name") or unit_id
            if not isinstance(name, str):
                raise NetatmoError("Invalid Netatmo device name")
            units[unit_id] = IndoorReading(
                device_id=unit_id,
                name=name,
                model=self._model,
                humidity=_measurement(dashboard.get("Humidity"), "humidity"),
                noise=_measurement(dashboard.get("Noise"), "noise"),
                measured_at=_timestamp(dashboard.get("time_utc")),
                reachable=reachable,
            )
        return units
