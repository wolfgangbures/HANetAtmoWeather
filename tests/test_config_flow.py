"""Run OAuth selection, persistence, duplicates, and options through HA flows."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import ClientSession
from homeassistant import config_entries, loader
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.netatmo_indoor import async_setup_entry, async_unload_entry
from custom_components.netatmo_indoor.api import (
    IndoorReading,
    NetatmoClient,
    NetatmoRateLimitError,
)
from custom_components.netatmo_indoor.const import CONF_WEBHOOK_ID, DOMAIN

UNIT = IndoorReading(
    "indoor-1", "Office", "NHC", 55, 40, datetime(2026, 1, 1, tzinfo=UTC), True
)


@pytest.fixture
async def flow_hass(hass):
    loader.async_setup(hass)
    folder = Path(__file__).parents[1] / "custom_components" / DOMAIN
    integration = loader.Integration(
        hass,
        f"custom_components.{DOMAIN}",
        folder,
        json.loads((folder / "manifest.json").read_text()),
        {child.name for child in folder.iterdir()},
    )
    hass.data[loader.DATA_CUSTOM_COMPONENTS] = {DOMAIN: integration}
    hass.config_entries = config_entries.ConfigEntries(hass, {})
    impl = AsyncMock(spec=config_entry_oauth2_flow.LocalOAuth2Implementation)
    impl.domain = DOMAIN
    impl.name = "Test application"
    impl.client_secret = "synthetic-app-secret"
    impl.async_generate_authorize_url.return_value = "https://api.netatmo.com/authorize"
    impl.async_resolve_external_data.return_value = {
        "access_token": "synthetic-access",
        "refresh_token": "synthetic-refresh",
        "expires_in": 10800,
    }
    config_entry_oauth2_flow.async_register_implementation(hass, DOMAIN, impl)
    with (
        patch("homeassistant.config_entries.async_process_deps_reqs", AsyncMock()),
        patch.object(hass.config_entries, "async_setup", AsyncMock(return_value=True)),
    ):
        async with ClientSession() as session:
            with patch(
                "custom_components.netatmo_indoor.config_flow.async_get_clientsession",
                return_value=session,
            ):
                yield hass


async def authorize(flow_hass, product="home_coach"):
    manager = flow_hass.config_entries.flow
    result = await manager.async_init(DOMAIN, context={"source": "user"})
    assert result["step_id"] == "user"
    flow_id = result["flow_id"]
    result = await manager.async_configure(flow_id, {"product": product})
    assert result["step_id"] == "pick_implementation"
    result = await manager.async_configure(flow_id, {"implementation": DOMAIN})
    assert result["type"] == FlowResultType.EXTERNAL_STEP
    expected_scope = "read_homecoach" if product == "home_coach" else "read_station"
    assert f"scope={expected_scope}" in result["url"]
    result = await manager.async_configure(flow_id, {"code": "synthetic-code"})
    assert result["type"] == FlowResultType.EXTERNAL_STEP_DONE
    return await manager.async_configure(flow_id)


@pytest.mark.parametrize("product", ["home_coach", "weather_station"])
async def test_oauth_device_selection_and_duplicate_prevention(flow_hass, product):
    with patch(
        "custom_components.netatmo_indoor.config_flow.NetatmoClient.async_get_units",
        AsyncMock(return_value={"indoor-1": UNIT}),
    ):
        result = await authorize(flow_hass, product)
        assert result["step_id"] == "device"
        result = await flow_hass.config_entries.flow.async_configure(
            result["flow_id"], {"device_id": "indoor-1"}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        entry = result["result"]
        assert entry.data["product"] == product
        assert entry.data["device_id"] == "indoor-1"
        assert entry.data["token"]["refresh_token"] == "synthetic-refresh"
        assert entry.unique_id == f"{product}_indoor-1"
        duplicate = await authorize(flow_hass, product)
        duplicate = await flow_hass.config_entries.flow.async_configure(
            duplicate["flow_id"], {"device_id": "indoor-1"}
        )
        assert duplicate["reason"] == "already_configured"


async def test_no_matching_indoor_units_aborts(flow_hass):
    with patch(
        "custom_components.netatmo_indoor.config_flow.NetatmoClient.async_get_units",
        AsyncMock(return_value={}),
    ):
        result = await authorize(flow_hass)
    assert result["reason"] == "no_devices"


async def test_discovery_retry_reuses_rate_limited_client(flow_hass):
    with (
        patch(
            "custom_components.netatmo_indoor.config_flow.NetatmoClient.async_get_units",
            AsyncMock(side_effect=NetatmoRateLimitError(600)),
        ),
        patch.object(
            NetatmoClient, "for_access_token", wraps=NetatmoClient.for_access_token
        ) as factory,
    ):
        result = await authorize(flow_hass)
        assert result["errors"] == {"base": "cannot_connect"}
        result = await flow_hass.config_entries.flow.async_configure(
            result["flow_id"], {}
        )
        assert result["errors"] == {"base": "cannot_connect"}
        factory.assert_called_once()


async def create_entry(flow_hass):
    with patch(
        "custom_components.netatmo_indoor.config_flow.NetatmoClient.async_get_units",
        AsyncMock(return_value={"indoor-1": UNIT}),
    ):
        result = await authorize(flow_hass, "weather_station")
        result = await flow_hass.config_entries.flow.async_configure(
            result["flow_id"], {"device_id": "indoor-1"}
        )
    return result["result"]


async def test_reauth_retains_product_device_and_options(flow_hass):
    entry = await create_entry(flow_hass)
    flow_hass.config_entries.async_update_entry(entry, options={"poll_interval": 30})
    manager = flow_hass.config_entries.flow
    result = await manager.async_init(
        DOMAIN,
        context={"source": "reauth", "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result["step_id"] == "reauth_confirm"
    result = await manager.async_configure(result["flow_id"], {})
    result = await manager.async_configure(
        result["flow_id"], {"implementation": DOMAIN}
    )
    assert "scope=read_station" in result["url"]
    result = await manager.async_configure(
        result["flow_id"], {"code": "synthetic-new-code"}
    )
    with patch.object(flow_hass.config_entries, "async_schedule_reload") as reload:
        result = await manager.async_configure(result["flow_id"])
    assert result["reason"] == "reauth_successful"
    reload.assert_called_once_with(entry.entry_id)
    assert entry.data["device_id"] == "indoor-1"
    assert entry.data["product"] == "weather_station"
    assert entry.options == {"poll_interval": 30}
    assert len(flow_hass.config_entries.async_entries(DOMAIN)) == 1


@pytest.mark.parametrize("interval", [30, 60, 600])
async def test_options_accept_exact_bounds(flow_hass, interval):
    entry = await create_entry(flow_hass)
    result = await flow_hass.config_entries.options.async_init(entry.entry_id)
    result = await flow_hass.config_entries.options.async_configure(
        result["flow_id"], {"poll_interval": interval}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options == {"poll_interval": interval}


@pytest.mark.parametrize("interval", [29, 601, "bad"])
async def test_options_reject_out_of_range_values(flow_hass, interval):
    entry = await create_entry(flow_hass)
    result = await flow_hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await flow_hass.config_entries.options.async_configure(
            result["flow_id"], {"poll_interval": interval}
        )
    assert entry.options == {}


async def test_namain_options_show_callback_url_without_changing_polling(flow_hass):
    entry = await create_entry(flow_hass)
    flow_hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_WEBHOOK_ID: "persisted-hook"}
    )
    url = "https://ha.example/api/webhook/persisted-hook"
    with patch(
        "custom_components.netatmo_indoor.config_flow.callback_url",
        return_value=url,
    ):
        result = await flow_hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "indoor_webhook"
    assert result["description_placeholders"]["webhook_url"] == url
    assert entry.options == {}


async def test_setup_registers_only_three_sensors_and_unload_stops_polling(flow_hass):
    entry = await create_entry(flow_hass)
    await er.async_get(flow_hass).async_load()
    await dr.async_load(flow_hass)
    flow_hass.config.components.add("application_credentials")
    flow_hass.config.components.add(DOMAIN)
    fresh = IndoorReading(
        "indoor-1",
        "Office",
        "NAMain",
        55,
        40,
        dt_util.utcnow().replace(microsecond=0),
        True,
    )
    assert await async_setup_component(flow_hass, "sensor", {})
    entry._async_set_state(
        flow_hass, config_entries.ConfigEntryState.SETUP_IN_PROGRESS, None
    )
    with (
        patch("homeassistant.helpers.config_entry_oauth2_flow.async_get_clientsession"),
        patch(
            "custom_components.netatmo_indoor.api.NetatmoClient.async_get_units",
            AsyncMock(return_value={"indoor-1": fresh}),
        ) as fetch,
    ):
        async with entry.setup_lock:
            assert await async_setup_entry(flow_hass, entry)
        await flow_hass.async_block_till_done()
    registry = er.async_get(flow_hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert {entity.unique_id for entity in entities} == {
        "indoor-1_humidity",
        "indoor-1_noise",
        "indoor-1_measured_at",
    }
    values = {
        entity.unique_id: flow_hass.states.get(entity.entity_id).state
        for entity in entities
    }
    assert values["indoor-1_humidity"] == "55"
    assert values["indoor-1_noise"] == "40"
    assert values["indoor-1_measured_at"] == fresh.measured_at.isoformat()
    fetch.assert_awaited_once_with("indoor-1")
    coordinator = entry.runtime_data
    webhook_id = entry.data[CONF_WEBHOOK_ID]
    assert webhook_id in flow_hass.data["webhook"]
    assert flow_hass.data["webhook"][webhook_id]["allowed_methods"] == {"POST"}
    assert coordinator._unsub_refresh is not None
    assert await async_unload_entry(flow_hass, entry)
    await entry._async_process_on_unload(flow_hass)
    await flow_hass.async_block_till_done()
    assert not coordinator._listeners
    assert coordinator._unsub_refresh is None
    assert webhook_id not in flow_hass.data["webhook"]
    assert entry.data[CONF_WEBHOOK_ID] == webhook_id
