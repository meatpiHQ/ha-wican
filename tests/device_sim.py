"""A simulated WiCAN device for end-to-end integration tests.

:class:`WiCANDeviceSimulator` drives the *real* code paths: it pushes data to
Home Assistant's actual webhook endpoint via ``hass_client`` (so the real
``handle_webhook`` handler, coordinator, and entities run), and Home Assistant
registers / OTA-updates the device through a configurable mock aiohttp session.

Used by ``test_device_simulation.py`` (baseline behaviour) and
``test_device_chaos.py`` (failure injection / hostile scenarios).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.wican.const import CONF_POST_INTERVAL, DOMAIN
from tests.conftest import MockConfigEntry


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
        self.entry: MockConfigEntry | None = None

    # -- lifecycle -------------------------------------------------------

    async def async_setup(
        self,
        *,
        store_device_id: bool = True,
        register_result: bool = True,
        options: dict[str, Any] | None = None,
    ) -> MockConfigEntry:
        """Create the config entry and set up the integration."""
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

    # -- payload builders -----------------------------------------------

    def status(self, **fields: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "fw_version": self.fw_version,
            "hw_version": self.hw_version,
            "batt_voltage": "12.6V",
            "wifi_mode": "Station",
            "vpn_status": "Not Connected",
            "uptime": "01:23:45",
            "ecu_status": "online",
            "obd_chip_status": "ok",
            "ble_status": "Disabled",
        }
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
