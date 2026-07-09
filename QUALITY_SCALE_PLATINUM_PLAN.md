# WiCAN — Plan to reach Platinum on the Home Assistant Quality Scale

Prerequisite: **Gold** (see [`QUALITY_SCALE_GOLD_PLAN.md`](QUALITY_SCALE_GOLD_PLAN.md)).
Platinum is the highest tier and adds three rules. This plan is grounded in an
actual `mypy --strict` run against the current code.

Rule reference:
- https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/async-dependency
- https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/inject-websession
- https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/strict-typing

## The three Platinum rules & current status

| Rule | Status | Assessment |
|---|---|---|
| `async-dependency` | ✅ / ➖ | The integration has **no external PyPI dependency** (`requirements: []`); all device I/O is `async` via `aiohttp` and HA's shared session. There is no synchronous library to make async. Record `done` (all I/O is async) — or `exempt` if a reviewer insists the rule only applies to a wrapped library. See the structural note below. |
| `inject-websession` | ✅ | All HTTP uses `async_get_clientsession(hass)` (in `__init__.py`, `update.py`, `github_releases.py`, `param_loader` via the passed session). Session reuse is already in place. |
| `strict-typing` | ❌ | `mypy --strict` currently reports **55 errors across 12 files**. This is the real work. |

### Structural note (important)
The `async-dependency` / `inject-websession` rules are written assuming the
integration wraps a **separate PyPI library** that talks to the device. WiCAN
embeds all device communication directly in the integration. Two positions:

1. **As-is:** no dependency ⇒ the rules are satisfied/exempt (nothing to fix).
2. **Core-aligned:** if this integration is ever submitted to HA **core**,
   reviewers typically expect device communication to live in a **separate,
   async, PEP‑561 (`py.typed`) library** on PyPI. That is a larger architectural
   change (extract a `wican` client library) and is **optional** for a custom
   (HACS) integration. It is out of scope for this plan unless core submission is
   a goal; if it is, add "Workstream 0: extract async client library" up front.

This plan targets **Platinum as a custom integration**: satisfy the three rules
in place, with `strict-typing` as the substantive effort.

---

## strict-typing — grounded assessment

Command used (approximates HA core's strict check for a custom component):

```bash
python -m mypy --strict --follow-imports=silent --ignore-missing-imports \
  custom_components/wican/
```

Result today: **55 errors in 12 files.**

### Errors by file
| File | Errors |
|---|---|
| `sensor.py` | 13 |
| `config_flow.py` | 13 |
| `update.py` | 6 |
| `attributes.py` | 5 |
| `device_tracker.py` | 4 |
| `binary_sensor.py` | 4 |
| `param_loader.py` | 3 |
| `entity.py` | 2 |
| `coordinator.py` | 2 |
| `__init__.py`, `helpers.py`, `github_releases.py` | 1 each |

### Errors by category (and fix approach)
| mypy code | Count | Root cause | Fix |
|---|---|---|---|
| `return-value` + `override` | 11 + 3 | Config-flow steps annotated `FlowResult`; HA now uses `ConfigFlowResult`. | Replace `FlowResult` → `ConfigFlowResult` (import from `homeassistant.config_entries`) on all step signatures. One mechanical sweep clears ~14 errors. |
| `no-untyped-def` | 11 | Entity `__init__`/callbacks lack annotations (`sensor.py`, `binary_sensor.py`, `config_flow.async_get_options_flow`, `device_tracker.device_info`). | Add full parameter + return annotations (mostly `-> None`, `config_entry: WiCANConfigEntry`, `entity_description: WiCANSensorEntityDescription`). |
| `no-untyped-call` | 5 | Calls to the above still-untyped `__init__`s. | Resolved automatically once the `__init__`s are typed. |
| `attr-defined` | 5 | Imports of `EntityCategory`, `SourceType`, `TrackerEntity` from modules that don't re-export them. | Import `EntityCategory` from `homeassistant.const`; `SourceType`/`TrackerEntity` from `homeassistant.components.device_tracker` canonical exports. |
| `assignment` | 5 | `_attr_extra_state_attributes = None` typed as `dict`; `_attr_in_progress = <int>` where the attr is `bool`. | Type attrs as `dict[str, Any] | None`; move progress percentages to `_attr_update_percentage` (int) and keep `_attr_in_progress: bool`. |
| `no-any-return` | 5 | Returning `Any` (e.g. `json.load`, dict `.get`) where a concrete type is declared. | Add explicit types / `cast()` / typed locals. |
| `typeddict-item` | 2 | `DeviceInfo(**device_info_dict)` — `**dict[str, Any]` into a TypedDict. | Build `DeviceInfo(...)` with explicit keyword args instead of `**dict`. |
| `union-attr` | 2 | Attribute access on a possibly-`None` value. | Narrow with a guard before access. |
| `misc` / `valid-type` / `type-arg` / `var-annotated` / `arg-type` | 6 | e.g. `DYNAMIC_PID_SENSORS` needs an annotation; generic missing type args. | Add the specific annotations mypy requests. |

None of these are architectural; they are localized, mechanical fixes. Estimated
effort: **~1 focused session**, file by file, re-running mypy after each.

### Additional strict-typing requirements
- **Custom typed config entry used throughout** (explicit rule requirement):
  `WiCANConfigEntry = ConfigEntry[WiCANRuntimeData]` already exists — ensure it is
  used in **every** function signature that takes a config entry (some currently
  use bare `ConfigEntry` or are untyped). Covered by the `no-untyped-def` fixes.
- **`py.typed`**: required for a distributed **library**, not for a custom
  component's own modules. If an external client library is extracted (see
  structural note), that library must ship `py.typed`.
- **Add to core `.strict-typing`**: only applies when merged into HA core; for a
  custom component the equivalent is enforcing `mypy --strict` in this repo's CI.

---

## strict-typing file checklist

Tick each file when `python -m mypy custom_components/wican` reports **0 errors
for that file**. Baseline: 55 errors / 12 files.

- [ ] `config_flow.py` (13)
- [ ] `sensor.py` (13)
- [ ] `update.py` (6)
- [ ] `attributes.py` (5)
- [ ] `device_tracker.py` (4)
- [ ] `binary_sensor.py` (4)
- [ ] `param_loader.py` (3)
- [ ] `entity.py` (2)
- [ ] `coordinator.py` (2)
- [ ] `__init__.py` (1)
- [ ] `helpers.py` (1)
- [ ] `github_releases.py` (1)

Tooling:
- [x] `[tool.mypy]` strict config in `pyproject.toml`
- [x] `mypy` added to `requirements_test.txt`
- [ ] CI job / pre-commit running `mypy custom_components/wican`

## Workstreams

### P1 — Tooling & baseline (do first)
- Add `mypy` to `requirements_test.txt`.
- Add a `[tool.mypy]` section to `pyproject.toml` mirroring HA's strict flags
  (`strict = true`, `follow_imports = silent`, `ignore_missing_imports = true`,
  `python_version` aligned with `requires-python`).
- Add a CI job / pre-commit hook running `mypy --strict custom_components/wican`.
- Capture the baseline (55) so progress is measurable.

### P2 — Fix strict-typing errors (file by file)
Recommended order (biggest, most mechanical first):
1. `config_flow.py` — `FlowResult` → `ConfigFlowResult` sweep + `async_get_options_flow` typing (13).
2. `sensor.py` — annotate entity `__init__`s and PID callbacks; annotate `DYNAMIC_PID_SENSORS`; fix `None`/dict attr (13).
3. `update.py` — `_attr_update_percentage` vs `_attr_in_progress`; `no-any-return` (6).
4. `attributes.py` — annotate `get_sensor_attributes`; entity-description typing (5).
5. `device_tracker.py` — import fixes; `device_info` typing; `DeviceInfo(**)` → kwargs (4).
6. `binary_sensor.py` — annotate `__init__`/callback; `None`/dict attr (4).
7. `param_loader.py`, `entity.py`, `coordinator.py`, `__init__.py`, `helpers.py`, `github_releases.py` — remaining (`no-any-return`, `union-attr`, `typeddict-item`) (10).

Re-run mypy after each file; target **0 errors**. Keep the full test suite green
after each change (typing changes shouldn't alter behavior, but the
`_attr_in_progress`/`_attr_update_percentage` change is behavioral — add/adjust a
test for the firmware progress values).

### P3 — Confirm async-dependency & inject-websession
- Record both as `done` in `quality_scale.yaml` with the rationale above (no
  external sync dependency; shared websession injected everywhere).
- If core submission becomes a goal, open **Workstream 0** to extract an async
  `py.typed` client library and depend on it (this is the only thing that would
  make these two rules non-trivial).

### P4 — Finalize
- `mypy --strict` clean in CI; full test suite green.
- Flip `strict-typing` (and confirm `async-dependency`, `inject-websession`) to
  `done` in `quality_scale.yaml`.
- Bump `manifest.json` `quality_scale` to `platinum` (only after Gold's `brands`
  PR has also merged — `quality_scale` reflects the fully-achieved tier).

---

## Risks & notes
- **`_attr_in_progress` change is behavioral.** Newer HA separates the boolean
  "in progress" from the integer percentage (`_attr_update_percentage`). Verify
  against the installed HA version (2026.2.x) and adjust the firmware-update UI
  progress accordingly; cover with a test.
- **HA API drift.** `FlowResult` → `ConfigFlowResult` reflects current HA; pin the
  expectation to the HA version in `requirements_test.txt`.
- **mypy strictness vs custom-component reality.** `--ignore-missing-imports` is
  used because HA core isn't a typed dependency here; in core CI the imports are
  fully typed. Our local gate is a close approximation, not identical.
- **Scope creep to a client library.** Resist extracting a PyPI library unless
  core submission is the explicit goal — it is not required for a custom-component
  Platinum.

## Definition of done (Platinum)
1. `mypy --strict custom_components/wican` → **0 errors**, enforced in CI.
2. `WiCANConfigEntry` used in every config-entry-typed signature.
3. `async-dependency`, `inject-websession`, `strict-typing` all `done` in
   `quality_scale.yaml`.
4. Full test suite green; coverage still > 95 %.
5. `manifest.json` `quality_scale: platinum` (after Gold `brands` is merged).
