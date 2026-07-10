"""Client for the HTTP API of MeatPi devices (WiCAN firmware V6+).

Telemetry always arrives via webhook push (the only channel legacy firmware
has). This client is the *control and discovery* channel: it probes whether
the device exposes the V6 ``/api/*`` surface, learns which firmware
components are present (the capability set), and executes control actions
such as reboot or time sync.

The probe is deliberately forgiving: any failure yields legacy capabilities,
never an exception, so a pre-V6 device — or an unreachable one — simply gets
no control entities and everything else behaves exactly as before.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError
from yarl import URL

from .const import (
    API_LEVEL_LEGACY,
    API_LEVEL_V6,
    API_PROBE_TIMEOUT,
    API_REQUEST_TIMEOUT,
    API_RESTART_PATH,
    API_RTC_SYNC_PATH,
    API_SETTINGS_PATH,
    API_STATUS_PATH,
    MAX_API_COMPONENT_NAME_LENGTH,
    MAX_API_COMPONENTS,
    MAX_API_RESPONSE_BYTES,
)
from .exceptions import MeatPiApiConnectionError, MeatPiApiError

if TYPE_CHECKING:
    from aiohttp import ClientSession

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeviceCapabilities:
    """What a running device can do, learned from the capability probe."""

    api_level: int = API_LEVEL_LEGACY
    components: frozenset[str] = field(default_factory=frozenset)

    @property
    def has_http_api(self) -> bool:
        """Return True when the device exposes the V6 control API."""
        return self.api_level >= API_LEVEL_V6

    def has_component(self, name: str) -> bool:
        """Return True when the named firmware component is present."""
        return name in self.components


LEGACY_CAPABILITIES = DeviceCapabilities()


class MeatPiApiClient:
    """Minimal async client for the MeatPi device HTTP API.

    Uses the injected shared Home Assistant aiohttp session (never creates
    its own) and raises :class:`MeatPiApiError` for any transport or HTTP
    failure so callers have exactly one error surface to handle.
    """

    def __init__(self, session: ClientSession, base_url: str) -> None:
        """Initialize the client against a device base URL (http://host[:port])."""
        self._session = session
        self._base = URL(base_url)

    @property
    def base_url(self) -> str:
        """Return the device base URL this client talks to."""
        return str(self._base)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        request_timeout: float = API_REQUEST_TIMEOUT,
    ) -> Any:
        """Perform one API request and return the decoded JSON body (or None)."""
        url = self._base.with_path(path)
        try:
            async with asyncio.timeout(request_timeout):
                response = await self._session.request(method, url)
                # A glitching or hostile device must not be able to balloon
                # memory with a giant body (chunked bodies without a length
                # are still bounded by the request timeout).
                declared_length = response.headers.get("Content-Length")
                if (
                    declared_length
                    and declared_length.isdigit()
                    and int(declared_length) > MAX_API_RESPONSE_BYTES
                ):
                    raise MeatPiApiError(
                        f"Device API {method} {path} response too large "
                        f"({declared_length} bytes)",
                    )
                if response.status >= 400:
                    body = await response.text()
                    raise MeatPiApiError(
                        f"Device API {method} {path} failed with "
                        f"HTTP {response.status}: {body[:200]}",
                    )
                # Parse tolerantly instead of trusting the Content-Type
                # header; a non-JSON body (e.g. a legacy web UI answering
                # the probe) simply yields None.
                try:
                    return await response.json(content_type=None)
                except (ValueError, UnicodeDecodeError):
                    return None
        except TimeoutError as err:
            raise MeatPiApiConnectionError(
                f"Device API {method} {path} timed out after {request_timeout}s",
            ) from err
        except (ClientError, ValueError, UnicodeDecodeError) as err:
            raise MeatPiApiConnectionError(
                f"Device API {method} {path} failed: {err}",
            ) from err

    async def async_probe(self) -> DeviceCapabilities | None:
        """Probe the device for its API level and firmware components.

        A V6 device is recognized by the ``bits`` object in ``GET
        /api/status``; the component list then comes from ``GET
        /api/settings``.

        Returns None when the device could not be reached at all (asleep,
        offline, mid-reboot) — the caller keeps its last-known capabilities.
        A reachable device that answers anything other than the V6 shape
        (HTTP error, HTML page, legacy JSON) is a *positive* legacy signal
        and yields legacy capabilities. Never raises.
        """
        try:
            status = await self._request(
                "GET", API_STATUS_PATH, request_timeout=API_PROBE_TIMEOUT,
            )
        except MeatPiApiConnectionError as err:
            _LOGGER.debug(
                "Capability probe of %s: device unreachable (%s)", self._base, err,
            )
            return None
        except MeatPiApiError as err:
            _LOGGER.debug("Capability probe of %s: no HTTP API (%s)", self._base, err)
            return LEGACY_CAPABILITIES

        if not isinstance(status, dict) or not isinstance(status.get("bits"), dict):
            _LOGGER.debug(
                "Capability probe of %s: /api/status has no V6 shape", self._base,
            )
            return LEGACY_CAPABILITIES

        components: frozenset[str] = frozenset()
        try:
            settings = await self._request(
                "GET", API_SETTINGS_PATH, request_timeout=API_PROBE_TIMEOUT,
            )
        except MeatPiApiConnectionError as err:
            # The device flaked out between the two probe requests; report
            # unreachable so the caller keeps its last-known capabilities
            # instead of downgrading to "V6 with no components".
            _LOGGER.debug(
                "Capability probe of %s: device lost mid-probe (%s)",
                self._base,
                err,
            )
            return None
        except MeatPiApiError as err:
            # Still a V6 device; component-gated entities just stay absent.
            _LOGGER.debug(
                "Capability probe of %s: component list unavailable (%s)",
                self._base,
                err,
            )
        else:
            components = self._parse_components(settings)

        capabilities = DeviceCapabilities(
            api_level=API_LEVEL_V6, components=components,
        )
        _LOGGER.debug(
            "Capability probe of %s: V6 API with %d components",
            self._base,
            len(components),
        )
        return capabilities

    def _parse_components(self, settings: Any) -> frozenset[str]:
        """Extract the firmware component names from a /api/settings body.

        Bounds what a glitching device can put in the capability set: caps
        the count and skips absurd or non-string names.
        """
        raw_components = (
            settings.get("components") if isinstance(settings, dict) else None
        )
        if not isinstance(raw_components, list):
            return frozenset()
        names: set[str] = set()
        for component in raw_components:
            if len(names) >= MAX_API_COMPONENTS:
                _LOGGER.debug(
                    "Capability probe of %s: component list capped at %d entries",
                    self._base,
                    MAX_API_COMPONENTS,
                )
                break
            if not isinstance(component, dict):
                continue
            name = component.get("name")
            if (
                isinstance(name, str)
                and name
                and len(name) <= MAX_API_COMPONENT_NAME_LENGTH
            ):
                names.add(name)
        return frozenset(names)

    async def async_restart(self) -> None:
        """Reboot the device (POST /api/restart)."""
        await self._request("POST", API_RESTART_PATH)

    async def async_sync_time(self) -> None:
        """Trigger an on-demand SNTP sync (POST /api/rtc/sync).

        The firmware blocks for up to ~32 s in the worst case, hence the
        generous request timeout.
        """
        await self._request("POST", API_RTC_SYNC_PATH)
