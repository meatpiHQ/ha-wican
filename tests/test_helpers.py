"""Tests for helper functions (webhook URL resolution)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError

from custom_components.wican.helpers import resolve_device_webhook_urls


def test_resolve_device_webhook_urls_prefers_local_http_and_external_https(
    hass: HomeAssistant,
) -> None:
    """Test devices prefer local HTTP and append external HTTPS when available."""
    with (
        patch(
            "custom_components.wican.helpers.resolve_local_webhook_url",
            return_value="http://homeassistant.local:8123/api/webhook/test",
        ),
        patch(
            "custom_components.wican.helpers.resolve_external_https_webhook_url",
            return_value="https://example.ui.nabu.casa/api/webhook/test",
        ),
    ):
        urls = resolve_device_webhook_urls(
            hass,
            "test",
            allow_external_https_fallback=True,
        )

    assert urls == [
        "http://homeassistant.local:8123/api/webhook/test",
        "https://example.ui.nabu.casa/api/webhook/test",
    ]


def test_resolve_device_webhook_urls_falls_back_to_external_https(
    hass: HomeAssistant,
) -> None:
    """Test Pro devices can fall back to external HTTPS when local HTTP is unavailable."""
    with (
        patch(
            "custom_components.wican.helpers.resolve_local_webhook_url",
            side_effect=NoURLAvailableError,
        ),
        patch(
            "custom_components.wican.helpers.resolve_external_https_webhook_url",
            return_value="https://example.ui.nabu.casa/api/webhook/test",
        ),
    ):
        urls = resolve_device_webhook_urls(
            hass,
            "test",
            allow_external_https_fallback=True,
        )

    assert urls == ["https://example.ui.nabu.casa/api/webhook/test"]


def test_resolve_device_webhook_urls_requires_local_http_without_fallback(
    hass: HomeAssistant,
) -> None:
    """Test non-Pro devices still require a local HTTP webhook URL."""
    with patch(
        "custom_components.wican.helpers.resolve_local_webhook_url",
        side_effect=NoURLAvailableError,
    ):
        with pytest.raises(NoURLAvailableError):
            resolve_device_webhook_urls(hass, "test")


def test_ensure_allowed_scheme_rejects_malformed_url() -> None:
    """A malformed URL raises NoURLAvailableError (covers ValueError branch)."""
    from custom_components.wican.helpers import _ensure_allowed_webhook_scheme

    with pytest.raises(NoURLAvailableError):
        _ensure_allowed_webhook_scheme("http://[bad", allowed_schemes={"http"})


def test_ensure_allowed_scheme_rejects_disallowed_scheme() -> None:
    """A disallowed scheme raises NoURLAvailableError."""
    from custom_components.wican.helpers import _ensure_allowed_webhook_scheme

    with pytest.raises(NoURLAvailableError):
        _ensure_allowed_webhook_scheme("https://x/api", allowed_schemes={"http"})


def test_resolve_local_webhook_url_uses_current_request(hass: HomeAssistant) -> None:
    """Falls back to the active request host when the normal URL is unavailable."""
    from custom_components.wican.helpers import resolve_local_webhook_url

    with patch(
        "custom_components.wican.helpers.get_url",
        side_effect=[
            NoURLAvailableError,
            "http://192.168.1.10:8123",
        ],
    ):
        url = resolve_local_webhook_url(
            hass, "wid", require_current_request=True,
        )
    assert url.startswith("http://192.168.1.10:8123/api/webhook/")


def test_resolve_local_webhook_url_uses_fallback(hass: HomeAssistant) -> None:
    """Falls back to a stored HTTP fallback URL when no URL can be resolved."""
    from custom_components.wican.helpers import resolve_local_webhook_url

    with patch(
        "custom_components.wican.helpers.get_url",
        side_effect=NoURLAvailableError,
    ):
        url = resolve_local_webhook_url(
            hass,
            "wid",
            fallback_url="http://192.168.1.20:8123/api/webhook/wid",
        )
    assert url == "http://192.168.1.20:8123/api/webhook/wid"


def test_resolve_local_webhook_url_raises_when_nothing_available(
    hass: HomeAssistant,
) -> None:
    """Raises the original error when no URL, request, or fallback is available."""
    from custom_components.wican.helpers import resolve_local_webhook_url

    with patch(
        "custom_components.wican.helpers.get_url",
        side_effect=NoURLAvailableError,
    ):
        with pytest.raises(NoURLAvailableError):
            resolve_local_webhook_url(hass, "wid")


def test_resolve_local_webhook_url_current_request_and_fallback_fail(
    hass: HomeAssistant,
) -> None:
    """A disallowed fallback scheme is ignored and the original error re-raised."""
    from custom_components.wican.helpers import resolve_local_webhook_url

    with patch(
        "custom_components.wican.helpers.get_url",
        side_effect=NoURLAvailableError,
    ):
        with pytest.raises(NoURLAvailableError):
            resolve_local_webhook_url(
                hass,
                "wid",
                require_current_request=True,
                # https is not allowed for the local (http-only) resolver.
                fallback_url="https://example.com/api/webhook/wid",
            )


def test_resolve_external_webhook_url(hass: HomeAssistant) -> None:
    """resolve_external_webhook_url builds an http/https URL from the external base."""
    from custom_components.wican.helpers import resolve_external_webhook_url

    with patch(
        "custom_components.wican.helpers.get_url",
        return_value="https://example.ui.nabu.casa",
    ):
        url = resolve_external_webhook_url(hass, "wid")
    assert url.startswith("https://example.ui.nabu.casa/api/webhook/")


def test_resolve_device_webhook_urls_external_https_unavailable(
    hass: HomeAssistant,
) -> None:
    """Local HTTP is returned even when the external HTTPS lookup fails."""
    with (
        patch(
            "custom_components.wican.helpers.resolve_local_webhook_url",
            return_value="http://homeassistant.local:8123/api/webhook/test",
        ),
        patch(
            "custom_components.wican.helpers.resolve_external_https_webhook_url",
            side_effect=NoURLAvailableError,
        ),
    ):
        urls = resolve_device_webhook_urls(
            hass, "test", allow_external_https_fallback=True,
        )
    assert urls == ["http://homeassistant.local:8123/api/webhook/test"]


def test_resolve_device_webhook_urls_raises_when_all_unavailable(
    hass: HomeAssistant,
) -> None:
    """With fallback allowed but nothing resolvable, the local error is raised."""
    with (
        patch(
            "custom_components.wican.helpers.resolve_local_webhook_url",
            side_effect=NoURLAvailableError,
        ),
        patch(
            "custom_components.wican.helpers.resolve_external_https_webhook_url",
            side_effect=NoURLAvailableError,
        ),
    ):
        with pytest.raises(NoURLAvailableError):
            resolve_device_webhook_urls(
                hass, "test", allow_external_https_fallback=True,
            )
