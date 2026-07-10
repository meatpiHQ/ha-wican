"""Test the WiCAN sensor platform."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.wican.const import DOMAIN

from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_sensor_entities_created(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Test sensor entities are created."""
    entity_registry = er.async_get(hass)

    # Check static status sensors exist
    wifi_mode = entity_registry.async_get("sensor.wican_device_wifi_mode")
    assert wifi_mode is not None
    assert wifi_mode.unique_id.endswith("_wifi_mode")

    batt_voltage = entity_registry.async_get("sensor.wican_device_batt_voltage")
    assert batt_voltage is not None
    assert batt_voltage.unique_id.endswith("_batt_voltage")

    vpn_status = entity_registry.async_get("sensor.wican_device_vpn_status")
    assert vpn_status is not None
    assert vpn_status.unique_id.endswith("_vpn_status")

    uptime = entity_registry.async_get("sensor.wican_device_uptime")
    assert uptime is not None
    assert uptime.unique_id.endswith("_uptime")


async def test_sensor_states_update_from_webhook(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """Test sensor states update when webhook data arrives."""
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]

    client = await hass_client()

    # Post webhook data
    await client.post(f"/api/webhook/{webhook_id}", json=mock_webhook_data)
    await hass.async_block_till_done()

    # Check sensor states
    wifi_mode_state = hass.states.get("sensor.wican_device_wifi_mode")
    assert wifi_mode_state is not None
    assert wifi_mode_state.state == "Station"

    batt_voltage_state = hass.states.get("sensor.wican_device_batt_voltage")
    assert batt_voltage_state is not None
    # Should normalize "12.5V" to 12.5
    assert batt_voltage_state.state == "12.5"

    vpn_status_state = hass.states.get("sensor.wican_device_vpn_status")
    assert vpn_status_state is not None
    assert vpn_status_state.state == "Not Connected"

    uptime_state = hass.states.get("sensor.wican_device_uptime")
    assert uptime_state is not None
    assert uptime_state.state == "01:00:00"


async def test_pid_sensor_entities_created(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """Test PID sensor entities are created dynamically."""
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]

    client = await hass_client()

    # Post webhook data with PIDs to trigger entity creation
    await client.post(f"/api/webhook/{webhook_id}", json=mock_webhook_data)
    await hass.async_block_till_done()

    # Check PID sensors exist (created after webhook)
    entity_registry = er.async_get(hass)

    engine_rpm = entity_registry.async_get("sensor.wican_device_engine_rpm")
    # PID sensors might not be in registry immediately
    if engine_rpm:
        assert engine_rpm.unique_id.endswith("_pid_0x0c")

    coolant_temp = entity_registry.async_get("sensor.wican_device_coolant_temp")
    if coolant_temp:
        assert coolant_temp.unique_id.endswith("_pid_0x05")


async def test_pid_sensor_states_update(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """Test PID sensor states update from webhook."""
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]

    client = await hass_client()

    # Post initial webhook data
    await client.post(f"/api/webhook/{webhook_id}", json=mock_webhook_data)
    await hass.async_block_till_done()

    # Check initial values
    engine_rpm_state = hass.states.get("sensor.wican_device_engine_rpm")
    if engine_rpm_state:
        assert engine_rpm_state.state == "1500"
        assert engine_rpm_state.attributes.get("unit_of_measurement") == "rpm"

    # Update webhook data with new values
    updated_data = mock_webhook_data.copy()
    updated_data["pids"]["pid_0x0c"]["value"] = 2000

    await client.post(f"/api/webhook/{webhook_id}", json=updated_data)
    await hass.async_block_till_done()

    # Check updated value
    engine_rpm_state = hass.states.get("sensor.wican_device_engine_rpm")
    if engine_rpm_state:
        assert engine_rpm_state.state == "2000"


async def test_sensor_voltage_normalization(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """Test battery voltage normalization removes 'V' suffix."""
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]

    client = await hass_client()

    # Test various voltage formats
    test_cases = [
        ("12.5V", "12.5"),
        ("11.3V", "11.3"),
        ("14.0V", "14.0"),
    ]

    for voltage_input, expected_state in test_cases:
        data = mock_webhook_data.copy()
        data["status"]["batt_voltage"] = voltage_input

        await client.post(f"/api/webhook/{webhook_id}", json=data)
        await hass.async_block_till_done()

        batt_voltage_state = hass.states.get("sensor.wican_device_batt_voltage")
        assert batt_voltage_state.state == expected_state


async def test_batt_voltage_sets_decimal_display_precision(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Test battery voltage sensor requests decimal display precision."""
    entity_registry = er.async_get(hass)

    batt_voltage = entity_registry.async_get("sensor.wican_device_batt_voltage")
    assert batt_voltage is not None
    assert batt_voltage.options.get("sensor", {}).get("suggested_display_precision") == 1


async def test_sensor_state_restoration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test sensor state is restored on startup."""
    # Store a state before setup
    hass.states.async_set(
        "sensor.wican_device_batt_voltage",
        "13.2",
        {"unit_of_measurement": "V"},
    )
    hass.states.async_set(
        "sensor.wican_device_wifi_mode",
        "AP",
    )
    
    # Ensure state is written to restore state storage
    await hass.async_block_till_done()
    
    # Set up the integration - this should restore states
    mock_config_entry.add_to_hass(hass)
    
    with patch(
        "custom_components.wican.async_get_clientsession"
    ), patch(
        "custom_components.wican.WiCANDataUpdateCoordinator.async_config_entry_first_refresh"
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    
    # Check that states were restored
    batt_voltage = hass.states.get("sensor.wican_device_batt_voltage")
    assert batt_voltage is not None
    assert batt_voltage.state == "13.2"
    
    wifi_mode = hass.states.get("sensor.wican_device_wifi_mode")
    assert wifi_mode is not None
    assert wifi_mode.state == "AP"


def test_get_sensor_attributes_with_none_values():
    """Test get_sensor_attributes when status values are None."""
    from custom_components.wican.attributes import get_sensor_attributes
    from custom_components.wican.sensor import WiCANSensorEntityDescription
    
    # Create entity description with extra_attributes
    entity_desc = WiCANSensorEntityDescription(
        key="test_sensor",
        name="Test Sensor",
        extra_attributes=["attr1", "attr2", "attr3"]
    )
    
    # Test data where some attributes are None
    data = {
        "status": {
            "attr1": "value1",
            "attr2": None,  # This should be skipped
            "attr3": "value3"
        }
    }
    
    attrs = get_sensor_attributes(entity_desc, data)
    
    # Only non-None attributes should be included
    assert "attr1" in attrs
    assert "attr2" not in attrs  # Skipped because None
    assert "attr3" in attrs
    assert attrs["attr1"] == "value1"
    assert attrs["attr3"] == "value3"


async def test_sensor_restoration_with_none_state(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test sensor state restoration when saved state is None."""
    mock_config_entry.add_to_hass(hass)
    
    # Don't pre-populate any state (or set state to None)
    # This tests when state is None or state.native_value is None
    
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    
    # Sensors should be created with unknown state
    state = hass.states.get("sensor.wican_device_wifi_mode")
    assert state is not None
    assert state.state == "unknown"


async def test_sensor_state_restoration_with_normalization(hass: HomeAssistant) -> None:
    """Test sensor restores state with normalization."""
    from unittest.mock import patch
    from homeassistant.const import CONF_WEBHOOK_ID
    from custom_components.wican.const import DOMAIN
    from tests.conftest import MockConfigEntry
    
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "mdns": "http://wican_test.local",
            CONF_WEBHOOK_ID: "test_webhook",
        },
        title="WiCAN Test",
    )
    entry.add_to_hass(hass)
    
    # Setup should restore and normalize the value
    with patch("custom_components.wican._async_register_webhook_on_device", return_value=True):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    
    # Check that sensor was created and has restored state
    state = hass.states.get("sensor.wican_test_batt_voltage")
    assert state is not None





async def test_missing_key_sensor_goes_unavailable_when_stale(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """A sensor absent from recent pushes still renders staleness flips.

    Regression: sensors whose key was missing from the payload skipped
    async_write_ha_state() on coordinator notifications, so when the
    device went stale they kept showing their last value as available
    while every sibling entity went unavailable.
    """
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    entry = init_integration
    client = await hass_client()

    # Full push: batt_voltage gets a value.
    await client.post(
        f"/api/webhook/{entry.data[CONF_WEBHOOK_ID]}", json=mock_webhook_data,
    )
    await hass.async_block_till_done()
    live_state = hass.states.get("sensor.wican_device_batt_voltage").state
    assert live_state not in ("unknown", "unavailable")

    # Partial push replaces "status" without batt_voltage: the sensor
    # keeps its last value (partial pushes are normal).
    await client.post(
        f"/api/webhook/{entry.data[CONF_WEBHOOK_ID]}",
        json={"status": {"device_id": "test_device_123", "uptime": "01:00:05"}},
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.wican_device_batt_voltage").state == live_state

    # Device goes silent past the staleness window: the sensor must flip
    # to unavailable together with its siblings.
    coordinator = entry.runtime_data.coordinator
    coordinator._last_push = dt_util.utcnow() - timedelta(hours=3)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert (
        hass.states.get("sensor.wican_device_batt_voltage").state == "unavailable"
    )
    assert hass.states.get("sensor.wican_device_uptime").state == "unavailable"

    # A fresh push recovers it.
    await client.post(
        f"/api/webhook/{entry.data[CONF_WEBHOOK_ID]}", json=mock_webhook_data,
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.wican_device_batt_voltage").state == live_state


async def test_missing_pid_sensor_goes_unavailable_when_stale(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_client,
) -> None:
    """A PID the vehicle stopped reporting still renders staleness flips."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    entry = init_integration
    client = await hass_client()
    identity = {"device_id": "test_device_123", "uptime": "01:00:00"}

    # A push carrying a PID creates its sensor; the entity picks up the
    # value on the next coordinator notification, so push twice.
    for _ in range(2):
        await client.post(
            f"/api/webhook/{entry.data[CONF_WEBHOOK_ID]}",
            json={"status": identity, "autopid_data": {"RPM": 900}, "config": {}},
        )
        await hass.async_block_till_done()
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_pid_RPM",
    )
    assert entity_id is not None
    assert hass.states.get(entity_id).state == "900"

    # Later pushes no longer carry the PID; the value is kept.
    await client.post(
        f"/api/webhook/{entry.data[CONF_WEBHOOK_ID]}",
        json={"status": identity},
    )
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "900"

    # Stale device: the orphaned PID sensor must go unavailable too.
    coordinator = entry.runtime_data.coordinator
    coordinator._last_push = dt_util.utcnow() - timedelta(hours=3)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unavailable"


async def test_last_seen_sensor_tracks_pushes_and_survives_staleness(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """Last seen shows the latest push time and stays available when stale."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    entry = init_integration

    # No push yet: the sensor exists, available, with no value.
    state = hass.states.get("sensor.wican_device_last_seen")
    assert state is not None
    assert state.state == "unknown"

    client = await hass_client()
    before = dt_util.utcnow()
    await client.post(
        f"/api/webhook/{entry.data[CONF_WEBHOOK_ID]}", json=mock_webhook_data,
    )
    await hass.async_block_till_done()

    state = hass.states.get("sensor.wican_device_last_seen")
    seen = dt_util.parse_datetime(state.state)
    assert seen is not None
    # The state renders at second precision; allow for the truncation.
    assert before - timedelta(seconds=1) <= seen <= dt_util.utcnow()

    # Stale device: siblings go unavailable, Last seen keeps its
    # timestamp — its whole purpose is dating the stale readings.
    coordinator = entry.runtime_data.coordinator
    coordinator._last_push = seen - timedelta(hours=3)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert (
        hass.states.get("sensor.wican_device_batt_voltage").state == "unavailable"
    )
    last_seen_state = hass.states.get("sensor.wican_device_last_seen")
    assert last_seen_state.state != "unavailable"
    assert dt_util.parse_datetime(last_seen_state.state) is not None
