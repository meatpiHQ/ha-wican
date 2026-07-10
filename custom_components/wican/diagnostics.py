"""Diagnostics platform for WiCAN integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_WEBHOOK_ID

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import WiCANConfigEntry


def _history_sync_diagnostics(config_entry: WiCANConfigEntry) -> dict[str, Any] | None:
    """Summarize the last history-backfill run for diagnostics."""
    result = config_entry.runtime_data.last_history_result
    if result is None:
        return None
    return {
        "ran": result.ran,
        "source": result.source,
        "rows_fetched": result.rows_fetched,
        "rows_used": result.rows_used,
        "rows_rejected": result.rows_rejected,
        "hours_imported": result.hours_imported,
        "hours_skipped_existing": result.hours_skipped_existing,
        "pids_imported": result.pids_imported,
        "pids_skipped": result.pids_skipped,
        "errors": list(result.errors),
        "watermark": result.watermark,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: WiCANConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = config_entry.runtime_data.coordinator

    # Redact sensitive data
    entry_data = dict(config_entry.data)
    if CONF_WEBHOOK_ID in entry_data:
        entry_data[CONF_WEBHOOK_ID] = "**REDACTED**"

    # Collect all WiCAN entity states
    wican_entities = {}
    for state in hass.states.async_all():
        if state.entity_id.startswith(("sensor.wican_", "binary_sensor.wican_")):
            wican_entities[state.entity_id] = {
                "state": state.state,
                "attributes": dict(state.attributes),
            }

    return {
        "entry": {
            "title": config_entry.title,
            "entry_id": config_entry.entry_id,
            "unique_id": config_entry.unique_id,
            "data": entry_data,
            "options": dict(config_entry.options),
        },
        "device_info": {
            "device_type": config_entry.data.get("device_type"),
            "profile_model": config_entry.runtime_data.device_profile.model,
            "fw_version": config_entry.data.get("fw_version"),
            "hw_version": config_entry.data.get("hw_version"),
            "device_id": config_entry.data.get("device_id"),
            "git_version": config_entry.data.get("git_version"),
            "mdns": config_entry.data.get("mdns"),
            "host": config_entry.data.get("host"),
            "ip": config_entry.data.get("ip"),
        },
        "capabilities": {
            "api_level": config_entry.runtime_data.capabilities.api_level,
            "components": sorted(config_entry.runtime_data.capabilities.components),
        },
        "history_sync": _history_sync_diagnostics(config_entry),
        "runtime_data": {
            "webhook_id": "**REDACTED**",
            "post_interval": config_entry.runtime_data.post_interval,
            "device_host": config_entry.runtime_data.device_host,
            "device_ip": config_entry.runtime_data.device_ip,
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval": str(coordinator.update_interval)
            if coordinator.update_interval
            else None,
            "data_keys": list(coordinator.data.keys()) if coordinator.data else [],
        },
        "entities": wican_entities,
        "entity_count": len(wican_entities),
    }
