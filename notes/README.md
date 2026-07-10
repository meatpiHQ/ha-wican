# Project Documentation Index

Documentation for the MeatPi Home Assistant integration (HA domain:
`wican`). Start here.

## Device contract — [device-contract/](device-contract/)

Everything a MeatPi device (firmware) needs to work with this
integration lives in one folder. Building or bringing up a new product?
Start at [device-contract/README.md](device-contract/README.md).

| Document | What it is |
|---|---|
| [device-contract/ADDING_A_DEVICE.md](device-contract/ADDING_A_DEVICE.md) | **How to add a new MeatPi product.** Part A: the device contract firmware must implement (mandatory webhook level + optional V6 control level). Part B: integration-side steps, with checklists. |
| [device-contract/DEVICE_COMPATIBILITY_GUIDE.md](device-contract/DEVICE_COMPATIBILITY_GUIDE.md) | The device-side contract as derived from integration source (Level 1 detail behind ADDING_A_DEVICE.md). |
| [device-contract/DEVICE_ENDPOINTS.md](device-contract/DEVICE_ENDPOINTS.md) | Webhook-registration endpoint + push-payload reference (incl. the GPS section and WiCAN-Pro multi-URL behavior). |
| [device-contract/HTTP_API.md](device-contract/HTTP_API.md) | Mirror of the WiCAN firmware V6 HTTP API route map (conventions + every `/api/*` surface). Source of truth lives with the firmware; keep in sync. |
| [device-contract/FIRMWARE_DEVICE_TYPES.md](device-contract/FIRMWARE_DEVICE_TYPES.md) | WiCAN hardware variants, firmware release streams (PRO/USB/OBD), and firmware-asset naming for the update entity. |
| [device-contract/DEVICE_CATALOG.md](device-contract/DEVICE_CATALOG.md) | The remote device-catalog spec: schema, validation bounds, publishing workflow for `meatpiHQ/meatpi-devices` (repo pending). |
| [device-contract/FIRMWARE_API_FEEDBACK.md](device-contract/FIRMWARE_API_FEEDBACK.md) | Requested firmware API changes (device identity, `_meatpi._tcp` mDNS, payload schema version, generic action invoke, logger export route, ESPNetlink telemetry shape) with status per ask. |

## Living documents (kept current — update these with code changes)

| Document | What it is |
|---|---|
| [MEATPI_INTEGRATION_PLAN.md](MEATPI_INTEGRATION_PLAN.md) | **The architecture document.** Brand/domain decision, device-type framework, control channel, and the executed phase-by-phase record (rebrand → profiles → V6 API → buttons → robustness → history backfill). |
| [HISTORICAL_DATA_SYNC.md](HISTORICAL_DATA_SYNC.md) | History backfill: research (what HA allows), the implemented design (SD card → long-term statistics), limitations, and future phases (sum-type totals, trip viewer). |
| [ROBUSTNESS_AUDIT_2026-07.md](ROBUSTNESS_AUDIT_2026-07.md) | The 2026-07 robustness audit: every finding (high through low) fixed with regression tests; kept as the record and for its verified-sound notes. |

## Historical records (dated snapshots — do not update, superseded by the above)

| Document | Snapshot of |
|---|---|
| [ROBUSTNESS_HARDENING.md](ROBUSTNESS_HARDENING.md) | The 2026-07-10 hardening pass that preceded the MeatPi restructure (421-test era). |
| [GPS_FEATURE.md](GPS_FEATURE.md) / [GPS_IMPLEMENTATION_SUMMARY.md](GPS_IMPLEMENTATION_SUMMARY.md) | The GPS device-tracker feature as delivered. |
| [FIRMWARE_UPDATE_PLAN.md](FIRMWARE_UPDATE_PLAN.md) / [FIRMWARE_UPDATE_TEST_STATUS.md](FIRMWARE_UPDATE_TEST_STATUS.md) | The OTA update-entity project. |
| [IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md) / [COMPLETION_SUMMARY.md](COMPLETION_SUMMARY.md) / [QUICKSTART.md](QUICKSTART.md) | The original 2025 improvement project (stats long superseded). |

Current quality gates live in [../quality_scale.yaml](../quality_scale.yaml)
(assessment) and [../custom_components/wican/quality_scale.yaml](../custom_components/wican/quality_scale.yaml)
(per-rule status); current test statistics in the repo README and the
plan document's "final gates" sections.
