# WiCAN — Plan to reach Gold on the Home Assistant Quality Scale

This is the **strategy** document. The concrete, step-by-step task list lives in
[`QUALITY_SCALE_EXECUTION.md`](QUALITY_SCALE_EXECUTION.md). The per-rule
compliance matrix lives in [`HA_CODING_STANDARDS.md`](HA_CODING_STANDARDS.md).

## Goal

Move the WiCAN integration from its current state (roughly Bronze-with-gaps) to
a fully compliant **Gold** integration: all Bronze + Silver + Gold rules pass as
`done` or justified `exempt`, `manifest.json` declares `quality_scale: gold`, and
`hassfest`/`ruff`/tests are green with >95 % coverage.

## Guiding principles

1. **Land the scaffolding first.** Add `quality_scale.yaml` and the manifest key
   so progress is trackable rule-by-rule and `hassfest` validates it.
2. **Code before docs before brands.** Code changes are in our control and
   testable; docs are mechanical; the brands PR is external and asynchronous, so
   start it early but don't block on it.
3. **Every code change ships with tests.** Coverage must climb to >95 %, so we
   never add code without covering it.
4. **No behavioural regressions.** The webhook registration / push pipeline is
   the heart of the integration — changes to `__init__.py`, `coordinator.py`, and
   entities must keep existing tests green.
5. **Record exemptions honestly.** Push/no-auth exemptions are legitimate but
   must be justified in `quality_scale.yaml`.

## Workstreams

### A. Tracking scaffold (unblocks everything)
- `quality_scale.yaml` with every rule marked `done` / `todo` / `exempt`.
- `manifest.json` `quality_scale` key (start at `bronze`, bump as we go).

### B. Bronze gaps
- Manual config flow: set unique ID + `_abort_if_unique_id_configured`
  (`unique-config-entry`).
- `data_description` translation keys for config-flow fields (`config-flow`).
- `test-before-setup`: raise `ConfigEntryNotReady` on transient setup failure.
- 100 % config-flow test coverage (`config-flow-test-coverage`).
- Docs: removal instructions (`docs-removal-instructions`) + verify high-level
  description and installation instructions.

### C. Silver gaps
- `device_tracker` `PARALLEL_UPDATES` (`parallel-updates`).
- Entity availability based on data staleness (`entity-unavailable`) +
  log-once-on-unavailable/recovery (`log-when-unavailable`).
- Docs: configuration + installation parameters.
- **Test coverage > 95 %** — the largest single effort.

### D. Gold — code
- `icons.json` icon translations; drop `_attr_icon`/`icon=` (`icon-translations`).
- `entity_registry_enabled_default = False` for high-volume PID sensors
  (`entity-disabled-by-default`).
- `async_step_reconfigure` (`reconfiguration-flow`).
- Repair issues for webhook registration failure / not-internet-accessible
  (`repair-issues`).
- Exception-translation audit (`exception-translations`).
- Zeroconf re-discovery updates connection info (`discovery-update-info`).

### E. Gold — docs
- `docs-data-update`, `docs-examples`, `docs-known-limitations`,
  `docs-supported-devices`, `docs-supported-functions`, `docs-troubleshooting`,
  `docs-use-cases`. Restructure `README.md` into the HA docs sections.

### F. Brands (external, parallel)
- PR to `home-assistant/brands` with `wican` logo + icon.

## Phasing

| Phase | Workstream | Outcome |
|---|---|---|
| 0 | A | Trackable scaffold; `hassfest` validates quality_scale. |
| 1 | B (code) + C (code) + D (code) | All in-repo code rules pass; tier claim → silver then gold candidate. |
| 2 | C (tests) | Coverage > 95 %; Silver fully unlocked. |
| 3 | B/C/E (docs) | All `docs-*` rules pass. |
| 4 | F (brands) | External PR merged. |
| 5 | Finalize | Flip `quality_scale.yaml` rules to `done`, manifest → `gold`, full CI green. |

## Risks & mitigations

- **Push-model exemptions rejected upstream.** Mitigation: justify each exemption
  precisely in `quality_scale.yaml`; mirror wording of other push integrations.
- **Coverage plateau on `__init__.py`** (webhook registration retry paths).
  Mitigation: parametrized tests around endpoint fallback, timeout, and retry
  branches; use `freezegun`/`async_fire_time_changed` for backoff.
- **Dynamic PID entity translations** can't be fully translated. Mitigation:
  document as a known limitation; ensure friendly names.
- **Brands PR latency.** Mitigation: open it first; Gold sign-off can wait on it
  while all code/doc work completes.

## Success criteria

See [`HA_CODING_STANDARDS.md` §9 "Definition of done for Gold"](HA_CODING_STANDARDS.md).
