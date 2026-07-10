"""Tests for the MeatPi device-type profile framework."""

from __future__ import annotations

import pytest

from custom_components.wican.const import MANUFACTURER
from custom_components.wican.devices import (
    DEVICE_PROFILES,
    DEVICE_TYPE_ESPNETLINK,
    DEVICE_TYPE_GENERIC,
    DEVICE_TYPE_WICAN,
    DEVICE_TYPE_WICAN_PRO,
    DEVICE_TYPE_WICAN_USB,
    get_profile,
    infer_device_type,
)


@pytest.mark.parametrize(
    ("hw_version", "expected"),
    [
        ("WiCAN-PRO", DEVICE_TYPE_WICAN_PRO),
        ("wican-pro v1.2", DEVICE_TYPE_WICAN_PRO),
        ("WiCAN-USB", DEVICE_TYPE_WICAN_USB),
        ("WiCAN-OBD", DEVICE_TYPE_WICAN),
        ("WiCAN v3.1", DEVICE_TYPE_WICAN),
        ("ESPNetlink R1", DEVICE_TYPE_ESPNETLINK),
        ("espnetlink", DEVICE_TYPE_ESPNETLINK),
        # Unknown / legacy hardware strings default to the base WiCAN profile
        # (every install that predates device types is a WiCAN variant).
        ("v3.1", DEVICE_TYPE_WICAN),
        ("", DEVICE_TYPE_WICAN),
    ],
)
def test_infer_device_type(hw_version: str, expected: str) -> None:
    """Hardware version strings map to the right device type."""
    assert infer_device_type(hw_version) == expected


@pytest.mark.parametrize("hw_version", [None, 42, 3.1, True, ["WiCAN-PRO"]])
def test_infer_device_type_non_string(hw_version: object) -> None:
    """Non-string hardware versions (glitching device) default to WiCAN."""
    assert infer_device_type(hw_version) == DEVICE_TYPE_WICAN


def test_get_profile_known_types() -> None:
    """Every registered device type resolves to its own profile."""
    for device_type, profile in DEVICE_PROFILES.items():
        assert get_profile(device_type) is profile
        assert profile.device_type == device_type


def test_get_profile_unknown_falls_back_to_generic() -> None:
    """Unknown slugs (future integration versions, manual edits) still load."""
    assert get_profile("frobnicator").device_type == DEVICE_TYPE_GENERIC
    assert get_profile(None).device_type == DEVICE_TYPE_GENERIC
    assert get_profile(123).device_type == DEVICE_TYPE_GENERIC


def test_profiles_are_well_formed() -> None:
    """Profiles carry the fields the integration relies on."""
    for profile in DEVICE_PROFILES.values():
        assert profile.model
        assert profile.manufacturer == MANUFACTURER
        # Firmware repos, when set, are owner/repo GitHub coordinates.
        if profile.firmware_repo is not None:
            assert profile.firmware_repo.count("/") == 1


def test_wican_profiles_support_obd_pids() -> None:
    """All WiCAN variants push autopid data; ESPNetlink does not."""
    assert DEVICE_PROFILES[DEVICE_TYPE_WICAN].supports_obd_pids
    assert DEVICE_PROFILES[DEVICE_TYPE_WICAN_USB].supports_obd_pids
    assert DEVICE_PROFILES[DEVICE_TYPE_WICAN_PRO].supports_obd_pids
    assert not DEVICE_PROFILES[DEVICE_TYPE_ESPNETLINK].supports_obd_pids
    assert DEVICE_PROFILES[DEVICE_TYPE_ESPNETLINK].supports_gps


def test_inference_specificity_order() -> None:
    """Composite hardware strings resolve to the most specific profile."""
    # "pro" and "usb" both contain "wican" family markers; the specific
    # variant must win over the base profile.
    assert infer_device_type("WiCAN-PRO-USB prototype") == DEVICE_TYPE_WICAN_PRO
    assert infer_device_type("MeatPi WiCAN usb rev2") == DEVICE_TYPE_WICAN_USB
