"""Failure-injection tests for the WiCAN integration.

Every test here simulates a device misbehaving in a way seen (or imaginable)
in the field — corrupted values, hostile payloads, identity glitches, floods,
network churn — and asserts the integration degrades gracefully and recovers:
no crash, no wedged entities, no polluted config entry, no unbounded growth.

The happy-path / baseline behaviour lives in ``test_device_simulation.py``.
"""

from __future__ import annotations

import asyncio
from http import HTTPStatus
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
import pytest

from custom_components.wican.const import MAX_DYNAMIC_PID_SENSORS
from custom_components.wican.sensor import DYNAMIC_PID_SENSORS
from tests.device_sim import MeatPiDeviceSimulator

# ===================================================================
# Group A — hostile values must never wedge the state pipeline
# ===================================================================


async def test_bad_voltage_does_not_block_other_entities(
    device: MeatPiDeviceSimulator,
) -> None:
    """A value HA's state machine rejects must not abort the whole push.

    batt_voltage declares a voltage device class, so a non-numeric state
    raises inside Home Assistant. One corrupted reading must not prevent the
    other entities in the same push from updating (regression test for the
    coordinator listener chain aborting at the first failure).
    """
    await device.async_setup()
    resp = await device.push_and_settle(
        device.status(batt_voltage="ERROR", vpn_status="Connected", ecu_status="online"),
    )
    assert resp.status == HTTPStatus.NO_CONTENT

    # Siblings in the same push were applied.
    assert device.hass.states.get("sensor.wican_sim_vpn_status").state == "Connected"
    assert device.hass.states.get("binary_sensor.wican_sim_ecu_online").state == "on"
    # The unusable voltage was dropped, not written and not crashed on.
    assert device.hass.states.get("sensor.wican_sim_battery_voltage").state == "unknown"

    # The device recovers: the next good reading flows through.
    await device.push_and_settle(device.status(batt_voltage="12.4V"))
    assert device.hass.states.get("sensor.wican_sim_battery_voltage").state == "12.4"


async def test_garbage_voltage_after_good_value_goes_unknown(
    device: MeatPiDeviceSimulator,
) -> None:
    """A good value followed by garbage yields 'unknown', then recovers."""
    await device.async_setup()
    await device.push_and_settle(device.status(batt_voltage="12.8V"))
    assert device.hass.states.get("sensor.wican_sim_battery_voltage").state == "12.8"

    await device.push_and_settle(device.status(batt_voltage="N/A"))
    assert device.hass.states.get("sensor.wican_sim_battery_voltage").state == "unknown"

    await device.push_and_settle(device.status(batt_voltage="13.1V"))
    assert device.hass.states.get("sensor.wican_sim_battery_voltage").state == "13.1"


async def test_overlong_status_string_is_truncated(
    device: MeatPiDeviceSimulator,
) -> None:
    """A status string longer than HA's 255-char limit is truncated, not fatal."""
    await device.async_setup()
    long_value = "x" * 300
    resp = await device.push_and_settle(
        device.status(vpn_status=long_value, batt_voltage="12.0V"),
    )
    assert resp.status == HTTPStatus.NO_CONTENT

    state = device.hass.states.get("sensor.wican_sim_vpn_status")
    assert state.state == "x" * 255
    # The sibling in the same push still updated.
    assert device.hass.states.get("sensor.wican_sim_battery_voltage").state == "12.0"


async def test_megabyte_status_value_is_survivable(
    device: MeatPiDeviceSimulator,
) -> None:
    """A pathologically large (1 MB) status value is truncated and survived."""
    await device.async_setup()
    resp = await device.push_and_settle(device.status(vpn_status="y" * (1024 * 1024)))
    assert resp.status == HTTPStatus.NO_CONTENT
    assert device.hass.states.get("sensor.wican_sim_vpn_status").state == "y" * 255


async def test_nonfinite_json_payload_is_rejected_and_survivable(
    device: MeatPiDeviceSimulator,
) -> None:
    """NaN / Infinity / 1e999 on the wire are rejected at the transport layer.

    Home Assistant's JSON parser refuses non-finite numbers, so the push gets
    a 422 — and the integration must simply carry on afterwards.
    """
    await device.async_setup()
    raw = (
        '{"status": {"device_id": "wican_sim_001"},'
        ' "autopid_data": {"RPM": NaN, "TEMP": Infinity, "BIG": 1e999, "SPEED": 55},'
        ' "config": {}}'
    )
    resp = await device.push(raw=raw)
    assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY

    good = await device.push_and_settle(device.pids({"SPEED": 55}))
    assert good.status == HTTPStatus.NO_CONTENT
    assert device.hass.states.get("sensor.wican_sim_speed").state == "55"


async def test_nonfinite_floats_injected_internally_are_dropped(
    device: MeatPiDeviceSimulator,
) -> None:
    """Defense in depth: non-finite floats inside the pipeline are dropped.

    Even if a payload with NaN/Infinity slips past the transport parser (via
    the dispatcher, a restore, or a future parser change), the coordinator
    must drop the values rather than let a state write blow up.
    """
    await device.async_setup()
    # Create the PID entities through the normal path first.
    await device.push_and_settle(device.pids({"RPM": 800, "SPEED": 10}))

    device.coordinator.handle_webhook_data(
        {
            "status": {
                "device_id": device.device_id,
                "batt_voltage": float("nan"),
                "uptime": "up",
            },
            "autopid_data": {"RPM": float("inf"), "SPEED": 55},
            "config": {},
        },
    )
    await device.hass.async_block_till_done()

    assert device.hass.states.get("sensor.wican_sim_battery_voltage").state == "unknown"
    assert device.hass.states.get("sensor.wican_sim_uptime").state == "up"
    assert device.hass.states.get("sensor.wican_sim_speed").state == "55"
    # The non-finite RPM was dropped: the sensor shows no value rather than
    # wedging the pipeline.
    assert device.hass.states.get("sensor.wican_sim_rpm").state == "unknown"


async def test_string_pid_works_and_numeric_pid_gets_statistics(
    device: MeatPiDeviceSimulator,
) -> None:
    """Text PIDs (gear position) work; numeric PIDs get long-term statistics.

    Older builds stamped every restored PID with state_class=measurement,
    which made a text-valued PID fail every state write after a restart.
    """
    await device.async_setup()
    await device.push_and_settle(device.pids({"GEAR": "D", "RPM": 1200}))

    gear = device.hass.states.get("sensor.wican_sim_gear")
    assert gear.state == "D"
    assert gear.attributes.get("state_class") is None

    rpm = device.hass.states.get("sensor.wican_sim_rpm")
    assert rpm.state == "1200"
    assert rpm.attributes.get("state_class") == "measurement"


async def test_pid_flips_numeric_to_garbage_and_back(
    device: MeatPiDeviceSimulator,
) -> None:
    """A numeric PID that glitches to text goes unknown, then recovers."""
    await device.async_setup()
    await device.push_and_settle(device.pids({"RPM": 1000}))
    assert device.hass.states.get("sensor.wican_sim_rpm").state == "1000"

    await device.push_and_settle(device.pids({"RPM": "SENSOR FAULT"}))
    rpm = device.hass.states.get("sensor.wican_sim_rpm")
    assert rpm.state == "unknown"
    # The statistics latch survives the glitch.
    assert rpm.attributes.get("state_class") == "measurement"

    await device.push_and_settle(device.pids({"RPM": 2000}))
    assert device.hass.states.get("sensor.wican_sim_rpm").state == "2000"


async def test_bool_values_do_not_break_numeric_sensors(
    device: MeatPiDeviceSimulator,
) -> None:
    """A JSON true/false in a numeric field is dropped, not written."""
    await device.async_setup()
    resp = await device.push_and_settle(device.status(batt_voltage=True))
    assert resp.status == HTTPStatus.NO_CONTENT
    assert device.hass.states.get("sensor.wican_sim_battery_voltage").state == "unknown"


async def test_unsatisfiable_device_classes_are_ignored(
    device: MeatPiDeviceSimulator,
) -> None:
    """timestamp/date/enum classes can never hold a JSON scalar; ignore them.

    Home Assistant would raise on every state write for such a sensor, so the
    class is dropped and the value still flows.
    """
    await device.async_setup()
    await device.push_and_settle(
        device.pids(
            {"ODO": 123456, "TRIP": 12},
            {"ODO": {"unit": "", "class": "timestamp"},
             "TRIP": {"unit": "", "class": "enum"}},
        ),
    )
    odo = device.hass.states.get("sensor.wican_sim_odo")
    assert odo.state == "123456"
    assert odo.attributes.get("device_class") is None
    assert device.hass.states.get("sensor.wican_sim_trip").state == "12"


async def test_null_values_everywhere(device: MeatPiDeviceSimulator) -> None:
    """A payload of nulls updates nothing and breaks nothing."""
    await device.async_setup()
    await device.push_and_settle(device.status(batt_voltage="12.2V", ecu_status="online"))

    resp = await device.push_and_settle(
        {
            "status": {
                "device_id": device.device_id,
                "batt_voltage": None,
                "ecu_status": None,
                "vpn_status": None,
            },
            "autopid_data": None,
            "config": None,
            "gps": None,
        },
    )
    assert resp.status == HTTPStatus.NO_CONTENT
    # Null voltage clears the reading; the binary sensor keeps its last state.
    assert device.hass.states.get("sensor.wican_sim_battery_voltage").state == "unknown"
    assert device.hass.states.get("binary_sensor.wican_sim_ecu_online").state == "on"


async def test_binary_sensor_scalar_interpretations(
    device: MeatPiDeviceSimulator,
) -> None:
    """Binary sensors interpret scalars; non-scalars keep the last state."""
    await device.async_setup()
    await device.push_and_settle(device.status(ecu_status=1))
    assert device.hass.states.get("binary_sensor.wican_sim_ecu_online").state == "on"

    await device.push_and_settle(device.status(ecu_status={"nested": True}))
    # Non-scalar says nothing: state unchanged.
    assert device.hass.states.get("binary_sensor.wican_sim_ecu_online").state == "on"

    await device.push_and_settle(device.status(ecu_status=0))
    assert device.hass.states.get("binary_sensor.wican_sim_ecu_online").state == "off"


# ===================================================================
# Group B — identity chaos
# ===================================================================


async def test_numeric_device_id_matches_stored_string(
    hass: HomeAssistant, hass_client: Any,
) -> None:
    """A firmware that serializes the id as a JSON number still matches."""
    device = MeatPiDeviceSimulator(hass, hass_client, device_id="12345")
    await device.async_setup()
    resp = await device.push({"status": {"device_id": 12345, "batt_voltage": "12.0V"}})
    assert resp.status == HTTPStatus.NO_CONTENT


async def test_non_scalar_device_id_is_rejected(
    device: MeatPiDeviceSimulator,
) -> None:
    """A structured device_id can never match and is rejected like an impostor."""
    await device.async_setup()
    resp = await device.push(
        {"status": {"device_id": {"weird": "object"}, "batt_voltage": "12.0V"}},
    )
    assert resp.status == HTTPStatus.FORBIDDEN
    # The rightful device still works afterwards.
    good = await device.push(device.status())
    assert good.status == HTTPStatus.NO_CONTENT


async def test_numeric_device_id_is_learned_as_string(
    hass: HomeAssistant, hass_client: Any,
) -> None:
    """A learned numeric id is stored as a string and keeps matching."""
    device = MeatPiDeviceSimulator(hass, hass_client, device_id=None)
    await device.async_setup(store_device_id=False)

    resp = await device.push({"status": {"device_id": 9876, "batt_voltage": "12.0V"}})
    assert resp.status == HTTPStatus.NO_CONTENT
    await hass.async_block_till_done()
    assert device.entry.data["device_id"] == "9876"

    # Subsequent pushes match whether the wire form is a number or a string.
    assert (await device.push({"status": {"device_id": 9876}})).status == HTTPStatus.NO_CONTENT
    assert (await device.push({"status": {"device_id": "9876"}})).status == HTTPStatus.NO_CONTENT
    assert (await device.push({"status": {"device_id": "other"}})).status == HTTPStatus.FORBIDDEN


# ===================================================================
# Group C — device-info pollution must never reach the config entry
# ===================================================================


async def test_hostile_device_info_fields_are_not_persisted(
    device: MeatPiDeviceSimulator,
) -> None:
    """Nested objects / arrays / oversized strings never pollute the entry.

    Everything stored in the config entry later flows into version parsing,
    URL building, and the device registry — all of which assume strings.
    """
    await device.async_setup()
    resp = await device.push_and_settle(
        device.status(
            fw_version={"nested": "object"},
            hw_version=["a", "list"],
            mdns={"not": "a-url"},
            git_version="g" * 10_000,
            host=True,
        ),
    )
    assert resp.status == HTTPStatus.NO_CONTENT

    data = device.entry.data
    assert data["fw_version"] == "v4.49"  # unchanged from setup
    assert data["hw_version"] == "WiCAN-Pro"
    assert data["mdns"] == "http://wican_sim.local"
    assert data["host"] == "http://192.168.1.123"  # bool never persisted
    assert "git_version" not in data

    # The device is still fully functional.
    await device.push_and_settle(device.status(batt_voltage="12.9V"))
    assert device.hass.states.get("sensor.wican_sim_battery_voltage").state == "12.9"


async def test_numeric_fw_version_is_stored_as_string(
    device: MeatPiDeviceSimulator,
) -> None:
    """A firmware that reports its version as a JSON number is tolerated."""
    await device.async_setup()
    await device.push_and_settle(device.status(fw_version=4.51))
    assert device.entry.data["fw_version"] == "4.51"


async def test_top_level_device_info_fields_are_read(
    device: MeatPiDeviceSimulator,
) -> None:
    """Info fields at the payload top level (older firmware) are persisted."""
    await device.async_setup()
    await device.push_and_settle({"fw_version": "v9.99"})
    assert device.entry.data["fw_version"] == "v9.99"


async def test_reboot_with_new_firmware_updates_entry_and_update_entity(
    device: MeatPiDeviceSimulator,
) -> None:
    """After an OTA reboot the new version reaches the entry and update entity."""
    await device.async_setup()
    await device.push_and_settle(device.status())

    device.reboot(fw_version="4.60")
    await device.push_and_settle(device.status())

    assert device.entry.data["fw_version"] == "4.60"
    update_state = device.hass.states.get("update.wican_sim_firmware")
    assert update_state is not None
    assert update_state.attributes.get("installed_version") == "4.60"


# ===================================================================
# Group D — floods and unbounded growth
# ===================================================================


async def test_pid_flood_is_capped(
    device: MeatPiDeviceSimulator, caplog: pytest.LogCaptureFixture,
) -> None:
    """A device inventing endless PID names cannot flood the registry."""
    await device.async_setup()

    with patch("custom_components.wican.sensor.MAX_DYNAMIC_PID_SENSORS", 25):
        flood = {f"JUNK_{i:04d}": i for i in range(40)}
        resp = await device.push_and_settle(device.pids(flood))
        assert resp.status == HTTPStatus.NO_CONTENT

        sensors = DYNAMIC_PID_SENSORS[device.entry.entry_id]
        assert len(sensors) == 25
        assert "more than 25 distinct PIDs" in caplog.text

        # A second flood does not grow the set (and does not warn-spam).
        warn_count = caplog.text.count("distinct PIDs")
        flood2 = {f"MORE_{i:04d}": i for i in range(40)}
        await device.push_and_settle(device.pids(flood2))
        assert len(sensors) == 25
        assert caplog.text.count("distinct PIDs") == warn_count

        # Existing (pre-cap) PIDs still update normally.
        await device.push_and_settle(device.pids({"JUNK_0000": 777}))
        assert device.hass.states.get("sensor.wican_sim_junk_0000").state == "777"


async def test_unusable_pid_keys_are_skipped(
    device: MeatPiDeviceSimulator,
) -> None:
    """Empty / whitespace / oversized PID keys never become entities."""
    await device.async_setup()
    resp = await device.push_and_settle(
        device.pids({"": 1, "   ": 2, "K" * 500: 3, "GOOD_PID": 4}),
    )
    assert resp.status == HTTPStatus.NO_CONTENT

    sensors = DYNAMIC_PID_SENSORS[device.entry.entry_id]
    assert set(sensors) == {"GOOD_PID"}
    assert device.entry.data["pid_keys"] == ["GOOD_PID"]
    assert device.hass.states.get("sensor.wican_sim_good_pid").state == "4"


async def test_junk_pid_config_is_trimmed_before_persisting(
    device: MeatPiDeviceSimulator,
) -> None:
    """Only bounded unit/class strings are written to the config entry."""
    await device.async_setup()
    await device.push_and_settle(
        device.pids(
            {"RPM": 900},
            {"RPM": {
                "unit": {"nested": "junk"},
                "class": "c" * 200,
                "payload": "p" * 50_000,
            }},
        ),
    )
    stored = device.entry.data["config"]["RPM"]
    assert stored == {}
    # The sensor itself still works with fallback metadata.
    assert device.hass.states.get("sensor.wican_sim_rpm").state == "900"


async def test_corrupt_stored_pid_keys_do_not_break_reload(
    device: MeatPiDeviceSimulator, hass: HomeAssistant,
) -> None:
    """A corrupted config entry (pid_keys not a list) sets up cleanly."""
    await device.async_setup()
    hass.config_entries.async_update_entry(
        device.entry,
        data={**device.entry.data, "pid_keys": "corrupt", "config": "junk"},
    )
    await hass.async_block_till_done()

    await device.async_reload()

    # Fresh PIDs still create entities after the reload.
    await device.push_and_settle(device.pids({"RPM": 1500}))
    assert device.hass.states.get("sensor.wican_sim_rpm").state == "1500"


# ===================================================================
# Group E — malformed transport payloads
# ===================================================================


async def test_deeply_nested_json_is_survivable(
    device: MeatPiDeviceSimulator,
) -> None:
    """A pathologically nested payload cannot take the webhook down."""
    await device.async_setup()
    depth = 2000
    raw = ('{"a":' * depth) + "1" + ("}" * depth)
    resp = await device.push(raw=raw)
    # Depending on the parser's recursion limit this is either accepted or
    # rejected — either way the integration must survive and keep working.
    assert resp.status in (HTTPStatus.OK, HTTPStatus.NO_CONTENT, HTTPStatus.UNPROCESSABLE_ENTITY)

    good = await device.push_and_settle(device.status(batt_voltage="12.3V"))
    assert good.status == HTTPStatus.NO_CONTENT
    assert device.hass.states.get("sensor.wican_sim_battery_voltage").state == "12.3"


async def test_wrong_content_type_with_valid_json_is_tolerated(
    device: MeatPiDeviceSimulator,
) -> None:
    """A firmware that mislabels its JSON as text/plain still works."""
    await device.async_setup()
    resp = await device.push(
        raw='{"status": {"device_id": "wican_sim_001", "batt_voltage": "12.7V"}}',
        content_type="text/plain",
    )
    assert resp.status == HTTPStatus.NO_CONTENT
    await device.hass.async_block_till_done()
    assert device.hass.states.get("sensor.wican_sim_battery_voltage").state == "12.7"


async def test_binary_garbage_body_is_rejected_cleanly(
    device: MeatPiDeviceSimulator,
) -> None:
    """Random bytes (crashed device mid-write) are rejected, then all is well."""
    await device.async_setup()
    resp = await device.push(raw=bytes(range(256)), content_type="application/octet-stream")
    assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY

    good = await device.push(device.status())
    assert good.status == HTTPStatus.NO_CONTENT


# ===================================================================
# Group F — GPS chaos
# ===================================================================


async def test_low_accuracy_fix_does_not_teleport_vehicle(
    device: MeatPiDeviceSimulator,
) -> None:
    """A worse-than-threshold fix is ignored once a good fix exists."""
    await device.async_setup()
    await device.push_and_settle(device.gps(10.0, 10.0, accuracy=5))
    state = device.hass.states.get("device_tracker.wican_sim_location")
    assert state.attributes["latitude"] == 10.0

    # Cold-start / parking-garage fix with 999 m accuracy: ignored.
    await device.push_and_settle(device.gps(20.0, 20.0, accuracy=999))
    state = device.hass.states.get("device_tracker.wican_sim_location")
    assert state.attributes["latitude"] == 10.0

    # A good fix moves the tracker again.
    await device.push_and_settle(device.gps(30.0, 30.0, accuracy=8))
    state = device.hass.states.get("device_tracker.wican_sim_location")
    assert state.attributes["latitude"] == 30.0


async def test_first_fix_is_accepted_even_with_poor_accuracy(
    device: MeatPiDeviceSimulator,
) -> None:
    """With no location at all, any in-range fix beats nothing."""
    await device.async_setup()
    await device.push_and_settle(device.gps(15.0, 15.0, accuracy=950))
    state = device.hass.states.get("device_tracker.wican_sim_location")
    assert state.attributes["latitude"] == 15.0


async def test_gps_partial_and_malformed_fields(
    device: MeatPiDeviceSimulator,
) -> None:
    """Each GPS field is parsed independently; one bad field costs only itself."""
    await device.async_setup()

    # Latitude without longitude: no fix.
    await device.push_and_settle({"gps": {"latitude": 42.0}})
    state = device.hass.states.get("device_tracker.wican_sim_location")
    assert state.state == "unavailable"

    # Boolean coordinates (JSON true/false) are not positions: no fix.
    await device.push_and_settle({"gps": {"latitude": True, "longitude": False}})
    state = device.hass.states.get("device_tracker.wican_sim_location")
    assert state.state == "unavailable"

    # String coordinates and fractional string accuracy are parsed; garbage
    # altitude and structured heading are dropped; numeric speed is kept.
    await device.push_and_settle(
        {
            "gps": {
                "latitude": "51.5",
                "longitude": "-0.12",
                "accuracy": "7.5",
                "altitude": "abc",
                "heading": {"x": 1},
                "speed": 12.5,
            },
        },
    )
    state = device.hass.states.get("device_tracker.wican_sim_location")
    assert state.attributes["latitude"] == 51.5
    assert state.attributes["longitude"] == -0.12
    assert state.attributes["gps_accuracy"] == 7
    assert "altitude" not in state.attributes
    assert "heading" not in state.attributes
    assert state.attributes["speed"] == 12.5


async def test_gps_nan_coordinates_are_ignored(
    device: MeatPiDeviceSimulator,
) -> None:
    """Non-finite coordinates inside the pipeline never move the tracker."""
    await device.async_setup()
    await device.push_and_settle(device.gps(37.0, -122.0, accuracy=5))

    # Injected past the transport parser (which would reject NaN itself).
    device.coordinator.handle_webhook_data(
        {"gps": {"latitude": float("nan"), "longitude": float("inf")}},
    )
    await device.hass.async_block_till_done()

    state = device.hass.states.get("device_tracker.wican_sim_location")
    assert state.attributes["latitude"] == 37.0


async def test_negative_accuracy_is_clamped(device: MeatPiDeviceSimulator) -> None:
    """A negative accuracy value is nonsense; it is clamped to 0."""
    await device.async_setup()
    await device.push_and_settle(device.gps(12.0, 12.0, accuracy=-50))
    state = device.hass.states.get("device_tracker.wican_sim_location")
    assert state.attributes["latitude"] == 12.0
    assert state.attributes["gps_accuracy"] == 0


# ===================================================================
# Group G — concurrency and lifecycle churn
# ===================================================================


async def test_concurrent_pushes_do_not_corrupt_state(
    device: MeatPiDeviceSimulator,
) -> None:
    """A burst of concurrent pushes all succeed and leave consistent state."""
    await device.async_setup()

    voltages = [f"{12 + i * 0.1:.1f}V" for i in range(10)]
    responses = await asyncio.gather(
        *(device.push(device.status(batt_voltage=v)) for v in voltages),
    )
    assert all(r.status == HTTPStatus.NO_CONTENT for r in responses)
    await device.hass.async_block_till_done()

    state = device.hass.states.get("sensor.wican_sim_battery_voltage")
    assert state.state in {v.rstrip("V") for v in voltages}
    assert device.coordinator.last_update_success is True


async def test_reload_under_fire(device: MeatPiDeviceSimulator) -> None:
    """Reloading while the device keeps pushing loses nothing."""
    await device.async_setup()
    await device.push_and_settle(device.pids({"RPM": 1100}))
    assert device.hass.states.get("sensor.wican_sim_rpm").state == "1100"

    await device.async_reload()

    # The restored PID entity exists and picks up the next push.
    await device.push_and_settle(device.pids({"RPM": 1300}))
    assert device.hass.states.get("sensor.wican_sim_rpm").state == "1300"
    # Status entities also keep flowing after the reload.
    await device.push_and_settle(device.status(batt_voltage="12.1V"))
    assert device.hass.states.get("sensor.wican_sim_battery_voltage").state == "12.1"


async def test_unload_cleans_dynamic_pid_state(
    device: MeatPiDeviceSimulator,
) -> None:
    """Unloading removes the entry's dynamic-PID bookkeeping (no leak)."""
    await device.async_setup()
    await device.push_and_settle(device.pids({"RPM": 1000}))
    assert device.entry.entry_id in DYNAMIC_PID_SENSORS

    assert await device.hass.config_entries.async_unload(device.entry.entry_id)
    await device.hass.async_block_till_done()
    assert device.entry.entry_id not in DYNAMIC_PID_SENSORS


async def test_connection_flapping_coalesces_reregistrations(
    device: MeatPiDeviceSimulator,
) -> None:
    """A device flapping between addresses cannot pile up registration tasks.

    While one (retried, backed-off) registration attempt is running, further
    connection changes fold into a single trailing re-run.
    """
    await device.async_setup()

    gate: asyncio.Event = asyncio.Event()
    calls: list[int] = []

    async def _slow_register(*_args: Any, **_kwargs: Any) -> bool:
        calls.append(1)
        await gate.wait()
        return True

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        AsyncMock(side_effect=_slow_register),
    ):
        # Three connection-info changes in quick succession.
        for host in ("http://10.0.0.2", "http://10.0.0.3", "http://10.0.0.4"):
            resp = await device.push(device.status(mdns=host))
            assert resp.status == HTTPStatus.NO_CONTENT
        # Let the created tasks start (the first blocks on the gate).
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(calls) == 1  # only one registration is in flight

        gate.set()
        await device.hass.async_block_till_done()

    # The flapping burst coalesced into the running attempt + one trailing rerun.
    assert len(calls) == 2


async def test_stale_then_recover_preserves_pid_data(
    device: MeatPiDeviceSimulator,
) -> None:
    """Going unavailable and recovering never loses previously pushed data."""
    await device.async_setup()
    await device.push_and_settle(device.pids({"SOC": 81}))
    assert device.hass.states.get("sensor.wican_sim_soc").state == "81"

    device.go_stale()
    await device.coordinator.async_refresh()
    await device.hass.async_block_till_done()
    assert device.hass.states.get("sensor.wican_sim_soc").state == "unavailable"

    # A keep-alive (even an empty one) brings everything back with its data.
    await device.push_and_settle({})
    assert device.hass.states.get("sensor.wican_sim_soc").state == "81"


async def test_unicode_pid_key_works(device: MeatPiDeviceSimulator) -> None:
    """A non-ASCII PID name becomes a working sensor."""
    await device.async_setup()
    await device.push_and_settle(device.pids({"Темп_Двиг": 88}))
    sensors = DYNAMIC_PID_SENSORS[device.entry.entry_id]
    assert "Темп_Двиг" in sensors
    # Find the entity through the registry-independent state machine.
    states = [
        s for s in device.hass.states.async_all("sensor") if s.state == "88"
    ]
    assert len(states) == 1


async def test_cap_warning_resets_after_reload(
    device: MeatPiDeviceSimulator, caplog: pytest.LogCaptureFixture,
) -> None:
    """The one-time PID-cap warning can fire again after a reload."""
    await device.async_setup()
    with patch("custom_components.wican.sensor.MAX_DYNAMIC_PID_SENSORS", 5):
        await device.push_and_settle(device.pids({f"A{i}": i for i in range(10)}))
        assert caplog.text.count("distinct PIDs") == 1

        await device.async_reload()

        await device.push_and_settle(device.pids({f"B{i}": i for i in range(10)}))
        assert caplog.text.count("distinct PIDs") == 2


async def test_default_pid_cap_matches_const(device: MeatPiDeviceSimulator) -> None:
    """Sanity: the real cap is high enough for the largest known vehicles."""
    assert MAX_DYNAMIC_PID_SENSORS >= 500
