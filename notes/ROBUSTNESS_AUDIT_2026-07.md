# Robustness Audit — 2026-07-10

**Branch:** `meatpi-multi-device` (at `e02e639`)
**Baseline:** 666 tests passing, 97.09% coverage.
**Method:** three parallel read-only audits (core setup/networking, data/file
handling, entities/update flow), each cross-checked against the existing test
suite; top findings re-verified by hand.

Line numbers refer to the tree at `e02e639`, before fixes.

Status legend: **FIXED** (this pass, with regression tests) · **OPEN**.

**Result of the fix pass (2026-07-11):** all 5 high-priority findings fixed
(plus M8's response-shape guard); 692 tests passing (was 666), 97.0%
coverage, `mypy` and `ruff` clean on `custom_components/wican`.

---

## High priority

### H1. History backfill silently loses data — watermark semantics — FIXED

Three related bugs in `history.py`, all violating the module's documented
invariant ("the watermark only advances after a fully successful import"):

- **Partial-failure advance** (`history.py:612`): `async_sync` advanced the
  watermark whenever *any* PID imported (`if result.pids_imported`). If one
  PID's `async_import_statistics` failed while another succeeded, the failed
  PID's hours landed behind the watermark and were never retried —
  permanent, silent loss.
- **File-cap drops the oldest files** (`history.py:341-347`):
  `_select_log_files` kept the *newest* `HISTORY_MAX_FILES_PER_SYNC`
  candidates and "deferred" the oldest. But importing the newest files
  advances the watermark past the dropped files' rows, so "deferred" was
  actually permanent loss. Deferring the newest is self-healing: the next
  sync picks them up.
- **Row-cap freezes a partial hour** (`history.py:224-225`): when
  `HISTORY_MAX_ROWS_PER_SYNC` landed mid-hour, that hour was imported from
  partial data, the watermark passed it, and live-wins prevented any
  correction — mean/min/max wrong forever.

**Fix:** watermark advances only to the newest hour that imported with no
errors after it (per-PID failures cap the watermark below the failed hours);
file selection defers the newest files beyond the cap; row-cap aggregation
discards the incomplete trailing hour so it re-imports next sync.

### H2. Diagnostics leaks the webhook secret — FIXED

`diagnostics.py:42-44` redacted only `webhook_id`, but `entry.data` also
contains `webhook_url` — which embeds the same secret in its path
(`/api/webhook/<id>`) — plus `vpn_ip` (network-identifying). A diagnostics
dump shared on a support forum handed out push access to the instance.

**Fix:** redact `webhook_url` and `vpn_ip` alongside `webhook_id`;
regression test asserts no occurrence of the webhook secret anywhere in the
serialized diagnostics output.

### H3. Failed setup leaves the webhook registered — entry wedged until restart — FIXED

`__init__.py:729-740` registered the webhook before
`async_forward_entry_setups` with no cleanup on failure. If platform setup
raised (even transiently), HA's setup retry hit core's
`ValueError("Handler is already defined!")` on re-register — every retry
failed until HA restart.

**Fix:** unregister the webhook if anything after registration raises during
setup, so retries start clean.

### H4. Wrong "latest" firmware offered (USB devices, late-learned PRO) — FIXED

- `github_releases.py:74-96` classified releases only PRO vs non-PRO, using
  `"P" in tag` — so (a) USB devices (`v4.13u` stream) were offered the newest
  OBD release, which then fails asset selection
  (`FirmwareVersionNotFoundError`): a permanently offered, uninstallable
  update; (b) any future tag containing a letter "p" (`v4.50-patch1`) would
  be misclassified as PRO.
- `__init__.py:596-600`: `is_pro` was frozen at setup from
  `entry.data["hw_version"]`. Manually-added devices have no `hw_version`
  until the first push, and learning it later never updated the coordinator —
  a PRO device tracked the standard release stream until reload.

**Fix:** releases are classified by tag suffix (`p` / `u` as a
version-suffix letter, not substring match). PRO devices only match PRO
releases; OBD/USB devices match standard releases **by installable asset**
(real releases like v4.13 ship the OBD and USB .bins together under an
unsuffixed tag), falling back to the tag stream when no assets are listed.
The releases coordinator re-derives the stream when device-reported
hardware info is persisted and refreshes without an entry reload. The
non-list-body guard from M8 was also applied while in this code path.

### H5. Unload races: webhook handler & registration task touch dead `runtime_data` — FIXED

HA deletes `entry.runtime_data` on unload. Two paths accessed it after an
await with no guard:

- `handle_webhook` (`__init__.py:700/704` →
  `_persist_device_reported_info:425`, `_update_vpn_state:369`): a push whose
  body read was in flight during unload resumed into `AttributeError`; HA's
  webhook component returns 200 to the device, so the push was silently
  dropped with error spam.
- Webhook registration (`__init__.py:1046-1086`): registration attempts (with
  backoff, 7+ s against an offline device) were created with
  `hass.async_create_task`, never cancelled on unload, and re-entered
  `entry.runtime_data` unguarded on the trailing coalescing-loop iteration.

**Fix:** the webhook handler re-checks `runtime_data` after the awaited
body read (everything after is synchronous) and answers 503 so the device
retries after the reload; `_async_register_webhook_on_device` binds the
runtime object once behind a guard and never re-reads the entry attribute;
the coalescing loop exits when the entry's lifecycle changed mid-run; a
repair issue is never raised for an unloaded entry; `_async_entry_updated`
guards teardown. (Entry-scoped tasks were considered and rejected:
`entry.async_create_task` is *awaited* on unload — it would block reload
behind registration backoff — and background tasks are invisible to
`async_block_till_done`.)

---

## Medium priority (status per item)

### M1. Size caps buffer the whole body before checking — FIXED

`api.py:132-137`, `param_loader.py:303-304`, `catalog.py:221-225`: chunked
responses (no `Content-Length`) were fully read into memory *before* the
`len(...) > MAX_BYTES` check.
**Fix (2026-07-11):** shared `helpers.async_read_capped()` — declared
length refused pre-read, chunked bodies streamed with the read aborting
at the first chunk past the cap; wired into all three sites.

### M2. Stored ETag trusted even when stored params fail validation — FIXED

`param_loader.py:379-381`: `_async_apply_stored` set `self._etag` before
checking `validated`. Corrupt stored params + intact ETag → every future
fetch 304s → integration wedged on bundled definitions until upstream's ETag
changed.
**Fix (2026-07-11):** the ETag is only applied alongside a valid stored
payload; corrupt storage now triggers an unconditional re-fetch.

### M3. `NoURLAvailableError` crashes the config flow — FIXED

`config_flow.py:68-72, 192-197`: `resolve_webhook_url(...,
require_current_request=True)` raises when no http URL can be resolved
(e.g. Nabu Casa cloud UI, no internal URL). Neither step caught it → user
saw "Unknown error occurred" instead of a friendly abort.
**Fix (2026-07-11):** both creation steps abort with `no_url_available`
(translated message pointing at Settings > System > Network).

### M4. Manual + zeroconf setup of the same device duplicates entries — FIXED

`config_flow.py:62-65` (normalized-URL unique_id) vs `:143-155` (MAC-based).
The two never collided → two entries, two webhooks, duplicated entities for
one physical device.
**Fix (2026-07-11):** both flows also match existing entries by
connection address (host tokens normalized across URL/hostname/IP forms).
Zeroconf discovery of a manual entry adopts it — upgrading it to the
stable MAC unique_id and refreshing its connection info — instead of
creating a duplicate; a manual add of a discovered device aborts.

### M5. Changed PID unit/class persists only if a new PID arrives in the same push — FIXED

`sensor.py:431-436`: `_persist_pid_entities` only ran when
`_discover_new_pid_entities` found something. A corrected unit on an existing
PID reverted after restart.
**Fix (2026-07-11):** persist runs on every PID push (`async_update_entry`
is a no-op when nothing changed).

### M6. Missing-key sensors skip `async_write_ha_state()` on staleness flips — FIXED

`sensor.py:503-504, 606-608`: `_handle_coordinator_update` early-returned when
the entity's key was absent from the payload, so when the device went stale
the entity kept showing its last value as *available* while siblings went
unavailable (binary_sensor writes unconditionally; sensors should too).

**Fix (2026-07-11):** both sensor paths keep the last value on partial
pushes but always write state, so staleness/recovery flips render on every
entity. Product decision (Ali): stale device → unavailable everywhere (HA
convention, quality-scale `entity-unavailable`), paired with a new
**"Last seen"** diagnostic timestamp sensor (`sensor.py`,
`WiCANLastSeenSensorEntity`) that tracks the last push, restores across
restarts, and deliberately *stays available* while the device is stale so
users can see how old the readings are.

### M7. `_load_params` at module import only catches FileNotFoundError/JSONDecodeError — FIXED

`param_loader.py:208-218, 472`: a PermissionError or invalid UTF-8
(`UnicodeDecodeError`) in the bundled `data/params.json` propagated at module
import → whole integration failed to load instead of degrading to empty
params.
**Fix (2026-07-11):** catches `(OSError, ValueError)` — covering
permission errors, decode errors, and JSON errors — and degrades to empty
params with a logged exception.

### M8. GitHub releases coordinator: shape + scaling

- ~~`github_releases.py:65-68`: HTTP 200 with a non-list JSON body (captive
  portal, proxy) → `AttributeError` outside the handled exception set.~~
  **FIXED** with H4 (non-list body → `UpdateFailed`; non-dict elements
  skipped).
- ~~One coordinator per config entry, each polling hourly, unauthenticated:
  N devices multiply against GitHub's 60/hr per-IP limit.~~ **FIXED**
  (2026-07-11): a per-hass shared fetch serves all coordinators from one
  request per interval; failures are not cached; per-stream filtering
  still happens per coordinator.

---

## Low priority (status per item)

- **L1** `__init__.py:264-268`: `X-Forwarded-For` trusted unconditionally as
  the authoritative device IP — spoofable redirect of control-API /
  registration traffic. HA convention: honor XFF only from trusted proxies.
- **L2** `coordinator.py:155`: `self._data.update(data)` never prunes —
  rotating top-level keys from a buggy firmware grow memory without bound
  (PID *sensors* are capped; the dict is not).
- **L3** — FIXED (2026-07-11): user-supplied install versions are
  normalized (`v4.45`, `4.45p`, `V4.45P` all resolve).
- **L4** — FIXED (2026-07-11): `_tag_version()` is null-safe and strips
  `v`/`V`; `_normalize_version` strips suffixes case-insensitively.
- **L5** `device_tracker.py:262-275`: restored GPS coordinates skip the range
  validation live updates get — corrupt restore (lat=999) accepted.
- **L6** — FIXED (2026-07-11): the tracker derives manufacturer/model
  from the device profile exactly like `WiCANEntity.device_info`.
- **L7** — FIXED (2026-07-11): diagnostics collect entities via the
  entity registry scoped to the config entry — no sibling leakage, rename
  safe.
- **L8** `__init__.py:1168-1200`, `api.py:111-124`: some HTTP responses never
  read/released → "Unclosed response" connection churn under the shared
  session.
- **L9** `__init__.py:755-757`: webhook unregistered before
  `async_unload_platforms`; if platform unload fails the entry stays loaded
  but deaf to pushes.
- **L10** `helpers.py:191-238`: `wican_exception_handler` is dead code; if
  ever wired up, its `last_update_success = False` side effect would mark all
  push-fed entities unavailable on a control-action failure.
- **L11** `entity.py:40-46`: `_attr_name` set unconditionally defeats
  `translation_key` — users see raw keys (`batt_voltage`) instead of
  translated names ("Battery Voltage"). Entity IDs in tests lock this in;
  fixing changes entity IDs for new installs (migration consideration).
- **L12** History backfill memory: up to ~200k parsed row dicts + an 8 MiB
  file text per iteration — bounded by design constants but a real spike on
  a Pi with several entries syncing concurrently.

---

## Verified-sound areas (no action needed)

- Webhook body handling: gzip/truncated/bomb/oversized/malformed all covered
  (`test_webhook_gzip.py`).
- Coordinator listener isolation, value normalization, PID flood caps,
  device-identity mismatch handling, multi-entry isolation
  (`test_hardening.py`, `test_device_chaos.py`, `test_multi_device_isolation.py`).
- History: UTC hour bucketing matches recorder statistics; live-wins,
  future-skew rejection, idempotent re-import all correct and e2e-tested
  against the real recorder.
- Catalog/params validation bounds (entry/key/field caps, reserved slugs,
  schema gate) thorough and heavily tested.
- `.storage` managers are hass-level singletons with locks; `Store` writes
  atomically; bundled-catalog read uses an executor.
- Update flow: upload/download failures map to `HomeAssistantError`,
  in-progress flag always cleared in `finally`.
