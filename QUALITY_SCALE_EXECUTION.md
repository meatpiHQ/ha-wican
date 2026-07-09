# WiCAN — Gold Quality Scale Execution Document

Concrete, ordered tasks to execute the [Gold plan](QUALITY_SCALE_GOLD_PLAN.md).
Each task lists: **files**, **what to do**, **acceptance criteria**, and **tests**.
Check off items as they land. Rule IDs reference the
[compliance matrix](HA_CODING_STANDARDS.md).

Conventions:
- Run tests with: `source .venv/bin/activate && python -m pytest tests/ -q`
- Coverage: `python -m pytest tests/ --cov=custom_components.wican --cov-report=term-missing`
- Lint: `ruff check custom_components/wican`
- Validate manifest/quality-scale: `python -m script.hassfest` (from a core
  checkout) or rely on the repo's `.github/workflows/hassfest.yaml`.

---

## Phase 0 — Tracking scaffold

### 0.1 Add `quality_scale.yaml`  ✅/❌
- **File:** `custom_components/wican/quality_scale.yaml` (new)
- **Do:** List every Bronze/Silver/Gold/Platinum rule with status
  `done` / `todo` / `exempt`. For each `exempt`, add a `comment` justification.
  Exemptions to record now:
  - `action-setup`, `docs-actions`, `docs-triggers`, `docs-conditions`,
    `action-exceptions` — integration exposes no service actions/triggers/conditions.
  - `appropriate-polling` — local push; coordinator only has a health-check fallback.
  - `test-before-configure` — device pushes to HA; nothing to connect to at config time.
  - `reauthentication-flow` — device has no authentication.
  - `dynamic-devices`, `stale-devices` — one device per config entry; no runtime
    device enumeration (dynamic **entities** are handled).
- **Acceptance:** `hassfest` parses the file; statuses match reality.

### 0.2 Manifest `quality_scale` key  ❌
- **File:** `custom_components/wican/manifest.json`
- **Do:** Add `"quality_scale": "bronze"`. Bump to `silver`, then `gold` as tiers
  complete.
- **Acceptance:** `hassfest` passes; key is a valid tier string.

---

## Phase 1 — Code changes (Bronze + Silver + Gold code rules)

### 1.1 Manual flow unique ID — `unique-config-entry`  ⚠️→✅
- **File:** `config_flow.py` (`async_step_user`)
- **Do:** When the user submits, derive a unique ID (host/mDNS-normalized) and
  call `await self.async_set_unique_id(...)` + `self._abort_if_unique_id_configured()`
  before creating the entry. Keep zeroconf behaviour unchanged.
- **Acceptance:** Re-adding the same host aborts with `already_configured`.
- **Tests:** `test_config_flow.py` — duplicate manual setup aborts.

### 1.2 Config-flow `data_description` — `config-flow`  ⚠️→✅
- **Files:** `translations/en.json`, `strings.json` (new, mirror of en.json)
- **Do:** Add `data_description` under `config.step.user.data` for `mdns` and
  `host` explaining each field. Add `data_description` for the options step
  (`post_interval`).
- **Acceptance:** Fields render help text; `hassfest` translation check passes.

### 1.3 `test-before-setup` — raise `ConfigEntryNotReady`  ⚠️→✅
- **File:** `__init__.py` (`async_setup_entry`)
- **Do:** Where a transient failure genuinely prevents setup, raise
  `ConfigEntryNotReady` instead of returning `True` after swallowing. Keep the
  push semantics: an empty first refresh is still success, but e.g. an
  unrecoverable webhook-component/registration precondition should surface as
  not-ready. Do **not** regress the "device hasn't pushed yet" success path.
- **Acceptance:** Setup still succeeds with no device data; genuine transient
  errors retry.
- **Tests:** `test_init.py` — not-ready path and normal path.

### 1.4 `device_tracker` `PARALLEL_UPDATES` — `parallel-updates`  ⚠️→✅
- **File:** `device_tracker.py`
- **Do:** Add module-level `PARALLEL_UPDATES = 0` (read-only, push-based).
- **Acceptance:** Constant present on all four platforms.
- **Tests:** trivial import assertion (optional).

### 1.5 Entity availability + logging — `entity-unavailable`, `log-when-unavailable`  ⚠️/❌→✅
- **Files:** `entity.py`, `coordinator.py`, `sensor.py`, `binary_sensor.py`
- **Do:** Track last-push time in the coordinator. Consider the device
  unavailable when now − last_push exceeds a threshold (e.g.
  `max(post_interval * N, floor)`), and reflect that in entity `available`.
  Log **once** when transitioning to unavailable and **once** on recovery
  (guard with a boolean flag on the coordinator).
- **Acceptance:** Entities show `unavailable` after the device stops pushing;
  exactly one log line per transition.
- **Tests:** `test_coordinator.py`/`test_sensor.py` — advance time, assert
  availability flips and log emitted once.

### 1.6 Icon translations — `icon-translations`  ❌→✅
- **Files:** `icons.json` (new); `attributes.py`, `sensor.py`, `binary_sensor.py`,
  `device_tracker.py`, `update.py`
- **Do:** Create `icons.json` mapping entity translation keys to icons under
  `entity.<platform>.<key>.default`. Remove static `icon=`/`_attr_icon` for
  entities whose keys are covered. Dynamic PID sensors keep programmatic icons
  (documented limitation) or map by device_class in `icons.json` where possible.
- **Acceptance:** Static entities get icons from `icons.json`; `hassfest` icon
  check passes.
- **Tests:** existing entity tests still pass.

### 1.7 Disable high-volume PID sensors by default — `entity-disabled-by-default`  ❌→✅
- **File:** `sensor.py` (`_async_process_pid_update`, restore loop)
- **Do:** Set `entity_registry_enabled_default=False` on high-volume/verbose PID
  descriptions (e.g. per-cell voltages `HV_C_V_*`, and other high-cardinality
  keys). Define the disable policy centrally (e.g. a prefix/pattern set in
  `param_loader.py` or `const.py`).
- **Acceptance:** Cell-voltage sensors are created disabled; common sensors stay
  enabled.
- **Tests:** `test_pid_sensor_config.py` — assert enabled_default per class.

### 1.8 Reconfigure flow — `reconfiguration-flow`  ❌→✅
- **Files:** `config_flow.py`, `translations/en.json`, `strings.json`
- **Do:** Add `async_step_reconfigure` allowing the user to update host/mDNS.
  Use `self._get_reconfigure_entry()` / `async_update_reload_and_abort`. Reuse
  the URL normalization helpers.
- **Acceptance:** Reconfigure updates connection data and reloads the entry.
- **Tests:** `test_config_flow.py` — reconfigure updates data, aborts with
  `reconfigure_successful`.

### 1.9 Repair issues — `repair-issues`  ❌→✅
- **Files:** `__init__.py` (webhook registration), new `repairs.py` if a custom
  fix flow is needed; `translations/en.json` `issues` section
- **Do:** When webhook registration ultimately fails after all retries, raise a
  repair issue via `homeassistant.helpers.issue_registry.async_create_issue`
  (e.g. `webhook_registration_failed`) with troubleshooting text; delete it on
  the next successful registration. Consider a repair for
  `webhook_not_internet_accessible` when a Pro device needs an external URL.
- **Acceptance:** Failure surfaces a repair; success clears it.
- **Tests:** `test_init.py`/new `test_repairs.py` — issue created on failure,
  removed on success.

### 1.10 Discovery updates connection info — `discovery-update-info`  ⚠️→✅
- **File:** `config_flow.py` (`async_step_zeroconf`)
- **Do:** On re-discovery of an already-configured device, update the stored
  host/mDNS/IP from the new discovery info before aborting (pattern:
  `_abort_if_unique_id_configured(updates={...})`).
- **Acceptance:** IP/host change on the device propagates to the config entry via
  discovery.
- **Tests:** `test_config_flow.py` — rediscovery with new host updates entry data.

### 1.11 Exception-translation audit — `exception-translations`  ⚠️→✅
- **Files:** `update.py`, `github_releases.py`, `__init__.py`,
  `translations/en.json`
- **Do:** Ensure every user-facing raised `HomeAssistantError`/`ConfigEntryError`
  uses `translation_domain=DOMAIN` + `translation_key` with a matching entry
  under `exceptions`. Add missing keys (firmware download/upload failures, etc.).
- **Acceptance:** No raised user-facing error uses a bare string message.
- **Tests:** `test_update.py`/`test_exceptions.py` — assert translation keys.

---

## Phase 2 — Test coverage > 95 % — `test-coverage`, `config-flow-test-coverage`

- **Baseline (measured):** 91.5 % overall, 296 passed / 1 failed.
  Per-module gaps: `param_loader.py` 80 %, `helpers.py` 82 %, `update.py` 88 %,
  `__init__.py` 89 %, `sensor.py` 95 %, `config_flow.py` 96 % (needs 100 %),
  `coordinator.py` 97 %. All other modules 100 %.
- **First:** Fix the failing `test_param_loader.py::test_ev_charging_params`
  (expects `kwh`, params.json now correctly yields `kWh`). Update the assertion.
- **Files:** everything under `tests/`
- **Do:** Add tests module by module, prioritizing lowest-covered files:
  - `__init__.py`: webhook handler branches (IP extraction, identity mismatch →
    403, connection-field change re-registration), registration retry/backoff,
    endpoint fallback (cached IP → host → mDNS), timeout path, entry-updated
    listener.
  - `config_flow.py`: **100 %** — user, zeroconf (all unique-id branches),
    confirm (onboarding vs form), options, reconfigure, aborts.
  - `sensor.py`: normalization, dynamic PID creation/restore, disabled-default.
  - `entity.py`/`coordinator.py`: availability/staleness, identity validation.
  - `update.py`/`github_releases.py`: asset selection, version handling, errors.
- **Acceptance:** Overall coverage > 95 %; `config_flow.py` == 100 %.
- **Note:** Use `async_fire_time_changed` + time freezing for backoff/staleness.

---

## Phase 3 — Documentation (`docs-*`)

Restructure `README.md` (and link supporting docs) to cover:

| Section | Rule |
|---|---|
| High-level description of WiCAN + integration | `docs-high-level-description` |
| Supported devices (WiCAN OBD-II, WiCAN-Pro) + firmware | `docs-supported-devices` |
| Installation (HACS + manual), prerequisites | `docs-installation-instructions` |
| Installation parameters (`mdns`, `host`) | `docs-installation-parameters` |
| Configuration parameters (`post_interval`) | `docs-configuration-parameters` |
| Supported functions (entities per platform) | `docs-supported-functions` |
| Data updates (push/webhook model + interval) | `docs-data-update` |
| Use cases (battery, EV charge, GPS trips) | `docs-use-cases` |
| Automation examples | `docs-examples` |
| Known limitations (raw PID names, single device, reachability) | `docs-known-limitations` |
| Troubleshooting (no webhook, not discovered, OTA fails) | `docs-troubleshooting` |
| Removal instructions | `docs-removal-instructions` |

- **Acceptance:** Each rule's content present and accurate.

---

## Phase 4 — Brands (external)

- **Repo:** `home-assistant/brands`
- **Do:** Add `core_integrations/wican/icon.png` and `logo.png` per brand specs;
  open PR.
- **Acceptance:** PR merged; `brands` rule satisfied.

---

## Phase 5 — Finalize Gold

1. Flip all closed rules to `done` in `quality_scale.yaml`.
2. `manifest.json` → `"quality_scale": "gold"`.
3. Full suite green; coverage > 95 %; `ruff` clean; `hassfest` clean.
4. Update the status tables in `HA_CODING_STANDARDS.md`.
5. Open PR from `quality-scale-gold` → `main`.

---

## Progress log

| Date | Task | Notes |
|---|---|---|
| _init_ | Docs created; branch `quality-scale-gold` | Baseline captured (91.5% cov) |
| _init_ | Fixed `test_ev_charging_params` (`kwh`→`kWh`) | Suite green again |
| Phase 0 | `quality_scale.yaml` + manifest `quality_scale: bronze` | 54 rules tracked |
| 1.4 | `device_tracker` `PARALLEL_UPDATES=0` | `parallel-updates` done |
| 1.1 | Manual flow unique_id + abort | `unique-config-entry` done |
| 1.10 | Zeroconf `updates=` on re-discovery | `discovery-update-info` done |
| 1.8 | `async_step_reconfigure` | `reconfiguration-flow` done |
| 1.2 | `data_description` + reconfigure strings | `config-flow` done |
| 1.7 | `_pid_enabled_by_default` for HV_C_V/D_### | `entity-disabled-by-default` done |
| docs | Move scratch `.md` to gitignored `notes/`; track quality docs | Convention set |
| 1.6 | `icons.json` for static sensor/binary_sensor; device_tracker translation_key | `icon-translations` done |
| 1.5 | Coordinator last-push tracking → `UpdateFailed` when stale | `entity-unavailable` + `log-when-unavailable` done |

### Rules closed this pass (code)
Bronze: config-flow, unique-config-entry.
Silver: parallel-updates, entity-unavailable, log-when-unavailable.
Gold: discovery-update-info, reconfiguration-flow, entity-disabled-by-default,
icon-translations.
Plus scaffold (quality_scale.yaml, manifest key) and a stale-test fix.

### Completed since
- 1.9 `repair-issues` — repair raised on webhook-registration failure, cleared on success/unload.
- 1.3 `test-before-setup` — marked exempt (push model; no device connection at setup).
- 1.11 `exception-translations` — all raised errors translatable; missing keys added.
- `entity-translations` — static entities translated; dynamic-PID limitation documented.
- Phase 2 `test-coverage` — **95.8 %** overall, config_flow.py & param_loader.py 100 %, 332 passing.
- Phase 3 docs — README rewritten to cover all `docs-*` rules.

### Remaining for Gold
- `brands` — external PR to `home-assistant/brands` (logo + icon). **Only blocker.**
- Finalize: once `brands` merges, bump manifest `quality_scale` to `gold`.

### Beyond Gold
- Platinum: `strict-typing` (see the Platinum plan). `async-dependency` and
  `inject-websession` already satisfied.
