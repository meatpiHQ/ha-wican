# Historical Data Sync — SD-card Backfill into Home Assistant

> Status: **IMPLEMENTED (phase 1) 2026-07-10** — `history.py` +
> `_schedule_history_sync` wiring, both fetch paths (cursor export route
> and rotated-file downloads over `/api/fs`), hourly statistics import,
> watermark store, options kill switch, diagnostics counters. Tested by
> `tests/test_history.py` (43 unit tests) and `tests/test_history_e2e.py`
> (simulator end-to-end incl. a real in-memory-recorder round trip).
> Phase 2 (sum-type totals / energy dashboard) and the trip viewer remain
> future work.
>
> Original question: a WiCAN (or any MeatPi device)
> drives offline, logs AutoPID data to SD with timestamps; when it
> reconnects to Home Assistant, can the integration backfill the gap so
> the data appears at the time it was recorded? Is that allowed, and is
> it doable?

## TL;DR

**Yes — doable and officially supported, with one architectural caveat.**

Home Assistant has two history layers, and only one of them accepts
data with past timestamps:

| Layer | Granularity | Backfill possible? |
|---|---|---|
| **States** (history detail view, logbook, automations) | every state change, ~seconds | **No.** `hass.states.async_set` always stamps "now"; there is no supported API to write states into the past, and direct recorder-DB writes are explicitly fragile/unsupported (the `ha-historical-sensor` project removed exactly this in v3 for that reason). |
| **Long-term statistics (LTS)** (statistics charts, more-info graph history, energy dashboard) | hourly rows: mean/min/max (measurements) or sum (totals) | **Yes — official recorder API.** `async_import_statistics` (onto real entities) and `async_add_external_statistics` (external ids). This is what core utility integrations (e.g. Opower) use to backfill days of past energy data. |

So the user story works like this: the drive's data lands as **hourly
mean/min/max points at the recorded times** in each PID sensor's
long-term statistics — the gap in the long-term graph is filled. What it
will *not* do is reconstruct second-by-second traces in the short-term
history/logbook, and it will never retro-trigger automations (that is by
design and a good thing).

For most vehicle telemetry this is exactly the right shape: battery SoC,
battery voltage, fuel level, coolant temperature, odometer, energy used —
hourly envelopes tell the story. For "replay my RPM trace through that
mountain pass" it is not enough; that use case belongs in an external
TSDB (see Alternatives).

## Why it's "allowed"

`homeassistant.components.recorder.statistics` exposes the import
functions as public, documented API:

- `async_import_statistics(hass, metadata, statistics)` — statistics
  belonging to an **existing entity**: `statistic_id` = the entity_id,
  `source` = `"recorder"`. The entity must have a `state_class` — our
  PID sensors already latch `MEASUREMENT` once numeric.
- `async_add_external_statistics(hass, metadata, statistics)` —
  **external** ids like `wican:soc_history` (`domain:id`), independent
  of any entity; selectable in statistics cards and the energy
  dashboard.

Both take hourly rows: `StatisticData(start=<timezone-aware, top-of-hour>,
mean=…, min=…, max=…)` (or `sum=`/`state=` for metered totals).
Re-importing the same hour **updates** the row, so a resync after a
crash is idempotent — no duplicate protection gymnastics needed.

**API currency note (important, we're on 2026.x):** since the
2025-10-16 recorder changes, `StatisticMetaData` must include
`unit_class` (the unit-converter key, or `None`) — mandatory since
2025.11 — and `mean_type` (`StatisticMeanType.ARITHMETIC` for our
measurements; replaces `has_mean`) — mandatory from 2026.11.

## Proposed design (integration side)

The V6 firmware already provides almost everything:

- `data_logger` writes the params stream to `/sd/logs/dl_<epoch>.*`
  (sqlite/csv/jsonl) with rotation and retention (`device-contract/HTTP_API.md` §6e6);
- `rtc_manager` + SNTP + the `time_synced` status bit give trustworthy
  UTC timestamps — non-negotiable for backfill;
- `/api/fs/list` + `/api/fs/download` can fetch rotated files (the
  active file is write-locked; pause via `/api/logger/gate` or sync
  only rotated files);
- parameter names in the log match the `autopid_data` webhook keys, so
  the log row ↔ PID sensor entity mapping is direct.

Flow:

1. **Trigger** — on reconnect (first successful push after the
   coordinator was stale) and/or periodically, if the capability set
   contains `data_logger`.
2. **Watermark** — the integration stores "last imported timestamp"
   (per entry). Only rows newer than the watermark are fetched.
3. **Fetch** — `GET /api/logger` for stream status; list `/sd/logs`;
   download rotated `dl_*.jsonl`/`.csv` files newer than the watermark
   (bounded chunks; never the write-locked active file).
4. **Aggregate** — bucket rows per PID per hour → mean/min/max.
   Skip rows with timestamps before the device's first valid time sync.
5. **Import** — for each PID that already backs an entity:
   `async_import_statistics` with `statistic_id=<entity_id>`,
   `source="recorder"`, the entity's unit, `unit_class` from its device
   class, `mean_type=ARITHMETIC`. Hours where HA already recorded live
   statistics are skipped (check `statistics_during_period` first) so
   live data always wins.
6. **Advance the watermark**, persist it, done. A device wiping its SD
   card or going backward in time (RTC glitch) can never corrupt
   anything: worst case is an hour's row being re-written.

Manifest: add `recorder` to `after_dependencies`.

Phase 2 (optional): metered totals (odometer, kWh charged, fuel used) as
`sum`-type statistics — these are what make the **energy dashboard**
work for EV charging history. Needs monotonic-counter handling
(`last_reset`), so keep it a separate iteration.

Out of scope / not possible:

- **GPS backfill**: `device_tracker` is a states-only entity; there is
  no statistics representation of a location. A recorded track cannot
  be inserted into HA history. (If wanted: export the track as GPX from
  the SD card, or feed an external TSDB.)
- **Logbook/automation replay**: never; by design.

## Sub-hour resolution: confirmed not possible natively

Verified against the recorder source (2026.x): `async_import_statistics`
and `async_add_external_statistics` are documented and implemented as
**hourly-only** imports. There is no public API for the 5-minute
short-term statistics table, and none for raw states. So no matter how
fine the SD-card data is, HA-native history can only absorb it at hourly
resolution. Note this is symmetric with live data: even live-recorded
sub-hour detail (states + 5-min stats) is purged after ~`purge_keep_days`
(default 10 days) — hourly LTS is the *only* permanent layer HA has,
which is why no finer backfill API exists.

Full-resolution options that still surface in the HA UI (not recorder):
1. A `wican.get_trip`-style service / websocket command returning the
   raw rows for a time range, rendered by a dashboard card that accepts
   a data callback (e.g. apexcharts-card `data_generator`, or a bespoke
   trip-viewer card). Data stays in our own store (or is fetched from
   the device on demand); graphs are full fidelity.
2. External TSDB (InfluxDB/VictoriaMetrics) fed at recorded timestamps,
   viewed via an embedded Grafana panel.

## Alternatives considered

- **Replaying buffered webhooks on reconnect** — does not work: HA
  stamps each state write with the arrival time, so a replay would
  compress the whole drive into "now" and *corrupt* history rather than
  fill it. The firmware should never do this.
- **Writing to the recorder database directly** — unsupported, breaks
  state chains and schema migrations; disqualifying for the quality
  scale. Rejected.
- **External TSDB (InfluxDB/VictoriaMetrics + Grafana)** — accepts
  arbitrary timestamps at full resolution; the right answer for
  second-level trace replay, but it lives outside HA-native history.
  Could be a later "export" feature, not a substitute.

## Firmware ask

One addition makes this dramatically cleaner than file-scraping — a
cursor-based export route on `data_logger` (recorded as ask #8 in
`device-contract/FIRMWARE_API_FEEDBACK.md`):

```
GET /api/logger/export?stream=params&since=<epoch>&limit=<n>
→ JSONL rows: {"ts": 1752102000, "name": "SOC", "value": 71.5}
  + header X-Next-Since: <epoch of next unread row>
```

- Serves from whichever storage format is configured (HA never needs to
  parse sqlite/binary or know file layouts, and the active-file write
  lock stops being HA's problem).
- Only rows recorded while `time_synced` was true (or a per-row
  validity flag).
- Names identical to the `autopid_data` webhook keys (already true).
- Optional: an "offline buffer" logger mode — log only while webhook
  delivery is failing — so the SD card isn't chewed through when HA is
  reachable anyway.

Until that route exists, the `/api/fs` + jsonl/csv path above works
against current V6 firmware as-is.

## Verdict

- **Allowed?** Yes — public recorder API, used by core integrations;
  fully quality-scale compatible.
- **Doable?** Yes — V6 firmware already logs what we need with
  timestamps; the integration work is fetch → hourly aggregate →
  `async_import_statistics`.
- **Honest limitation to set with users:** the gap fills in the
  long-term statistics view at hourly resolution; the fine-grained
  history detail view and logbook stay empty for the offline period,
  and location history cannot be backfilled at all.

Sources: [recorder statistics API changes (dev blog, 2025-10-16)](https://developers.home-assistant.io/blog/2025/10/16/recorder-statistics-api-changes/),
[ha-historical-sensor](https://github.com/ldotlopez/ha-historical-sensor) (statistics-only rationale),
[homeassistant-statistics importer](https://github.com/klausj1/homeassistant-statistics),
[frontend discussion: backfilling history graphs with statistics](https://github.com/home-assistant/frontend/discussions/19760).
