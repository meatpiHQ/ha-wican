"""Tests for the VPN-tunnel backup control endpoint.

A car on the road via WireGuard/Tailscale can push to HA but its LAN
address is dead. The device reports its tunnel address (``vpn_ip`` +
``vpn_status`` in the pushed status); the integration uses it as the
backup control endpoint: local address first, VPN second.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.wican import _validated_vpn_ip
from tests.device_sim import WiCANDeviceSimulator

VPN_IP = "100.98.7.6"  # Tailscale-style CGNAT address


@pytest.fixture
def mock_capability_probe() -> None:
    """Override conftest's autouse probe stub: run the REAL API client."""
    return


# ---------------------------------------------------------------------------
# Address validation (SSRF guard)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["10.6.0.2", "192.168.4.7", "172.16.0.9", "100.64.0.1", "100.127.255.254", "fd00::1"],
)
def test_validated_vpn_ip_accepts_tunnel_ranges(value: str) -> None:
    """RFC1918, CGNAT (Tailscale), and ULA addresses are accepted."""
    assert _validated_vpn_ip(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "8.8.8.8",  # public: a hostile payload must not steer control traffic
        "1.2.3.4",
        "2001:4860:4860::8888",  # public v6
        "127.0.0.1",
        "169.254.1.1",  # link-local
        "224.0.0.1",  # multicast
        "not-an-ip",
        "wican.local",
        "",
        "   ",
        None,
        42,
        ["10.0.0.1"],
    ],
)
def test_validated_vpn_ip_rejects_unsafe(value: Any) -> None:
    """Public, special, and garbage addresses are rejected."""
    assert _validated_vpn_ip(value) is None


# ---------------------------------------------------------------------------
# Capture / clear from pushes
# ---------------------------------------------------------------------------


async def _pro_sim(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> WiCANDeviceSimulator:
    sim = WiCANDeviceSimulator.from_preset(hass, hass_client, "wican_pro_v6")
    sim.attach_api(aioclient_mock)
    await sim.async_setup()
    return sim


async def test_vpn_ip_captured_and_cleared(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """A connected VPN's address is recorded; disconnecting clears it."""
    sim = await _pro_sim(hass, hass_client, aioclient_mock)
    sim.set_vpn(VPN_IP)

    await sim.push_and_settle(
        sim.status(vpn_status="connected", vpn_ip=VPN_IP),
    )
    assert sim.entry.runtime_data.device_vpn_ip == VPN_IP
    assert sim.entry.data["vpn_ip"] == VPN_IP

    await sim.push_and_settle(
        sim.status(vpn_status="Not Connected", vpn_ip=VPN_IP),
    )
    assert sim.entry.runtime_data.device_vpn_ip is None
    assert "vpn_ip" not in sim.entry.data


async def test_hostile_vpn_ip_ignored(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """A public/garbage vpn_ip can never become a control endpoint."""
    sim = await _pro_sim(hass, hass_client, aioclient_mock)

    await sim.push_and_settle(
        sim.status(vpn_status="connected", vpn_ip="8.8.8.8"),
    )
    assert sim.entry.runtime_data.device_vpn_ip is None

    await sim.push_and_settle(
        sim.status(vpn_status="connected", vpn_ip={"nested": "junk"}),
    )
    assert sim.entry.runtime_data.device_vpn_ip is None


async def test_vpn_ip_survives_reload(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """The persisted vpn_ip is restored into runtime data on reload."""
    sim = await _pro_sim(hass, hass_client, aioclient_mock)
    sim.set_vpn(VPN_IP)
    await sim.push_and_settle(
        sim.status(vpn_status="connected", vpn_ip=VPN_IP),
    )

    await sim.async_reload()

    assert sim.entry.runtime_data.device_vpn_ip == VPN_IP


# ---------------------------------------------------------------------------
# Endpoint fallback: local first, VPN as backup
# ---------------------------------------------------------------------------


async def test_control_falls_back_to_vpn_when_away(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """The car leaves home: control rebinds to the VPN tunnel address."""
    sim = await _pro_sim(hass, hass_client, aioclient_mock)
    sim.set_vpn(VPN_IP)
    await sim.push_and_settle(
        sim.status(vpn_status="connected", vpn_ip=VPN_IP),
    )

    # Drive away: LAN dead, VPN alive. A firmware-version change in the
    # next push triggers a re-probe, which binds the backup endpoint.
    sim.leave_home()
    sim.reboot(fw_version="6.0.2")
    await sim.push_and_settle(
        sim.status(vpn_status="connected", vpn_ip=VPN_IP),
    )

    assert sim.entry.runtime_data.api is not None
    assert VPN_IP in sim.entry.runtime_data.api.base_url

    # Buttons work over the tunnel.
    await hass.services.async_call(
        "button", "press",
        {"entity_id": "button.wican_pro_sim_restart"}, blocking=True,
    )
    assert sim.api.restart_calls == 1


async def test_local_endpoint_preferred_at_home(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """With both addresses alive, the local one wins (no tunnel detour)."""
    sim = await _pro_sim(hass, hass_client, aioclient_mock)
    sim.set_vpn(VPN_IP)

    await sim.push_and_settle(
        sim.status(vpn_status="connected", vpn_ip=VPN_IP),
    )

    api = sim.entry.runtime_data.api
    assert api is not None
    assert VPN_IP not in api.base_url


async def test_press_failure_triggers_rebind(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """A press against a dead LAN endpoint fails once, then rebinds to VPN."""
    sim = await _pro_sim(hass, hass_client, aioclient_mock)
    sim.set_vpn(VPN_IP)
    await sim.push_and_settle(
        sim.status(vpn_status="connected", vpn_ip=VPN_IP),
    )
    assert VPN_IP not in sim.entry.runtime_data.api.base_url  # bound to LAN

    # Car drives off without any push reaching HA yet: the bound endpoint
    # is now dead. The first press fails but schedules a re-probe...
    sim.leave_home()
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "button", "press",
            {"entity_id": "button.wican_pro_sim_restart"}, blocking=True,
        )
    await hass.async_block_till_done()

    # ...which rebinds to the VPN, so the second press succeeds.
    assert VPN_IP in sim.entry.runtime_data.api.base_url
    await hass.services.async_call(
        "button", "press",
        {"entity_id": "button.wican_pro_sim_restart"}, blocking=True,
    )
    assert sim.api.restart_calls == 1


async def test_returning_home_rebinds_local(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """Back on WiFi, a re-probe prefers the local address again."""
    sim = await _pro_sim(hass, hass_client, aioclient_mock)
    sim.set_vpn(VPN_IP)
    await sim.push_and_settle(
        sim.status(vpn_status="connected", vpn_ip=VPN_IP),
    )
    sim.leave_home()
    sim.reboot(fw_version="6.0.2")
    await sim.push_and_settle(
        sim.status(vpn_status="connected", vpn_ip=VPN_IP),
    )
    assert VPN_IP in sim.entry.runtime_data.api.base_url

    sim.return_home()
    sim.reboot(fw_version="6.0.3")
    await sim.push_and_settle(
        sim.status(vpn_status="connected", vpn_ip=VPN_IP),
    )
    assert VPN_IP not in sim.entry.runtime_data.api.base_url


async def test_no_vpn_reporting_leaves_state_untouched(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """Firmware without VPN fields never disturbs the recorded address."""
    sim = await _pro_sim(hass, hass_client, aioclient_mock)
    sim.set_vpn(VPN_IP)
    await sim.push_and_settle(
        sim.status(vpn_status="connected", vpn_ip=VPN_IP),
    )
    assert sim.entry.runtime_data.device_vpn_ip == VPN_IP

    # A push without any VPN keys (e.g. a different status subset).
    payload = sim.status()
    payload["status"].pop("vpn_status", None)
    payload["status"].pop("vpn_ip", None)
    await sim.push_and_settle(payload)

    assert sim.entry.runtime_data.device_vpn_ip == VPN_IP
