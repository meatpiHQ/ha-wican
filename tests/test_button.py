"""Tests for the capability-gated MeatPi control buttons."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wican.api import DeviceCapabilities
from custom_components.wican.const import (
    API_LEVEL_V6,
    SIGNAL_CAPABILITIES_UPDATED,
)
from custom_components.wican.exceptions import MeatPiApiError

RESTART_ENTITY = "button.wican_device_restart"
SYNC_TIME_ENTITY = "button.wican_device_sync_time"

V6_FULL = DeviceCapabilities(
    api_level=API_LEVEL_V6,
    components=frozenset({"rtc_manager", "wifi_manager"}),
)
V6_NO_RTC = DeviceCapabilities(
    api_level=API_LEVEL_V6, components=frozenset({"wifi_manager"}),
)


async def _setup(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry,
) -> MockConfigEntry:
    """Set up the integration with webhook registration stubbed."""
    mock_config_entry.add_to_hass(hass)
    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    return mock_config_entry


async def test_no_buttons_on_legacy_device(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A pre-V6 device (default probe result) gets no control buttons."""
    await _setup(hass, mock_config_entry)

    assert hass.states.get(RESTART_ENTITY) is None
    assert hass.states.get(SYNC_TIME_ENTITY) is None


async def test_buttons_created_for_v6_device(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_capability_probe: MagicMock,
) -> None:
    """A V6 device with an RTC gets both control buttons."""
    mock_capability_probe.return_value.async_probe.return_value = V6_FULL

    await _setup(hass, mock_config_entry)

    assert hass.states.get(RESTART_ENTITY) is not None
    assert hass.states.get(SYNC_TIME_ENTITY) is not None


async def test_component_gated_button_absent(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_capability_probe: MagicMock,
) -> None:
    """Without the rtc_manager component only the restart button exists."""
    mock_capability_probe.return_value.async_probe.return_value = V6_NO_RTC

    await _setup(hass, mock_config_entry)

    assert hass.states.get(RESTART_ENTITY) is not None
    assert hass.states.get(SYNC_TIME_ENTITY) is None


async def test_buttons_appear_when_capabilities_arrive_later(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Buttons are added dynamically when a later probe finds the V6 API.

    This is the OTA-to-V6 scenario: the device was legacy at setup and the
    re-probe (triggered by the reported firmware change) finds the API.
    """
    entry = await _setup(hass, mock_config_entry)
    assert hass.states.get(RESTART_ENTITY) is None

    entry.runtime_data.capabilities = V6_FULL
    async_dispatcher_send(
        hass, f"{SIGNAL_CAPABILITIES_UPDATED}_{entry.entry_id}",
    )
    await hass.async_block_till_done()

    assert hass.states.get(RESTART_ENTITY) is not None
    assert hass.states.get(SYNC_TIME_ENTITY) is not None


async def test_duplicate_capability_signal_adds_once(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_capability_probe: MagicMock,
) -> None:
    """Repeated capability updates never duplicate button entities."""
    mock_capability_probe.return_value.async_probe.return_value = V6_FULL
    entry = await _setup(hass, mock_config_entry)

    for _ in range(3):
        async_dispatcher_send(
            hass, f"{SIGNAL_CAPABILITIES_UPDATED}_{entry.entry_id}",
        )
    await hass.async_block_till_done()

    buttons = [
        state
        for state in hass.states.async_all("button")
        if state.entity_id in (RESTART_ENTITY, SYNC_TIME_ENTITY)
    ]
    assert len(buttons) == 2


async def test_restart_press_calls_api(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_capability_probe: MagicMock,
) -> None:
    """Pressing restart sends the reboot command to the device."""
    mock_capability_probe.return_value.async_probe.return_value = V6_FULL
    await _setup(hass, mock_config_entry)

    await hass.services.async_call(
        "button", "press", {"entity_id": RESTART_ENTITY}, blocking=True,
    )

    mock_capability_probe.return_value.async_restart.assert_awaited_once()


async def test_sync_time_press_calls_api(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_capability_probe: MagicMock,
) -> None:
    """Pressing sync time sends the SNTP-sync command to the device."""
    mock_capability_probe.return_value.async_probe.return_value = V6_FULL
    await _setup(hass, mock_config_entry)

    await hass.services.async_call(
        "button", "press", {"entity_id": SYNC_TIME_ENTITY}, blocking=True,
    )

    mock_capability_probe.return_value.async_sync_time.assert_awaited_once()


async def test_press_failure_raises_homeassistant_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_capability_probe: MagicMock,
) -> None:
    """An API failure surfaces as a translated HomeAssistantError."""
    mock_capability_probe.return_value.async_probe.return_value = V6_FULL
    mock_capability_probe.return_value.async_restart.side_effect = MeatPiApiError(
        "HTTP 503",
    )
    await _setup(hass, mock_config_entry)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "button", "press", {"entity_id": RESTART_ENTITY}, blocking=True,
        )


async def test_press_without_api_raises(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_capability_probe: MagicMock,
) -> None:
    """A press with no usable API client raises instead of crashing."""
    mock_capability_probe.return_value.async_probe.return_value = V6_FULL
    entry = await _setup(hass, mock_config_entry)

    entry.runtime_data.api = None
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "button", "press", {"entity_id": RESTART_ENTITY}, blocking=True,
        )


async def test_buttons_survive_reload(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_capability_probe: MagicMock,
) -> None:
    """Buttons come back after a config entry reload."""
    mock_capability_probe.return_value.async_probe.return_value = V6_FULL
    entry = await _setup(hass, mock_config_entry)
    assert hass.states.get(RESTART_ENTITY) is not None

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get(RESTART_ENTITY) is not None
