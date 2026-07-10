"""Tests for the capability-probe machinery in the integration setup module.

Covers the defensive paths the integration-level suites don't reach:
address resolution fallbacks, probe coalescing, unload races, and
exception containment.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.wican import (
    _async_probe_capabilities,
    _async_request_capability_probe,
    _device_api_base_url,
)
from custom_components.wican.api import LEGACY_CAPABILITIES, DeviceCapabilities
from custom_components.wican.button import async_setup_entry as button_setup
from custom_components.wican.const import API_LEVEL_V6
from custom_components.wican.models import WiCANRuntimeData


def _entry(**data: object) -> MagicMock:
    """Return a mock config entry with real runtime data."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.title = "WiCAN Device"
    entry.data = data
    entry.runtime_data = WiCANRuntimeData(
        coordinator=MagicMock(),
        github_coordinator=MagicMock(),
        webhook_id="hook",
        post_interval=15,
    )
    return entry


def test_device_api_base_url_prefers_runtime_host() -> None:
    """The live runtime host wins over stored entry data."""
    entry = _entry(host="http://stored.local", mdns="http://mdns.local")
    entry.runtime_data.device_host = "http://runtime.local"
    assert _device_api_base_url(entry) == "http://runtime.local"


def test_device_api_base_url_falls_back_to_entry_data() -> None:
    """Stored host, then mdns, are used when no runtime host is known."""
    entry = _entry(host="http://stored.local", mdns="http://mdns.local")
    assert _device_api_base_url(entry) == "http://stored.local"

    entry = _entry(mdns="http://mdns.local")
    assert _device_api_base_url(entry) == "http://mdns.local"


def test_device_api_base_url_falls_back_to_ip() -> None:
    """A bare runtime IP is turned into an http URL as the last resort."""
    entry = _entry()
    entry.runtime_data.device_ip = "192.168.1.50"
    assert _device_api_base_url(entry) == "http://192.168.1.50"


def test_device_api_base_url_none_when_unknown() -> None:
    """No address information at all yields None (probe is skipped)."""
    assert _device_api_base_url(_entry()) is None


async def test_probe_request_without_runtime_data(hass: HomeAssistant) -> None:
    """A probe requested after unload (no runtime data) is a no-op."""
    entry = MagicMock(spec=["entry_id", "title"])
    # Must not raise.
    await _async_request_capability_probe(hass, entry)


async def test_probe_requests_coalesce(hass: HomeAssistant) -> None:
    """A request while a probe runs folds into the running loop."""
    entry = _entry(host="http://stored.local")
    entry.runtime_data.probe_running = True

    with patch(
        "custom_components.wican._async_probe_capabilities",
    ) as probe:
        await _async_request_capability_probe(hass, entry)

    probe.assert_not_called()
    assert entry.runtime_data.probe_pending is True


async def test_probe_skipped_without_address(hass: HomeAssistant) -> None:
    """No known device address means no probe attempt (and no crash)."""
    entry = _entry()

    with patch("custom_components.wican.MeatPiApiClient") as client_cls:
        await _async_probe_capabilities(hass, entry)

    client_cls.assert_not_called()
    assert entry.runtime_data.capabilities == LEGACY_CAPABILITIES


async def test_probe_unexpected_error_contained(hass: HomeAssistant) -> None:
    """An unexpected probe error is logged and changes nothing."""
    entry = _entry(host="http://stored.local")

    with patch("custom_components.wican.MeatPiApiClient") as client_cls:
        client_cls.return_value = AsyncMock()
        client_cls.return_value.async_probe.side_effect = RuntimeError("boom")
        await _async_probe_capabilities(hass, entry)

    assert entry.runtime_data.capabilities == LEGACY_CAPABILITIES
    assert entry.runtime_data.api is None


async def test_probe_result_dropped_after_unload(hass: HomeAssistant) -> None:
    """A probe finishing after unload does not touch anything."""
    entry = _entry(host="http://stored.local")
    runtime = entry.runtime_data

    async def _probe_and_unload() -> DeviceCapabilities:
        del entry.runtime_data
        return DeviceCapabilities(api_level=API_LEVEL_V6)

    with patch("custom_components.wican.MeatPiApiClient") as client_cls:
        client_cls.return_value = AsyncMock()
        client_cls.return_value.async_probe.side_effect = _probe_and_unload
        await _async_probe_capabilities(hass, entry)

    # The pre-unload runtime object was never updated.
    assert runtime.capabilities == LEGACY_CAPABILITIES


async def test_button_platform_without_runtime_data(
    hass: HomeAssistant,
) -> None:
    """The button platform tolerates a vanished runtime (unload race)."""
    entry = MagicMock(spec=["entry_id", "title", "async_on_unload"])
    entry.entry_id = "test_entry"

    added: list[object] = []
    await button_setup(hass, entry, lambda entities: added.extend(entities))

    assert added == []
