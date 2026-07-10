from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntityDescription
from homeassistant.components.sensor import SensorDeviceClass, SensorEntityDescription
from homeassistant.const import EntityCategory


@dataclass(frozen=True, kw_only=True)
class WiCANBinarySensorEntityDescription(BinarySensorEntityDescription):
    extra_attributes: list[str] | None = field(default_factory=list)
    requires_obd: bool = False
    """Only create this entity on device types that poll a vehicle
    (profile.supports_obd_pids); a GPS/LTE product has no ECU."""

@dataclass(frozen=True, kw_only=True)
class WiCANSensorEntityDescription(SensorEntityDescription):
    extra_attributes: list[str] | None = field(default_factory=list)

SENSOR_DESCRIPTIONS: tuple[WiCANSensorEntityDescription, ...] = (
    WiCANSensorEntityDescription(
        key="batt_voltage",
        translation_key="batt_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement="V",
        suggested_display_precision=1,
        extra_attributes=[
            "batt_alert",
            "batt_alert_volt",
            "batt_alert_protocol",
            "batt_alert_topic",
            "batt_alert_time",
        ],
    ),
    WiCANSensorEntityDescription(
        key="wifi_mode",
        translation_key="wifi_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        extra_attributes=[
            "ap_ch",
            "ap_auto_disable",
            "sta_status",
            "mdns",
        ],
    ),
    WiCANSensorEntityDescription(
        key="vpn_status",
        translation_key="vpn_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        extra_attributes=[
            "vpn_ip",
        ],
    ),
    WiCANSensorEntityDescription(
        key="uptime",
        translation_key="uptime",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)

BINARY_SENSOR_DESCRIPTIONS: tuple[WiCANBinarySensorEntityDescription, ...] = (
    WiCANBinarySensorEntityDescription(
        key="ecu_status",
        translation_key="ecu_status",
        requires_obd=True,
        extra_attributes=[
            "obd_chip_status",
        ],
    ),
    WiCANBinarySensorEntityDescription(
        key="ble_status",
        translation_key="ble_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        extra_attributes=[
            "ble_power",
        ],
    ),
)

def get_sensor_attributes(entity_description: Any, data: dict[str, Any]) -> dict[str, Any]:
    status = data.get("status", {}) if isinstance(data, dict) else {}
    if not isinstance(status, dict):
        return {}
    attrs = {}
    for attr_key in getattr(entity_description, "extra_attributes", None) or []:
        if status.get(attr_key) is not None:
            attrs[attr_key] = status[attr_key]
    return attrs

