"""Tests for the remote device catalog (catalog.py + devices.py registry)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.wican.catalog import (
    _async_fetch_remote,
    _CatalogManager,
    async_setup_catalog,
    parse_catalog,
)
from custom_components.wican.const import (
    CATALOG_MAX_DEVICE_TYPES,
    CATALOG_MAX_SENSORS,
    DEVICE_CATALOG_URL,
)
from custom_components.wican.devices import (
    DEVICE_TYPE_ESPNETLINK,
    DEVICE_TYPE_GENERIC,
    DEVICE_TYPE_WICAN_PRO,
    apply_catalog,
    catalog_profiles,
    get_profile,
    infer_device_type,
    is_known_device_type,
)

SOLARPI = {
    "model": "SolarPi",
    "hw_keywords": ["solarpi"],
    "supports_obd_pids": False,
    "supports_gps": False,
    "firmware": {"repo": "meatpiHQ/solarpi-fw", "asset_pattern": "solarpi-fw_*.bin"},
    "sensors": [
        {"key": "solar_watts", "name": "Solar power", "device_class": "power", "unit": "W", "diagnostic": False},
        {"key": "string_status", "name": "String status"},
    ],
}


def _catalog(**device_types: Any) -> dict[str, Any]:
    return {"schema": 1, "device_types": device_types}


# NOTE: the conftest autouse fixture patches the module attribute
# ``catalog._async_fetch_remote``; the direct import above holds the
# original function, so the fetch tests below exercise the real code
# against aioclient_mock while everything else stays off the network.

# ---------------------------------------------------------------------------
# Parsing / validation
# ---------------------------------------------------------------------------


def test_parse_valid_catalog() -> None:
    """A well-formed catalog yields fully populated profiles."""
    profiles = parse_catalog(_catalog(solarpi=SOLARPI))

    profile = profiles["solarpi"]
    assert profile.model == "SolarPi"
    assert profile.hw_version_keywords == ("solarpi",)
    assert profile.firmware_repo == "meatpiHQ/solarpi-fw"
    assert profile.firmware_asset_pattern == "solarpi-fw_*.bin"
    assert len(profile.extra_sensors) == 2
    watts = profile.extra_sensors[0]
    assert watts.key == "solar_watts"
    assert watts.device_class == "power"
    assert watts.unit == "W"
    assert watts.diagnostic is False
    # Defaults: no metadata → plain diagnostic sensor named after itself.
    status = profile.extra_sensors[1]
    assert status.diagnostic is True
    assert status.name == "String status"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "catalog",
        ["schema", 1],
        {"device_types": {}},  # missing schema
        {"schema": 2, "device_types": {}},  # future schema
        {"schema": 1, "device_types": ["not", "a", "dict"]},
        {"schema": 1},
    ],
)
def test_parse_structurally_invalid_catalog(raw: Any) -> None:
    """Structurally unusable documents are rejected wholesale."""
    assert parse_catalog(raw) is None


@pytest.mark.parametrize(
    ("slug", "entry"),
    [
        ("UPPER", SOLARPI),  # invalid slug casing
        ("bad slug", SOLARPI),
        ("x" * 40, SOLARPI),
        ("noentry", "not a dict"),
        ("nomodel", {"hw_keywords": ["a"]}),
        ("hugemodel", {"model": "x" * 200}),
    ],
)
def test_parse_drops_invalid_entries(slug: str, entry: Any) -> None:
    """Individually bad entries are dropped without poisoning the rest."""
    profiles = parse_catalog(_catalog(solarpi=SOLARPI, **{slug: entry}))
    assert set(profiles) == {"solarpi"}


def test_parse_hostile_content_is_bounded() -> None:
    """Floods and absurd fields are capped/sanitized, never fatal."""
    flood = {
        f"type_{i:03d}": {"model": f"Type {i}"}
        for i in range(CATALOG_MAX_DEVICE_TYPES * 2)
    }
    profiles = parse_catalog(_catalog(**flood))
    assert len(profiles) == CATALOG_MAX_DEVICE_TYPES

    hostile = {
        "model": "Weird",
        "hw_keywords": ["ok", 42, "x" * 500, None, "SHOUTY"] + ["k"] * 50,
        "firmware": {"repo": "not a repo!!", "asset_pattern": "x" * 500},
        "sensors": (
            [{"key": "dup", "name": "a"}, {"key": "dup", "name": "b"}]
            + [{"key": f"s{i}"} for i in range(CATALOG_MAX_SENSORS * 2)]
            + [{"key": "bad key!"}, {"key": "x" * 500}, "not a sensor", {"name": "keyless"}]
        ),
    }
    profile = parse_catalog(_catalog(weird=hostile))["weird"]
    assert profile.hw_version_keywords == ("ok", "shouty", "k")
    assert profile.firmware_repo is None  # invalid repo rejected
    assert len(profile.extra_sensors) <= CATALOG_MAX_SENSORS
    keys = [s.key for s in profile.extra_sensors]
    assert keys.count("dup") == 1
    assert profile.extra_sensors[0].name in ("a", "dup")


def test_parse_sensor_icon_validation() -> None:
    """Only mdi: icons are accepted from the catalog."""
    entry = {
        "model": "X",
        "sensors": [
            {"key": "a", "icon": "mdi:solar-power"},
            {"key": "b", "icon": "javascript:alert(1)"},
        ],
    }
    profile = parse_catalog(_catalog(x=entry))["x"]
    assert profile.extra_sensors[0].icon == "mdi:solar-power"
    assert profile.extra_sensors[1].icon is None


# ---------------------------------------------------------------------------
# Registry semantics
# ---------------------------------------------------------------------------


def test_catalog_overrides_builtin_but_not_reserved() -> None:
    """Catalog wins for open slugs; reserved slugs are immune."""
    profiles = parse_catalog(
        _catalog(
            espnetlink={"model": "ESPNetlink R2"},
            wican_pro={"model": "Evil WiCAN"},
            meatpi={"model": "Evil Generic"},
        ),
    )
    apply_catalog(profiles)

    assert get_profile(DEVICE_TYPE_ESPNETLINK).model == "ESPNetlink R2"
    assert get_profile(DEVICE_TYPE_WICAN_PRO).model == "WiCAN Pro"
    assert get_profile(DEVICE_TYPE_GENERIC).model == "MeatPi device"
    assert set(catalog_profiles()) == {DEVICE_TYPE_ESPNETLINK}


def test_unknown_catalog_slug_resolves_and_infers() -> None:
    """A catalog-only device type is known, resolvable, and inferable."""
    assert not is_known_device_type("solarpi")
    apply_catalog(parse_catalog(_catalog(solarpi=SOLARPI)))

    assert is_known_device_type("solarpi")
    assert get_profile("solarpi").model == "SolarPi"
    assert infer_device_type("SolarPi R1 rev2") == "solarpi"
    # Built-in inference is unaffected.
    assert infer_device_type("WiCAN-PRO") == DEVICE_TYPE_WICAN_PRO
    assert infer_device_type("mystery board") == "wican"


def test_short_catalog_keywords_are_token_matched() -> None:
    """A short catalog keyword cannot substring-hijack other hardware."""
    apply_catalog(
        parse_catalog(_catalog(airpi={"model": "AirPi", "hw_keywords": ["air"]})),
    )
    assert infer_device_type("dairy processor") == "wican"  # no token "air"
    assert infer_device_type("MeatPi AIR v2") == "airpi"


# ---------------------------------------------------------------------------
# Manager: load, persist, refresh
# ---------------------------------------------------------------------------


async def test_bundled_catalog_applies_on_setup(hass: HomeAssistant) -> None:
    """With no stored copy, the bundled catalog is loaded and applied."""
    with patch(
        "custom_components.wican.catalog._async_fetch_remote",
        new_callable=AsyncMock,
        return_value=None,
    ):
        await async_setup_catalog(hass)
        await hass.async_block_till_done()

    profile = get_profile(DEVICE_TYPE_ESPNETLINK)
    assert [s.key for s in profile.extra_sensors] == ["lte_rssi", "lte_operator"]
    assert profile.extra_sensors[0].unit == "dBm"


async def test_stored_catalog_wins_over_bundled(hass: HomeAssistant) -> None:
    """A previously fetched catalog is preferred to the bundled copy."""
    manager = _CatalogManager(hass)
    await manager._store.async_save(
        {"catalog": _catalog(solarpi=SOLARPI), "fetched_at": 0},
    )
    with patch(
        "custom_components.wican.catalog._async_fetch_remote",
        new_callable=AsyncMock,
        return_value=None,
    ):
        await manager.async_ensure_loaded()
        await hass.async_block_till_done()

    assert is_known_device_type("solarpi")
    # Bundled espnetlink extras absent: the stored document replaced it.
    assert not get_profile(DEVICE_TYPE_ESPNETLINK).extra_sensors


async def test_corrupt_stored_catalog_falls_back_to_bundled(
    hass: HomeAssistant,
) -> None:
    """Garbage in storage degrades to the bundled catalog."""
    manager = _CatalogManager(hass)
    await manager._store.async_save({"catalog": {"schema": 99}})
    with patch(
        "custom_components.wican.catalog._async_fetch_remote",
        new_callable=AsyncMock,
        return_value=None,
    ):
        await manager.async_ensure_loaded()
        await hass.async_block_till_done()

    assert get_profile(DEVICE_TYPE_ESPNETLINK).extra_sensors


async def test_refresh_applies_and_persists(hass: HomeAssistant) -> None:
    """A successful remote fetch applies immediately and persists."""
    manager = _CatalogManager(hass)
    with patch(
        "custom_components.wican.catalog._async_fetch_remote",
        new_callable=AsyncMock,
        return_value=_catalog(solarpi=SOLARPI),
    ):
        assert await manager.async_refresh() is True

    assert is_known_device_type("solarpi")
    stored = await manager._store.async_load()
    assert stored["catalog"]["device_types"]["solarpi"]["model"] == "SolarPi"


async def test_refresh_rejects_invalid_document(hass: HomeAssistant) -> None:
    """An invalid fetched document never replaces the current catalog."""
    apply_catalog(parse_catalog(_catalog(solarpi=SOLARPI)))
    manager = _CatalogManager(hass)
    with patch(
        "custom_components.wican.catalog._async_fetch_remote",
        new_callable=AsyncMock,
        return_value={"schema": 99},
    ):
        assert await manager.async_refresh() is False

    assert is_known_device_type("solarpi")  # untouched


async def test_refresh_rate_limited_per_run(hass: HomeAssistant) -> None:
    """ensure_loaded schedules at most one fetch per refresh interval."""
    manager = _CatalogManager(hass)
    with patch.object(
        manager, "async_refresh", new_callable=AsyncMock,
    ) as refresh:
        await manager.async_ensure_loaded()
        await manager.async_ensure_loaded()
        await hass.async_block_till_done()

    assert refresh.await_count == 1


async def test_setup_catalog_never_raises(hass: HomeAssistant) -> None:
    """Even a broken storage layer cannot break integration setup."""
    with patch(
        "homeassistant.helpers.storage.Store.async_load",
        side_effect=OSError("storage exploded"),
    ):
        await async_setup_catalog(hass)  # must not raise


def test_parse_keywords_and_sensors_wrong_container_types() -> None:
    """Non-list keyword/sensor containers degrade to empty, never raise."""
    entry = {"model": "X", "hw_keywords": "not a list", "sensors": {"key": "a"}}
    profile = parse_catalog(_catalog(x=entry))["x"]
    assert profile.hw_version_keywords == ()
    assert profile.extra_sensors == ()


def test_parse_edge_branches() -> None:
    """Keyword overflow break, sensor non-dict/bool fields, non-str slugs."""
    entry = {
        "model": "X",
        # More than the cap of VALID keywords → the break path.
        "hw_keywords": [f"kw{i:02d}" for i in range(20)],
        "sensors": [
            "not a dict",  # non-dict sensor entry
            {"key": "ok", "diagnostic": "yes"},  # non-bool diagnostic
        ],
    }
    profile = parse_catalog(_catalog(x=entry))["x"]
    assert len(profile.hw_version_keywords) == 8
    assert len(profile.extra_sensors) == 1
    assert profile.extra_sensors[0].diagnostic is True

    # Non-string slug keys are dropped (JSON can't produce them, but a
    # stored/hand-edited document could).
    profiles = parse_catalog({"schema": 1, "device_types": {1: {"model": "N"}, "ok_type": {"model": "OK"}}})
    assert set(profiles) == {"ok_type"}


async def test_bundled_catalog_unreadable_degrades(hass: HomeAssistant) -> None:
    """No stored copy + unreadable bundled file → built-ins only, no crash."""
    manager = _CatalogManager(hass)
    with (
        patch(
            "custom_components.wican.catalog._load_bundled_catalog",
            return_value=None,
        ),
        patch(
            "custom_components.wican.catalog._async_fetch_remote",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        await manager.async_ensure_loaded()
        await hass.async_block_till_done()

    assert catalog_profiles() == {}
    assert get_profile(DEVICE_TYPE_ESPNETLINK).model == "ESPNetlink"  # built-in


def test_bundled_catalog_read_failure_returns_none() -> None:
    """A broken bundled file read is contained."""
    from pathlib import Path

    from custom_components.wican.catalog import _load_bundled_catalog

    with patch.object(Path, "read_text", side_effect=OSError("gone")):
        assert _load_bundled_catalog() is None


async def test_refresh_persist_failure_still_applies(hass: HomeAssistant) -> None:
    """A storage failure loses only the cache; the catalog still applies."""
    manager = _CatalogManager(hass)
    with (
        patch(
            "custom_components.wican.catalog._async_fetch_remote",
            new_callable=AsyncMock,
            return_value=_catalog(solarpi=SOLARPI),
        ),
        patch(
            "homeassistant.helpers.storage.Store.async_save",
            side_effect=OSError("disk full"),
        ),
    ):
        assert await manager.async_refresh() is True

    assert is_known_device_type("solarpi")


def test_catalog_sensor_description_oversized_unit_dropped() -> None:
    """Defense in depth: an absurd unit is dropped at description build."""
    from custom_components.wican.devices import CatalogSensorDef
    from custom_components.wican.sensor import _build_catalog_sensor_description

    description = _build_catalog_sensor_description(
        CatalogSensorDef(key="x", name="X", unit="u" * 200),
    )
    assert description.native_unit_of_measurement is None


# ---------------------------------------------------------------------------
# End-to-end: a catalog-defined product through the device simulator
# ---------------------------------------------------------------------------


async def _seed_stored_catalog(hass: HomeAssistant, doc: dict[str, Any]) -> None:
    """Persist a catalog document as if a previous fetch had stored it."""
    from homeassistant.helpers.storage import Store

    from custom_components.wican.const import (
        CATALOG_STORAGE_KEY,
        CATALOG_STORAGE_VERSION,
    )

    await Store(hass, CATALOG_STORAGE_VERSION, CATALOG_STORAGE_KEY).async_save(
        {"catalog": doc, "fetched_at": 0},
    )


async def test_catalog_defined_device_end_to_end(
    hass: HomeAssistant, hass_client: Any,
) -> None:
    """A brand-new product defined only in the catalog works first-class.

    No integration code knows "solarpi": the profile, model name, and
    product sensors all come from the (stored) catalog document.
    """
    from tests.device_sim import MeatPiDeviceSimulator

    await _seed_stored_catalog(hass, _catalog(solarpi=SOLARPI))

    sim = MeatPiDeviceSimulator.from_preset(
        hass, hass_client, "meatpi_generic",
        title="SolarPi Sim", hw_version="SolarPi R1",
    )
    await sim.async_setup(device_type="solarpi")

    assert sim.entry.runtime_data.device_profile.model == "SolarPi"

    await sim.push_and_settle(
        sim.status(solar_watts=512.5, string_status="nominal"),
    )

    watts = hass.states.get("sensor.solarpi_sim_solar_power")
    assert watts is not None
    assert float(watts.state) == 512.5
    assert watts.attributes["unit_of_measurement"] == "W"
    assert watts.attributes["device_class"] == "power"
    status = hass.states.get("sensor.solarpi_sim_string_status")
    assert status is not None
    assert status.state == "nominal"


async def test_catalog_inference_from_hw_version_end_to_end(
    hass: HomeAssistant, hass_client: Any,
) -> None:
    """A catalog device without a device_type TXT record is inferred."""
    from tests.device_sim import MeatPiDeviceSimulator

    await _seed_stored_catalog(hass, _catalog(solarpi=SOLARPI))

    sim = MeatPiDeviceSimulator.from_preset(
        hass, hass_client, "meatpi_generic",
        title="SolarPi Inferred", hw_version="SolarPi R1",
    )
    await sim.async_setup()  # no device_type: inferred from hw_version

    assert sim.entry.data["device_type"] == "solarpi"


async def test_bundled_espnetlink_lte_sensors_end_to_end(
    hass: HomeAssistant, hass_client: Any,
) -> None:
    """The bundled catalog gives ESPNetlink its LTE sensors out of the box."""
    from tests.device_sim import MeatPiDeviceSimulator

    sim = MeatPiDeviceSimulator.from_preset(hass, hass_client, "espnetlink")
    await sim.async_setup()
    await sim.push_and_settle(sim.status())

    rssi = hass.states.get("sensor.espnetlink_sim_lte_signal")
    assert rssi is not None
    assert float(rssi.state) == -71
    assert rssi.attributes["unit_of_measurement"] == "dBm"
    operator = hass.states.get("sensor.espnetlink_sim_lte_operator")
    assert operator is not None
    assert operator.state == "TestNet"


# ---------------------------------------------------------------------------
# Remote fetch (real function against the aiohttp mocker)
# ---------------------------------------------------------------------------


async def test_fetch_remote_success(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    """A healthy fetch returns the decoded document."""
    aioclient_mock.get(DEVICE_CATALOG_URL, json=_catalog(solarpi=SOLARPI))
    raw = await _async_fetch_remote(hass)
    assert raw["device_types"]["solarpi"]["model"] == "SolarPi"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"status": 404, "text": "gone"}, "http error"),
        ({"text": "not json"}, "garbage body"),
        ({"exc": asyncio.TimeoutError}, "timeout"),
        (
            {"json": {"schema": 1}, "headers": {"Content-Length": str(10**9)}},
            "oversized declared",
        ),
        ({"text": "x" * (300 * 1024)}, "oversized body"),
    ],
)
async def test_fetch_remote_failures_return_none(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    kwargs: dict[str, Any],
    reason: str,
) -> None:
    """Every fetch failure mode degrades to None, never an exception."""
    aioclient_mock.get(DEVICE_CATALOG_URL, **kwargs)
    assert await _async_fetch_remote(hass) is None, reason


def test_reserved_slug_catalog_entry_extends_sensors_only() -> None:
    """A reserved slug in the catalog may ADD sensors, nothing else.

    WiCAN entries live in the catalog for consistency and to ship new
    diagnostic sensors without an integration release — but their
    identity, capability flags, and firmware matching stay code-defined,
    so a catalog merge can never change behavior installs depend on.
    """
    profiles = parse_catalog(
        _catalog(
            wican_pro={
                "model": "Renamed Pro",          # ignored (identity)
                "hw_keywords": ["hijack"],        # ignored (inference)
                "supports_obd_pids": False,       # ignored (capability)
                "supports_gps": False,            # ignored (capability)
                "sensors": [
                    {"key": "pro_diag", "name": "Pro diagnostic", "unit": "%"},
                ],
            },
        ),
    )
    apply_catalog(profiles)

    profile = get_profile(DEVICE_TYPE_WICAN_PRO)
    # Only the sensor was adopted...
    sensor_keys = [sensor.key for sensor in profile.extra_sensors]
    assert "pro_diag" in sensor_keys
    # ...identity and capabilities remain the built-in's.
    assert profile.model == "WiCAN Pro"
    assert profile.supports_obd_pids is True
    assert infer_device_type("hijack board") == "wican"


def test_reserved_slug_without_new_sensors_is_not_installed() -> None:
    """A reserved entry adding nothing resolves straight to the built-in."""
    apply_catalog(parse_catalog(_catalog(wican={"model": "WiCAN OBD"})))
    assert "wican" not in catalog_profiles()
    assert get_profile("wican").model == "WiCAN OBD"


async def test_bundled_wican_diagnostic_sensors_create_entities(
    hass, init_integration, mock_webhook_data, hass_client,
) -> None:
    """The bundled catalog's WiCAN diagnostic sensors become live entities.

    First real use of extend-only reserved entries: sleep_mode,
    can_protocol, ecu_pids_num, and loop_status ship via the catalog,
    not via code.
    """
    from homeassistant.const import CONF_WEBHOOK_ID

    entry = init_integration
    client = await hass_client()
    await client.post(
        f"/api/webhook/{entry.data[CONF_WEBHOOK_ID]}", json=mock_webhook_data,
    )
    await hass.async_block_till_done()

    assert hass.states.get("sensor.wican_device_sleep_mode").state == "off"
    assert hass.states.get("sensor.wican_device_can_protocol").state == "Auto"
    assert hass.states.get("sensor.wican_device_ecu_pid_count").state == "5"
    assert hass.states.get("sensor.wican_device_autopid_loop").state == "Stopped"
