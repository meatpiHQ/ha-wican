"""Tests for the param_loader module."""

from __future__ import annotations

import pytest

from custom_components.wican.param_loader import (
    get_param_unit,
    get_param_device_class,
    get_param_icon,
    get_param_description,
    is_binary_sensor,
    get_all_params,
     is_valid_device_class,
    is_valid_class_unit_combo,
    DEFAULT_PARAM_ICON,
)


class TestGetParamUnit:
    """Tests for get_param_unit function."""

    def test_known_param_uppercase(self) -> None:
        """Test known parameter with uppercase name."""
        assert get_param_unit("SOC") == "%"
        assert get_param_unit("HV_V") == "V"
        assert get_param_unit("SPEED") == "km/h"

    def test_known_param_lowercase(self) -> None:
        """Test known parameter with lowercase name (case-insensitive)."""
        assert get_param_unit("soc") == "%"
        assert get_param_unit("hv_v") == "V"
        assert get_param_unit("speed") == "km/h"

    def test_known_param_mixed_case(self) -> None:
        """Test known parameter with mixed case."""
        assert get_param_unit("SoC") == "%"
        assert get_param_unit("Hv_V") == "V"

    def test_unknown_param_returns_none(self) -> None:
        """Test unknown parameter returns None."""
        assert get_param_unit("unknown_pid") is None
        assert get_param_unit("NONEXISTENT") is None

    def test_param_with_none_unit_returns_none(self) -> None:
        """Test parameter with empty/none unit returns None."""
        # GEAR has unit="" in params.json
        assert get_param_unit("GEAR") is None


class TestGetParamDeviceClass:
    """Tests for get_param_device_class function."""

    def test_known_param_with_class(self) -> None:
        """Test known parameter with valid device class."""
        assert get_param_device_class("SOC") == "battery"
        assert get_param_device_class("HV_V") == "voltage"
        assert get_param_device_class("COOLANT_TMP") == "temperature"
        assert get_param_device_class("SPEED") == "speed"

    def test_param_with_none_class_returns_none(self) -> None:
        """Test parameter with class='none' returns None."""
        assert get_param_device_class("ENGINE_RPM") == "frequency"
        # THROTTLE has class="none"
        assert get_param_device_class("THROTTLE") is None

    def test_unknown_param_returns_none(self) -> None:
        """Test unknown parameter returns None."""
        assert get_param_device_class("unknown_pid") is None


class TestGetParamIcon:
    """Tests for get_param_icon function."""

    def test_icon_from_device_class(self) -> None:
        """Test icon resolution from device class."""
        assert get_param_icon("any_param", "temperature") == "mdi:thermometer"
        assert get_param_icon("any_param", "voltage") == "mdi:flash"
        assert get_param_icon("any_param", "battery") == "mdi:battery"

    def test_icon_from_param_name(self) -> None:
        """Test icon resolution from parameter name."""
        assert get_param_icon("soc", None) == "mdi:battery"
        assert get_param_icon("hv_v", None) == "mdi:battery-high"
        assert get_param_icon("engine_rpm", None) == "mdi:engine"
        assert get_param_icon("fuel", None) == "mdi:fuel"

    def test_icon_device_class_takes_priority(self) -> None:
        """Test that device class icon takes priority over param name."""
        # Even though "soc" has its own icon, temperature device class should win
        assert get_param_icon("soc", "temperature") == "mdi:thermometer"

    def test_unknown_param_returns_default(self) -> None:
        """Test unknown parameter returns default icon."""
        assert get_param_icon("unknown_xyz", None) == DEFAULT_PARAM_ICON

    def test_unknown_device_class_falls_back_to_param_name(self) -> None:
        """Test unknown device class falls back to param name icon."""
        assert get_param_icon("soc", "unknown_class") == "mdi:battery"


class TestGetParamDescription:
    """Tests for get_param_description function."""

    def test_known_param_description(self) -> None:
        """Test getting description for known parameter."""
        assert get_param_description("SOC") == "State Of Charge"
        assert get_param_description("HV_V") == "High Voltage Battery Voltage"
        assert get_param_description("COOLANT_TMP") == "Coolant Temperature"

    def test_unknown_param_returns_none(self) -> None:
        """Test unknown parameter returns None."""
        assert get_param_description("unknown_pid") is None


class TestIsBinarySensor:
    """Tests for is_binary_sensor function."""

    def test_binary_sensor_params(self) -> None:
        """Test parameters defined as binary sensors."""
        assert is_binary_sensor("CHARGING") is True
        assert is_binary_sensor("CHARGER_CONNECTED") is True
        assert is_binary_sensor("READY") is True
        assert is_binary_sensor("PARK_BRAKE") is True

    def test_non_binary_sensor_params(self) -> None:
        """Test parameters that are not binary sensors."""
        assert is_binary_sensor("SOC") is False
        assert is_binary_sensor("HV_V") is False
        assert is_binary_sensor("SPEED") is False

    def test_unknown_param_returns_false(self) -> None:
        """Test unknown parameter returns False."""
        assert is_binary_sensor("unknown_pid") is False


class TestGetAllParams:
    """Tests for get_all_params function."""

    def test_returns_dict(self) -> None:
        """Test that get_all_params returns a dictionary."""
        params = get_all_params()
        assert isinstance(params, dict)

    def test_contains_known_params(self) -> None:
        """Test that returned dict contains known parameters."""
        params = get_all_params()
        assert "SOC" in params
        assert "HV_V" in params
        assert "SPEED" in params
        assert "COOLANT_TMP" in params

    def test_returns_copy(self) -> None:
        """Test that get_all_params returns a copy, not the original."""
        params1 = get_all_params()
        params2 = get_all_params()
        # Modifying one should not affect the other
        params1["TEST_KEY"] = {"description": "test"}
        assert "TEST_KEY" not in params2


class TestEvParameters:
    """Test EV-specific parameters from params.json."""

    def test_ev_battery_params(self) -> None:
        """Test EV battery parameters are loaded correctly."""
        assert get_param_unit("SOC") == "%"
        assert get_param_unit("SOH") == "%"
        assert get_param_unit("HV_V") == "V"
        assert get_param_unit("HV_A") == "A"
        assert get_param_unit("HV_W") == "W"
        assert get_param_unit("HV_CAPACITY") == "Ah"
        assert get_param_unit("HV_CAPACITY_KWH") == "kWh"

    def test_ev_charging_params(self) -> None:
        """Test EV charging parameters are loaded correctly."""
        assert get_param_unit("CHARGER_DC_PWR") == "kW"
        assert get_param_unit("KWH_CHARGED") == "kWh"
        assert get_param_unit("AC_C_C") == "A"
        assert get_param_unit("AC_C_V") == "V"

    def test_ev_range_params(self) -> None:
        """Test EV range parameters are loaded correctly."""
        assert get_param_unit("RANGE") == "km"
        assert get_param_unit("DIST_SINCE_FULL_CHARGE") == "km"

    def test_ev_temperature_params(self) -> None:
        """Test EV temperature parameters are loaded correctly."""
        for i in range(1, 6):
            assert get_param_unit(f"HV_T_{i}") == "°C"
        assert get_param_unit("HV_T_A") == "°C"
        assert get_param_unit("HV_T_MAX") == "°C"
        assert get_param_unit("HV_T_MIN") == "°C"


class TestIceParameters:
    """Test ICE (Internal Combustion Engine) parameters from params.json."""

    def test_engine_params(self) -> None:
        """Test engine parameters are loaded correctly."""
        assert get_param_unit("ENGINE_RPM") == "RPM"
        assert get_param_unit("SPEED") == "km/h"
        assert get_param_unit("COOLANT_TMP") == "°C"
        assert get_param_unit("THROTTLE") == "%"

    def test_fuel_params(self) -> None:
        """Test fuel parameters are loaded correctly."""
        assert get_param_unit("FUEL") == "%"
        assert get_param_unit("FUEL_PRESSURE") == "kPa"
        assert get_param_unit("FUEL_RATE") == "g/s"

    def test_tyre_params(self) -> None:
        """Test tyre parameters are loaded correctly."""
        for pos in ["FL", "FR", "RL", "RR"]:
            assert get_param_unit(f"TYRE_P_{pos}") == "psi"
            assert get_param_unit(f"TYRE_T_{pos}") == "°C"


class TestIsValidDeviceClass:
    """Tests for is_valid_device_class function."""

    def test_valid_device_classes(self) -> None:
        """Test valid HA device classes return True."""
        valid_classes = [
            "temperature", "voltage", "current", "power", "energy",
            "battery", "speed", "distance", "pressure", "humidity",
            "frequency", "duration",
        ]
        for dc in valid_classes:
            assert is_valid_device_class(dc) is True, f"{dc} should be valid"

    def test_invalid_device_classes(self) -> None:
        """Test invalid/firmware-specific device classes return False."""
        invalid_classes = [
            "invalid_class",
            "unknown",
            "none",            # "none" string should be invalid
            "encoded",         # Firmware uses this for some PIDs
            "not_a_class",
        ]
        for dc in invalid_classes:
            assert is_valid_device_class(dc) is False, f"{dc} should be invalid"

    def test_none_returns_false(self) -> None:
        """Test None returns False."""
        assert is_valid_device_class(None) is False

    def test_empty_string_returns_false(self) -> None:
        """Test empty string returns False."""
        assert is_valid_device_class("") is False


class TestIsValidClassUnitCombo:
    """Tests for is_valid_class_unit_combo function."""

    def test_valid_speed_units(self) -> None:
        """Test valid speed + unit combinations."""
        valid_combos = [
            ("speed", "km/h"),
            ("speed", "mph"),
            ("speed", "m/s"),
        ]
        for dc, unit in valid_combos:
            assert is_valid_class_unit_combo(dc, unit) is True, f"{dc}+{unit} should be valid"

    def test_invalid_speed_rpm_combo(self) -> None:
        """Test speed + rpm is invalid (this causes HA statistics issues)."""
        assert is_valid_class_unit_combo("speed", "rpm") is False
        assert is_valid_class_unit_combo("speed", "RPM") is False

    def test_valid_temperature_units(self) -> None:
        """Test valid temperature + unit combinations."""
        valid_combos = [
            ("temperature", "°C"),
            ("temperature", "°F"),
            ("temperature", "K"),
            ("temperature", "degC"),
        ]
        for dc, unit in valid_combos:
            assert is_valid_class_unit_combo(dc, unit) is True, f"{dc}+{unit} should be valid"

    def test_valid_pressure_units(self) -> None:
        """Test valid pressure + unit combinations."""
        valid_combos = [
            ("pressure", "kPa"),
            ("pressure", "bar"),
            ("pressure", "psi"),
            ("pressure", "hPa"),
        ]
        for dc, unit in valid_combos:
            assert is_valid_class_unit_combo(dc, unit) is True, f"{dc}+{unit} should be valid"

    def test_none_values_return_true(self) -> None:
        """Test None values return True (no validation possible)."""
        assert is_valid_class_unit_combo(None, "km/h") is True
        assert is_valid_class_unit_combo("speed", None) is True
        assert is_valid_class_unit_combo(None, None) is True

    def test_unknown_class_returns_true(self) -> None:
        """Test unknown device class returns True (no rules to validate)."""
        assert is_valid_class_unit_combo("unknown_class", "unknown_unit") is True


class TestPidAliasNormalization:
    """Tests for PID alias normalization (OBD-II naming variants)."""

    def test_obd_hex_prefix_enginerpm(self) -> None:
        """Test 0C-EngineRPM variants normalize correctly."""
        # All these should map to ENGINE_RPM and get its unit
        variants = [
            "0c-enginerpm",
            "0c_enginerpm",
            "0C-EngineRPM",
            "enginerpm",
            "engine_rpm",
            "EngineRPM",
        ]
        for variant in variants:
            unit = get_param_unit(variant)
            assert unit == "RPM", f"{variant} should have unit RPM, got {unit}"

    def test_obd_hex_prefix_vehiclespeed(self) -> None:
        """Test 0D-VehicleSpeed variants normalize correctly."""
        variants = [
            "0d-vehiclespeed",
            "0d_vehiclespeed",
            "0D-VehicleSpeed",
            "vehiclespeed",
            "vehicle_speed",
        ]
        for variant in variants:
            unit = get_param_unit(variant)
            assert unit == "km/h", f"{variant} should have unit km/h, got {unit}"

    def test_obd_coolant_temp_variants(self) -> None:
        """Test coolant temp variants normalize correctly."""
        variants = [
            "05-enginecoolanttemp",
            "05_enginecoolanttemp",
            "coolant_temp",
            "coolanttemp",
        ]
        for variant in variants:
            unit = get_param_unit(variant)
            assert unit == "°C", f"{variant} should have unit °C, got {unit}"

    def test_obd_fuel_level_variants(self) -> None:
        """Test fuel level variants normalize correctly."""
        variants = [
            "2f-fuellevel",
            "2f_fuellevel",
            "fuel_level",
            "fuellevel",
        ]
        for variant in variants:
            unit = get_param_unit(variant)
            assert unit == "%", f"{variant} should have unit %, got {unit}"

    def test_normalize_hex_prefix_with_alias_suffix(self) -> None:
        """A hex prefix + aliased suffix that is not itself a full alias."""
        from custom_components.wican.param_loader import _normalize_param_name

        # "0f-rpm" is not a full alias, but suffix "rpm" is -> ENGINE_RPM.
        assert _normalize_param_name("0F-rpm") == "ENGINE_RPM"

    def test_normalize_hex_prefix_with_camelcase_suffix(self) -> None:
        """A hex prefix + CamelCase suffix that converts to a known param."""
        from custom_components.wican.param_loader import _normalize_param_name

        assert _normalize_param_name("0F-CoolantTmp") == "COOLANT_TMP"

    def test_normalize_camelcase_without_prefix(self) -> None:
        """CamelCase without a hex prefix converts to UPPER_SNAKE_CASE."""
        from custom_components.wican.param_loader import _normalize_param_name

        assert _normalize_param_name("CoolantTmp") == "COOLANT_TMP"

    def test_normalize_unknown_falls_back_to_upper(self) -> None:
        """An unmatched name falls back to its uppercase form."""
        from custom_components.wican.param_loader import _normalize_param_name

        assert _normalize_param_name("zz-unknown") == "ZZ-UNKNOWN"


class TestParamFileLoading:
    """Tests for params.json loading and hashing error paths."""

    def test_load_params_missing_file(self, tmp_path) -> None:
        """A missing params.json yields an empty dict."""
        from unittest.mock import patch

        from custom_components.wican import param_loader as pl

        with patch.object(
            pl, "_get_params_file_path", return_value=tmp_path / "missing.json",
        ):
            assert pl._load_params() == {}

    def test_load_params_invalid_json(self, tmp_path) -> None:
        """Malformed params.json yields an empty dict."""
        from unittest.mock import patch

        from custom_components.wican import param_loader as pl

        bad = tmp_path / "params.json"
        bad.write_text("{not valid json", encoding="utf-8")
        with patch.object(pl, "_get_params_file_path", return_value=bad):
            assert pl._load_params() == {}


def _mock_session(
    status: int = 200,
    body: bytes = b"{}",
    headers: dict | None = None,
):
    """Build a mocked aiohttp session returning one canned response."""
    from unittest.mock import AsyncMock, MagicMock

    response = AsyncMock()
    response.status = status
    response.headers = headers or {}
    response.read = AsyncMock(return_value=body)
    response.release = MagicMock()
    session = MagicMock()
    session.get = AsyncMock(return_value=response)
    return session


class TestGitHubParamsFetch:
    """Tests for the (validated, ETag-aware) GitHub fetch."""

    @pytest.mark.asyncio
    async def test_fetch_success(self) -> None:
        """A healthy fetch returns validated params and the ETag."""
        from custom_components.wican.param_loader import async_fetch_params_from_github

        session = _mock_session(
            body=b'{"TEST_PARAM": {"description": "Test", "settings": {"unit": "V"}}}',
            headers={"ETag": 'W/"abc123"'},
        )
        result = await async_fetch_params_from_github(session)

        assert result.ok and not result.unchanged
        assert result.data["TEST_PARAM"]["settings"]["unit"] == "V"
        assert result.etag == 'W/"abc123"'

    @pytest.mark.asyncio
    async def test_fetch_not_modified(self) -> None:
        """HTTP 304 reports unchanged and keeps the cached ETag."""
        from custom_components.wican.param_loader import async_fetch_params_from_github

        session = _mock_session(status=304)
        result = await async_fetch_params_from_github(session, etag='W/"old"')

        assert result.ok and result.unchanged
        assert result.etag == 'W/"old"'
        # The conditional header was sent.
        assert session.get.call_args.kwargs["headers"] == {"If-None-Match": 'W/"old"'}

    @pytest.mark.asyncio
    async def test_fetch_http_error(self) -> None:
        """HTTP errors report a failed fetch."""
        from custom_components.wican.param_loader import async_fetch_params_from_github

        result = await async_fetch_params_from_github(_mock_session(status=404))
        assert not result.ok

    @pytest.mark.asyncio
    async def test_fetch_invalid_json(self) -> None:
        """Garbage bodies report a failed fetch."""
        from custom_components.wican.param_loader import async_fetch_params_from_github

        result = await async_fetch_params_from_github(
            _mock_session(body=b"invalid json {{{"),
        )
        assert not result.ok

    @pytest.mark.asyncio
    async def test_fetch_wrong_shape(self) -> None:
        """A JSON body that is not an object reports a failed fetch."""
        from custom_components.wican.param_loader import async_fetch_params_from_github

        result = await async_fetch_params_from_github(
            _mock_session(body=b'["a", "list"]'),
        )
        assert not result.ok

    @pytest.mark.asyncio
    async def test_fetch_timeout(self) -> None:
        """Timeouts report a failed fetch, never raise."""
        import asyncio
        from unittest.mock import MagicMock

        from custom_components.wican.param_loader import async_fetch_params_from_github

        session = MagicMock()
        session.get = MagicMock(side_effect=asyncio.TimeoutError())
        result = await async_fetch_params_from_github(session)
        assert not result.ok

    @pytest.mark.asyncio
    async def test_fetch_oversized_declared(self) -> None:
        """A giant declared Content-Length is refused before the read."""
        from custom_components.wican.param_loader import async_fetch_params_from_github

        session = _mock_session(headers={"Content-Length": str(10**9)})
        result = await async_fetch_params_from_github(session)
        assert not result.ok

    @pytest.mark.asyncio
    async def test_fetch_oversized_body(self) -> None:
        """A giant undeclared body is refused after the bounded read."""
        from custom_components.wican.const import PARAMS_MAX_BYTES
        from custom_components.wican.param_loader import async_fetch_params_from_github

        session = _mock_session(body=b"x" * (PARAMS_MAX_BYTES + 1))
        result = await async_fetch_params_from_github(session)
        assert not result.ok

    def test_compute_hash_consistency(self) -> None:
        """Hash computation is deterministic and SHA256-shaped."""
        from custom_components.wican.param_loader import _compute_hash

        data = b'{"test": "data"}'
        assert _compute_hash(data) == _compute_hash(data)
        assert len(_compute_hash(data)) == 64

    def test_reload_params(self) -> None:
        """reload_params reloads the bundled file."""
        from custom_components.wican.param_loader import get_all_params, reload_params

        reload_params()
        params = get_all_params()
        assert isinstance(params, dict)
        assert "SOC" in params


class TestValidateRemoteParams:
    """Bounds on remote params documents (hostile-content hardening)."""

    def test_wrong_shape_rejected(self) -> None:
        from custom_components.wican.param_loader import _validate_remote_params

        assert _validate_remote_params(["not", "a", "dict"]) is None
        assert _validate_remote_params("nope") is None
        assert _validate_remote_params(None) is None

    def test_entry_flood_capped(self) -> None:
        from custom_components.wican.const import PARAMS_MAX_ENTRIES
        from custom_components.wican.param_loader import _validate_remote_params

        flood = {f"P{i}": {"description": "x", "settings": {}} for i in range(PARAMS_MAX_ENTRIES * 2)}
        validated = _validate_remote_params(flood)
        assert len(validated) == PARAMS_MAX_ENTRIES

    def test_invalid_entries_dropped(self) -> None:
        from custom_components.wican.param_loader import _validate_remote_params

        raw = {
            "GOOD": {"description": "ok", "settings": {"unit": "V", "class": "voltage"}},
            "": {"description": "empty key"},
            "x" * 500: {"description": "huge key"},
            "NON_DICT": "not a dict",
            "WEIRD_SETTINGS": {"description": "d", "settings": {"unit": 42, "huge": "y" * 5000}},
        }
        validated = _validate_remote_params(raw)
        assert set(validated) == {"GOOD", "WEIRD_SETTINGS"}
        assert validated["GOOD"]["settings"] == {"unit": "V", "class": "voltage"}
        # Non-string / oversized settings fields are dropped, entry kept.
        assert validated["WEIRD_SETTINGS"]["settings"] == {}


class TestParamsManagerSync:
    """Storage-backed sync flows (no package-directory writes exist anymore)."""

    @pytest.fixture(autouse=True)
    def _restore_params(self):
        """Keep the module-global params table pristine across tests."""
        from custom_components.wican import param_loader as pl

        saved = dict(pl._PARAMS)
        yield
        pl._PARAMS.clear()
        pl._PARAMS.update(saved)

    @staticmethod
    def _fetch_result(**kwargs):
        from custom_components.wican.param_loader import ParamsFetchResult

        return ParamsFetchResult(**kwargs)

    @pytest.mark.asyncio
    async def test_sync_applies_and_persists(self, hass) -> None:
        """A successful fetch updates memory and lands in .storage."""
        from unittest.mock import AsyncMock, patch

        from custom_components.wican import param_loader as pl

        new_params = {"NEWP": {"description": "New", "settings": {"unit": "V"}}}
        with patch.object(
            pl, "async_fetch_params_from_github",
            AsyncMock(return_value=self._fetch_result(ok=True, data=new_params, etag="e1")),
        ):
            assert await pl.async_update_params_from_github(hass) is True

        assert pl._PARAMS == new_params
        stored = await pl._get_manager(hass)._store.async_load()
        assert stored["params"] == new_params
        assert stored["etag"] == "e1"

    @pytest.mark.asyncio
    async def test_sync_fetch_failure_keeps_current(self, hass) -> None:
        """A failed fetch changes nothing and reports no update."""
        from unittest.mock import AsyncMock, patch

        from custom_components.wican import param_loader as pl

        before = dict(pl._PARAMS)
        with patch.object(
            pl, "async_fetch_params_from_github",
            AsyncMock(return_value=self._fetch_result(ok=False)),
        ):
            assert await pl.async_update_params_from_github(hass) is False
        assert pl._PARAMS == before

    @pytest.mark.asyncio
    async def test_sync_dedupes_concurrent_setups(self, hass) -> None:
        """Back-to-back setup syncs fold into one fetch; force bypasses."""
        from unittest.mock import AsyncMock, patch

        from custom_components.wican import param_loader as pl

        fetch = AsyncMock(return_value=self._fetch_result(ok=True, unchanged=True))
        with patch.object(pl, "async_fetch_params_from_github", fetch):
            await pl.async_update_params_from_github(hass)
            await pl.async_update_params_from_github(hass)  # deduped
            assert fetch.await_count == 1
            await pl.async_force_params_refresh(hass)  # user-initiated
            assert fetch.await_count == 2

    @pytest.mark.asyncio
    async def test_stored_params_apply_when_github_down(self, hass) -> None:
        """The last fetched copy applies from .storage even offline."""
        from unittest.mock import AsyncMock, patch

        from custom_components.wican import param_loader as pl

        stored_params = {"STOREDP": {"description": "s", "settings": {"unit": "A"}}}
        await pl._get_manager(hass)._store.async_save(
            {"params": stored_params, "etag": "e0", "fetched_at": 0},
        )
        with patch.object(
            pl, "async_fetch_params_from_github",
            AsyncMock(return_value=self._fetch_result(ok=False)),
        ):
            assert await pl.async_update_params_from_github(hass) is False

        assert pl._PARAMS == stored_params

    @pytest.mark.asyncio
    async def test_force_refresh_reports_failure(self, hass) -> None:
        """The button path distinguishes failure (None) from no-change."""
        from unittest.mock import AsyncMock, patch

        from custom_components.wican import param_loader as pl

        with patch.object(
            pl, "async_fetch_params_from_github",
            AsyncMock(return_value=self._fetch_result(ok=False)),
        ):
            assert await pl.async_force_params_refresh(hass) is None
        with patch.object(
            pl, "async_fetch_params_from_github",
            AsyncMock(return_value=self._fetch_result(ok=True, unchanged=True)),
        ):
            assert await pl.async_force_params_refresh(hass) is False
