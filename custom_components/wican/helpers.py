"""Helper functions and decorators for WiCAN integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components import webhook
from homeassistant.helpers.network import NoURLAvailableError, get_url
from yarl import URL

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Chunk size for capped body reads; small enough that the cap overshoots
# by at most one chunk, large enough not to slow normal bodies down.
_READ_CHUNK_BYTES = 64 * 1024


async def async_read_capped(response: Any, max_bytes: int) -> bytes | None:
    """Read an HTTP response body, refusing anything past ``max_bytes``.

    A declared Content-Length above the cap is refused before any read;
    the body is then streamed chunk by chunk so a chunked response (no
    length header) cannot balloon memory before a post-read length check —
    the read stops at the first chunk past the cap. Returns None when the
    body is (or would grow) too large.
    """
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        response.close()
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(_READ_CHUNK_BYTES):
        total += len(chunk)
        if total > max_bytes:
            # The rest of the body is deliberately unread: drop the
            # connection rather than leave it half-consumed in the pool.
            response.close()
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def build_webhook_url(base_url: str, webhook_id: str) -> str:
    """Build an absolute webhook URL from a base URL and webhook id."""
    return str(URL(base_url) / webhook.async_generate_path(webhook_id).lstrip("/"))


def _ensure_allowed_webhook_scheme(url: str, *, allowed_schemes: set[str]) -> str:
    """Validate webhook URL scheme against an allowed set."""
    try:
        parsed = URL(url)
    except ValueError as err:
        raise NoURLAvailableError from err

    if parsed.scheme not in allowed_schemes:
        raise NoURLAvailableError

    return str(parsed)


def resolve_local_webhook_url(
    hass: HomeAssistant,
    webhook_id: str,
    *,
    fallback_url: str | None = None,
    require_current_request: bool = False,
) -> str:
    """Resolve a local-only HTTP webhook URL for devices that cannot use HTTPS."""
    try:
        local_base = get_url(
            hass,
            allow_cloud=False,
            allow_ip=True,
            prefer_external=False,
        )
        return _ensure_allowed_webhook_scheme(
            build_webhook_url(local_base, webhook_id),
            allowed_schemes={"http"},
        )
    except NoURLAvailableError:
        if require_current_request:
            try:
                current_request_base = get_url(
                    hass,
                    require_current_request=True,
                    allow_cloud=False,
                    allow_ip=True,
                    prefer_external=False,
                )
                return _ensure_allowed_webhook_scheme(
                    build_webhook_url(current_request_base, webhook_id),
                    allowed_schemes={"http"},
                )
            except NoURLAvailableError:
                pass

        if fallback_url:
            try:
                return _ensure_allowed_webhook_scheme(
                    fallback_url,
                    allowed_schemes={"http"},
                )
            except NoURLAvailableError:
                pass

        raise


def resolve_external_webhook_url(hass: HomeAssistant, webhook_id: str) -> str:
    """Resolve the preferred external webhook URL for cloud or remote access."""
    external_base = get_url(
        hass,
        allow_ip=False,
        prefer_external=True,
    )
    return _ensure_allowed_webhook_scheme(
        build_webhook_url(external_base, webhook_id),
        allowed_schemes={"http", "https"},
    )


def resolve_external_https_webhook_url(hass: HomeAssistant, webhook_id: str) -> str:
    """Resolve the preferred external HTTPS webhook URL for remote access."""
    external_base = get_url(
        hass,
        allow_ip=False,
        prefer_external=True,
    )
    return _ensure_allowed_webhook_scheme(
        build_webhook_url(external_base, webhook_id),
        allowed_schemes={"https"},
    )


def resolve_device_webhook_urls(
    hass: HomeAssistant,
    webhook_id: str,
    *,
    fallback_url: str | None = None,
    require_current_request: bool = False,
    allow_external_https_fallback: bool = False,
) -> list[str]:
    """Resolve ordered webhook URLs to send to a device.

    Non-Pro devices require a local HTTP webhook URL. Pro devices can also use
    an external HTTPS URL, and may fall back to it when no local HTTP URL is
    available.
    """
    local_url: str | None = None
    local_error: NoURLAvailableError | None = None

    try:
        local_url = resolve_local_webhook_url(
            hass,
            webhook_id,
            fallback_url=fallback_url,
            require_current_request=require_current_request,
        )
    except NoURLAvailableError as err:
        local_error = err
        if not allow_external_https_fallback:
            raise

    external_url: str | None = None
    if allow_external_https_fallback:
        try:
            external_url = resolve_external_https_webhook_url(hass, webhook_id)
        except NoURLAvailableError:
            external_url = None

    urls: list[str] = []
    if local_url:
        urls.append(local_url)
    if external_url and external_url != local_url:
        urls.append(external_url)

    if urls:
        return urls

    if local_error is not None:
        raise local_error

    raise NoURLAvailableError


def resolve_webhook_url(
    hass: HomeAssistant,
    webhook_id: str,
    *,
    fallback_url: str | None = None,
    require_current_request: bool = False,
) -> str:
    """Resolve the best available absolute webhook URL.

    Prefer Home Assistant's normal URL resolution with IPs allowed and internal
    addresses favored, then optionally fall back to the active request host.
    Finally, reuse a previously stored webhook URL if one exists.
    """
    return resolve_local_webhook_url(
        hass,
        webhook_id,
        fallback_url=fallback_url,
        require_current_request=require_current_request,
    )
