"""MeatPi integration (WiCAN and other MeatPi devices)."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
import logging
import re
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from aiohttp import ClientError, ClientResponseError
from aiohttp.web import Request, Response
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_WEBHOOK_ID, EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
import voluptuous as vol
from yarl import URL

from .api import MeatPiApiClient
from .catalog import async_setup_catalog
from .const import (
    COMPONENT_DATA_LOGGER,
    CONF_DEVICE_TYPE,
    CONF_HISTORY_SYNC,
    CONF_POST_INTERVAL,
    DEFAULT_HISTORY_SYNC,
    DEFAULT_POST_INTERVAL,
    DOMAIN,
    HISTORY_SYNC_MIN_INTERVAL,
    IP_CACHE_DURATION,
    MAX_DEVICE_INFO_FIELD_LENGTH,
    PRO_DUAL_WEBHOOK_MIN_FW_VERSION,
    PROBE_RETRY_INTERVAL,
    SIGNAL_CAPABILITIES_UPDATED,
    WEBHOOK_MAX_RETRIES,
    WEBHOOK_REGISTRATION_TIMEOUT,
    WEBHOOK_RETRY_DELAY_BASE,
)
from .coordinator import WiCANDataUpdateCoordinator
from .devices import WICAN_FAMILY_TYPES, get_profile, infer_device_type
from .exceptions import WiCANWebhookError
from .github_releases import GitHubReleasesCoordinator
from .helpers import resolve_device_webhook_urls
from .history import async_remove_history_store, async_sync_history
from .models import WiCANRuntimeData
from .param_loader import async_update_params_from_github

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.DEVICE_TRACKER,
    Platform.UPDATE,
]

# Type alias for config entry with runtime data
WiCANConfigEntry = ConfigEntry[WiCANRuntimeData]


class _WebhookEndpointsFailedError(WiCANWebhookError):
    """Raised when all webhook endpoints failed for this attempt."""


def _parse_version(version: str | None) -> tuple[int, ...] | None:
    """Parse a version string like v4.49 into a comparable tuple."""
    # Legacy entries may hold a non-string here; treat it as unknown.
    if not version or not isinstance(version, str):
        return None

    parts = re.findall(r"\d+", version)
    if not parts:
        return None

    return tuple(int(part) for part in parts)


def _is_version_at_least(version: str | None, minimum: tuple[int, ...]) -> bool:
    """Return True when version is greater than or equal to minimum."""
    parsed_version = _parse_version(version)
    if parsed_version is None:
        return False

    max_len = max(len(parsed_version), len(minimum))
    padded_version = parsed_version + (0,) * (max_len - len(parsed_version))
    padded_minimum = minimum + (0,) * (max_len - len(minimum))
    return padded_version >= padded_minimum


def _supports_dual_webhook_urls(entry: WiCANConfigEntry) -> bool:
    """Return True when the device firmware can accept multiple webhook URLs."""
    hw_version = str(entry.data.get("hw_version", "")).lower()
    if "pro" not in hw_version:
        return False

    return _is_version_at_least(
        entry.data.get("fw_version"),
        PRO_DUAL_WEBHOOK_MIN_FW_VERSION,
    )


def _build_webhook_payload(
    hass: HomeAssistant,
    entry: WiCANConfigEntry,
    post_interval: int,
) -> dict[str, str | bool | int | list[str]]:
    """Build the webhook registration payload for the device firmware."""
    urls = resolve_device_webhook_urls(
        hass,
        entry.runtime_data.webhook_id,
        fallback_url=entry.data.get("webhook_url"),
        allow_external_https_fallback=_supports_dual_webhook_urls(entry),
    )

    payload: dict[str, str | bool | int | list[str]] = {
        "url": urls[0],
        "enabled": True,
        "interval": post_interval,
    }

    if _supports_dual_webhook_urls(entry) and len(urls) > 1:
        payload["urls"] = urls

    return payload


def _ensure_http_scheme(value: str | None) -> str | None:
    """Return value with http:// if no scheme is provided."""
    if not value:
        return value
    if value.startswith(("http://", "https://")):
        return value
    return f"http://{value}"


def _http_url_from_host(host: str | None, port: int | None = None) -> str | None:
    """Build an http URL from a bare host/ip string."""
    if not host:
        return None
    try:
        return str(URL.build(scheme="http", host=host, port=port))
    except ValueError:
        return None


def _build_webhook_endpoint(base: str | None) -> URL | None:
    """Return the device webhook endpoint URL constructed from a base URL."""
    if not base:
        return None

    candidate = base.strip()
    try:
        url = URL(candidate)
    except ValueError:
        return None

    if not url.scheme:
        try:
            schemed_candidate = _ensure_http_scheme(candidate)
            if schemed_candidate is None:
                return None
            url = URL(schemed_candidate)
        except ValueError:
            return None

    # Drop any path/query/fragment parts before appending the webhook path
    url = url.with_path("").with_query(None).with_fragment(None)
    return url / "api" / "webhook"


# Device-reported fields that are persisted into the config entry. Order in
# the payload: nested "status" wins over a top-level key, matching identity
# validation precedence.
_DEVICE_INFO_KEYS = (
    "fw_version",
    "hw_version",
    "device_id",
    "git_version",
    "mdns",
    "host",
    "ip",
)


def _sanitize_device_field(value: object) -> str | None:
    """Return a safe string form of a device-reported info field, or None.

    These values end up in the config entry (written to disk) and later flow
    into version parsing, URL building, and the device registry — all of which
    assume strings. A glitching or hostile device must not be able to persist
    anything else.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_DEVICE_INFO_FIELD_LENGTH:
        return None
    return value


def _normalize_ip(ip: str | None) -> str | None:
    """Normalize IPv4-mapped IPv6 strings into IPv4 when possible."""
    if not ip:
        return None
    if ip.startswith("::ffff:") and ip.count(":") >= 2:
        return ip.split("::ffff:", maxsplit=1)[-1]
    return ip


def _extract_request_ip(request: Request) -> str | None:
    """Best-effort extraction of the originating peer IP address."""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        first = forwarded_for.split(",")[0].strip()
        if first:
            return _normalize_ip(first)

    transport = request.transport
    if transport is not None:
        peername = transport.get_extra_info("peername")
        if isinstance(peername, (tuple, list)) and peername:
            return _normalize_ip(peername[0])

    if request.remote:
        return _normalize_ip(request.remote)

    return None


def _collect_device_reported_fields(data: dict[str, Any]) -> dict[str, str]:
    """Extract sanitized device info fields from a webhook payload.

    Fields come from the nested "status" object or the top level; only
    bounded, non-empty strings are kept (a glitching device may send nested
    objects or numbers here) so nothing unusable ever reaches the config
    entry, version parsing, or URL building.
    """
    fields: dict[str, str] = {}
    status = data.get("status")
    if not isinstance(status, dict):
        status = {}
    for key in _DEVICE_INFO_KEYS:
        if key in status:
            raw_field = status[key]
        elif key in data:
            raw_field = data[key]
        else:
            continue
        field_value = _sanitize_device_field(raw_field)
        if field_value is None:
            if raw_field not in (None, ""):
                _LOGGER.debug(
                    "Ignoring unusable device-reported %s: %r", key, raw_field,
                )
            continue
        fields[key] = field_value
    return fields


def _merge_device_fields(
    entry: WiCANConfigEntry,
    device_info_fields: dict[str, str],
) -> tuple[dict[str, Any], bool, bool]:
    """Merge device-reported fields into a copy of the entry data.

    Returns the merged data plus whether anything changed and whether a
    connection-relevant field (host/ip/mdns) changed.
    """
    new_data = dict(entry.data)
    connection_field_changed = False
    data_changed = False
    for key, value in device_info_fields.items():
        # Skip if value hasn't actually changed
        if new_data.get(key) == value:
            continue
        new_data[key] = value
        data_changed = True
        # Only mark as connection change if host/ip/mdns actually changed
        if key in {"host", "ip", "mdns"}:
            connection_field_changed = True
    return new_data, data_changed, connection_field_changed


def _persist_device_reported_info(
    hass: HomeAssistant,
    entry: WiCANConfigEntry,
    data: dict[str, Any],
    request: Request,
) -> None:
    """Persist device-reported info in the config entry and react to changes."""
    device_info_fields = _collect_device_reported_fields(data)

    # Capture device IP from the inbound request as an authoritative source
    remote_ip = _extract_request_ip(request)
    if remote_ip:
        entry.runtime_data.device_ip = remote_ip
        device_info_fields["ip"] = remote_ip
        # Don't set host from IP if we already have a hostname
        # (avoid triggering re-registration when IP resolves to stored hostname)
        host_from_ip = _http_url_from_host(remote_ip)
        if host_from_ip and not entry.data.get("host"):
            entry.runtime_data.device_host = host_from_ip
            device_info_fields["host"] = host_from_ip

    if not device_info_fields:
        return

    new_data, data_changed, connection_field_changed = _merge_device_fields(
        entry, device_info_fields,
    )

    if not data_changed:
        return

    # Keep the device-type slug in sync with the reported hardware version
    # (a manually added entry learns its real product type on first push).
    # Only re-infer inside the WiCAN family: a device_type set by discovery
    # (espnetlink, meatpi, future slugs) is authoritative and must never be
    # clobbered by hardware-string guessing.
    current_type = new_data.get(CONF_DEVICE_TYPE)
    if "hw_version" in device_info_fields and (
        not current_type or current_type in WICAN_FAMILY_TYPES
    ):
        inferred_type = infer_device_type(new_data.get("hw_version"))
        if current_type != inferred_type:
            new_data[CONF_DEVICE_TYPE] = inferred_type
            entry.runtime_data.device_profile = get_profile(inferred_type)

    hass.config_entries.async_update_entry(entry, data=new_data)
    if connection_field_changed:
        entry.runtime_data.device_host = new_data.get("host") or entry.runtime_data.device_host
        entry.runtime_data.device_ip = new_data.get("ip") or entry.runtime_data.device_ip
        # Refresh registration out-of-band so future retries use the new address
        _LOGGER.info(
            "Connection info changed for %s, re-registering webhook",
            entry.title,
        )
        hass.async_create_task(
            _async_request_webhook_registration(hass, entry),
        )

    # A new address or a new firmware version can change what the control
    # API offers (e.g. a device OTA-updated to V6 gains control entities).
    if connection_field_changed or "fw_version" in device_info_fields:
        hass.async_create_task(
            _async_request_capability_probe(hass, entry),
        )


async def async_migrate_entry(
    hass: HomeAssistant,
    entry: WiCANConfigEntry,
) -> bool:
    """Migrate old config entries to the current schema.

    1.1 → 1.2: backfill the device_type slug (brand framework). Every entry
    that predates device types is a WiCAN variant, inferred from the stored
    hardware version.
    """
    if entry.version > 1:
        # Downgrade from a future major version; cannot know the schema.
        return False

    if entry.minor_version < 2:
        # Load the device catalog first so inference can resolve
        # catalog-defined product types, not just the built-ins.
        await async_setup_catalog(hass)
        new_data = dict(entry.data)
        if not new_data.get(CONF_DEVICE_TYPE):
            new_data[CONF_DEVICE_TYPE] = infer_device_type(
                new_data.get("hw_version"),
            )
        hass.config_entries.async_update_entry(
            entry, data=new_data, version=1, minor_version=2,
        )
        _LOGGER.debug(
            "Migrated entry %s to 1.2 (device_type=%s)",
            entry.title,
            new_data[CONF_DEVICE_TYPE],
        )

    return True


def _webhook_repair_issue_id(entry: WiCANConfigEntry) -> str:
    """Return the repair issue id for a failed webhook registration."""
    return f"webhook_registration_failed_{entry.entry_id}"


def _raise_webhook_repair(
    hass: HomeAssistant,
    entry: WiCANConfigEntry,
    endpoints: str,
) -> None:
    """Surface a repair issue when the device cannot be reached to register."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        _webhook_repair_issue_id(entry),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="webhook_registration_failed",
        translation_placeholders={
            "device": entry.title,
            "endpoints": endpoints,
        },
    )


def _clear_webhook_repair(hass: HomeAssistant, entry: WiCANConfigEntry) -> None:
    """Clear a previously raised webhook-registration repair issue."""
    ir.async_delete_issue(hass, DOMAIN, _webhook_repair_issue_id(entry))


async def async_setup_entry(  # noqa: C901, PLR0915
    hass: HomeAssistant,
    entry: WiCANConfigEntry,
) -> bool:
    """Set up WiCAN from a config entry."""
    # Sync PID parameter definitions (stored copy applies immediately; a
    # coalesced, ETag-cheap GitHub fetch keeps the reload-to-refresh
    # support flow instant). Best-effort: bundled/stored copy on failure.
    try:
        await async_update_params_from_github(hass)
    except Exception as err:
        _LOGGER.debug("Could not update params from GitHub: %s", err)
        # Continue with bundled/cached version

    # Ensure webhook_id exists (older entries may lack it); generate if missing
    webhook_id = entry.data.get(CONF_WEBHOOK_ID)
    if not webhook_id:
        try:
            webhook_id = uuid4().hex
            new_data = dict(entry.data)
            new_data[CONF_WEBHOOK_ID] = webhook_id
            hass.config_entries.async_update_entry(entry, data=new_data)
            _LOGGER.info("Generated missing webhook_id for entry %s", entry.title)
        except Exception:
            _LOGGER.warning("Failed to generate webhook_id; setup may fail")
            return False

    # Load the device catalog (bundled/stored copy applies immediately, a
    # remote refresh runs in the background at most once per day) so
    # catalog-defined products resolve to their real profiles.
    await async_setup_catalog(hass)

    # Resolve the device-type profile (brand framework). Migration backfills
    # device_type for old entries; this guard covers entries created by
    # flows that could not know the type yet (e.g. manual setup).
    device_type = entry.data.get(CONF_DEVICE_TYPE)
    if not device_type:
        device_type = infer_device_type(entry.data.get("hw_version"))
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DEVICE_TYPE: device_type},
        )
    device_profile = get_profile(device_type)

    # Get post interval from options
    post_interval = entry.options.get(CONF_POST_INTERVAL, DEFAULT_POST_INTERVAL)

    # Create coordinator for this entry
    coordinator = WiCANDataUpdateCoordinator(
        hass,
        entry,
    )

    # Determine if this is a WiCAN-PRO device
    hw_version = entry.data.get("hw_version", "").lower()
    is_pro = "pro" in hw_version

    github_coordinator = GitHubReleasesCoordinator(hass, is_pro=is_pro)
    try:
        await github_coordinator.async_config_entry_first_refresh()
    except Exception as err:
        _LOGGER.warning(
            "Failed to fetch GitHub releases (firmware updates unavailable): %s",
            err,
        )
        # Don't fail setup - update entity will just show as unavailable

    # Set runtime_data with all necessary data
    entry.runtime_data = WiCANRuntimeData(
        coordinator=coordinator,
        github_coordinator=github_coordinator,
        webhook_id=webhook_id,
        post_interval=post_interval,
        device_host=entry.data.get("host"),
        device_ip=entry.data.get("ip"),
        device_profile=device_profile,
    )

    # Perform first refresh to initialize coordinator
    # For push-based WiCAN, this succeeds immediately with empty data
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        _LOGGER.warning(
            "First refresh failed for %s (push-based integration will retry): %s",
            entry.title,
            err,
        )
        # Don't fail setup - entities will update when first webhook arrives

    async def handle_webhook(
        hass: HomeAssistant,
        webhook_id: str,
        request: Request,
    ) -> Response:
        """Handle incoming WiCAN webhook request."""
        _LOGGER.info("Received WiCAN webhook: %s", webhook_id)
        try:
            data = await request.json()
            _LOGGER.debug("Webhook payload: %s", data)
        except vol.MultipleInvalid as error:
            return Response(
                text=error.error_message, status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        except (ValueError, UnicodeDecodeError):
            # Body was not valid JSON (json.JSONDecodeError is a ValueError).
            _LOGGER.warning("Received WiCAN webhook with an invalid JSON body")
            return Response(
                text="Invalid JSON body",
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )

        # WiCAN always sends a JSON object. Reject anything else defensively so a
        # malformed device (or unrelated caller) cannot crash the handler.
        if not isinstance(data, dict):
            _LOGGER.warning(
                "Received WiCAN webhook with non-object JSON payload (%s)",
                type(data).__name__,
            )
            return Response(
                text="Expected a JSON object",
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )

        # Validate identity and apply data BEFORE persisting any device-reported
        # connection info. Otherwise an impostor's device_id would be written to
        # the config entry first and the identity check would pass against it.
        try:
            resumed_after_gap = coordinator.handle_webhook_data(data)
        except ConfigEntryError:
            _LOGGER.exception(
                "Rejecting webhook due to device identity validation failure",
            )
            return Response(
                text="Device identity mismatch",
                status=HTTPStatus.FORBIDDEN,
            )

        # Persist any device-reported info (fw/hw versions, connection data).
        _persist_device_reported_info(hass, entry, data, request)

        # A device that pushes is clearly awake. If the capability probe has
        # never reached it (device was asleep/unreachable at setup), retry
        # now — rate-limited — so it still gains its control entities.
        runtime = getattr(entry, "runtime_data", None)
        if runtime is not None and not runtime.probe_successful:
            now = time.monotonic()
            if (
                runtime.last_probe_retry == 0.0
                or now - runtime.last_probe_retry >= PROBE_RETRY_INTERVAL
            ):
                runtime.last_probe_retry = now
                hass.async_create_task(
                    _async_request_capability_probe(hass, entry),
                )

        # This push ended a silence gap: the device may have logged data to
        # its SD card while away — backfill it into long-term statistics.
        if resumed_after_gap:
            _schedule_history_sync(hass, entry)

        # Keep dispatcher for backward compatibility during migration
        async_dispatcher_send(hass, DOMAIN, webhook_id, data)
        return Response(status=HTTPStatus.NO_CONTENT)

    webhook.async_register(
        hass, DOMAIN, entry.title, webhook_id, handle_webhook,
    )

    # Normalize host/mdns schemes BEFORE scheduling registration
    # to avoid triggering update listener which would cause duplicate registrations
    _normalize_connection_urls(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _schedule_webhook_registration(hass, entry)

    # Discover the device's control capabilities (V6 HTTP API) out-of-band.
    # A legacy or unreachable device simply keeps legacy capabilities.
    hass.async_create_task(_async_request_capability_probe(hass, entry))

    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: WiCANConfigEntry,
) -> bool:
    """Unload a config entry."""
    webhook.async_unregister(hass, entry.runtime_data.webhook_id)
    _clear_webhook_repair(hass, entry)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(
    hass: HomeAssistant, entry: WiCANConfigEntry,
) -> None:
    """Clean up persisted state when a config entry is removed for good."""
    await async_remove_history_store(hass, entry)


def _normalize_connection_urls(hass: HomeAssistant, entry: WiCANConfigEntry) -> None:
    """Normalize host/mdns URLs by ensuring they have http:// scheme.

    This is done once during setup to avoid triggering update listener
    which would cause duplicate webhook registrations.
    """
    updated_data = dict(entry.data)
    data_changed = False

    # Normalize mdns
    mdns = updated_data.get("mdns")
    if mdns:
        normalized_mdns = _ensure_http_scheme(mdns)
        if normalized_mdns != mdns:
            updated_data["mdns"] = normalized_mdns
            data_changed = True

    # Normalize host
    host = updated_data.get("host")
    if host:
        normalized_host = _ensure_http_scheme(host)
        if normalized_host != host:
            updated_data["host"] = normalized_host
            data_changed = True

    if data_changed:
        hass.config_entries.async_update_entry(entry, data=updated_data)


def _schedule_webhook_registration(hass: HomeAssistant, entry: WiCANConfigEntry) -> None:
    if hass.is_running:
        hass.async_create_task(_async_request_webhook_registration(hass, entry))
        return

    fired = False

    @callback
    def _on_started(_: object) -> None:
        nonlocal fired
        fired = True
        hass.async_create_task(_async_request_webhook_registration(hass, entry))

    unsub = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)

    def _cancel_startup_registration() -> None:
        # Only unsubscribe if the once-listener has not fired (it removes
        # itself on fire; removing again would log an error).
        if not fired:
            unsub()

    entry.async_on_unload(_cancel_startup_registration)


def _device_api_base_url(entry: WiCANConfigEntry) -> str | None:
    """Return the best base URL for talking to the device HTTP API."""
    runtime = entry.runtime_data
    for candidate in (
        runtime.device_host,
        entry.data.get("host"),
        entry.data.get("mdns"),
    ):
        if candidate:
            return str(candidate)
    if runtime.device_ip:
        return _http_url_from_host(runtime.device_ip)
    return None


async def _async_request_capability_probe(
    hass: HomeAssistant,
    entry: WiCANConfigEntry,
) -> None:
    """Probe device capabilities, coalescing concurrent requests.

    Setup, address changes, and firmware-version changes can all request a
    probe; like webhook registration, run one at a time and fold requests
    that arrive meanwhile into a single trailing re-run.
    """
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return
    runtime.probe_pending = True
    if runtime.probe_running:
        return
    runtime.probe_running = True
    try:
        while runtime.probe_pending:
            runtime.probe_pending = False
            await _async_probe_capabilities(hass, entry)
    finally:
        runtime.probe_running = False


async def _async_probe_capabilities(
    hass: HomeAssistant,
    entry: WiCANConfigEntry,
) -> None:
    """Probe the device control API once and publish the result.

    Never raises: a legacy or unreachable device yields legacy capabilities,
    and the integration behaves exactly as it did before control support.
    """
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return
    base_url = _device_api_base_url(entry)
    if not base_url:
        _LOGGER.debug(
            "Entry %s has no device address; skipping capability probe",
            entry.entry_id,
        )
        return

    api = MeatPiApiClient(async_get_clientsession(hass), base_url)
    try:
        capabilities = await api.async_probe()
    except Exception:  # Defensive: a probe must never break anything else.
        _LOGGER.exception("Unexpected error probing device capabilities")
        return

    # The entry may have been unloaded while the probe was in flight.
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return

    runtime.api = api

    if capabilities is None:
        # Device unreachable (asleep, offline, mid-reboot): keep last-known
        # capabilities and let telemetry pushes retry the probe later.
        runtime.probe_successful = False
        return
    runtime.probe_successful = True

    previous = runtime.capabilities
    runtime.capabilities = capabilities

    if capabilities.has_http_api and not previous.has_http_api:
        _LOGGER.info(
            "Device %s exposes the control HTTP API (%d components); "
            "control entities enabled",
            entry.title,
            len(capabilities.components),
        )

    if capabilities != previous:
        async_dispatcher_send(
            hass, f"{SIGNAL_CAPABILITIES_UPDATED}_{entry.entry_id}",
        )

    # A reachable data_logger device may hold rows recorded while HA was
    # down or the device was away — catch up (rate-limited, coalesced).
    if capabilities.has_component(COMPONENT_DATA_LOGGER):
        _schedule_history_sync(hass, entry)


def _schedule_history_sync(hass: HomeAssistant, entry: WiCANConfigEntry) -> None:
    """Schedule a history backfill run if enabled, capable, and not rate-limited."""
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return
    if not entry.options.get(CONF_HISTORY_SYNC, DEFAULT_HISTORY_SYNC):
        return
    if not runtime.capabilities.has_component(COMPONENT_DATA_LOGGER):
        return
    now = time.monotonic()
    if (
        runtime.last_history_sync
        and now - runtime.last_history_sync < HISTORY_SYNC_MIN_INTERVAL
    ):
        return
    runtime.last_history_sync = now
    hass.async_create_task(_async_request_history_sync(hass, entry))


async def _async_request_history_sync(
    hass: HomeAssistant,
    entry: WiCANConfigEntry,
) -> None:
    """Run a history backfill, coalescing concurrent requests.

    Same pattern as webhook registration and the capability probe: one run
    at a time, requests arriving meanwhile fold into a single trailing
    re-run. The sync itself never raises.
    """
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return
    runtime.history_sync_pending = True
    if runtime.history_sync_running:
        return
    runtime.history_sync_running = True
    try:
        while runtime.history_sync_pending:
            runtime.history_sync_pending = False
            result = await async_sync_history(hass, entry)
            # The entry may have been unloaded while the sync was running.
            current = getattr(entry, "runtime_data", None)
            if current is None:
                return
            current.last_history_result = result
    finally:
        runtime.history_sync_running = False


async def _async_request_webhook_registration(
    hass: HomeAssistant,
    entry: WiCANConfigEntry,
) -> None:
    """Register the webhook on the device, coalescing concurrent requests.

    Pushes and option updates can request re-registration faster than one
    (retried, backed-off) registration attempt completes — for example a
    device that alternates between two IP addresses. Run a single registration
    at a time and fold any requests that arrive meanwhile into one trailing
    re-run, so registration attempts can never pile up against the device.
    """
    # The entry may have been unloaded between scheduling and execution;
    # runtime_data is deleted on unload.
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return
    runtime.registration_pending = True
    if runtime.registration_running:
        return
    runtime.registration_running = True
    try:
        while runtime.registration_pending:
            runtime.registration_pending = False
            await _async_register_webhook_on_device(hass, entry)
    finally:
        runtime.registration_running = False


async def _async_register_webhook_on_device(  # noqa: C901, PLR0912, PLR0915
    hass: HomeAssistant,
    entry: WiCANConfigEntry,
    max_retries: int = WEBHOOK_MAX_RETRIES,
) -> bool:
    """Push webhook URL and interval to the WiCAN device with retry."""
    # Prefer direct IP/host if available (similar to WLED), fallback to mDNS
    host = entry.runtime_data.device_host or entry.data.get("host")
    ip = entry.runtime_data.device_ip or entry.data.get("ip")
    mdns = entry.data.get("mdns")

    if not host and ip:
        host = _http_url_from_host(ip)
        entry.runtime_data.device_host = host

    if not host and not mdns:
        _LOGGER.debug(
            "Entry %s missing host/mdns; cannot register webhook",
            entry.entry_id,
        )
        return False

    # URLs already normalized during setup, just ensure runtime_data is updated
    if host:
        entry.runtime_data.device_host = host
    elif ip and not host:
        # Derive host from IP if needed
        derived_host = _http_url_from_host(ip)
        if derived_host:
            host = derived_host
            entry.runtime_data.device_host = derived_host

    # Get post interval from runtime_data
    post_interval = entry.runtime_data.post_interval

    try:
        payload = _build_webhook_payload(
            hass,
            entry,
            post_interval,
        )
    except Exception as err:
        _LOGGER.warning(
            "Cannot generate webhook URL payload for %s: %s",
            entry.entry_id,
            err,
        )
        return False

    _LOGGER.info(
        "Registering WiCAN webhook %s with interval %ss",
        payload["url"],
        post_interval,
    )

    # Backfill webhook_url for older entries after successfully building payload.
    if "webhook_url" not in entry.data:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, "webhook_url": payload["url"]},
        )
        _LOGGER.debug("Backfilled webhook_url for entry %s", entry.entry_id)

    # Use HA's shared session (reuses connections)
    session = async_get_clientsession(hass)

    # Build endpoint candidates: prefer direct host/IP over mDNS
    # Use cached resolved IP if available and fresh (< 5 minutes old)
    endpoints: list[URL] = []

    # Check if we have a cached IP and it's still valid
    if (
        entry.runtime_data.cached_resolved_ip
        and entry.runtime_data.cache_timestamp
        and (time.time() - entry.runtime_data.cache_timestamp) < IP_CACHE_DURATION
    ):
        cached_endpoint = _build_webhook_endpoint(
            f"http://{entry.runtime_data.cached_resolved_ip}",
        )
        if cached_endpoint:
            endpoints.append(cached_endpoint)
            _LOGGER.debug(
                "Using cached IP %s (age: %.1fs)",
                entry.runtime_data.cached_resolved_ip,
                time.time() - entry.runtime_data.cache_timestamp,
            )

    # Add host and mDNS as fallback
    for candidate in (host, mdns):
        endpoint = _build_webhook_endpoint(candidate)
        if endpoint:
            endpoints.append(endpoint)

    if not endpoints:
        _LOGGER.debug(
            "Entry %s has no valid device endpoints after normalization",
            entry.entry_id,
        )
        return False
    _LOGGER.debug(
        "Entry %s will register webhook against endpoints: %s",
        entry.entry_id,
        ", ".join(str(ep) for ep in endpoints),
    )

    # Retry loop with exponential backoff
    for attempt in range(max_retries):
        try:
            # Add timeout protection
            async with asyncio.timeout(WEBHOOK_REGISTRATION_TIMEOUT):
                # Try each endpoint candidate until one succeeds
                for ep in endpoints:
                    try:
                        resp = await session.post(
                            str(ep),
                            json=payload,
                            headers={"Content-Type": "application/json"},
                        )

                        if resp.status < 300:
                            _LOGGER.info(
                                "WiCAN webhook registered successfully at %s (attempt %d/%d)",
                                ep,
                                attempt + 1,
                                max_retries,
                            )

                            # Cache the successful IP for future registrations
                            try:
                                # Extract IP from endpoint URL
                                endpoint_host = ep.host
                                if endpoint_host and not endpoint_host.endswith(".local"):
                                    entry.runtime_data.cached_resolved_ip = endpoint_host
                                    entry.runtime_data.cache_timestamp = time.time()
                                    _LOGGER.debug(
                                        "Cached resolved IP: %s",
                                        endpoint_host,
                                    )
                            except Exception as cache_err:
                                _LOGGER.debug(
                                    "Failed to cache IP: %s", cache_err,
                                )

                            # Clear any prior "cannot register" repair issue.
                            _clear_webhook_repair(hass, entry)
                            return True

                        text = await resp.text()
                        _LOGGER.warning(
                            "WiCAN webhook registration failed with HTTP %d at %s: %s (attempt %d/%d)",
                            resp.status,
                            ep,
                            text,
                            attempt + 1,
                            max_retries,
                        )
                    except ClientError as err:
                        # Keep trying other endpoints if one fails to resolve/connect
                        _LOGGER.warning(
                            "WiCAN webhook registration connection error at %s: %s (attempt %d/%d)",
                            ep,
                            err,
                            attempt + 1,
                            max_retries,
                        )

                # No endpoint succeeded during this attempt
                raise _WebhookEndpointsFailedError  # noqa: TRY301

        except TimeoutError:
            _LOGGER.warning(
                "WiCAN webhook registration timeout after %ds (attempt %d/%d)",
                WEBHOOK_REGISTRATION_TIMEOUT,
                attempt + 1,
                max_retries,
            )
        except _WebhookEndpointsFailedError:
            _LOGGER.debug(
                "All WiCAN endpoints failed for entry %s on attempt %d/%d",
                entry.entry_id,
                attempt + 1,
                max_retries,
            )
        except ClientResponseError as err:
            _LOGGER.warning(
                "WiCAN webhook registration HTTP error: %s (attempt %d/%d)",
                err,
                attempt + 1,
                max_retries,
            )
        except ClientError as err:
            _LOGGER.warning(
                "WiCAN webhook registration connection error: %s (attempt %d/%d)",
                err,
                attempt + 1,
                max_retries,
            )
        except Exception:
            _LOGGER.exception(
                "WiCAN webhook registration unexpected error (attempt %d/%d)",
                attempt + 1,
                max_retries,
            )

        # Exponential backoff before retry (except on last attempt)
        if attempt < max_retries - 1:
            backoff_seconds = WEBHOOK_RETRY_DELAY_BASE ** attempt  # 1s, 2s, 4s
            _LOGGER.debug("Retrying in %ds...", backoff_seconds)
            await asyncio.sleep(backoff_seconds)

    # All retries failed
    endpoints_str = ", ".join(str(ep) for ep in endpoints)
    _LOGGER.error(
        "Failed to register webhook after %d attempts. "
        "Device may not send updates to Home Assistant. "
        "Please check: 1) Device is powered on and connected to network, "
        "2) Home Assistant can reach device at %s, "
        "3) Device firewall allows connections on port 80",
        max_retries,
        endpoints_str,
    )
    _raise_webhook_repair(hass, entry, endpoints_str)
    return False


async def _async_entry_updated(hass: HomeAssistant, entry: WiCANConfigEntry) -> None:
    """Handle config entry updates (options) by re-registering the webhook."""
    # Update post_interval in runtime_data
    new_post_interval = entry.options.get(CONF_POST_INTERVAL, DEFAULT_POST_INTERVAL)
    entry.runtime_data.post_interval = new_post_interval

    # Re-register webhook with new interval (coalesced with any in-flight run)
    await _async_request_webhook_registration(hass, entry)
