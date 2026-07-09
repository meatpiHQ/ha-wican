# WiCAN — Home Assistant Quality Scale & Coding Standards

This document is the authoritative reference for the coding standards and the
**Integration Quality Scale** requirements that the WiCAN integration must meet.
It maps every quality-scale rule to our current implementation status and states
exactly what is required to reach **Gold**.

Sources:
- Quality scale overview: https://developers.home-assistant.io/docs/core/integration-quality-scale/
- Checklist: https://developers.home-assistant.io/docs/core/integration-quality-scale/checklist
- Rules: https://developers.home-assistant.io/docs/core/integration-quality-scale/rules

Legend: ✅ Done · ⚠️ Partial · ❌ Missing · ➖ Exempt / Not applicable (must be
recorded as `exempt` in `quality_scale.yaml` with a reason).

---

## 1. Integration profile

| Property | Value |
|---|---|
| Domain | `wican` |
| Integration type | `device` |
| IoT class | `local_push` (device POSTs to a Home Assistant webhook) |
| Dependencies | `webhook` (internal); no external PyPI requirements |
| Discovery | Zeroconf (`_wican._tcp.local.`, `_http._tcp.local.`) |
| Platforms | `sensor`, `binary_sensor`, `device_tracker`, `update` |
| Config entry data | via `entry.runtime_data` (`WiCANRuntimeData`) |
| Code owner | `@jay-oswald` |

Because WiCAN is **local push with no authentication**, several rules that
target polling and credential-based cloud integrations are **exempt**. Every
exemption below must be recorded in `quality_scale.yaml` with a justification.

---

## 2. Quality-scale status at a glance

To reach a tier, **all** rules of that tier **and every lower tier** must pass
(a rule may pass by being `done` or `exempt`).

| Tier | Rules | Done | Exempt | Remaining |
|---|---|---|---|---|
| Bronze | 20 | 16 | 3 | 1 (`brands`, external) |
| Silver | 10 | 6 | 1 | 0 |
| Gold | 21 | 18 | 3 | 0 |
| Platinum | 3 | 3 | 0 | 0 (`mypy --strict` clean) |

> **Status:** Every Bronze/Silver/Gold rule is `done` or `exempt` **except
> `brands`**, which requires an external PR to `home-assistant/brands` (logo +
> icon). Once that merges, the integration meets Gold. Test coverage is **95.8 %**
> overall (config_flow.py and param_loader.py at 100 %); full suite 332 passing.
> Platinum `strict-typing` is out of scope but tracked.

The detailed per-rule status of record lives in
[`custom_components/wican/quality_scale.yaml`](custom_components/wican/quality_scale.yaml);
the table below reflects the pre-work assessment and remains as a reference.

---

## 3. Bronze tier

| Rule | Status | Evidence / Action required |
|---|---|---|
| `action-setup` | ➖ | No service actions. Record as `exempt`. |
| `appropriate-polling` | ➖/✅ | Push-based; `WiCANDataUpdateCoordinator` uses a 300 s fallback interval only. Record as `exempt` (no active polling). |
| `brands` | ❌ | Submit logo + icon to `home-assistant/brands` for domain `wican`. External PR required. |
| `common-modules` | ✅ | Coordinator in `coordinator.py`, base entity in `entity.py`, models in `models.py`. |
| `config-flow-test-coverage` | ⚠️ | `test_config_flow.py` exists; must reach **100 %** line coverage of `config_flow.py`. |
| `config-flow` | ⚠️ | User + zeroconf flows work. Gaps: no `data_description` keys in translations; manual (`user`) flow does not set a unique ID. |
| `dependency-transparency` | ✅ | `requirements: []`; only the internal `webhook` dependency. |
| `docs-actions` | ➖ | No actions. Record as `exempt`. |
| `docs-triggers` | ➖ | No triggers. Record as `exempt`. |
| `docs-conditions` | ➖ | No conditions. Record as `exempt`. |
| `docs-high-level-description` | ⚠️ | `README.md` exists; verify it opens with a clear description of WiCAN and what the integration does. |
| `docs-installation-instructions` | ⚠️ | `README.md`/`QUICKSTART.md`; ensure explicit prerequisites + step-by-step HACS/manual install. |
| `docs-removal-instructions` | ❌ | Add a "Removing the integration" section (delete entry + optional device webhook cleanup). |
| `entity-event-setup` | ✅ | Dispatcher subscribed in `async_added_to_hass` via `async_on_remove`. |
| `entity-unique-id` | ✅ | Every entity sets `_attr_unique_id`. |
| `has-entity-name` | ✅ | `_attr_has_entity_name = True` on the base entity and device tracker. |
| `runtime-data` | ✅ | `entry.runtime_data: WiCANRuntimeData`; typed `WiCANConfigEntry`. |
| `test-before-configure` | ➖ | Device pushes to HA; there is nothing to connect to at config time. Record as `exempt`. |
| `test-before-setup` | ⚠️ | `async_setup_entry` swallows all errors. Raise `ConfigEntryNotReady` for transient failures where appropriate (still must not block push setup). |
| `unique-config-entry` | ⚠️ | Zeroconf uses MAC/`device_id` unique ID + `_abort_if_unique_id_configured`. Manual flow must do the same to prevent duplicates. |

---

## 4. Silver tier

| Rule | Status | Evidence / Action required |
|---|---|---|
| `action-exceptions` | ➖ | No actions. Record as `exempt`. |
| `config-entry-unloading` | ✅ | `async_unload_entry` unregisters the webhook and unloads platforms. |
| `docs-configuration-parameters` | ❌ | Document the `post_interval` option (range, default, effect). |
| `docs-installation-parameters` | ❌ | Document the manual setup inputs (`mdns`, `host`). |
| `entity-unavailable` | ⚠️ | `device_tracker` reports availability; sensors/binary sensors never go unavailable when the device stops pushing. Add staleness-based availability. |
| `integration-owner` | ✅ | `codeowners: ["@jay-oswald"]`. |
| `log-when-unavailable` | ❌ | Log once when the device becomes unavailable and once when it recovers. |
| `parallel-updates` | ⚠️ | Set on `sensor`, `binary_sensor`, `update`. **Missing on `device_tracker`.** Add `PARALLEL_UPDATES`. |
| `reauthentication-flow` | ➖ | No authentication. Record as `exempt`. |
| `test-coverage` | ⚠️ | Currently **91.5 %** overall (1 failing test to fix). Must exceed **95 %**. Weakest modules: `param_loader.py` (80 %), `helpers.py` (82 %), `update.py` (88 %), `__init__.py` (89 %). |

---

## 5. Gold tier

| Rule | Status | Evidence / Action required |
|---|---|---|
| `devices` | ✅ | Entities provide `DeviceInfo` (identifiers, MAC connection, model, sw_version). |
| `diagnostics` | ✅ | `diagnostics.py` with webhook ID redaction. |
| `discovery` | ✅ | Zeroconf discovery + confirm step. |
| `discovery-update-info` | ⚠️ | Webhook handler refreshes device IP from inbound requests. Zeroconf re-discovery should also update stored host/connection info (use discovery to refresh network details). |
| `docs-data-update` | ❌ | Document the push/webhook data-update mechanism and interval option. |
| `docs-examples` | ❌ | Provide automation examples (e.g., low battery voltage, ECU online). |
| `docs-known-limitations` | ❌ | Document limitations (raw PID names, no unit for some PIDs, single device per entry, webhook reachability). |
| `docs-supported-devices` | ❌ | List supported hardware (WiCAN OBD-II, WiCAN-Pro) and firmware expectations. |
| `docs-supported-functions` | ❌ | Describe all entities/platforms provided. |
| `docs-troubleshooting` | ❌ | Add troubleshooting (webhook not received, device not discovered, firmware update fails). |
| `docs-use-cases` | ❌ | Describe real use cases (battery monitoring, EV charge state, GPS trip logging). |
| `dynamic-devices` | ➖/⚠️ | One device per entry; dynamic **entities** (PID sensors) are added at runtime. Record as `exempt` (no post-setup device enumeration) with rationale. |
| `entity-category` | ✅ | Diagnostic entities categorized; dynamic PID sensors set `EntityCategory.DIAGNOSTIC`. |
| `entity-device-class` | ✅ | Device classes assigned where valid (voltage, temperature, etc.). |
| `entity-disabled-by-default` | ❌ | High-volume PID sensors (e.g. `HV_C_V_001…188` cell voltages) must default to disabled. |
| `entity-translations` | ⚠️ | Static entities translated. Dynamic PID sensors use raw device keys (no translation); document as a known limitation and use `has_entity_name` + friendly formatting where possible. |
| `exception-translations` | ⚠️ | `update.py` mostly uses `translation_domain`/`translation_key`; audit all raised `HomeAssistantError`s and add translation keys under `exceptions` in the strings. |
| `icon-translations` | ❌ | Replace `_attr_icon`/`icon=` with an `icons.json` icon-translations file. |
| `reconfiguration-flow` | ❌ | Add `async_step_reconfigure` to change host/mDNS after setup. |
| `repair-issues` | ❌ | Raise repair issues for actionable problems (webhook registration failed, webhook not internet-accessible where required). |
| `stale-devices` | ➖/⚠️ | Single device per entry removed on entry removal. Record as `exempt` (no multi-device enumeration) with rationale. |

---

## 6. Platinum tier (tracked, not required for Gold)

| Rule | Status | Notes |
|---|---|---|
| `async-dependency` | ✅ | No synchronous external dependency; all I/O is async. |
| `inject-websession` | ✅ | Uses `async_get_clientsession(hass)`. |
| `strict-typing` | ⚠️ | Most code typed; some function signatures (e.g. `WiCANSensorEntity.__init__`) lack annotations. Enable stricter typing before pursuing Platinum. |

---

## 7. Required repository artifacts

| Artifact | Status | Purpose |
|---|---|---|
| `custom_components/wican/manifest.json` → `quality_scale` key | ❌ | Declare the current tier. |
| `custom_components/wican/quality_scale.yaml` | ❌ | Per-rule status tracking (`done` / `exempt` + comments). Required for scored integrations. |
| `custom_components/wican/icons.json` | ❌ | Icon translations. |
| `custom_components/wican/strings.json` | ❌ | Source strings (translations/en.json is generated from it in core; for a custom component keep both in sync). |
| `custom_components/wican/translations/en.json` | ✅ | Entity/config/exception translations. |
| Brands assets (external `home-assistant/brands`) | ❌ | Logo + icon. |

---

## 8. General coding standards (baseline — already largely met)

These underpin the quality-scale rules and must be preserved in all changes.

- **Module structure:** `from __future__ import annotations`; imports grouped
  stdlib → third-party → homeassistant → local; absolute imports.
- **Logging:** module-level `_LOGGER = logging.getLogger(__name__)`; lazy
  `%`-formatting (never f-strings in log calls); appropriate levels.
- **Typing:** full annotations on public functions; modern syntax (`list[str]`,
  `X | None`). Close the remaining untyped signatures for Platinum.
- **Config flow:** `VERSION`/`MINOR_VERSION` set; steps return `FlowResult`;
  all user-facing strings via translation keys with `data_description`.
- **Coordinator:** subclass `DataUpdateCoordinator`; push updates via
  `async_set_updated_data`; validate device identity.
- **Entities:** inherit `CoordinatorEntity`; `_attr_has_entity_name = True`;
  `__slots__`; stable unique IDs (entry-scoped / MAC / device_id); `DeviceInfo`.
- **Runtime data:** `entry.runtime_data` dataclass, typed config-entry alias.
- **Exceptions:** subclass `HomeAssistantError`; use `ConfigEntryNotReady`
  (transient) vs `ConfigEntryError` (permanent); translatable messages.
- **Constants:** all magic values in `const.py` (already done).
- **Async:** `asyncio.timeout`; never block the event loop; cancel tasks on
  unload; `@callback` for sync callbacks.
- **Diagnostics:** redact secrets (`CONF_WEBHOOK_ID`), include device/config/state.
- **Testing:** pytest + `pytest-homeassistant-custom-component`; mock external
  I/O; target >95 % coverage and 100 % on `config_flow.py`.

---

## 9. Definition of done for Gold

1. Every Bronze, Silver, and Gold rule is `done` or a justified `exempt` in
   `quality_scale.yaml`.
2. `manifest.json` declares `"quality_scale": "gold"`.
3. `hassfest` and `ruff` pass with no errors.
4. Test coverage > 95 % overall, 100 % on the config flow.
5. Documentation covers every `docs-*` rule.
6. Brands assets merged in `home-assistant/brands`.
