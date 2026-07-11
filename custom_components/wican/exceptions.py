"""Exceptions for WiCAN integration."""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError


class MeatPiError(HomeAssistantError):
    """Base exception for WiCAN integration."""


class MeatPiConnectionError(MeatPiError):
    """Exception raised when connection to device fails."""


class MeatPiDeviceNotFoundError(MeatPiError):
    """Exception raised when device is not reachable."""


class MeatPiWebhookError(MeatPiError):
    """Exception raised for webhook-related errors."""


class MeatPiDataError(MeatPiError):
    """Exception raised when data from device is invalid or malformed."""


class MeatPiApiError(MeatPiError):
    """Exception raised when a device HTTP API request fails.

    ``status`` carries the HTTP status code when the failure was an
    HTTP-level error from a reachable device (None for transport errors),
    so callers can react to specific codes (e.g. 404 = route not
    implemented by this firmware).
    """

    def __init__(self, *args: object, status: int | None = None) -> None:
        """Initialize the error, optionally recording the HTTP status."""
        super().__init__(*args)
        self.status = status


class MeatPiApiConnectionError(MeatPiApiError):
    """The device could not be reached at all (transport error or timeout).

    Distinguished from plain MeatPiApiError (an HTTP-level failure from a
    reachable device) so the capability probe can keep last-known-good
    capabilities when a device is merely asleep or offline.
    """


class MeatPiFirmwareError(MeatPiError):
    """Base exception for firmware update errors."""


class FirmwareDownloadError(MeatPiFirmwareError):
    """Raised when firmware download from GitHub fails."""


class FirmwareUploadError(MeatPiFirmwareError):
    """Raised when firmware upload to device fails."""


class FirmwareVersionNotFoundError(MeatPiFirmwareError):
    """Raised when requested firmware version doesn't exist."""
