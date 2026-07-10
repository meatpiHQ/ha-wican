# MeatPi Integration — Restructuring Plan (WiCAN → MeatPi, multi-device)

> Status: **EXECUTED 2026-07-10** — this document is both the plan and the
> record of its execution. Each phase ends with an "Executed" note stating
> what was actually done.

## 1. Goal

Turn the WiCAN-specific integration into a **MeatPi brand integration**:

- **MeatPi is the brand, WiCAN is a device.** The integration presents
  itself as "MeatPi"; each configured device carries its own model
  (WiCAN OBD, WiCAN USB, WiCAN Pro, ESPNetlink, …).
- **Generic device-type framework** so a new product is added by
  *declaring* it (a profile + entity descriptions), not by forking code.
- **Nothing breaks for existing users**: a WiCAN on old firmware
  (pre-V6, webhook-only) must keep working unchanged after the
  integration update.
- **Firmware V6 ready**: WiCAN V6 ships a full HTTP API
  (`notes/HTTP_API.md`). The integration gains a V6 API client and uses
  it for capability discovery and device *control* (buttons/actions),
  while telemetry stays push-based (webhooks) on every firmware.
- **Quality scale stays at the platinum bar**: strict typing, full test
  coverage of new code, injected web sessions, no blocking I/O.

## 2. Key decisions (and why)

### D1 — Keep the Home Assistant domain `wican`; rebrand everything user-facing to MeatPi

The HA *domain* is a permanent internal identifier: every existing
config entry, entity-registry row (`platform: wican`), device-registry
identifier (`(wican, <device_id>)`), and stored unique_id is keyed by
it. Home Assistant has **no supported way to move registry entries to a
new domain** — a rename would orphan every existing install (entities
lose history, automations break) or require unsupported registry-file
surgery, which is disqualifying for a platinum-bar integration.

This is the same trade-off HA core makes: domains never change even
when brands do (`hue` is Signify, `shelly` covers dozens of models,
`mobile_app` covers every phone). What users see is the **manifest
name**, and that is now **"MeatPi"**. When the integration is submitted
to core, the [brands repository](https://github.com/home-assistant/brands)
gets a `meatpi` **brand** whose `integrations` list contains `wican` —
users then find it by searching "MeatPi" and see the MeatPi logo.

If a hard domain flip to `meatpi` is ever wanted, it is a separate,
opt-in project: ship a `meatpi` integration plus a `wican` shim that
captures each old entry's entity_ids, creates a twin `meatpi` entry,
and renames the new entities back to the old entity_ids (recorder
history and long-term statistics follow the entity_id, so history
survives). It is doable but adds real risk for purely cosmetic benefit;
it is explicitly **out of scope** here.

### D2 — Device types are declarative profiles

`devices.py` introduces `MeatPiDeviceProfile`, a frozen dataclass
describing one product: slug, display model, zeroconf matching hints,
firmware-repo coordinates for the update entity, and which telemetry
sections of the push payload it is expected to produce. A registry maps
`device_type` slugs to profiles; each config entry stores its
`device_type` (backfilled by config-entry migration, inferred from the
stored `hw_version`). Adding a product = adding a profile (plus entity
descriptions if it has product-specific entities). See
`notes/ADDING_A_DEVICE.md` for the full recipe.

Declared today: `wican` (OBD), `wican_usb`, `wican_pro`, `espnetlink`
(profile present; hardware not yet released), and `meatpi` (generic
fallback for any future V6-API device so it works before it gets a
dedicated profile).

### D3 — Two channels: webhook push for telemetry, HTTP API for control

- **Telemetry** stays exactly as today: the device POSTs
  `{status, autopid_data, config, gps}` to a HA webhook. This is the
  only channel old firmware has, and V6 keeps it (the `ha_webhooks`
  firmware component owns `/api/webhook`). Nothing on this path changed
  — the hardened coordinator/webhook pipeline is untouched.
- **Control + discovery** uses the V6 HTTP API when present. At setup
  (and when the device's address or firmware version changes) the
  integration probes `GET /api/status`; a V6 device is recognized by
  its `bits` object, and `GET /api/settings` then yields the list of
  firmware components — the **capability set**. Capabilities gate which
  control entities exist. A legacy device fails the probe and simply
  gets no control entities: old behavior, zero risk.

This mirrors how **WLED** works (push/notify for state, REST for
control, one `WLEDDevice` info object driving entity creation) and how
**ESPHome** works (the device *tells* the integration what entities it
has; the integration hardcodes nothing per-product). We take
capability-driven entity creation from ESPHome and the
client-object-plus-profiles shape from WLED.

### D4 — Control entities are capability-gated, starting with buttons

New `button` platform:

| Button | Capability required | Endpoint |
|---|---|---|
| Restart | V6 API (`restart_tracker` is part of every V6 build) | `POST /api/restart` |
| Sync time | `rtc_manager` component present | `POST /api/rtc/sync` |

Buttons are added **dynamically** when the capability probe completes
(same dispatcher pattern the dynamic PID sensors use), so a device that
is offline at HA start, or that gets upgraded to V6 later, grows its
buttons without a reload. The same mechanism is the template for future
switches/numbers/selects (LED alert, logger gate, DTC scan, …).

### D5 — Firmware asks are documented, not blockers

Everything above works against the **current** V6 API. The gaps that
would make the integration cleaner/fully generic are collected in
`notes/FIRMWARE_API_FEEDBACK.md` (device identity in `/api/status`, a
shared `_meatpi._tcp` mDNS service, a payload schema version, a generic
action-invocation endpoint, V6 OTA route). None of them block this
release.

## 3. Architecture after this change

```
custom_components/wican/          # domain stays `wican` (D1); brand = MeatPi
├── manifest.json                 # name: "MeatPi"
├── const.py                      # + API paths, capability consts
├── devices.py                    # NEW — MeatPiDeviceProfile + registry + inference
├── api.py                        # NEW — MeatPiApiClient (V6 HTTP API) + DeviceCapabilities
├── coordinator.py                # unchanged — hardened push pipeline
├── __init__.py                   # + entry migration, capability probe task, re-probe hooks
├── models.py                     # runtime data + api client + capabilities + profile
├── entity.py                     # device_info: model from profile, manufacturer MeatPi
├── button.py                     # NEW — capability-gated control buttons
├── sensor.py / binary_sensor.py / device_tracker.py / update.py   # unchanged behavior
├── config_flow.py                # VERSION 1 / MINOR 2; stores device_type
├── exceptions.py                 # + MeatPiApiError
└── translations/en.json          # + buttons, MeatPi wording
```

Data flow:

```
                    ┌────────────── telemetry (all firmware) ───────────────┐
 device ── POST /api/webhook/<id> ──▶ webhook handler ─▶ coordinator ─▶ entities
                                                        (sensors, binary, tracker)

                    ┌────────────── control (V6 firmware only) ─────────────┐
 setup ─▶ probe /api/status + /api/settings ─▶ DeviceCapabilities ─▶ dispatcher
                                                     │                    │
                                              runtime_data         button platform
                                                     │                    │
 button press ──────────────▶ MeatPiApiClient ─▶ POST /api/restart, /api/rtc/sync
```

## 4. Execution phases

### Phase 1 — Rebrand (no behavior change)
- `manifest.json`: name **MeatPi**, docs/issue links → meatpiHQ repos,
  version bump.
- `hacs.json` name → MeatPi.
- `entity.py`: model falls back to the profile's display model instead
  of "Unknown"; manufacturer already MeatPi.
- Translations/README/pyproject reworded: MeatPi is the integration,
  WiCAN/ESPNetlink are devices.

**Executed:** as described.

### Phase 2 — Device-type framework
- `devices.py`: `MeatPiDeviceProfile`, `DEVICE_PROFILES`,
  `infer_device_type(hw_version)`, `get_profile(device_type)`.
- Config-entry migration `async_migrate_entry` (1.1 → 1.2): backfills
  `device_type` from stored `hw_version`. Old entries load seamlessly.
- `config_flow.py`: `MINOR_VERSION = 2`; new entries store
  `device_type` (inferred at discovery/first webhook).

**Executed:** as described; migration is forward-compatible (a future
1.x minor bump only adds keys).

### Phase 3 — V6 API client + capability discovery
- `api.py`: `MeatPiApiClient` (injected `aiohttp` session — platinum
  rule), `DeviceCapabilities` (api_level + component set),
  `async_probe()`, `async_restart()`, `async_sync_time()`.
- Setup schedules a non-blocking probe task; result lands in
  `runtime_data.capabilities` and fires the
  `wican_capabilities_<entry_id>` dispatcher signal.
- Re-probe when the device reports a new firmware version or a new
  address (hooked into `_persist_device_reported_info`) — a device OTA'd
  to V6 grows control entities without a reload.

**Executed:** as described. Probe failure ⇒ legacy capabilities; never
fails setup.

### Phase 4 — Button platform
- `button.py` with `RESTART` (device_class RESTART, config category)
  and `SYNC_TIME` (config category) descriptions, each carrying its
  required capability + the client coroutine to call.
- Entities added on the capabilities signal; duplicate-add guarded;
  press failures raise `HomeAssistantError` with translation keys.

**Executed:** as described.

### Phase 5 — Tests
- New suites: `test_devices.py`, `test_api.py`, `test_button.py`,
  `test_config_migration.py`, `test_capability_probe.py` (60 new tests).
- All 421 pre-existing tests stay green; `tests/conftest.py` gained an
  autouse fixture keeping the capability probe off the network so the
  legacy suites behave exactly as before control support existed.

**Executed — final gates (2026-07-10, after the Phase-7 robustness round):**
- pytest: **512 passed**, 0 failed, coverage **97.13%**
  (api.py, devices.py, button.py, binary_sensor.py, diagnostics.py,
  coordinator.py, device_tracker.py at 100%)
- mypy --strict: 0 errors (19 source files)
- ruff: clean on `custom_components/wican` (including 3 pre-existing
  findings fixed along the way)

### Phase 6 — Documentation
- `notes/ADDING_A_DEVICE.md` — the full "add a new MeatPi device" guide
  (integration-side steps + what the firmware must implement).
- `notes/FIRMWARE_API_FEEDBACK.md` — requested WiCAN/ESPNetlink
  firmware API changes.
- README (MeatPi brand, device table, button platform, developer links),
  `quality_scale.yaml` (both), `hacs.json`, `pyproject.toml`,
  `notes/HTTP_API.md` + `notes/DEVICE_COMPATIBILITY_GUIDE.md`
  cross-references refreshed.

**Executed:** as described.

### Phase 7 — Robustness round: device-type simulation + reliability fixes
(added 2026-07-10, same day, on request)

**Simulator**: `tests/device_sim.py` gained `DEVICE_PRESETS` (legacy WiCAN
OBD, V6 WiCAN Pro, V6 WiCAN USB without RTC, ESPNetlink, generic future
MeatPi device) and `SimulatedDeviceApi` — the device-side HTTP surface
wired into the aiohttp mocker, answering the *real* capability probe and
control commands with programmable failure modes (`offline`, `timeout`,
`forbidden` 403 lockdown, `error` 500, `garbage`, `html`) plus raw
payload-override hooks for hostile-shape injection.

**New suite** `tests/test_device_type_matrix.py` (27 tests, no integration
internals stubbed): per-type end-to-end paths, the OTA-to-V6 lifecycle,
device-asleep-at-setup recovery, unreachable/downgrade re-probes, control
failure injection, malformed probe bodies, component floods,
firmware-flapping probe coalescing, and multi-device-type isolation.

**Reliability fixes the suite drove into the integration:**
1. `async_probe` now distinguishes *unreachable* (returns None → keep
   last-known capabilities; new `MeatPiApiConnectionError`) from a
   *positive legacy answer* (downgrade). A flake — including losing the
   device between the two probe requests — can no longer wipe known
   capabilities.
2. Push-triggered probe retry: while no probe has ever reached the device
   (`runtime_data.probe_successful` False), each telemetry push retries
   the probe (rate-limited to one per `PROBE_RETRY_INTERVAL`), so a V6
   device asleep at HA startup gains its control entities when it wakes.
3. Bounds on device API responses: component count capped
   (`MAX_API_COMPONENTS`), component name length capped, declared
   response bodies over `MAX_API_RESPONSE_BYTES` refused unread.
4. Device-type inference hardened: short keywords ("pro", "usb") match
   whole tokens only — hw "MeatPi ProtoBoard" no longer classifies as
   WiCAN Pro — and hardware-version sync re-infers only *within* the
   WiCAN family, never clobbering a discovery-provided device_type
   (espnetlink, meatpi, future slugs).
5. Profile-gated entities: OBD-only entities (ECU Online) are created
   only on device types that poll a vehicle (`requires_obd` +
   `profile.supports_obd_pids`) — a GPS/LTE product no longer gets a
   meaningless ECU sensor.

### Phase 8 — History backfill (offline-drive SD-card data → long-term statistics)
(added 2026-07-10, same day, on request; design: `HISTORICAL_DATA_SYNC.md`)

- **`history.py`**: fetch → validate → hourly-aggregate → import pipeline.
  Two fetch paths: the cursor-based `GET /api/logger/export` route
  (firmware ask #8; preferred when the firmware ships it) with automatic
  fallback to rotated-file downloads over `/api/fs` (works against
  current V6 firmware; jsonl + csv parsers with tolerant field aliases).
- **Import**: official `async_import_statistics` onto the existing PID
  sensor entities (statistic_id = entity_id, `mean_type=ARITHMETIC`,
  `unit_class` derived or reused from existing metadata — 2025.11/2026.11
  API requirements met). Live data always wins: existing hours and the
  current hour are never imported; incompatible (sum-type) metadata is
  refused; per-PID import isolation.
- **Triggers**: on capability probe finding `data_logger`, and on a
  telemetry push that ends a staleness gap (the "arrived home" moment,
  detected by the coordinator). Coalesced + rate-limited
  (`HISTORY_SYNC_MIN_INTERVAL`), with an options kill switch
  (`history_sync`, default on).
- **Watermark** per entry in a `Store` (advances only after a successful
  import; removed with the entry via `async_remove_entry`); re-imports
  are idempotent by recorder semantics.
- **Hardening**: bounds on rows/files/bytes/PIDs per sync, timestamp
  sanity window (max age, future skew), cursor-stall detection, active
  (write-locked) log file skipped, oversized files skipped, garbage
  rows/lines counted and dropped, every failure contained (a sync can
  never disturb telemetry or control). Diagnostics expose the last run's
  counters.
- **Simulator**: `SimulatedDeviceApi` serves `/api/logger`,
  `/api/logger/export` (cursor protocol), `/api/fs/list`,
  `/api/fs/download` (423 on the active file), with
  `seed_log_rows(via="export"|"file")` seeding.
- **Tests**: `test_history.py` (43 unit tests: parsers, normalization,
  aggregation windows/caps, file selection, full sync flows against a
  mocked recorder) and `test_history_e2e.py` (10 end-to-end: export +
  file paths through real webhook/probe/client, kill switch, rate limit,
  offline-mid-sync, hostile bodies, watermark, plus **two real
  in-memory-recorder round-trips** verifying imported statistics read
  back correctly and re-imports don't duplicate).

Known limitation (by HA design, documented in README): backfill is
hourly; raw states/logbook stay empty for the offline period; GPS
location history cannot be backfilled.

### Phase 9 — Remote device catalog (new products without integration releases)
(added 2026-07-10, same day, on request; spec: `DEVICE_CATALOG.md`)

- **`catalog.py`**: fetch/validate/apply/persist manager for a JSON
  catalog of device-type definitions published on GitHub (placeholder
  repo `meatpiHQ/meatpi-devices` — pending). Bundled fallback in
  `data/device_catalog.json`; fetched copy persisted in **`.storage`**
  (deliberately not the package dir — the params.json lesson); refresh
  at most once per 24 h; strict bounds on everything (types, slugs,
  keywords, sensors, sizes); an invalid/hostile/unreachable catalog
  degrades to the previous good copy, never breaks setup.
- **`devices.py`**: dynamic registry layered over the built-ins.
  Catalog profiles override same-named built-ins **except the reserved
  slugs** (`wican`, `wican_usb`, `wican_pro`, `meatpi` — code-coupled).
  Inference includes catalog keywords (short keywords token-matched);
  `is_known_device_type` backs the zeroconf TXT check; profiles gain
  `extra_sensors` (declarative `CatalogSensorDef`s) and
  `firmware_asset_pattern` (reserved for the update platform).
- **Sensors from data**: catalog sensor definitions build entity
  descriptions through the same validation as dynamic PID sensors; the
  bundled catalog already gives **ESPNetlink** its LTE signal/operator
  sensors. Base entity now honors an explicit description name.
- **Catalog-aware migration**: 1.1→1.2 loads the catalog before
  inferring device types.
- **Tests** (`test_catalog.py`, 35): parsing/validation incl. hostile
  documents (floods, bad slugs/icons/repos, oversized fields), registry
  semantics (override/reserved/inference/token rules), manager flows
  (bundled/stored/corrupt-storage/refresh/rate-limit/never-raises),
  real fetch failure modes over the aiohttp mocker, and end-to-end: a
  "solarpi" product defined **only** in the catalog gets its model,
  inference, and working product sensors through the device simulator.

## 5. What explicitly did NOT change (backward compatibility)

- Webhook payload handling, identity validation, sanitization,
  registration retry/coalescing/repair issues: byte-for-byte the same
  code paths.
- Legacy (pre-V6) devices: probe fails quietly → no control entities →
  behavior identical to the previous release.
- Entity unique_ids, device identifiers, entry data keys: unchanged
  (migration only *adds* `device_type`).
- Zeroconf matchers: unchanged (`_wican._tcp` + `_http._tcp` fallback);
  new services are a firmware ask (see FIRMWARE_API_FEEDBACK).

## 6. Follow-ups (deliberately out of scope)

- ESPNetlink entity set (GPS tracker via existing `gps` payload works
  already; LTE signal/operator sensors need the firmware surface —
  feedback doc §7).
- More control entities: LED alert (light), logger gate (switch), DTC
  scan (button) — the capability mechanism is ready for them.
- Generic action-discovery buttons via `/api/events/actions` — blocked
  on a firmware invoke endpoint (feedback doc §4).
- `home-assistant/brands` PR registering the **meatpi** brand.
- Optional future domain flip to `meatpi` (see D1; not recommended).
