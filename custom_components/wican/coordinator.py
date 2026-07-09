"""DataUpdateCoordinator for WiCAN integration."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_POST_INTERVAL,
    DEFAULT_POST_INTERVAL,
    DEVICE_STALE_FACTOR,
    DOMAIN,
    MIN_DEVICE_STALE_SECONDS,
    WICAN_DATA_UPDATE_INTERVAL,
)

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant

    from . import WiCANConfigEntry

_LOGGER = logging.getLogger(__name__)

# WiCAN is push-based via webhooks, so we don't need frequent polling
# This is just for fallback/health check
UPDATE_INTERVAL = timedelta(seconds=WICAN_DATA_UPDATE_INTERVAL)


class WiCANDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching WiCAN data."""

    config_entry: WiCANConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: WiCANConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        self.config_entry = config_entry
        self._data: dict[str, Any] = {}
        self._last_push: datetime | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
            config_entry=config_entry,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Health-check the push-based device.

        This is a push-based integration, so we don't actively poll. This
        periodic callback exists to detect when the device has stopped pushing:
        if no webhook data has arrived within the staleness window, raise
        UpdateFailed so entities become unavailable. DataUpdateCoordinator logs
        the transition to unavailable and the subsequent recovery once each
        (log-when-unavailable).
        """
        if self._last_push is None:
            # No push received yet; stay available while waiting for the device.
            return self._data

        post_interval = self.config_entry.options.get(
            CONF_POST_INTERVAL, DEFAULT_POST_INTERVAL,
        )
        threshold = max(post_interval * DEVICE_STALE_FACTOR, MIN_DEVICE_STALE_SECONDS)
        elapsed = (dt_util.utcnow() - self._last_push).total_seconds()
        if elapsed > threshold:
            raise UpdateFailed(
                f"No data received from WiCAN device in {elapsed:.0f}s "
                f"(threshold {threshold}s)",
            )

        return self._data

    async def async_config_entry_first_refresh(self) -> None:
        """Perform first refresh of the coordinator.

        For WiCAN, this is a push-based integration, so we don't poll for data.
        This method initializes the coordinator with empty data and succeeds immediately.
        Entities will be created and will update when the first webhook push arrives.
        """
        _LOGGER.debug(
            "First refresh for WiCAN coordinator (push-based, no polling required)",
        )
        # Initialize with empty data - webhook pushes will populate it
        await self.async_refresh()

    def handle_webhook_data(self, data: dict[str, Any]) -> None:
        """Handle incoming webhook data.

        This is called by the webhook handler when new data arrives.
        It updates the coordinator's data and notifies all listeners.
        """
        # Defensive: ignore malformed (non-object) payloads instead of raising,
        # so a misbehaving device cannot break state updates for others.
        if not isinstance(data, dict):
            _LOGGER.warning(
                "Ignoring non-object WiCAN webhook data (%s)", type(data).__name__,
            )
            return

        # Validate device identity before processing data
        self._validate_device_identity(data)

        # Update internal data store
        self._data.update(data)

        # Record the push time so the health-check can detect a stale device.
        self._last_push = dt_util.utcnow()

        # Notify all entities that data has been updated (also marks the
        # coordinator successful again, recovering from any stale state).
        self.async_set_updated_data(self._data)

    def _validate_device_identity(self, data: dict[str, Any]) -> None:
        """Ensure device identity hasn't changed.

        Validates that the device_id in the webhook data matches the stored
        device_id from initial configuration. This prevents a different device
        from impersonating the configured device.

        Raises:
            ConfigEntryError: If device_id mismatch is detected.
        """
        # Extract device_id from webhook data (can be in status dict or top-level)
        status = data.get("status", {})
        if not isinstance(status, dict):
            status = {}
        incoming_device_id = status.get("device_id") or data.get("device_id")

        if not incoming_device_id:
            # No device_id provided - skip validation
            # This maintains backward compatibility with older firmware
            return

        # Get stored device_id from config entry
        stored_device_id = self.config_entry.data.get("device_id")

        if not stored_device_id:
            # First time seeing device_id - this is okay
            # The webhook handler will store it in the config entry
            _LOGGER.debug(
                "No stored device_id yet, accepting incoming device_id: %s",
                incoming_device_id,
            )
            return

        # Validate device_id matches
        if incoming_device_id != stored_device_id:
            _LOGGER.error(
                "Device ID mismatch detected! Expected %s, got %s",
                stored_device_id,
                incoming_device_id,
            )
            raise ConfigEntryError(
                translation_domain=DOMAIN,
                translation_key="device_mismatch",
                translation_placeholders={
                    "expected": stored_device_id,
                    "actual": incoming_device_id,
                },
            )

        _LOGGER.debug("Device identity validated: %s", incoming_device_id)

    def normalize_sensor_value(self, key: str, raw_value: Any) -> Any:
        """Normalize raw sensor values.

        Converts string values with unit suffixes to proper numeric types.
        This centralizes value normalization logic for consistency.

        Args:
            key: Sensor key (e.g., "batt_voltage")
            raw_value: Raw value from device

        Returns:
            Normalized value suitable for Home Assistant
        """
        if raw_value is None:
            return None

        # A sensor state must be a scalar. Reject nested structures defensively
        # so a malformed device cannot push a dict/list into an entity state
        # (which Home Assistant would reject at state-write time).
        if isinstance(raw_value, (dict, list)):
            _LOGGER.debug("Ignoring non-scalar value for %s: %r", key, raw_value)
            return None

        # Battery voltage: strip "V" / " V" suffix (any case) and convert to float
        # Handles firmware variants: "12.5V", "12.5 V", "12.5v", " 12.5 V "
        if key == "batt_voltage" and isinstance(raw_value, str):
            _LOGGER.debug("Raw batt_voltage from device: %r", raw_value)
            stripped = raw_value.strip()
            if stripped.upper().endswith("V"):
                numeric_part = stripped[:-1].strip()
                try:
                    return float(numeric_part)
                except ValueError:
                    _LOGGER.warning(
                        "Failed to parse battery voltage: %r (numeric part: %r)",
                        raw_value, numeric_part,
                    )
                    return raw_value

        # Generic numeric string conversion
        if isinstance(raw_value, str):
            # Check if it looks like a number
            cleaned = raw_value.replace(".", "", 1).replace("-", "", 1)
            if cleaned.isdigit():
                try:
                    return float(raw_value) if "." in raw_value else int(raw_value)
                except ValueError:
                    pass

        return raw_value

    def get_sensor_value(self, sensor_key: str) -> Any | None:
        """Get value for a specific sensor."""
        return self._data.get(sensor_key)
