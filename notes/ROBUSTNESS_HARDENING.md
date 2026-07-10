# Robustness Hardening & Failure-Injection Test Suite

> **HISTORICAL RECORD** — dated snapshot kept for reference; statistics and
> structure are superseded. Current documentation: `notes/README.md`.

**Date:** 2026-07-10
**Branch:** `quality-scale-gold`
**Result:** 421 tests passing (100%), 96.9% coverage, `mypy --strict` clean.

Goal: make the integration **unbreakable** — a glitching, corrupted, or hostile
device must never wedge entities, crash the pipeline, pollute stored config, or
grow Home Assistant without bound. Every defense below is proven by a test.

---

## 1. The bug that motivated everything: one bad value wedged the pipeline

**Failure mode.** `batt_voltage` declares `device_class=voltage`, so HA's
sensor platform **raises** when the state is a non-parseable string, a
non-finite number (`NaN`/`Infinity` — Python's JSON parser accepts these!), or
when any state string exceeds 255 chars. HA's
`DataUpdateCoordinator.async_update_listeners()` stops at the first listener
that raises. Consequences of a single corrupted reading:

- every entity registered *after* the failing one stopped updating,
- device-info persistence and new-PID discovery were skipped for that push,
- and because the bad value stayed in `coordinator.data`, this repeated on
  **every** subsequent push until the device sent a different value.

**Defenses (layered):**

| Layer | Where | What |
|---|---|---|
| Listener isolation | `coordinator.async_update_listeners` override | Each listener wrapped; one failure logged, rest continue |
| Value guards | `sensor.py::_coerce_numeric_value` + `_usable_value` | Mirrors HA's numeric-state rules; unusable values → `None` (state `unknown`), warn once per entity |
| Normalization | `coordinator.normalize_sensor_value` | Non-finite floats dropped; strings > 255 chars truncated; dict/list values dropped |
| Notify containment | `handle_webhook_data` | `async_set_updated_data` wrapped so handler post-steps always run |

**Tests:** `test_bad_voltage_does_not_block_other_entities`,
`test_overlong_status_string_is_truncated`, `test_listener_isolation_lets_later_listeners_run`,
`test_nonfinite_floats_injected_internally_are_dropped`, and friends.

Note: HA's transport parser (orjson) already rejects `NaN`/`Infinity` payloads
with 422 — the internal guards are defense-in-depth for values that arrive via
dispatcher, restore, or a future parser change.

## 2. PID `state_class` latch (fixes text PIDs after restart)

Restored PID sensors were stamped `state_class="measurement"` while
newly-created ones had none. A **text** PID (gear position `"D"`, VIN) with
`measurement` fails every state write after a restart. Now `state_class` is a
property that latches `MEASUREMENT` on the first numeric value per PID:
numeric PIDs keep long-term statistics (restored included), text PIDs work as
plain text sensors. Dead `_pending_value` mechanism removed.

## 3. Sanitized persistence (config entry can't be polluted)

Device-reported info fields (`fw_version`, `hw_version`, `device_id`,
`git_version`, `mdns`, `host`, `ip`) are sanitized before persisting:
only non-empty strings ≤ 255 chars (numbers coerced to strings; bools/objects
rejected). Previously a dict `fw_version` written to the entry would crash
version parsing/URL building forever after. Top-level fields are now read too
(status takes precedence), matching the identity-validation order.

Per-PID config stored in the entry is trimmed to `{unit, class}` strings
≤ 64 chars — a device can no longer write kilobyte blobs to disk via config.

## 4. Identity tolerance and strictness

- `device_id` comparison is type-tolerant: firmware sending `12345` (JSON
  number) matches stored `"12345"`; learned numeric ids are stored as strings.
- Structured garbage (dict/list) as `device_id` can never match → 403.
- Legacy entries that stored a numeric id still match string ids.

## 5. Bounded growth

- `MAX_DYNAMIC_PID_SENSORS = 1000` (largest real vehicles ≈ 500 incl. per-cell
  HV PIDs). Beyond the cap: one warning, new PIDs ignored, existing keep working.
- PID keys must be non-blank strings ≤ 128 chars; junk keys never become
  entities or reach `pid_keys` storage.
- Corrupt stored `pid_keys`/`config` (wrong types) are skipped on restore.

## 6. GPS hardening

- Every field parsed independently (`_as_finite_float`): one bad field costs
  only itself; garbage altitude/heading no longer retains stale raw values.
- Non-finite and boolean coordinates rejected; out-of-range warned and ignored.
- `GPS_ACCURACY_THRESHOLD` (200 m) — defined since forever but never wired —
  now filters low-accuracy fixes so a cold-start/garage fix can't teleport an
  already-located vehicle. First fix is always accepted (something beats nothing).
- Negative accuracy clamps to 0.

## 7. Lifecycle & concurrency

- **Registration coalescing** (`_async_request_webhook_registration`): while
  one (retried, backed-off) registration runs, further requests fold into a
  single trailing rerun. A device flapping between addresses can't pile up tasks.
- Startup-deferred registration listener is cancelled on unload (no stale
  once-listener); a registration racing an unload is a no-op
  (`runtime_data` is deleted by HA on unload).
- `DYNAMIC_PID_SENSORS` and the cap-warning set are cleaned on unload; a PID
  dispatch racing an unload does nothing.
- Concurrent push bursts proven safe (single event loop; last write wins).

## 8. Test architecture

| File | Role |
|---|---|
| `tests/device_sim.py` | `WiCANDeviceSimulator` — drives the **real** webhook endpoint, coordinator, entities. Hermetic: entry pre-seeds `ip: 127.0.0.1` so pushes don't trigger real-network re-registration (this also removed ~3 s of retry sleeps per test in the old suite). |
| `tests/test_device_simulation.py` | Baseline behaviour: happy paths, identity, availability/recovery, migration safety. Weak assertions strengthened (hostile-value and GPS tests now assert sibling updates and tracker purity, not just HTTP 204). |
| `tests/test_device_chaos.py` | 40 failure-injection scenarios: hostile values, floods, transport garbage (binary, 2000-deep JSON, 1 MB values, wrong content-type), identity glitches, GPS chaos, concurrency bursts, reload-under-fire, connection flapping. |
| `tests/test_hardening.py` | 17 targeted guard tests: listener isolation, sanitizer edge types (Decimal, bool, `1e999`), lifecycle races, restore hygiene, deferred-registration paths. |

Run everything: `.venv/bin/python -m pytest tests/ -q`
Type gate: `.venv/bin/python -m mypy custom_components/wican`

## 9. Coverage after this pass

```
TOTAL 96.94%  (421 passed)
100%: binary_sensor, config_flow, const, coordinator, device_tracker,
      diagnostics, entity, exceptions, github_releases, models, param_loader
 98.9%: helpers   97.7%: sensor   95.5%: attributes   95.1%: __init__   87.9%: update
```

Remaining uncovered lines are rare exception paths (registration internals,
OTA error branches) that pre-date this pass.
