"""SD-card history backfill into Home Assistant long-term statistics.

A MeatPi device that was driving offline logs timestamped AutoPID rows to
its SD card (the firmware ``data_logger`` params stream). When the device
reconnects, this module fetches the rows recorded during the gap,
aggregates them into hourly mean/min/max buckets, and imports them onto
the matching PID sensor entities via Home Assistant's official recorder
statistics API — filling the gap in each sensor's long-term history at
the times the data was actually recorded.

Design and constraints: notes/HISTORICAL_DATA_SYNC.md. Key invariants:

- Only the official ``async_import_statistics`` API is used (hourly
  resolution; raw states are never touched).
- Live data always wins: hours that already have statistics, and the
  current (incomplete) hour, are never imported.
- Everything is bounded (rows, files, bytes, distinct PIDs, age window)
  so a glitching or hostile device cannot balloon memory or statistics.
- The watermark never passes data that was not durably imported: a
  failed PID holds it at that PID's earliest failed hour, files beyond
  the per-sync cap defer the newest (still ahead of the watermark), and
  an hour cut short by the row cap is dropped rather than imported from
  partial data. An interrupted sync simply re-runs; re-importing an
  hour is idempotent.
- A sync failure is contained: it never disturbs telemetry or control.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from functools import partial
import io
import json
import logging
from math import isfinite
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    STATISTIC_UNIT_TO_UNIT_CONVERTER,
    async_import_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    HISTORY_EXPORT_MAX_PAGES,
    HISTORY_EXPORT_PAGE_ROWS,
    HISTORY_FUTURE_SKEW,
    HISTORY_MAX_AGE_DAYS,
    HISTORY_MAX_FILE_BYTES,
    HISTORY_MAX_FILES_PER_SYNC,
    HISTORY_MAX_PIDS_PER_SYNC,
    HISTORY_MAX_ROWS_PER_SYNC,
    HISTORY_STORAGE_VERSION,
    MAX_PID_KEY_LENGTH,
)
from .exceptions import MeatPiApiError

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant

    from . import WiCANConfigEntry
    from .api import MeatPiApiClient

_LOGGER = logging.getLogger(__name__)

# Row-field aliases accepted from device logs (jsonl/csv headers vary by
# firmware version; the export route uses the first alias of each).
_TS_KEYS = ("ts", "timestamp", "time", "epoch")
_NAME_KEYS = ("name", "param", "pid")
_VALUE_KEYS = ("value", "val")


@dataclass(slots=True)
class HistorySyncResult:
    """Outcome counters of one sync run (surfaced in diagnostics)."""

    ran: bool = False
    source: str | None = None  # "export" | "files" | None
    rows_fetched: int = 0
    rows_used: int = 0
    rows_rejected: int = 0
    hours_imported: int = 0
    hours_skipped_existing: int = 0
    pids_imported: int = 0
    pids_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    watermark: float | None = None


@dataclass(slots=True)
class _Bucket:
    """Accumulates one PID's values within one hour."""

    total: float = 0.0
    count: int = 0
    minimum: float = 0.0
    maximum: float = 0.0

    def add(self, value: float) -> None:
        if self.count == 0:
            self.minimum = value
            self.maximum = value
        else:
            self.minimum = min(self.minimum, value)
            self.maximum = max(self.maximum, value)
        self.total += value
        self.count += 1

    @property
    def mean(self) -> float:
        return self.total / self.count


def _coerce_float(value: Any) -> float | None:
    """Return a finite float from a device value, or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if isfinite(value) else None
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
        return number if isfinite(number) else None
    return None


def _first_key(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row:
            return row[key]
    return None


def normalize_row(raw: Any) -> tuple[float, str, float] | None:
    """Validate one raw log row into (ts, name, value), or None.

    Accepts dict rows with aliased field names. Anything malformed —
    non-dict, missing fields, non-finite values, absurd names — is
    rejected (the caller counts rejects; a bad row never aborts a sync).
    """
    if not isinstance(raw, dict):
        return None
    ts = _coerce_float(_first_key(raw, _TS_KEYS))
    if ts is None or ts <= 0:
        return None
    name = _first_key(raw, _NAME_KEYS)
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > MAX_PID_KEY_LENGTH
    ):
        return None
    value = _coerce_float(_first_key(raw, _VALUE_KEYS))
    if value is None:
        return None
    return ts, name.strip(), value


def parse_jsonl(text: str) -> tuple[list[dict[str, Any]], int]:
    """Parse NDJSON/JSONL text into raw rows; returns (rows, undecodable)."""
    rows: list[dict[str, Any]] = []
    bad = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            decoded = json.loads(stripped)
        except ValueError:
            bad += 1
            continue
        if isinstance(decoded, dict):
            rows.append(decoded)
        else:
            bad += 1
    return rows, bad


def parse_csv(text: str) -> tuple[list[dict[str, Any]], int]:
    """Parse CSV log text into raw rows; returns (rows, undecodable).

    The first row must be a header naming (aliases of) ts/name/value
    columns; unknown columns are ignored.
    """
    rows: list[dict[str, Any]] = []
    bad = 0
    try:
        reader = csv.DictReader(io.StringIO(text))
        for record in reader:
            if not isinstance(record, dict):
                bad += 1
                continue
            rows.append({k: v for k, v in record.items() if isinstance(k, str)})
    except csv.Error:
        return rows, bad + 1
    return rows, bad


def _in_window(
    ts: float, *, oldest: float, now_ts: float, current_hour: int,
) -> bool:
    """Sanity window: nothing older than the watermark/age limit, nothing
    from the future (bad RTC), nothing in the current (incomplete,
    live-recorded) hour. ``oldest`` is inclusive-start: a row exactly at
    the watermark belongs to the first hour not yet imported."""
    return oldest <= ts <= now_ts + HISTORY_FUTURE_SKEW and ts < current_hour


def _discard_unfinished_hours(
    remaining: list[Any],
    buckets: dict[str, dict[int, _Bucket]],
    result: HistorySyncResult,
    *,
    oldest: float,
    now_ts: float,
    current_hour: int,
) -> None:
    """Drop bucket hours that may be missing rows left beyond the row cap.

    Any hour at or after the earliest unprocessed in-window row may hold
    only part of its rows; importing it would freeze wrong mean/min/max
    behind the watermark forever. Dropping the whole hour keeps it ahead
    of the watermark, so the next sync re-fetches and imports it complete.
    """
    cutoff: int | None = None
    for raw in remaining:
        row = normalize_row(raw)
        if row is None:
            continue
        ts = row[0]
        if not _in_window(
            ts, oldest=oldest, now_ts=now_ts, current_hour=current_hour,
        ):
            continue
        hour = int(ts // 3600) * 3600
        if cutoff is None or hour < cutoff:
            cutoff = hour
    if cutoff is None:
        return
    dropped = 0
    for name in list(buckets):
        pid_buckets = buckets[name]
        for hour in [h for h in pid_buckets if h >= cutoff]:
            del pid_buckets[hour]
            dropped += 1
        if not pid_buckets:
            del buckets[name]
    if dropped:
        result.errors.append(
            f"{dropped} hour bucket(s) cut short by the row cap deferred",
        )


def aggregate_rows(
    raw_rows: list[Any],
    *,
    since: float,
    now_ts: float,
    result: HistorySyncResult,
) -> dict[str, dict[int, _Bucket]]:
    """Aggregate raw rows into per-PID, per-hour buckets, bounded.

    Rows outside [since, current-hour) or beyond the sanity window are
    rejected. Returns {pid_name: {hour_epoch: bucket}}. When the row cap
    is hit, hours the leftover rows could still touch are discarded so a
    partially-aggregated hour is never imported.
    """
    current_hour = int(now_ts // 3600) * 3600
    oldest = max(since, now_ts - HISTORY_MAX_AGE_DAYS * 86400)
    buckets: dict[str, dict[int, _Bucket]] = {}
    used = 0
    for index, raw in enumerate(raw_rows):
        if used >= HISTORY_MAX_ROWS_PER_SYNC:
            result.errors.append("row cap reached; remaining rows deferred")
            _discard_unfinished_hours(
                raw_rows[index:],
                buckets,
                result,
                oldest=oldest,
                now_ts=now_ts,
                current_hour=current_hour,
            )
            break
        row = normalize_row(raw)
        if row is None:
            result.rows_rejected += 1
            continue
        ts, name, value = row
        if not _in_window(
            ts, oldest=oldest, now_ts=now_ts, current_hour=current_hour,
        ):
            result.rows_rejected += 1
            continue
        pid_buckets = buckets.get(name)
        if pid_buckets is None:
            if len(buckets) >= HISTORY_MAX_PIDS_PER_SYNC:
                result.rows_rejected += 1
                continue
            pid_buckets = buckets[name] = {}
        hour = int(ts // 3600) * 3600
        bucket = pid_buckets.get(hour)
        if bucket is None:
            bucket = pid_buckets[hour] = _Bucket()
        bucket.add(value)
        used += 1
        result.rows_used = used
    return buckets


class HistorySync:
    """Per-entry history backfill orchestrator."""

    def __init__(self, hass: HomeAssistant, entry: WiCANConfigEntry) -> None:
        """Initialize the sync for one config entry."""
        self._hass = hass
        self._entry = entry
        self._store: Store[dict[str, Any]] = Store(
            hass, HISTORY_STORAGE_VERSION, f"{DOMAIN}.history_{entry.entry_id}",
        )

    # -- watermark ---------------------------------------------------------

    async def async_get_watermark(self) -> float:
        """Return the newest timestamp already imported (0.0 = never)."""
        data = await self._store.async_load()
        if isinstance(data, dict):
            watermark = _coerce_float(data.get("watermark"))
            if watermark is not None and watermark > 0:
                return watermark
        return 0.0

    async def _async_save_watermark(self, watermark: float) -> None:
        await self._store.async_save({"watermark": watermark})

    async def async_remove(self) -> None:
        """Delete the persisted watermark (config entry removed)."""
        await self._store.async_remove()

    # -- fetching ----------------------------------------------------------

    async def _async_fetch_export(
        self,
        api: MeatPiApiClient,
        since: float,
        result: HistorySyncResult,
    ) -> list[Any] | None:
        """Fetch rows via the cursor export route; None when unsupported."""
        rows: list[Any] = []
        cursor = since
        for _ in range(HISTORY_EXPORT_MAX_PAGES):
            page = await api.async_export_log_rows(
                cursor, HISTORY_EXPORT_PAGE_ROWS,
            )
            if page is None:
                return None
            page_rows, next_since = page
            rows.extend(page_rows)
            if len(rows) >= HISTORY_MAX_ROWS_PER_SYNC:
                break
            if len(page_rows) < HISTORY_EXPORT_PAGE_ROWS:
                break
            if next_since is None or next_since <= cursor:
                # Device did not advance the cursor; stop rather than loop.
                result.errors.append("export cursor did not advance")
                break
            cursor = next_since
        return rows

    @staticmethod
    def _select_log_files(
        entries: list[dict[str, Any]],
        active_file: str | None,
        result: HistorySyncResult,
    ) -> list[str]:
        """Pick downloadable rotated log files, bounded and sorted oldest-first."""
        candidates: list[str] = []
        for entry in entries:
            name = entry.get("name")
            if (
                not isinstance(name, str)
                or entry.get("dir")
                or not name.startswith("dl_")
                or not name.endswith((".jsonl", ".csv", ".ndjson", ".log"))
            ):
                continue
            if name == active_file:
                continue  # write-locked; synced after rotation
            size = entry.get("size")
            if isinstance(size, int) and size > HISTORY_MAX_FILE_BYTES:
                result.errors.append(f"file too large, skipped: {name}")
                continue
            candidates.append(name)

        # Names embed the rotation epoch (dl_<epoch>.<ext>); oldest first.
        # Over the cap, the NEWEST files are the ones deferred: their rows
        # stay ahead of the watermark and the next sync picks them up,
        # whereas dropping the oldest would strand rows behind it forever.
        candidates.sort()
        if len(candidates) > HISTORY_MAX_FILES_PER_SYNC:
            result.errors.append(
                f"{len(candidates) - HISTORY_MAX_FILES_PER_SYNC} log file(s) "
                "beyond the per-sync cap were deferred",
            )
            candidates = candidates[:HISTORY_MAX_FILES_PER_SYNC]
        return candidates

    async def _async_fetch_files(
        self,
        api: MeatPiApiClient,
        result: HistorySyncResult,
    ) -> list[Any]:
        """Fetch rows by downloading rotated log files (current firmware).

        The per-row time-window filter in :func:`aggregate_rows` enforces
        the watermark; this fetch only bounds volume.
        """
        status = await api.async_get_logger_status()
        log_dir = "/sd/logs"
        active_file: str | None = None
        if status is not None:
            raw_dir = status.get("dir")
            if isinstance(raw_dir, str) and raw_dir.startswith("/"):
                log_dir = raw_dir
            raw_active = status.get("file")
            if isinstance(raw_active, str) and raw_active:
                active_file = raw_active

        entries = await api.async_fs_list(log_dir)
        candidates = self._select_log_files(entries, active_file, result)

        rows: list[Any] = []
        for name in candidates:
            try:
                text = await api.async_fs_download(
                    f"{log_dir}/{name}", max_bytes=HISTORY_MAX_FILE_BYTES,
                )
            except MeatPiApiError as err:
                result.errors.append(f"download failed for {name}: {err}")
                continue
            file_rows, bad = (
                parse_csv(text) if name.endswith(".csv") else parse_jsonl(text)
            )
            result.rows_rejected += bad
            rows.extend(file_rows)
            if len(rows) >= HISTORY_MAX_ROWS_PER_SYNC:
                break
        return rows

    # -- importing ---------------------------------------------------------

    async def _async_existing_hours(
        self,
        entity_ids: set[str],
        start: datetime,
    ) -> dict[str, set[int]]:
        """Return hours that already have statistics per entity (live wins)."""
        stats = await get_instance(self._hass).async_add_executor_job(
            statistics_during_period,
            self._hass,
            start,
            None,
            entity_ids,
            "hour",
            None,
            {"mean"},
        )
        existing: dict[str, set[int]] = {}
        for entity_id, rows in stats.items():
            hours: set[int] = set()
            for row in rows:
                raw_start = row.get("start")
                ts = (
                    raw_start
                    if isinstance(raw_start, (int, float))
                    else getattr(raw_start, "timestamp", lambda: None)()
                )
                if isinstance(ts, (int, float)):
                    hours.add(int(ts))
            existing[entity_id] = hours
        return existing

    async def _async_entity_metadata(
        self,
        entity_id: str,
        unit: str | None,
    ) -> dict[str, Any] | None:
        """Build import metadata for an entity, reusing existing stats metadata.

        Returns None when the entity's existing statistics are incompatible
        (sum-type) — importing mean rows there would corrupt them.
        """
        metadata_map = await get_instance(self._hass).async_add_executor_job(
            partial(get_metadata, self._hass, statistic_ids={entity_id}),
        )
        if entity_id in metadata_map:
            existing = metadata_map[entity_id][1]
            if existing.get("has_sum") or (
                existing.get("mean_type") == StatisticMeanType.NONE
            ):
                return None
            return {
                "source": "recorder",
                "statistic_id": entity_id,
                "name": None,
                "unit_of_measurement": existing.get("unit_of_measurement"),
                "unit_class": existing.get("unit_class"),
                "mean_type": existing.get(
                    "mean_type", StatisticMeanType.ARITHMETIC,
                ),
                "has_sum": False,
            }

        converter = STATISTIC_UNIT_TO_UNIT_CONVERTER.get(unit)
        return {
            "source": "recorder",
            "statistic_id": entity_id,
            "name": None,
            "unit_of_measurement": unit,
            "unit_class": converter.UNIT_CLASS if converter else None,
            "mean_type": StatisticMeanType.ARITHMETIC,
            "has_sum": False,
        }

    def _resolve_entities(
        self,
        pid_names: list[str],
    ) -> dict[str, tuple[str, str | None]]:
        """Map PID names to (entity_id, unit) for PIDs that back entities."""
        registry = er.async_get(self._hass)
        resolved: dict[str, tuple[str, str | None]] = {}
        for name in pid_names:
            entity_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{self._entry.entry_id}_pid_{name}",
            )
            if entity_id is None:
                continue
            registry_entry = registry.async_get(entity_id)
            unit = (
                registry_entry.unit_of_measurement
                if registry_entry is not None
                else None
            )
            resolved[name] = (entity_id, unit)
        return resolved

    # -- the sync ----------------------------------------------------------

    async def _async_import_buckets(
        self,
        resolved: dict[str, tuple[str, str | None]],
        buckets: dict[str, dict[int, _Bucket]],
        existing_hours: dict[str, set[int]],
        result: HistorySyncResult,
    ) -> tuple[float, float | None]:
        """Import per-PID hourly buckets.

        Returns ``(max_imported_ts, failed_floor)``: the newest imported
        hour end, and the earliest hour whose import *failed* (None when
        every import succeeded). The watermark must never pass
        ``failed_floor``, or the failed hours would be unrecoverable.
        Permanently incompatible entities (sum-type statistics) do not
        hold the watermark — retrying cannot fix them.

        Each PID is isolated: one failing import never blocks the others.
        Hours that already have live statistics are skipped (live wins).
        """
        max_imported_ts = 0.0
        failed_floor: float | None = None
        for name, (entity_id, unit) in resolved.items():
            taken = existing_hours.get(entity_id, set())
            pending: list[int] = []
            statistics = []
            skipped_hours = 0
            for hour, bucket in sorted(buckets[name].items()):
                if hour in taken:
                    skipped_hours += 1
                    continue
                pending.append(hour)
                statistics.append(
                    {
                        "start": dt_util.utc_from_timestamp(hour),
                        "mean": bucket.mean,
                        "min": bucket.minimum,
                        "max": bucket.maximum,
                    },
                )
            result.hours_skipped_existing += skipped_hours
            if not statistics:
                continue
            try:
                metadata = await self._async_entity_metadata(entity_id, unit)
                if metadata is None:
                    result.errors.append(
                        f"{entity_id}: incompatible existing statistics",
                    )
                    continue
                async_import_statistics(self._hass, metadata, statistics)  # type: ignore[arg-type]
            except Exception as err:  # isolate per PID
                _LOGGER.warning(
                    "History sync: import failed for %s: %s", entity_id, err,
                )
                result.errors.append(f"{entity_id}: {err}")
                if failed_floor is None or pending[0] < failed_floor:
                    failed_floor = float(pending[0])
                continue
            result.pids_imported += 1
            result.hours_imported += len(statistics)
            last_hour_end = max(hour for hour in buckets[name]) + 3600
            max_imported_ts = max(max_imported_ts, last_hour_end)
        return max_imported_ts, failed_floor

    async def _async_collect(
        self,
        api: MeatPiApiClient,
        since: float,
        now_ts: float,
        result: HistorySyncResult,
    ) -> tuple[dict[str, tuple[str, str | None]], dict[str, dict[int, _Bucket]]] | None:
        """Fetch, aggregate, and resolve; None when there is nothing to import."""
        try:
            raw_rows = await self._async_fetch_export(api, since, result)
            if raw_rows is None:
                result.source = "files"
                raw_rows = await self._async_fetch_files(api, result)
            else:
                result.source = "export"
        except MeatPiApiError as err:
            result.errors.append(f"fetch failed: {err}")
            return None

        result.ran = True
        result.rows_fetched = len(raw_rows)
        if not raw_rows:
            return None

        buckets = aggregate_rows(
            raw_rows, since=since, now_ts=now_ts, result=result,
        )
        if not buckets:
            return None

        resolved = self._resolve_entities(list(buckets))
        result.pids_skipped = len(buckets) - len(resolved)
        if not resolved:
            return None
        return resolved, buckets

    async def async_sync(self) -> HistorySyncResult:
        """Run one backfill pass. Never raises."""
        result = HistorySyncResult()
        runtime = getattr(self._entry, "runtime_data", None)
        api = getattr(runtime, "api", None)
        if api is None:
            return result

        now_ts = dt_util.utcnow().timestamp()
        watermark = await self.async_get_watermark()
        since = max(watermark, now_ts - HISTORY_MAX_AGE_DAYS * 86400)

        collected = await self._async_collect(api, since, now_ts, result)
        if collected is None:
            return result
        resolved, buckets = collected

        earliest_hour = min(
            hour for pid in resolved for hour in buckets[pid]
        )
        try:
            existing_hours = await self._async_existing_hours(
                {entity_id for entity_id, _ in resolved.values()},
                dt_util.utc_from_timestamp(earliest_hour),
            )
        except Exception as err:  # recorder unavailable — never break setup
            _LOGGER.warning("History sync: cannot query recorder: %s", err)
            result.errors.append(f"recorder query failed: {err}")
            return result

        max_imported_ts, failed_floor = await self._async_import_buckets(
            resolved, buckets, existing_hours, result,
        )

        new_watermark = max_imported_ts
        if failed_floor is not None:
            # A failed PID's hours must stay ahead of the watermark so
            # the next sync re-fetches and re-imports them (idempotent).
            new_watermark = min(new_watermark, failed_floor)

        if result.pids_imported and new_watermark > watermark:
            result.watermark = new_watermark
            try:
                await self._async_save_watermark(new_watermark)
            except Exception as err:  # storage failure only re-syncs later
                _LOGGER.warning("History sync: watermark save failed: %s", err)
                result.errors.append(f"watermark save failed: {err}")

        _LOGGER.info(
            "History sync for %s (%s): %d rows -> %d hour(s) across %d "
            "sensor(s); %d hour(s) already recorded live; %d row(s) rejected",
            self._entry.title,
            result.source,
            result.rows_fetched,
            result.hours_imported,
            result.pids_imported,
            result.hours_skipped_existing,
            result.rows_rejected,
        )
        return result


async def async_sync_history(
    hass: HomeAssistant,
    entry: WiCANConfigEntry,
) -> HistorySyncResult:
    """Run one history backfill pass for a config entry. Never raises."""
    try:
        return await HistorySync(hass, entry).async_sync()
    except Exception:  # Defensive: backfill must never break anything else.
        _LOGGER.exception("Unexpected error during history sync")
        return HistorySyncResult()


async def async_remove_history_store(
    hass: HomeAssistant,
    entry: WiCANConfigEntry,
) -> None:
    """Delete the watermark store when the config entry is removed."""
    await HistorySync(hass, entry).async_remove()
