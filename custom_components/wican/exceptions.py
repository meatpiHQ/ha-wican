"""Exceptions for WiCAN integration."""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError


class WiCANError(HomeAssistantError):
    """Base exception for WiCAN integration."""


class WiCANConnectionError(WiCANError):
    """Exception raised when connection to device fails."""


class WiCANDeviceNotFoundError(WiCANError):
    """Exception raised when device is not reachable."""


class WiCANWebhookError(WiCANError):
    """Exception raised for webhook-related errors."""


class WiCANDataError(WiCANError):
    """Exception raised when data from device is invalid or malformed."""


class MeatPiApiError(WiCANError):
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


class WiCANFirmwareError(WiCANError):
    """Base exception for firmware update errors."""


class FirmwareDownloadError(WiCANFirmwareError):
    """Raised when firmware download from GitHub fails."""


class FirmwareUploadError(WiCANFirmwareError):
    """Raised when firmware upload to device fails."""


class FirmwareVersionNotFoundError(WiCANFirmwareError):
    """Raised when requested firmware version doesn't exist."""
