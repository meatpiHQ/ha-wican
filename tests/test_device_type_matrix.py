"""End-to-end tests driving every MeatPi device type through real code paths.

Each test runs a :class:`MeatPiDeviceSimulator` (real webhook endpoint, real
coordinator, real entities) against a :class:`SimulatedDeviceApi` (the real
``MeatPiApiClient`` probing and commanding over the mocked aiohttp session).
No integration internals are stubbed — the ``mock_capability_probe`` autouse
fixture from conftest is overridden so the *real* capability probe runs.

Covers: per-device-type behavior (legacy WiCAN, V6 WiCAN Pro/USB,
ESPNetlink, generic MeatPi), the OTA-to-V6 lifecycle, control-command
failure injection, hostile API responses, and multi-device isolation.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.wican.const import CONF_DEVICE_TYPE, MAX_API_COMPONENTS
from custom_components.wican.devices import (
    DEVICE_TYPE_ESPNETLINK,
    DEVICE_TYPE_GENERIC,
    DEVICE_TYPE_WICAN,
    DEVICE_TYPE_WICAN_PRO,
)
from tests.device_sim import MeatPiDeviceSimulator


@pytest.fixture
def mock_capability_probe() -> None:
    """Override conftest's autouse probe stub: run the REAL API client.

    HTTP is intercepted by ``aioclient_mock`` (the SimulatedDeviceApi), so
    the genuine probe/control paths execute end-to-end.
    """
    return


async def _sim(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    preset: str,
    *,
    mode: str = "ok",
    device_type: str | None = None,
) -> MeatPiDeviceSimulator:
    """Create a preset simulator with its device API attached, and set up."""
    sim = MeatPiDeviceSimulator.from_preset(hass, hass_client, preset)
    sim.attach_api(aioclient_mock, mode=mode)
    await sim.async_setup(device_type=device_type)
    return sim


def _entity_prefix(sim: MeatPiDeviceSimulator) -> str:
    return sim.title.lower().replace(" ", "_")


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id}, blocking=True,
    )


# ---------------------------------------------------------------------------
# Per-device-type happy paths
# ---------------------------------------------------------------------------


async def test_legacy_wican_telemetry_but_no_buttons(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """A pre-V6 WiCAN keeps full telemetry and gets no control entities."""
    sim = await _sim(hass, hass_client, aioclient_mock, "wican_legacy")

    resp = await sim.push_and_settle(
        {**sim.status(), **sim.pids({"RPM": 900}, {"RPM": {"unit": "rpm"}})},
    )
    assert resp.status == 204

    prefix = _entity_prefix(sim)
    battery = hass.states.get(f"sensor.{prefix}_battery_voltage")
    assert battery is not None
    assert float(battery.state) == 12.6
    assert hass.states.get(f"sensor.{prefix}_rpm") is not None

    # Probe reached the device, saw legacy → positively no control surface.
    assert hass.states.get(f"button.{prefix}_restart") is None
    assert sim.entry.runtime_data.probe_successful is True
    assert not sim.entry.runtime_data.capabilities.has_http_api
    assert sim.entry.data[CONF_DEVICE_TYPE] == DEVICE_TYPE_WICAN


async def test_wican_pro_v6_control_buttons_work(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """A V6 WiCAN Pro gets restart + sync-time buttons that command it."""
    sim = await _sim(hass, hass_client, aioclient_mock, "wican_pro_v6")
    prefix = _entity_prefix(sim)

    assert hass.states.get(f"button.{prefix}_restart") is not None
    assert hass.states.get(f"button.{prefix}_sync_time") is not None
    assert sim.entry.data[CONF_DEVICE_TYPE] == DEVICE_TYPE_WICAN_PRO

    await _press(hass, f"button.{prefix}_restart")
    assert sim.api.restart_calls == 1

    await _press(hass, f"button.{prefix}_sync_time")
    assert sim.api.sync_time_calls == 1


async def test_wican_usb_v6_without_rtc_lacks_sync_button(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """Component gating: no rtc_manager → restart only."""
    sim = await _sim(hass, hass_client, aioclient_mock, "wican_usb_v6")
    prefix = _entity_prefix(sim)

    assert hass.states.get(f"button.{prefix}_restart") is not None
    assert hass.states.get(f"button.{prefix}_sync_time") is None


async def test_espnetlink_gps_device(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """An ESPNetlink (GPS/LTE, no OBD) works end to end."""
    sim = await _sim(hass, hass_client, aioclient_mock, "espnetlink")
    prefix = _entity_prefix(sim)

    resp = await sim.push_and_settle(
        {**sim.status(), **sim.gps(48.8584, 2.2945, accuracy=8, speed=13.4)},
    )
    assert resp.status == 204

    tracker = hass.states.get(f"device_tracker.{prefix}_location")
    assert tracker is not None
    assert tracker.attributes["latitude"] == pytest.approx(48.8584)
    assert tracker.attributes["longitude"] == pytest.approx(2.2945)

    # No OBD payload → no PID sensors; unknown LTE keys are ignored safely.
    assert hass.states.get(f"binary_sensor.{prefix}_ecu_online") is None

    # Device type inferred from the hardware string; V6 control available.
    assert sim.entry.data[CONF_DEVICE_TYPE] == DEVICE_TYPE_ESPNETLINK
    assert hass.states.get(f"button.{prefix}_restart") is not None
    assert hass.states.get(f"button.{prefix}_sync_time") is not None


async def test_generic_future_meatpi_device(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """A future product with an unknown-to-us hardware string works.

    The entry carries device_type "meatpi" (as a _meatpi._tcp discovery
    would set); telemetry, control, and identity all work, and the pushed
    hardware version must NOT reclassify it into the WiCAN family.
    """
    sim = await _sim(
        hass, hass_client, aioclient_mock, "meatpi_generic",
        device_type=DEVICE_TYPE_GENERIC,
    )
    prefix = _entity_prefix(sim)

    resp = await sim.push_and_settle(sim.status())
    assert resp.status == 204

    assert hass.states.get(f"sensor.{prefix}_battery_voltage") is not None
    assert hass.states.get(f"button.{prefix}_restart") is not None
    # "MeatPi ProtoBoard X" contains "pro..." — must not become wican_pro.
    assert sim.entry.data[CONF_DEVICE_TYPE] == DEVICE_TYPE_GENERIC


# ---------------------------------------------------------------------------
# Firmware lifecycle
# ---------------------------------------------------------------------------


async def test_ota_upgrade_to_v6_grows_buttons_without_reload(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """A legacy device OTA-updated to V6 gains control entities on its own."""
    sim = await _sim(hass, hass_client, aioclient_mock, "wican_legacy")
    prefix = _entity_prefix(sim)

    await sim.push_and_settle(sim.status())
    assert hass.states.get(f"button.{prefix}_restart") is None

    # OTA: device reboots into V6, then reports the new firmware version.
    sim.ota_to_v6(fw_version="6.0.1")
    await sim.push_and_settle(sim.status())

    assert hass.states.get(f"button.{prefix}_restart") is not None
    assert sim.entry.runtime_data.capabilities.has_http_api

    await _press(hass, f"button.{prefix}_restart")
    assert sim.api.restart_calls == 1


async def test_unreachable_reprobe_keeps_known_capabilities(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """A probe that can't reach the device must not wipe known capabilities."""
    sim = await _sim(hass, hass_client, aioclient_mock, "wican_pro_v6")
    prefix = _entity_prefix(sim)
    assert sim.entry.runtime_data.capabilities.has_http_api

    # Device drops off the network, then reports a new firmware version via
    # a push that arrives through a different path (e.g. an old queued push).
    sim.api.mode = "offline"
    sim.reboot(fw_version="6.0.2")
    await sim.push_and_settle(sim.status())

    assert sim.entry.runtime_data.capabilities.has_http_api  # kept
    assert sim.entry.runtime_data.probe_successful is False
    assert hass.states.get(f"button.{prefix}_restart") is not None

    # Device comes back: the next push retries the probe and recovers.
    sim.api.mode = "ok"
    sim.entry.runtime_data.last_probe_retry = 0.0
    await sim.push_and_settle(sim.status())
    assert sim.entry.runtime_data.probe_successful is True
    assert sim.entry.runtime_data.capabilities.has_http_api


async def test_device_asleep_at_setup_gains_buttons_when_it_wakes(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """A V6 device unreachable at HA start gets its buttons on first push."""
    sim = MeatPiDeviceSimulator.from_preset(hass, hass_client, "wican_pro_v6")
    sim.attach_api(aioclient_mock, mode="offline")
    await sim.async_setup()
    prefix = _entity_prefix(sim)

    assert sim.entry.runtime_data.probe_successful is False
    assert hass.states.get(f"button.{prefix}_restart") is None

    # The device wakes up and starts pushing telemetry.
    sim.api.mode = "ok"
    await sim.push_and_settle(sim.status())

    assert sim.entry.runtime_data.probe_successful is True
    assert hass.states.get(f"button.{prefix}_restart") is not None


async def test_downgrade_to_legacy_disables_control(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """A device downgraded to legacy firmware keeps entities; presses fail."""
    sim = await _sim(hass, hass_client, aioclient_mock, "wican_pro_v6")
    prefix = _entity_prefix(sim)
    assert hass.states.get(f"button.{prefix}_restart") is not None

    sim.api.api_level = 0  # device now answers with the legacy shape
    sim.reboot(fw_version="v4.40")
    await sim.push_and_settle(sim.status())

    assert not sim.entry.runtime_data.capabilities.has_http_api
    # The button entity is not removed, but pressing it fails cleanly...
    with pytest.raises(HomeAssistantError):
        await _press(hass, f"button.{prefix}_restart")
    # ...and telemetry keeps flowing.
    await sim.push_and_settle(sim.status(batt_voltage="11.9V"))
    battery = hass.states.get(f"sensor.{prefix}_battery_voltage")
    assert float(battery.state) == 11.9


# ---------------------------------------------------------------------------
# Control-command failure injection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["offline", "timeout", "forbidden", "error"])
async def test_control_failures_surface_cleanly(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    mode: str,
) -> None:
    """Every control failure mode raises a clean error and harms nothing."""
    sim = await _sim(hass, hass_client, aioclient_mock, "wican_pro_v6")
    prefix = _entity_prefix(sim)

    sim.api.mode = mode
    with pytest.raises(HomeAssistantError):
        await _press(hass, f"button.{prefix}_restart")

    # The integration is fully alive afterwards: telemetry still updates.
    sim.api.mode = "ok"
    await sim.push_and_settle(sim.status(batt_voltage="12.1V"))
    battery = hass.states.get(f"sensor.{prefix}_battery_voltage")
    assert float(battery.state) == 12.1

    await _press(hass, f"button.{prefix}_restart")
    assert sim.api.restart_calls >= 1


# ---------------------------------------------------------------------------
# Hostile / malformed device API responses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["garbage", "html", "error", "forbidden"])
async def test_non_v6_answers_probe_as_legacy(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    mode: str,
) -> None:
    """Reachable-but-not-V6 answers are a positive legacy signal.

    The probe succeeded (no per-push retry hammering) and no control
    entities exist; telemetry is unaffected.
    """
    sim = await _sim(hass, hass_client, aioclient_mock, "wican_pro_v6", mode=mode)
    prefix = _entity_prefix(sim)

    assert sim.entry.runtime_data.probe_successful is True
    assert not sim.entry.runtime_data.capabilities.has_http_api
    assert hass.states.get(f"button.{prefix}_restart") is None

    await sim.push_and_settle(sim.status())
    assert hass.states.get(f"sensor.{prefix}_battery_voltage") is not None


async def test_hostile_component_flood_is_capped(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """A device reporting thousands of components cannot balloon memory."""
    sim = MeatPiDeviceSimulator.from_preset(hass, hass_client, "wican_pro_v6")
    api = sim.attach_api(aioclient_mock)
    api.settings_payload = {
        "components": [{"name": f"component_{i}"} for i in range(10000)]
        + [{"name": "x" * 5000}],
    }
    await sim.async_setup()

    capabilities = sim.entry.runtime_data.capabilities
    assert capabilities.has_http_api
    assert len(capabilities.components) <= MAX_API_COMPONENTS
    assert "x" * 5000 not in capabilities.components


@pytest.mark.parametrize(
    "status_payload",
    [
        {"bits": "yes", "version": "6.0.1"},
        {"bits": ["sta_connected"]},
        {"version": "6.0.1"},
        ["not", "a", "dict"],
        "just a string",
        None,
    ],
)
async def test_malformed_status_shapes_probe_as_legacy(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    status_payload: Any,
) -> None:
    """Any non-V6 /api/status shape degrades to legacy without breaking."""
    sim = MeatPiDeviceSimulator.from_preset(hass, hass_client, "wican_pro_v6")
    api = sim.attach_api(aioclient_mock)
    api.status_payload = status_payload
    await sim.async_setup()

    assert not sim.entry.runtime_data.capabilities.has_http_api
    await sim.push_and_settle(sim.status())
    prefix = _entity_prefix(sim)
    assert hass.states.get(f"sensor.{prefix}_battery_voltage") is not None


async def test_settings_garbage_still_yields_v6_restart(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """Garbage in /api/settings loses component gating, not the V6 API."""
    sim = MeatPiDeviceSimulator.from_preset(hass, hass_client, "wican_pro_v6")
    api = sim.attach_api(aioclient_mock)
    api.settings_payload = {"components": "many"}
    await sim.async_setup()
    prefix = _entity_prefix(sim)

    assert sim.entry.runtime_data.capabilities.has_http_api
    assert hass.states.get(f"button.{prefix}_restart") is not None
    assert hass.states.get(f"button.{prefix}_sync_time") is None


# ---------------------------------------------------------------------------
# Flapping / concurrency / isolation
# ---------------------------------------------------------------------------


async def test_firmware_flapping_probes_coalesce(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """Rapid firmware-version flapping cannot pile up probe traffic."""
    sim = await _sim(hass, hass_client, aioclient_mock, "wican_pro_v6")
    prefix = _entity_prefix(sim)

    for i in range(10):
        sim.reboot(fw_version=f"6.0.{i % 2}")
        await sim.push(sim.status())
    await hass.async_block_till_done()

    # Coalescing bounds the probe traffic to at most one per push plus the
    # setup probe; the integration is alive and consistent afterwards.
    assert sim.api.probe_calls <= 12
    await sim.push_and_settle(sim.status(batt_voltage="12.4V"))
    battery = hass.states.get(f"sensor.{prefix}_battery_voltage")
    assert float(battery.state) == 12.4


@pytest.mark.parametrize(
    "preset",
    ["wican_legacy", "wican_pro_v6", "wican_usb_v6", "espnetlink", "meatpi_generic"],
)
async def test_refresh_definitions_button_on_every_device_type(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    preset: str,
) -> None:
    """Every MeatPi device type gets the refresh-definitions button."""
    sim = await _sim(hass, hass_client, aioclient_mock, preset)
    prefix = _entity_prefix(sim)
    assert hass.states.get(f"button.{prefix}_refresh_integration_definitions") is not None


async def test_two_device_types_are_isolated(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """A legacy WiCAN and a V6 ESPNetlink coexist without cross-talk."""
    legacy = MeatPiDeviceSimulator.from_preset(hass, hass_client, "wican_legacy")
    legacy.attach_api(aioclient_mock)
    await legacy.async_setup()

    netlink = MeatPiDeviceSimulator.from_preset(hass, hass_client, "espnetlink")
    netlink.attach_api(aioclient_mock)
    await netlink.async_setup()

    legacy_prefix = _entity_prefix(legacy)
    netlink_prefix = _entity_prefix(netlink)

    await legacy.push_and_settle(legacy.status())
    await netlink.push_and_settle(
        {**netlink.status(), **netlink.gps(51.5, -0.12)},
    )

    # Control entities exist only for the V6 device.
    assert hass.states.get(f"button.{legacy_prefix}_restart") is None
    assert hass.states.get(f"button.{netlink_prefix}_restart") is not None

    # A press commands only its own device.
    await _press(hass, f"button.{netlink_prefix}_restart")
    assert netlink.api.restart_calls == 1
    assert legacy.api.restart_calls == 0

    # Telemetry stayed separate: only the ESPNetlink tracker got a GPS fix.
    assert hass.states.get(f"sensor.{legacy_prefix}_battery_voltage") is not None
    netlink_tracker = hass.states.get(f"device_tracker.{netlink_prefix}_location")
    legacy_tracker = hass.states.get(f"device_tracker.{legacy_prefix}_location")
    assert netlink_tracker.attributes.get("latitude") == pytest.approx(51.5)
    assert legacy_tracker.attributes.get("latitude") is None
