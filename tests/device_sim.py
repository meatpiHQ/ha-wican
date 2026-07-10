"""A simulated MeatPi device for end-to-end integration tests.

:class:`WiCANDeviceSimulator` drives the *real* code paths: it pushes data to
Home Assistant's actual webhook endpoint via ``hass_client`` (so the real
``handle_webhook`` handler, coordinator, and entities run), and Home Assistant
registers / OTA-updates the device through a configurable mock aiohttp session.

:class:`SimulatedDeviceApi` is the device-side HTTP surface (firmware V6+):
wired into ``aioclient_mock``, it answers the integration's *real* capability
probe and control commands, with programmable failure modes (offline, timeout,
HTTP 403/500, garbage bodies, legacy shapes). Together with the device-type
presets it can impersonate every MeatPi product — legacy WiCAN, V6 WiCAN
Pro/USB, ESPNetlink, or a generic future device.

Used by ``test_device_simulation.py`` (baseline behaviour),
``test_device_chaos.py`` (failure injection / hostile scenarios) and
``test_device_type_matrix.py`` (per-device-type end-to-end + control API).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

import aiohttp
from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.wican.const import CONF_POST_INTERVAL, DOMAIN
from tests.conftest import MockConfigEntry

# Marker for "no payload override" (None must stay usable as a payload).
_UNSET = object()

# Firmware components a fully loaded V6 build reports.
V6_COMPONENTS_FULL: tuple[str, ...] = (
    "wifi_manager",
    "rtc_manager",
    "battery_monitor",
    "imu_manager",
    "autopid",
    "data_logger",
    "event_manager",
)

# Device-type presets: constructor overrides for WiCANDeviceSimulator plus
# the device-side API shape for SimulatedDeviceApi. Each preset gets its own
# identity so several presets can coexist in one test.
DEVICE_PRESETS: dict[str, dict[str, Any]] = {
    "wican_legacy": {
        "title": "WiCAN OBD Sim",
        "fw_version": "v3.30",
        "hw_version": "WiCAN-OBD",
        "device_id": "wican_legacy_001",
        "mac": "AA:BB:CC:00:00:01",
        "hostname": "wican_legacy.local",
        "ip": "192.168.1.101",
        "webhook_id": "sim_hook_legacy",
        "api": {"api_level": 0},
    },
    "wican_pro_v6": {
        "title": "WiCAN Pro Sim",
        "fw_version": "6.0.1",
        "hw_version": "WiCAN-PRO v1.4",
        "device_id": "wican_pro_001",
        "mac": "AA:BB:CC:00:00:02",
        "hostname": "wican_pro.local",
        "ip": "192.168.1.102",
        "webhook_id": "sim_hook_pro",
        "api": {"api_level": 6, "components": V6_COMPONENTS_FULL},
    },
    "wican_usb_v6": {
        "title": "WiCAN USB Sim",
        "fw_version": "6.0.1",
        "hw_version": "WiCAN-USB",
        "device_id": "wican_usb_001",
        "mac": "AA:BB:CC:00:00:03",
        "hostname": "wican_usb.local",
        "ip": "192.168.1.103",
        "webhook_id": "sim_hook_usb",
        # No RTC on this build: the sync-time button must not appear.
        "api": {
            "api_level": 6,
            "components": ("wifi_manager", "battery_monitor", "autopid"),
        },
    },
    "espnetlink": {
        "title": "ESPNetlink Sim",
        "fw_version": "1.0.0",
        "hw_version": "ESPNetlink R1",
        "device_id": "espnetlink_001",
        "mac": "AA:BB:CC:00:00:04",
        "hostname": "espnetlink_sim.local",
        "ip": "192.168.1.104",
        "webhook_id": "sim_hook_netlink",
        # LTE/GPS product: no OBD fields, extra LTE status keys.
        "omit_obd_fields": True,
        "status_fields": {
            "lte_rssi": -71,
            "lte_operator": "TestNet",
            "lte_connected": "online",
        },
        "api": {
            "api_level": 6,
            "components": (
                "wifi_manager",
                "rtc_manager",
                "battery_monitor",
                "imu_manager",
            ),
        },
    },
    "meatpi_generic": {
        "title": "MeatPi Proto Sim",
        "fw_version": "0.9.0",
        "hw_version": "MeatPi ProtoBoard X",
        "device_id": "meatpi_proto_001",
        "mac": "AA:BB:CC:00:00:05",
        "hostname": "meatpi_proto.local",
        "ip": "192.168.1.105",
        "webhook_id": "sim_hook_proto",
        "api": {"api_level": 6, "components": ("wifi_manager", "rtc_manager")},
    },
}


class SimulatedDeviceApi:
    """The device-side HTTP API, wired into the aiohttp mocker.

    Answers the integration's real ``MeatPiApiClient`` requests. All state is
    live: change :attr:`mode`, :attr:`api_level`, or the payload overrides at
    any point in a test and the next request sees the new behavior.

    Modes:
      ok         normal behavior for the configured api_level
      offline    every request raises a connection error (device asleep/away)
      timeout    every request times out
      forbidden  HTTP 403 (the V6 network-trust lockdown)
      error      HTTP 500 on every request
      garbage    HTTP 200 with a non-JSON body
      html       HTTP 200 with an HTML page (very old firmware's catch-all UI)
    """

    def __init__(
        self,
        aioclient_mock: AiohttpClientMocker,
        base_urls: list[str],
        *,
        api_level: int = 6,
        components: tuple[str, ...] = V6_COMPONENTS_FULL,
        fw_version: str = "6.0.1",
        mode: str = "ok",
    ) -> None:
        self.api_level = api_level
        self.components = list(components)
        self.fw_version = fw_version
        self.mode = mode
        # Optional raw payload overrides (hostile-shape injection). _UNSET
        # (not None) is the "no override" marker so tests can inject a
        # literal JSON null.
        self.status_payload: Any = _UNSET
        self.settings_payload: Any = _UNSET
        self.calls: list[tuple[str, str]] = []

        for base in base_urls:
            base_url = URL(base)
            for method, path in (
                ("get", "/api/status"),
                ("get", "/api/settings"),
                ("post", "/api/restart"),
                ("post", "/api/rtc/sync"),
                ("get", "/api/webhook"),
                ("post", "/api/webhook"),
                ("delete", "/api/webhook"),
            ):
                getattr(aioclient_mock, method)(
                    str(base_url.with_path(path)), side_effect=self._respond,
                )

    # -- request counters --------------------------------------------------

    def count(self, method: str, path: str) -> int:
        return self.calls.count((method.upper(), path))

    @property
    def restart_calls(self) -> int:
        return self.count("POST", "/api/restart")

    @property
    def sync_time_calls(self) -> int:
        return self.count("POST", "/api/rtc/sync")

    @property
    def probe_calls(self) -> int:
        return self.count("GET", "/api/status")

    # -- behavior ------------------------------------------------------------

    async def _respond(
        self, method: str, url: URL, data: Any,
    ) -> AiohttpClientMockResponse:
        self.calls.append((method.upper(), url.path))

        if self.mode == "offline":
            raise aiohttp.ClientConnectionError("simulated: device offline")
        if self.mode == "timeout":
            raise TimeoutError("simulated: request timed out")
        if self.mode == "forbidden":
            return self._json(
                method, url, {"error": "configuration disabled on this network"},
                status=403,
            )
        if self.mode == "error":
            return self._json(
                method, url, {"error": "simulated internal error"}, status=500,
            )
        if self.mode == "garbage":
            return AiohttpClientMockResponse(method, url, text="<<<not json>>>")
        if self.mode == "html":
            return AiohttpClientMockResponse(
                method, url, text="<html>device ui</html>",
                headers={"Content-Type": "text/html"},
            )

        return self._route(method, url)

    def _route(self, method: str, url: URL) -> AiohttpClientMockResponse:
        path = url.path
        if path == "/api/status":
            if self.status_payload is not _UNSET:
                return self._json(method, url, self.status_payload)
            if self.api_level >= 6:
                return self._json(
                    method,
                    url,
                    {
                        "bits": {"sta_connected": True, "ap_enabled": False},
                        "version": self.fw_version,
                        "uptime": "0d 01:02:03",
                        "boot_count": 4,
                    },
                )
            # Legacy firmware answers /api/status with the flat status object
            # (no "bits") — a positive "not V6" signal.
            return self._json(
                method, url, {"batt_voltage": "12.6V", "ecu_status": "online"},
            )
        if path == "/api/settings":
            if self.settings_payload is not _UNSET:
                return self._json(method, url, self.settings_payload)
            if self.api_level >= 6:
                return self._json(
                    method,
                    url,
                    {
                        "components": [
                            {"name": name, "version": 1}
                            for name in self.components
                        ],
                    },
                )
            return self._json(method, url, {"error": "not found"}, status=404)
        if path in ("/api/restart", "/api/rtc/sync") and self.api_level < 6:
            # Legacy firmware has no control routes.
            return self._json(method, url, {"error": "not found"}, status=404)
        if path == "/api/restart":
            return self._json(method, url, {"ok": True})
        if path == "/api/rtc/sync":
            return self._json(method, url, {"time": "2026-07-10T00:00:00Z"})
        if path == "/api/webhook":
            if method.upper() == "DELETE":
                return AiohttpClientMockResponse(method, url, status=204)
            return self._json(method, url, {"url": "", "enabled": True})
        return self._json(method, url, {"error": "unknown route"}, status=404)

    @staticmethod
    def _json(
        method: str, url: URL, payload: Any, *, status: int = 200,
    ) -> AiohttpClientMockResponse:
        return AiohttpClientMockResponse(method, url, json=payload, status=status)


class WiCANDeviceSimulator:
    """Simulate a WiCAN device talking to the integration."""

    def __init__(
        self,
        hass: HomeAssistant,
        hass_client_factory: Any,
        *,
        title: str = "WiCAN Sim",
        webhook_id: str = "sim_webhook_id",
        device_id: str | None = "wican_sim_001",
        mac: str = "AA:BB:CC:00:11:22",
        fw_version: str = "v4.49",
        hw_version: str = "WiCAN-Pro",
        hostname: str = "wican_sim.local",
        ip: str = "192.168.1.123",
        status_fields: dict[str, Any] | None = None,
        omit_obd_fields: bool = False,
    ) -> None:
        self.hass = hass
        self._client_factory = hass_client_factory
        self._client: Any = None
        self.title = title
        self.webhook_id = webhook_id
        self.device_id = device_id
        self.mac = mac
        self.fw_version = fw_version
        self.hw_version = hw_version
        self.hostname = hostname
        self.ip = ip
        self.status_fields = dict(status_fields or {})
        self.omit_obd_fields = omit_obd_fields
        self.entry: MockConfigEntry | None = None
        self.api: SimulatedDeviceApi | None = None
        self._preset_api_kwargs: dict[str, Any] = {}

    @classmethod
    def from_preset(
        cls,
        hass: HomeAssistant,
        hass_client_factory: Any,
        preset: str,
        **overrides: Any,
    ) -> WiCANDeviceSimulator:
        """Create a simulator for one of the DEVICE_PRESETS product types."""
        config = dict(DEVICE_PRESETS[preset])
        api_kwargs = dict(config.pop("api"))
        config.update(overrides)
        sim = cls(hass, hass_client_factory, **config)
        sim._preset_api_kwargs = api_kwargs
        return sim

    def attach_api(
        self,
        aioclient_mock: AiohttpClientMocker,
        **overrides: Any,
    ) -> SimulatedDeviceApi:
        """Wire up the device-side HTTP API on this device's addresses.

        Must be called *before* :meth:`async_setup` so the integration's real
        setup-time capability probe finds the routes registered.
        """
        kwargs: dict[str, Any] = {"fw_version": self.fw_version}
        kwargs.update(self._preset_api_kwargs)
        kwargs.update(overrides)
        self.api = SimulatedDeviceApi(
            aioclient_mock,
            [f"http://{self.ip}", f"http://{self.hostname}"],
            **kwargs,
        )
        return self.api

    # -- lifecycle -------------------------------------------------------

    async def async_setup(
        self,
        *,
        store_device_id: bool = True,
        register_result: bool = True,
        options: dict[str, Any] | None = None,
        device_type: str | None = None,
    ) -> MockConfigEntry:
        """Create the config entry and set up the integration.

        ``device_type`` simulates an entry created from a discovery that
        carried a device_type TXT record (new firmware); without it the
        integration infers the type from the hardware version.
        """
        data: dict[str, Any] = {
            CONF_WEBHOOK_ID: self.webhook_id,
            "mdns": f"http://{self.hostname}",
            "host": f"http://{self.ip}",
            "mac": self.mac,
            "fw_version": self.fw_version,
            "hw_version": self.hw_version,
            # The test client connects from localhost; pre-seeding the captured
            # request IP keeps ordinary pushes from looking like a connection
            # change (which would fire a real re-registration attempt).
            "ip": "127.0.0.1",
        }
        if device_type is not None:
            data["device_type"] = device_type
        if store_device_id and self.device_id:
            data["device_id"] = self.device_id

        self.entry = MockConfigEntry(
            domain=DOMAIN,
            title=self.title,
            data=data,
            options=options or {CONF_POST_INTERVAL: 15},
            unique_id=self.mac.replace(":", "").lower(),
        )
        self.entry.add_to_hass(self.hass)

        with patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=register_result,
        ):
            assert await self.hass.config_entries.async_setup(self.entry.entry_id)
            await self.hass.async_block_till_done()
        return self.entry

    async def async_reload(self) -> None:
        """Reload the config entry (device keeps pushing afterwards)."""
        assert self.entry is not None
        with patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ):
            assert await self.hass.config_entries.async_reload(self.entry.entry_id)
            await self.hass.async_block_till_done()

    @property
    def coordinator(self) -> Any:
        assert self.entry is not None
        return self.entry.runtime_data.coordinator

    # -- device -> HA push ----------------------------------------------

    async def push(
        self,
        payload: Any = None,
        *,
        raw: str | bytes | None = None,
        content_type: str = "application/json",
        headers: dict[str, str] | None = None,
    ) -> Any:
        """POST a webhook payload to HA as the device would."""
        if self._client is None:
            self._client = await self._client_factory()
        url = f"/api/webhook/{self.webhook_id}"
        if raw is not None:
            all_headers = {"Content-Type": content_type, **(headers or {})}
            return await self._client.post(url, data=raw, headers=all_headers)
        if headers is not None:
            return await self._client.post(url, json=payload, headers=headers)
        return await self._client.post(url, json=payload)

    async def push_and_settle(self, payload: Any) -> Any:
        """Push a payload and let all resulting tasks run."""
        resp = await self.push(payload)
        await self.hass.async_block_till_done()
        await self.coordinator.async_refresh()
        await self.hass.async_block_till_done()
        return resp

    def go_stale(self) -> None:
        """Simulate the device having stopped pushing for a long time."""
        self.coordinator._last_push = dt_util.utcnow() - timedelta(hours=6)

    def reboot(
        self,
        *,
        fw_version: str | None = None,
        hw_version: str | None = None,
    ) -> None:
        """Simulate a device reboot, optionally with new firmware.

        The next :meth:`status` payload reports the new versions, exactly as a
        real device does after an OTA update.
        """
        if fw_version is not None:
            self.fw_version = fw_version
        if hw_version is not None:
            self.hw_version = hw_version

    def ota_to_v6(
        self,
        fw_version: str = "6.0.1",
        components: tuple[str, ...] = V6_COMPONENTS_FULL,
    ) -> None:
        """Simulate an OTA update to V6 firmware (device + its HTTP API).

        The next status push reports the new firmware version, which makes
        the integration re-probe and discover the new control surface.
        """
        self.reboot(fw_version=fw_version)
        if self.api is not None:
            self.api.api_level = 6
            self.api.fw_version = fw_version
            self.api.components = list(components)

    # -- payload builders -----------------------------------------------

    def status(self, **fields: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "fw_version": self.fw_version,
            "hw_version": self.hw_version,
            "batt_voltage": "12.6V",
            "wifi_mode": "Station",
            "vpn_status": "Not Connected",
            "uptime": "01:23:45",
        }
        if not self.omit_obd_fields:
            base.update(
                {
                    "ecu_status": "online",
                    "obd_chip_status": "ok",
                    "ble_status": "Disabled",
                },
            )
        base.update(self.status_fields)
        if self.device_id:
            base["device_id"] = self.device_id
        base.update(fields)
        return {"status": base}

    def pids(
        self,
        values: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {"autopid_data": values, "config": config or {}}

    def gps(self, lat: float, lon: float, **extra: Any) -> dict[str, Any]:
        return {"gps": {"latitude": lat, "longitude": lon, **extra}}
