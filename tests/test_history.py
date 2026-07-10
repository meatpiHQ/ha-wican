"""Unit tests for the SD-card history backfill engine (history.py)."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wican.api import MeatPiApiClient
from custom_components.wican.const import (
    DOMAIN,
    HISTORY_MAX_FILES_PER_SYNC,
    HISTORY_MAX_PIDS_PER_SYNC,
)
from custom_components.wican.exceptions import MeatPiApiError
from custom_components.wican.history import (
    HistorySync,
    HistorySyncResult,
    aggregate_rows,
    async_remove_history_store,
    async_sync_history,
    normalize_row,
    parse_csv,
    parse_jsonl,
)

NOW = time.time()
HOUR = int(NOW // 3600) * 3600
H1 = HOUR - 3 * 3600  # three full hours ago (top of hour)
H2 = HOUR - 2 * 3600


# ---------------------------------------------------------------------------
# Row normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"ts": 100.0, "name": "SOC", "value": 71.5}, (100.0, "SOC", 71.5)),
        ({"timestamp": 100, "param": "RPM", "val": "900"}, (100.0, "RPM", 900.0)),
        ({"time": "100.5", "pid": " SOC ", "value": 1}, (100.5, "SOC", 1.0)),
        ({"epoch": 100, "name": "x", "value": -3}, (100.0, "x", -3.0)),
    ],
)
def test_normalize_row_accepts_aliases(raw: dict, expected: tuple) -> None:
    """Field aliases and string numbers are handled."""
    assert normalize_row(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "not a dict",
        ["ts", 1],
        None,
        {},
        {"ts": 100, "name": "SOC"},  # no value
        {"ts": 100, "value": 1},  # no name
        {"name": "SOC", "value": 1},  # no ts
        {"ts": 0, "name": "SOC", "value": 1},  # zero ts
        {"ts": -5, "name": "SOC", "value": 1},
        {"ts": 100, "name": "", "value": 1},
        {"ts": 100, "name": "   ", "value": 1},
        {"ts": 100, "name": "x" * 500, "value": 1},  # absurd name
        {"ts": 100, "name": "SOC", "value": "garbage"},
        {"ts": 100, "name": "SOC", "value": float("nan")},
        {"ts": 100, "name": "SOC", "value": float("inf")},
        {"ts": 100, "name": "SOC", "value": True},  # bool is not a reading
        {"ts": 100, "name": "SOC", "value": {"nested": 1}},
        {"ts": 100, "name": 42, "value": 1},
        {"ts": "soon", "name": "SOC", "value": 1},
    ],
)
def test_normalize_row_rejects_garbage(raw: Any) -> None:
    """Every malformed row shape is rejected, never raises."""
    assert normalize_row(raw) is None


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def test_parse_jsonl_mixed_content() -> None:
    """Valid lines survive; garbage lines are counted, not fatal."""
    text = "\n".join(
        [
            '{"ts": 1, "name": "SOC", "value": 70}',
            "",
            "not json at all",
            '["a", "list"]',
            '{"ts": 2, "name": "SOC", "value": 71}',
            "   ",
        ],
    )
    rows, bad = parse_jsonl(text)
    assert len(rows) == 2
    assert bad == 2


def test_parse_csv_with_aliases_and_extras() -> None:
    """CSV headers may use aliases; unknown columns are ignored."""
    text = "timestamp,param,val,extra\n100,SOC,70,x\n200,RPM,900,y\n"
    rows, bad = parse_csv(text)
    assert bad == 0
    assert normalize_row(rows[0]) == (100.0, "SOC", 70.0)
    assert normalize_row(rows[1]) == (200.0, "RPM", 900.0)


def test_parse_csv_garbage() -> None:
    """Unparseable CSV never raises."""
    rows, _bad = parse_csv('a,b\x00,c\n"unclosed')
    assert isinstance(rows, list)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_aggregate_rows_buckets_by_hour() -> None:
    """Rows land in per-PID hourly buckets with correct mean/min/max."""
    result = HistorySyncResult()
    rows = [
        {"ts": H1 + 60, "name": "SOC", "value": 70},
        {"ts": H1 + 120, "name": "SOC", "value": 74},
        {"ts": H1 + 180, "name": "SOC", "value": 72},
        {"ts": H2 + 60, "name": "SOC", "value": 60},
        {"ts": H1 + 60, "name": "RPM", "value": 900},
    ]
    buckets = aggregate_rows(rows, since=0, now_ts=NOW, result=result)

    soc_h1 = buckets["SOC"][H1]
    assert soc_h1.mean == pytest.approx(72.0)
    assert soc_h1.minimum == 70
    assert soc_h1.maximum == 74
    assert buckets["SOC"][H2].mean == 60
    assert buckets["RPM"][H1].count == 1
    assert result.rows_used == 5
    assert result.rows_rejected == 0


def test_aggregate_rows_window_filters() -> None:
    """Rows outside the time window are rejected: old, future, current hour."""
    result = HistorySyncResult()
    rows = [
        {"ts": H1 - 40 * 86400, "name": "SOC", "value": 1},  # too old
        {"ts": NOW + 4000, "name": "SOC", "value": 2},  # future
        {"ts": HOUR + 60, "name": "SOC", "value": 3},  # current hour (live)
        {"ts": H1 + 60, "name": "SOC", "value": 4},  # good
        {"ts": H1 + 30, "name": "SOC", "value": 5},  # before since
    ]
    buckets = aggregate_rows(rows, since=H1 + 45, now_ts=NOW, result=result)

    assert buckets["SOC"][H1].count == 1
    assert buckets["SOC"][H1].mean == 4
    assert result.rows_rejected == 4


def test_aggregate_rows_pid_cap() -> None:
    """A device inventing thousands of PID names cannot balloon memory."""
    result = HistorySyncResult()
    rows = [
        {"ts": H1 + i % 300, "name": f"PID_{i}", "value": i}
        for i in range(HISTORY_MAX_PIDS_PER_SYNC * 2)
    ]
    buckets = aggregate_rows(rows, since=0, now_ts=NOW, result=result)
    assert len(buckets) == HISTORY_MAX_PIDS_PER_SYNC
    assert result.rows_rejected == HISTORY_MAX_PIDS_PER_SYNC


# ---------------------------------------------------------------------------
# File selection
# ---------------------------------------------------------------------------


def test_select_log_files_filters_and_caps() -> None:
    """Only rotated dl_* log files below the size cap are selected."""
    result = HistorySyncResult()
    entries = [
        {"name": "dl_100.jsonl", "dir": False, "size": 10},
        {"name": "dl_200.csv", "dir": False, "size": 10},
        {"name": "dl_300.jsonl", "dir": False, "size": 10},  # active
        {"name": "dl_400.jsonl", "dir": False, "size": 10**9},  # oversized
        {"name": "can_100.wdl", "dir": False, "size": 10},  # other stream
        {"name": "notes.txt", "dir": False, "size": 10},
        {"name": "dl_dir.jsonl", "dir": True, "size": 0},
        {"nonsense": True},
        {"name": 42, "dir": False},
    ]
    selected = HistorySync._select_log_files(entries, "dl_300.jsonl", result)
    assert selected == ["dl_100.jsonl", "dl_200.csv"]
    assert any("too large" in e for e in result.errors)


def test_select_log_files_defers_beyond_cap() -> None:
    """More files than the per-sync cap keeps the newest, notes the rest."""
    result = HistorySyncResult()
    entries = [
        {"name": f"dl_{1000 + i}.jsonl", "dir": False, "size": 5}
        for i in range(HISTORY_MAX_FILES_PER_SYNC + 10)
    ]
    selected = HistorySync._select_log_files(entries, None, result)
    assert len(selected) == HISTORY_MAX_FILES_PER_SYNC
    assert selected[-1] == f"dl_{1000 + HISTORY_MAX_FILES_PER_SYNC + 9}.jsonl"
    assert any("deferred" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Full sync with a mocked device API + mocked recorder
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_recorder_calls():
    """Mock the recorder touchpoints used by the sync."""
    with (
        patch("custom_components.wican.history.get_instance") as instance,
        patch(
            "custom_components.wican.history.statistics_during_period",
            return_value={},
        ) as during,
        patch(
            "custom_components.wican.history.get_metadata", return_value={},
        ) as metadata,
        patch(
            "custom_components.wican.history.async_import_statistics",
        ) as import_stats,
    ):

        async def _run_job(func: Any, *args: Any) -> Any:
            return func(*args)

        instance.return_value.async_add_executor_job = AsyncMock(
            side_effect=_run_job,
        )
        yield {
            "import": import_stats,
            "during": during,
            "metadata": metadata,
        }


async def _entry_with_api(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    *,
    pids: tuple[str, ...] = ("SOC",),
) -> MockConfigEntry:
    """Set up the integration, register PID entities, attach a mock API."""
    mock_config_entry.add_to_hass(hass)
    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    registry = er.async_get(hass)
    for pid in pids:
        registry.async_get_or_create(
            "sensor", DOMAIN, f"{mock_config_entry.entry_id}_pid_{pid}",
        )
    mock_config_entry.runtime_data.api = AsyncMock(spec=MeatPiApiClient)
    return mock_config_entry


def _export_rows(rows: list[dict[str, Any]]) -> AsyncMock:
    """Return an export mock serving all rows in one page, then empty."""
    return AsyncMock(side_effect=[(rows, None)])


async def test_sync_imports_hourly_statistics(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """The happy path: rows become hourly statistics on the PID entity."""
    entry = await _entry_with_api(hass, mock_config_entry)
    entry.runtime_data.api.async_export_log_rows = _export_rows(
        [
            {"ts": H1 + 60, "name": "SOC", "value": 70},
            {"ts": H1 + 120, "name": "SOC", "value": 74},
            {"ts": H2 + 60, "name": "SOC", "value": 65},
        ],
    )

    result = await async_sync_history(hass, entry)

    assert result.ran
    assert result.source == "export"
    assert result.pids_imported == 1
    assert result.hours_imported == 2
    imported = mock_recorder_calls["import"].call_args
    metadata, statistics = imported.args[1], imported.args[2]
    assert metadata["statistic_id"].startswith("sensor.")
    assert metadata["source"] == "recorder"
    assert statistics[0]["start"] == dt_util.utc_from_timestamp(H1)
    assert statistics[0]["mean"] == pytest.approx(72.0)
    assert statistics[0]["min"] == 70
    assert statistics[0]["max"] == 74
    assert statistics[1]["mean"] == 65
    # Watermark = end of the newest imported hour.
    assert result.watermark == H2 + 3600
    sync = HistorySync(hass, entry)
    assert await sync.async_get_watermark() == H2 + 3600


async def test_sync_falls_back_to_files(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """Firmware without the export route is served from rotated files."""
    entry = await _entry_with_api(hass, mock_config_entry)
    api = entry.runtime_data.api
    api.async_export_log_rows = AsyncMock(return_value=None)  # 404 route
    api.async_get_logger_status = AsyncMock(
        return_value={"dir": "/sd/logs", "file": "dl_900.jsonl"},
    )
    api.async_fs_list = AsyncMock(
        return_value=[
            {"name": "dl_800.jsonl", "dir": False, "size": 100},
            {"name": "dl_900.jsonl", "dir": False, "size": 100},  # active
        ],
    )
    api.async_fs_download = AsyncMock(
        return_value=f'{{"ts": {H1 + 60}, "name": "SOC", "value": 50}}',
    )

    result = await async_sync_history(hass, entry)

    assert result.source == "files"
    assert result.hours_imported == 1
    api.async_fs_download.assert_awaited_once()  # active file never fetched
    assert "dl_800.jsonl" in api.async_fs_download.call_args.args[0]


async def test_sync_fetch_failure_is_contained(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """A device that dies mid-fetch aborts cleanly; watermark untouched."""
    entry = await _entry_with_api(hass, mock_config_entry)
    entry.runtime_data.api.async_export_log_rows = AsyncMock(
        side_effect=MeatPiApiError("boom"),
    )

    result = await async_sync_history(hass, entry)

    assert not result.ran
    assert result.errors
    mock_recorder_calls["import"].assert_not_called()
    assert await HistorySync(hass, entry).async_get_watermark() == 0.0


async def test_sync_skips_unknown_pids(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """Rows for PIDs that back no entity are counted, not imported."""
    entry = await _entry_with_api(hass, mock_config_entry, pids=("SOC",))
    entry.runtime_data.api.async_export_log_rows = _export_rows(
        [
            {"ts": H1 + 60, "name": "SOC", "value": 70},
            {"ts": H1 + 60, "name": "NEVER_SEEN", "value": 1},
        ],
    )

    result = await async_sync_history(hass, entry)

    assert result.pids_imported == 1
    assert result.pids_skipped == 1


async def test_sync_live_hours_win(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """Hours that already carry live statistics are never overwritten."""
    entry = await _entry_with_api(hass, mock_config_entry)
    entry.runtime_data.api.async_export_log_rows = _export_rows(
        [
            {"ts": H1 + 60, "name": "SOC", "value": 70},
            {"ts": H2 + 60, "name": "SOC", "value": 65},
        ],
    )
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_pid_SOC",
    )
    mock_recorder_calls["during"].return_value = {
        entity_id: [{"start": float(H1)}],
    }

    result = await async_sync_history(hass, entry)

    assert result.hours_skipped_existing == 1
    assert result.hours_imported == 1
    statistics = mock_recorder_calls["import"].call_args.args[2]
    assert statistics[0]["start"] == dt_util.utc_from_timestamp(H2)


async def test_sync_incompatible_metadata_skipped(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """Sum-type existing statistics are never polluted with mean rows."""
    entry = await _entry_with_api(hass, mock_config_entry)
    entry.runtime_data.api.async_export_log_rows = _export_rows(
        [{"ts": H1 + 60, "name": "SOC", "value": 70}],
    )
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_pid_SOC",
    )
    mock_recorder_calls["metadata"].return_value = {
        entity_id: (1, {"has_sum": True, "unit_of_measurement": None}),
    }

    result = await async_sync_history(hass, entry)

    assert result.pids_imported == 0
    assert any("incompatible" in e for e in result.errors)
    mock_recorder_calls["import"].assert_not_called()


async def test_sync_per_pid_isolation(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """One PID failing to import never blocks the others."""
    entry = await _entry_with_api(hass, mock_config_entry, pids=("SOC", "RPM"))
    entry.runtime_data.api.async_export_log_rows = _export_rows(
        [
            {"ts": H1 + 60, "name": "SOC", "value": 70},
            {"ts": H1 + 60, "name": "RPM", "value": 900},
        ],
    )
    calls: list[str] = []

    def _import(hass_arg: Any, metadata: dict, statistics: list) -> None:
        calls.append(metadata["statistic_id"])
        if len(calls) == 1:
            raise ValueError("simulated import failure")

    mock_recorder_calls["import"].side_effect = _import

    result = await async_sync_history(hass, entry)

    assert result.pids_imported == 1
    assert len(calls) == 2
    assert result.errors


async def test_sync_export_cursor_stall_stops(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """A device that never advances the export cursor cannot loop forever."""
    entry = await _entry_with_api(hass, mock_config_entry)
    full_page = [
        {"ts": H1 + i, "name": "SOC", "value": i} for i in range(2000)
    ]
    entry.runtime_data.api.async_export_log_rows = AsyncMock(
        return_value=(full_page, 0.0),  # full page but stalled cursor
    )

    result = await async_sync_history(hass, entry)

    assert any("cursor" in e for e in result.errors)
    assert entry.runtime_data.api.async_export_log_rows.await_count == 1


async def test_sync_without_api_is_noop(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """No API client (legacy/unprobed device) → nothing happens."""
    entry = await _entry_with_api(hass, mock_config_entry)
    entry.runtime_data.api = None

    result = await async_sync_history(hass, entry)

    assert not result.ran
    mock_recorder_calls["import"].assert_not_called()


async def test_sync_recorder_failure_contained(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """Recorder being unavailable degrades to a logged error."""
    entry = await _entry_with_api(hass, mock_config_entry)
    entry.runtime_data.api.async_export_log_rows = _export_rows(
        [{"ts": H1 + 60, "name": "SOC", "value": 70}],
    )
    mock_recorder_calls["during"].side_effect = KeyError("recorder")

    result = await async_sync_history(hass, entry)

    assert any("recorder" in e for e in result.errors)
    mock_recorder_calls["import"].assert_not_called()


async def test_watermark_store_roundtrip_and_removal(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """The watermark persists and is deleted with the entry."""
    entry = await _entry_with_api(hass, mock_config_entry)
    sync = HistorySync(hass, entry)
    assert await sync.async_get_watermark() == 0.0

    await sync._async_save_watermark(12345.0)
    assert await HistorySync(hass, entry).async_get_watermark() == 12345.0

    await async_remove_history_store(hass, entry)
    assert await HistorySync(hass, entry).async_get_watermark() == 0.0


async def test_unexpected_error_never_escapes(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """async_sync_history never raises, whatever breaks inside."""
    entry = await _entry_with_api(hass, mock_config_entry)
    entry.runtime_data.api.async_export_log_rows = AsyncMock(
        side_effect=RuntimeError("totally unexpected"),
    )

    result = await async_sync_history(hass, entry)

    assert result == HistorySyncResult()
