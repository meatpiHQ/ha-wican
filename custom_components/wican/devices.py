"""Device-type profiles for MeatPi products.

MeatPi is the brand; each product (WiCAN OBD/USB/Pro, ESPNetlink, ...) is a
device type described by a declarative profile. Adding a new product means
adding a profile here (see notes/device-contract/ADDING_A_DEVICE.md) — the rest of the
integration keys off the profile and the runtime capability probe.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import re

from .const import MANUFACTURER

# Device-type slugs stored in the config entry ("device_type" key).
DEVICE_TYPE_WICAN = "wican"
DEVICE_TYPE_WICAN_USB = "wican_usb"
DEVICE_TYPE_WICAN_PRO = "wican_pro"
DEVICE_TYPE_ESPNETLINK = "espnetlink"
DEVICE_TYPE_GENERIC = "meatpi"


@dataclass(frozen=True, kw_only=True)
class CatalogSensorDef:
    """A declarative sensor definition from the device catalog.

    Maps a key in the device's pushed ``status`` object to a sensor
    entity. Purely data — it feeds the existing, hardened sensor
    machinery and can never ship behavior.
    """

    key: str
    name: str
    device_class: str | None = None
    unit: str | None = None
    diagnostic: bool = True
    icon: str | None = None


@dataclass(frozen=True, kw_only=True)
class MeatPiDeviceProfile:
    """Declarative description of one MeatPi product.

    A profile carries only what is static per product line. Everything a
    running device can tell us (firmware components, API level) comes from
    the capability probe instead, so firmware evolution never requires a
    profile change.
    """

    device_type: str
    """Slug stored in the config entry; key into DEVICE_PROFILES."""

    model: str
    """Display model used in the device registry when the device has not
    reported a hardware version yet."""

    manufacturer: str = MANUFACTURER

    firmware_repo: str | None = None
    """GitHub ``owner/repo`` that publishes OTA firmware for this product,
    or None when the product has no firmware-update entity yet."""

    hw_version_keywords: tuple[str, ...] = ()
    """Lowercase substrings of the device-reported ``hw_version`` that
    identify this product (most specific profile wins, see
    :func:`infer_device_type`)."""

    supports_obd_pids: bool = False
    """Whether the product pushes ``autopid_data`` (dynamic PID sensors)."""

    supports_gps: bool = False
    """Whether the product can push a ``gps`` section (device tracker)."""

    firmware_asset_pattern: str | None = None
    """Glob-style release-asset name pattern for OTA updates (catalog
    field; reserved for the update platform, not yet consumed)."""

    extra_sensors: tuple[CatalogSensorDef, ...] = ()
    """Product-specific sensors from the device catalog, keyed off the
    pushed ``status`` object."""


DEVICE_PROFILES: dict[str, MeatPiDeviceProfile] = {
    DEVICE_TYPE_WICAN: MeatPiDeviceProfile(
        device_type=DEVICE_TYPE_WICAN,
        model="WiCAN OBD",
        firmware_repo="meatpiHQ/wican-fw",
        hw_version_keywords=("wican",),
        supports_obd_pids=True,
        supports_gps=False,
    ),
    DEVICE_TYPE_WICAN_USB: MeatPiDeviceProfile(
        device_type=DEVICE_TYPE_WICAN_USB,
        model="WiCAN USB",
        firmware_repo="meatpiHQ/wican-fw",
        hw_version_keywords=("usb",),
        supports_obd_pids=True,
        supports_gps=False,
    ),
    DEVICE_TYPE_WICAN_PRO: MeatPiDeviceProfile(
        device_type=DEVICE_TYPE_WICAN_PRO,
        model="WiCAN Pro",
        firmware_repo="meatpiHQ/wican-fw",
        hw_version_keywords=("pro",),
        supports_obd_pids=True,
        supports_gps=True,
    ),
    DEVICE_TYPE_ESPNETLINK: MeatPiDeviceProfile(
        device_type=DEVICE_TYPE_ESPNETLINK,
        model="ESPNetlink",
        # Repo to be confirmed when the product firmware is published.
        firmware_repo=None,
        hw_version_keywords=("espnetlink", "netlink"),
        supports_obd_pids=False,
        supports_gps=True,
    ),
    # Fallback profile: any future MeatPi device that speaks the V6-style
    # HTTP API and webhook contract works out of the box under this profile
    # until it gets a dedicated one.
    DEVICE_TYPE_GENERIC: MeatPiDeviceProfile(
        device_type=DEVICE_TYPE_GENERIC,
        model="MeatPi device",
        firmware_repo=None,
        hw_version_keywords=(),
        supports_obd_pids=True,
        supports_gps=True,
    ),
}

# Inference order matters: the most specific keywords must be checked first
# ("WiCAN-PRO" contains both "wican" and "pro").
_INFERENCE_ORDER = (
    DEVICE_TYPE_ESPNETLINK,
    DEVICE_TYPE_WICAN_PRO,
    DEVICE_TYPE_WICAN_USB,
    DEVICE_TYPE_WICAN,
)

# The WiCAN-family slugs. Hardware-version inference may move an entry
# between these freely, but must never overwrite a device_type outside the
# family (e.g. "espnetlink" or "meatpi" set from discovery TXT records).
WICAN_FAMILY_TYPES = frozenset(
    {DEVICE_TYPE_WICAN, DEVICE_TYPE_WICAN_USB, DEVICE_TYPE_WICAN_PRO},
)

# Slugs whose behavior is coupled to integration code (firmware-asset
# matching in update.py, OBD gating, the generic fallback): the remote
# device catalog can never redefine these.
RESERVED_DEVICE_TYPES = frozenset(
    {*WICAN_FAMILY_TYPES, DEVICE_TYPE_GENERIC},
)

# Device-type profiles from the remote catalog (catalog.py). Replaced
# atomically by apply_catalog(); consulted after the built-ins.
_CATALOG_PROFILES: dict[str, MeatPiDeviceProfile] = {}


def apply_catalog(profiles: dict[str, MeatPiDeviceProfile]) -> None:
    """Install catalog-defined profiles (reserved slugs are extend-only).

    Catalog entries take precedence over same-named built-ins (the catalog
    is the living source; built-ins are the offline fallback) — except the
    reserved slugs, whose identity, capability flags, and firmware
    matching are code-coupled and never come from the catalog. Reserved
    entries may only ADD sensors: a catalog merge can grow a WiCAN's
    diagnostic surface, but can never alter behavior existing installs
    depend on.
    """
    global _CATALOG_PROFILES  # noqa: PLW0603 — module-level registry by design
    installed: dict[str, MeatPiDeviceProfile] = {}
    for slug, profile in profiles.items():
        if slug not in RESERVED_DEVICE_TYPES:
            installed[slug] = profile
            continue
        builtin = DEVICE_PROFILES[slug]
        known_keys = {sensor.key for sensor in builtin.extra_sensors}
        added = tuple(
            sensor
            for sensor in profile.extra_sensors
            if sensor.key not in known_keys
        )
        if added:
            installed[slug] = replace(
                builtin, extra_sensors=builtin.extra_sensors + added,
            )
    _CATALOG_PROFILES = installed


def catalog_profiles() -> dict[str, MeatPiDeviceProfile]:
    """Return the currently installed catalog profiles (a copy)."""
    return dict(_CATALOG_PROFILES)


def is_known_device_type(device_type: object) -> bool:
    """Return True when the slug maps to a built-in or catalog profile."""
    return isinstance(device_type, str) and (
        device_type in DEVICE_PROFILES or device_type in _CATALOG_PROFILES
    )

# Short, ambiguous keywords must match a whole token of the hardware string
# ("WiCAN-PRO" → yes; "MeatPi ProtoBoard" → no). Longer brand keywords match
# as plain substrings. The same rule protects catalog-defined keywords.
_TOKEN_ONLY_KEYWORDS = frozenset({"pro", "usb"})
_MIN_SUBSTRING_KEYWORD_LENGTH = 4


def _keyword_matches(keyword: str, hw_lower: str, tokens: frozenset[str]) -> bool:
    if (
        keyword in _TOKEN_ONLY_KEYWORDS
        or len(keyword) < _MIN_SUBSTRING_KEYWORD_LENGTH
    ):
        return keyword in tokens
    return keyword in hw_lower


def infer_device_type(hw_version: object) -> str:
    """Infer the device-type slug from a device-reported hardware version.

    Existing installs predate the device_type key and are all WiCAN
    variants, so anything unrecognized (including a missing hw_version)
    defaults to the base WiCAN profile — never to the generic one, which
    would silently drop OBD-specific behavior for current users.
    """
    if not isinstance(hw_version, str):
        return DEVICE_TYPE_WICAN

    hw_lower = hw_version.lower()
    tokens = frozenset(re.split(r"[^a-z0-9]+", hw_lower))

    def _matches(profile: MeatPiDeviceProfile) -> bool:
        return any(
            _keyword_matches(keyword, hw_lower, tokens)
            for keyword in profile.hw_version_keywords
        )

    # Catalog profiles override same-named built-ins; then the built-in
    # specific types; unmatched catalog types come before the wican
    # default (they are more specific than the fallback).
    for device_type in _INFERENCE_ORDER:
        if device_type == DEVICE_TYPE_WICAN:
            for slug in sorted(_CATALOG_PROFILES):
                if _matches(_CATALOG_PROFILES[slug]):
                    return slug
        candidate = _CATALOG_PROFILES.get(device_type) or DEVICE_PROFILES[device_type]
        if _matches(candidate):
            return device_type

    return DEVICE_TYPE_WICAN


def get_profile(device_type: object) -> MeatPiDeviceProfile:
    """Return the profile for a stored device-type slug.

    Catalog profiles win over same-named built-ins (except reserved slugs,
    which never enter the catalog registry). Unknown slugs (from a newer
    integration version's entry, or manual edits) fall back to the generic
    profile so the entry still loads.
    """
    if isinstance(device_type, str):
        if device_type in _CATALOG_PROFILES:
            return _CATALOG_PROFILES[device_type]
        if device_type in DEVICE_PROFILES:
            return DEVICE_PROFILES[device_type]
    return DEVICE_PROFILES[DEVICE_TYPE_GENERIC]
