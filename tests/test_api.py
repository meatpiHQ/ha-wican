"""Tests for the MeatPi device HTTP API client (firmware V6+)."""

from __future__ import annotations

import asyncio

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.wican.api import (
    LEGACY_CAPABILITIES,
    DeviceCapabilities,
    MeatPiApiClient,
)
from custom_components.wican.const import (
    API_LEVEL_LEGACY,
    API_LEVEL_V6,
    MAX_API_COMPONENTS,
)
from custom_components.wican.exceptions import MeatPiApiError

BASE_URL = "http://wican_test.local:80"

V6_STATUS = {
    "bits": {"sta_connected": True, "ap_enabled": False},
    "version": "6.0.1",
    "uptime": "0d 01:02:03",
}

V6_SETTINGS = {
    "components": [
        {"name": "wifi_manager", "version": 4},
        {"name": "rtc_manager", "version": 1},
        {"name": "battery_monitor", "version": 2},
    ],
}


def _client(hass: HomeAssistant) -> MeatPiApiClient:
    return MeatPiApiClient(async_get_clientsession(hass), BASE_URL)


def test_capabilities_model() -> None:
    """Capability accessors behave as documented."""
    legacy = LEGACY_CAPABILITIES
    assert legacy.api_level == API_LEVEL_LEGACY
    assert not legacy.has_http_api
    assert not legacy.has_component("rtc_manager")

    v6 = DeviceCapabilities(
        api_level=API_LEVEL_V6, components=frozenset({"rtc_manager"}),
    )
    assert v6.has_http_api
    assert v6.has_component("rtc_manager")
    assert not v6.has_component("vpn_manager")


async def test_base_url_property(hass: HomeAssistant) -> None:
    """The client reports the base URL it talks to (default port dropped)."""
    assert _client(hass).base_url == "http://wican_test.local"


async def test_probe_v6_device(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    """A V6 device is recognized and its component set collected."""
    aioclient_mock.get(f"{BASE_URL}/api/status", json=V6_STATUS)
    aioclient_mock.get(f"{BASE_URL}/api/settings", json=V6_SETTINGS)

    capabilities = await _client(hass).async_probe()

    assert capabilities.has_http_api
    assert capabilities.components == {
        "wifi_manager",
        "rtc_manager",
        "battery_monitor",
    }


async def test_probe_v6_device_settings_unavailable(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    """A failed component listing still yields a V6 device, no components."""
    aioclient_mock.get(f"{BASE_URL}/api/status", json=V6_STATUS)
    aioclient_mock.get(f"{BASE_URL}/api/settings", status=500, text="boom")

    capabilities = await _client(hass).async_probe()

    assert capabilities.has_http_api
    assert capabilities.components == frozenset()


async def test_probe_v6_device_malformed_components(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    """Garbage component entries are skipped, valid ones kept."""
    aioclient_mock.get(f"{BASE_URL}/api/status", json=V6_STATUS)
    aioclient_mock.get(
        f"{BASE_URL}/api/settings",
        json={
            "components": [
                {"name": "rtc_manager"},
                {"name": 42},
                "not-a-dict",
                {"version": 1},
            ],
        },
    )

    capabilities = await _client(hass).async_probe()

    assert capabilities.components == frozenset({"rtc_manager"})


async def test_probe_legacy_device_no_bits(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    """A legacy /api/status (no bits object) yields legacy capabilities."""
    aioclient_mock.get(
        f"{BASE_URL}/api/status",
        json={"batt_voltage": "12.5V", "ecu_status": "Online"},
    )

    assert await _client(hass).async_probe() == LEGACY_CAPABILITIES


async def test_probe_legacy_device_non_dict_body(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    """A non-object JSON body yields legacy capabilities."""
    aioclient_mock.get(f"{BASE_URL}/api/status", json=["nope"])

    assert await _client(hass).async_probe() == LEGACY_CAPABILITIES


async def test_probe_unreachable_device_returns_none(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    """An unreachable device yields None so callers keep last-known state."""
    aioclient_mock.get(f"{BASE_URL}/api/status", exc=asyncio.TimeoutError)

    assert await _client(hass).async_probe() is None


async def test_probe_lost_mid_probe_returns_none(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    """Losing the device between the two probe requests reads as unreachable.

    Downgrading to "V6 with no components" would silently disable
    component-gated entities on a mere flake.
    """
    aioclient_mock.get(f"{BASE_URL}/api/status", json=V6_STATUS)
    aioclient_mock.get(
        f"{BASE_URL}/api/settings", exc=aiohttp.ClientConnectionError("gone"),
    )

    assert await _client(hass).async_probe() is None


async def test_probe_component_flood_is_capped(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    """The component set is bounded against a hostile device."""
    aioclient_mock.get(f"{BASE_URL}/api/status", json=V6_STATUS)
    aioclient_mock.get(
        f"{BASE_URL}/api/settings",
        json={
            "components": [{"name": f"c{i}"} for i in range(MAX_API_COMPONENTS * 3)],
        },
    )

    capabilities = await _client(hass).async_probe()

    assert capabilities.has_http_api
    assert len(capabilities.components) == MAX_API_COMPONENTS


async def test_probe_oversized_component_names_skipped(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    """Absurdly long or empty component names never enter the set."""
    aioclient_mock.get(f"{BASE_URL}/api/status", json=V6_STATUS)
    aioclient_mock.get(
        f"{BASE_URL}/api/settings",
        json={"components": [{"name": "x" * 1000}, {"name": ""}, {"name": "rtc_manager"}]},
    )

    capabilities = await _client(hass).async_probe()

    assert capabilities.components == frozenset({"rtc_manager"})


async def test_oversized_response_rejected(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    """A giant response body is refused before it is read."""
    aioclient_mock.post(
        f"{BASE_URL}/api/restart",
        json={"ok": True},
        headers={"Content-Length": str(50 * 1024 * 1024)},
    )

    with pytest.raises(MeatPiApiError, match="too large"):
        await _client(hass).async_restart()


async def test_probe_http_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    """HTTP errors during the probe yield legacy capabilities."""
    aioclient_mock.get(f"{BASE_URL}/api/status", status=404, text="not found")

    assert await _client(hass).async_probe() == LEGACY_CAPABILITIES


async def test_probe_non_json_body(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    """A non-JSON body (legacy web UI answering) yields legacy capabilities."""
    aioclient_mock.get(
        f"{BASE_URL}/api/status", text="<html>hi</html>", headers={"Content-Type": "text/html"},
    )

    assert await _client(hass).async_probe() == LEGACY_CAPABILITIES


async def test_restart(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    """Restart posts to /api/restart."""
    aioclient_mock.post(f"{BASE_URL}/api/restart", json={"ok": True})

    await _client(hass).async_restart()

    assert aioclient_mock.call_count == 1
    method, url, _, _ = aioclient_mock.mock_calls[0]
    assert method == "POST"
    assert url.path == "/api/restart"


async def test_sync_time(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    """Time sync posts to /api/rtc/sync."""
    aioclient_mock.post(f"{BASE_URL}/api/rtc/sync", json={"time": "2026-07-10T00:00:00Z"})

    await _client(hass).async_sync_time()

    method, url, _, _ = aioclient_mock.mock_calls[0]
    assert method == "POST"
    assert url.path == "/api/rtc/sync"


async def test_request_http_error_raises(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    """4xx/5xx responses raise MeatPiApiError with the body in the message."""
    aioclient_mock.post(
        f"{BASE_URL}/api/restart", status=503, text='{"error":"busy"}',
    )

    with pytest.raises(MeatPiApiError, match="HTTP 503"):
        await _client(hass).async_restart()


async def test_request_connection_error_raises(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    """Transport errors raise MeatPiApiError."""
    aioclient_mock.post(
        f"{BASE_URL}/api/restart", exc=aiohttp.ClientError("connection reset"),
    )

    with pytest.raises(MeatPiApiError, match="failed"):
        await _client(hass).async_restart()


async def test_request_timeout_raises(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    """Timeouts raise MeatPiApiError mentioning the timeout."""
    aioclient_mock.post(f"{BASE_URL}/api/restart", exc=asyncio.TimeoutError)

    with pytest.raises(MeatPiApiError, match="timed out"):
        await _client(hass).async_restart()


async def test_base_url_with_path_is_normalized(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    """A base URL carrying a path still targets the /api routes."""
    aioclient_mock.get(f"{BASE_URL}/api/status", json=V6_STATUS)
    aioclient_mock.get(f"{BASE_URL}/api/settings", json=V6_SETTINGS)

    client = MeatPiApiClient(
        async_get_clientsession(hass), f"{BASE_URL}/some/page",
    )
    capabilities = await client.async_probe()

    assert capabilities.has_http_api
