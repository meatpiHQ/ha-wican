"""End-to-end tests for the SD-card history backfill.

Drives the full pipeline with the device simulator: real webhook pushes,
real capability probe, real API client fetching from the simulated device's
data-logger routes — through to the recorder statistics import (mocked for
flow tests, real in-memory recorder for the round-trip tests).
"""

from __future__ import annotations

from functools import partial
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.wican.api import LEGACY_CAPABILITIES, MeatPiApiClient
from custom_components.wican.const import CONF_HISTORY_SYNC, DOMAIN
from custom_components.wican.history import async_sync_history
from tests.device_sim import MeatPiDeviceSimulator

NOW = time.time()
HOUR = int(NOW // 3600) * 3600
H1 = HOUR - 3 * 3600
H2 = HOUR - 2 * 3600

OFFLINE_DRIVE_ROWS = [
    {"ts": H1 + 60, "name": "SOC", "value": 70},
    {"ts": H1 + 600, "name": "SOC", "value": 74},
    {"ts": H2 + 60, "name": "SOC", "value": 65},
    {"ts": H2 + 60, "name": "RPM", "value": 1500},
]


@pytest.fixture
def mock_capability_probe() -> None:
    """Override conftest's autouse probe stub: run the REAL API client."""
    return


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_db_url: str,
    enable_custom_integrations: None,
) -> None:
    """Override conftest's autouse fixture with recorder-safe ordering.

    The recorder DB fixture must initialize before the first ``hass``
    instance; requesting it first here makes the in-memory recorder tests
    in this module possible.
    """
    return


@pytest.fixture
def mock_recorder_calls():
    """Mock the recorder touchpoints used by the sync."""
    with (
        patch("custom_components.wican.history.get_instance") as instance,
        patch(
            "custom_components.wican.history.statistics_during_period",
            return_value={},
        ),
        patch(
            "custom_components.wican.history.get_metadata", return_value={},
        ),
        patch(
            "custom_components.wican.history.async_import_statistics",
        ) as import_stats,
    ):

        async def _run_job(func: Any, *args: Any) -> Any:
            return func(*args)

        instance.return_value.async_add_executor_job = AsyncMock(
            side_effect=_run_job,
        )
        yield import_stats


async def _online_device(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> MeatPiDeviceSimulator:
    """A V6 WiCAN Pro that has pushed once (PID entities exist)."""
    sim = MeatPiDeviceSimulator.from_preset(hass, hass_client, "wican_pro_v6")
    sim.attach_api(aioclient_mock)
    await sim.async_setup()
    await sim.push_and_settle(
        {**sim.status(), **sim.pids({"SOC": 71, "RPM": 900})},
    )
    return sim


async def _reconnect_after_gap(sim: MeatPiDeviceSimulator) -> None:
    """Simulate the device coming home after an offline drive."""
    sim.go_stale()
    sim.entry.runtime_data.last_history_sync = 0.0  # bypass rate limit
    await sim.push_and_settle(
        {**sim.status(), **sim.pids({"SOC": 65, "RPM": 800})},
    )


async def test_offline_drive_backfilled_via_export_route(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    mock_recorder_calls: MagicMock,
) -> None:
    """Reconnecting after a gap imports the SD-card rows (export route)."""
    sim = await _online_device(hass, hass_client, aioclient_mock)
    sim.seed_log_rows(OFFLINE_DRIVE_ROWS, via="export")

    await _reconnect_after_gap(sim)

    assert sim.api.count("GET", "/api/logger/export") >= 1
    imported = {
        call.args[1]["statistic_id"]: call.args[2]
        for call in mock_recorder_calls.call_args_list
    }
    soc_stats = imported["sensor.wican_pro_sim_soc"]
    assert soc_stats[0]["start"] == dt_util.utc_from_timestamp(H1)
    assert soc_stats[0]["mean"] == pytest.approx(72.0)
    assert soc_stats[1]["mean"] == 65
    rpm_stats = imported["sensor.wican_pro_sim_rpm"]
    assert rpm_stats[0]["mean"] == 1500
    # Diagnostics surface the run.
    result = sim.entry.runtime_data.last_history_result
    assert result is not None and result.hours_imported == 3


@pytest.mark.parametrize("file_format", ["jsonl", "csv"])
async def test_offline_drive_backfilled_via_files(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    mock_recorder_calls: MagicMock,
    file_format: str,
) -> None:
    """Current firmware (no export route) is served from rotated files."""
    sim = await _online_device(hass, hass_client, aioclient_mock)
    sim.seed_log_rows(OFFLINE_DRIVE_ROWS, via="file", file_format=file_format)
    # An active (write-locked) file must be skipped without failing the sync.
    sim.seed_log_rows(
        [{"ts": H2 + 90, "name": "SOC", "value": 1}],
        via="file",
        filename="dl_active.jsonl",
        active=True,
    )

    await _reconnect_after_gap(sim)

    result = sim.entry.runtime_data.last_history_result
    assert result is not None
    assert result.source == "files"
    assert result.hours_imported == 3
    imported = {
        call.args[1]["statistic_id"]: call.args[2]
        for call in mock_recorder_calls.call_args_list
    }
    assert imported["sensor.wican_pro_sim_soc"][0]["mean"] == pytest.approx(72.0)


async def test_sync_disabled_by_option(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    mock_recorder_calls: MagicMock,
) -> None:
    """The options kill switch prevents any backfill."""
    sim = MeatPiDeviceSimulator.from_preset(hass, hass_client, "wican_pro_v6")
    sim.attach_api(aioclient_mock)
    await sim.async_setup(options={CONF_HISTORY_SYNC: False})
    await sim.push_and_settle(
        {**sim.status(), **sim.pids({"SOC": 71})},
    )
    sim.seed_log_rows(OFFLINE_DRIVE_ROWS, via="export")

    await _reconnect_after_gap(sim)

    mock_recorder_calls.assert_not_called()
    assert sim.api.count("GET", "/api/logger/export") == 0


async def test_sync_rate_limited(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    mock_recorder_calls: MagicMock,
) -> None:
    """Back-to-back gap pushes cannot hammer the device."""
    sim = await _online_device(hass, hass_client, aioclient_mock)
    sim.seed_log_rows(OFFLINE_DRIVE_ROWS, via="export")
    baseline = sim.api.count("GET", "/api/logger/export")

    await _reconnect_after_gap(sim)
    first_round = sim.api.count("GET", "/api/logger/export")
    assert first_round > baseline

    # Second gap immediately after: rate limit (not reset this time) holds.
    sim.go_stale()
    await sim.push_and_settle(sim.status())
    assert sim.api.count("GET", "/api/logger/export") == first_round


async def test_device_offline_during_sync_is_contained(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    mock_recorder_calls: MagicMock,
) -> None:
    """The device vanishing right after reconnect harms nothing."""
    sim = await _online_device(hass, hass_client, aioclient_mock)
    sim.seed_log_rows(OFFLINE_DRIVE_ROWS, via="export")
    sim.api.mode = "offline"

    sim.go_stale()
    sim.entry.runtime_data.last_history_sync = 0.0
    # The push itself still arrives (webhook is device→HA, not HTTP API).
    await sim.push_and_settle(sim.status())

    mock_recorder_calls.assert_not_called()
    # Telemetry unaffected; a later successful sync is still possible.
    sim.api.mode = "ok"
    sim.entry.runtime_data.last_history_sync = 0.0
    sim.go_stale()
    await sim.push_and_settle(sim.status())
    assert mock_recorder_calls.called


async def test_hostile_export_body_is_contained(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    mock_recorder_calls: MagicMock,
) -> None:
    """A garbage export body degrades to zero rows, never an exception."""
    sim = await _online_device(hass, hass_client, aioclient_mock)
    sim.api.export_supported = True
    sim.api.export_payload_override = "ceci n'est pas du JSON\n{{{{\n\x00"

    await _reconnect_after_gap(sim)

    mock_recorder_calls.assert_not_called()
    # Integration alive and pushing.
    await sim.push_and_settle(
        {**sim.status(), **sim.pids({"SOC": 42})},
    )
    assert hass.states.get("sensor.wican_pro_sim_soc").state == "42"


async def test_watermark_prevents_reimport(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    mock_recorder_calls: MagicMock,
) -> None:
    """A second sync fetches only rows newer than the watermark."""
    sim = await _online_device(hass, hass_client, aioclient_mock)
    sim.seed_log_rows(OFFLINE_DRIVE_ROWS, via="export")

    await _reconnect_after_gap(sim)
    first_imports = mock_recorder_calls.call_count
    assert first_imports > 0

    # Reconnect again with no new rows: the export filter (ts > since)
    # returns nothing, so no further imports happen.
    await _reconnect_after_gap(sim)
    assert mock_recorder_calls.call_count == first_imports


# ---------------------------------------------------------------------------
# Real recorder round-trip
# ---------------------------------------------------------------------------


async def _recorder_entry(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry,
) -> tuple[MockConfigEntry, str]:
    """Set up the integration against the real in-memory recorder."""
    mock_config_entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ),
        # The round-trip tests swap in a mocked API client below; keep the
        # setup-time capability probe off the network too.
        patch("custom_components.wican.MeatPiApiClient") as client_cls,
    ):
        client_cls.return_value = AsyncMock()
        client_cls.return_value.async_probe.return_value = LEGACY_CAPABILITIES
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    registry = er.async_get(hass)
    entry_reg = registry.async_get_or_create(
        "sensor", DOMAIN, f"{mock_config_entry.entry_id}_pid_SOC",
    )
    mock_config_entry.runtime_data.api = AsyncMock(spec=MeatPiApiClient)
    return mock_config_entry, entry_reg.entity_id


async def _read_back(hass: HomeAssistant, entity_id: str) -> list[dict]:
    await async_wait_recording_done(hass)
    stats = await get_instance(hass).async_add_executor_job(
        partial(
            statistics_during_period,
            hass,
            dt_util.utc_from_timestamp(H1 - 3600),
            None,
            {entity_id},
            "hour",
            None,
            {"mean", "min", "max"},
        ),
    )
    return stats.get(entity_id, [])


async def test_round_trip_into_real_recorder(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Imported statistics land in the recorder and read back correctly."""
    entry, entity_id = await _recorder_entry(hass, mock_config_entry)
    entry.runtime_data.api.async_export_log_rows = AsyncMock(
        side_effect=[
            (
                [
                    {"ts": H1 + 60, "name": "SOC", "value": 70},
                    {"ts": H1 + 600, "name": "SOC", "value": 74},
                    {"ts": H2 + 60, "name": "SOC", "value": 65},
                ],
                None,
            ),
        ],
    )

    result = await async_sync_history(hass, entry)
    assert result.hours_imported == 2

    rows = await _read_back(hass, entity_id)
    assert len(rows) == 2
    assert rows[0]["mean"] == pytest.approx(72.0)
    assert rows[0]["min"] == 70
    assert rows[0]["max"] == 74
    assert rows[1]["mean"] == pytest.approx(65.0)
    hour_starts = {int(row["start"]) for row in rows}
    assert hour_starts == {H1, H2}


async def test_round_trip_reimport_is_idempotent(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Re-importing the same hours (watermark lost) creates no duplicates."""
    entry, entity_id = await _recorder_entry(hass, mock_config_entry)
    rows = [
        ([{"ts": H1 + 60, "name": "SOC", "value": 70}], None),
        ([{"ts": H1 + 60, "name": "SOC", "value": 70}], None),
    ]
    entry.runtime_data.api.async_export_log_rows = AsyncMock(side_effect=rows)

    first = await async_sync_history(hass, entry)
    assert first.hours_imported == 1
    await async_wait_recording_done(hass)

    # Simulate a lost watermark (e.g. storage wiped) → same rows re-fetched.
    from custom_components.wican.history import HistorySync

    await HistorySync(hass, entry)._async_save_watermark(0.0)
    second = await async_sync_history(hass, entry)
    await async_wait_recording_done(hass)

    read = await _read_back(hass, entity_id)
    assert len(read) == 1  # updated in place, not duplicated
    assert read[0]["mean"] == pytest.approx(70.0)
    assert second.errors == [] or second.hours_imported <= 1
