"""GitHub Releases coordinator for WiCAN firmware updates."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    GITHUB_API_RELEASES_URL,
    GITHUB_API_TIMEOUT,
    GITHUB_OWNER,
    GITHUB_RELEASES_UPDATE_INTERVAL,
    GITHUB_REPO,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(seconds=GITHUB_RELEASES_UPDATE_INTERVAL)

_SHARED_FETCH_KEY = f"{DOMAIN}_releases_shared_fetch"

# WiCAN firmware ships in three hardware streams, distinguished by the
# version-tag suffix letter: v4.45p (PRO), v4.13u (USB), v4.46 (OBD).
STREAM_PRO = "pro"
STREAM_USB = "usb"
STREAM_OBD = "obd"


def hardware_stream(hw_version: Any) -> str:
    """Classify a device's hardware version string into a firmware stream."""
    hw = str(hw_version or "").lower()
    if "pro" in hw:
        return STREAM_PRO
    if "usb" in hw:
        return STREAM_USB
    return STREAM_OBD


def release_stream(release: dict[str, Any]) -> str:
    """Classify a GitHub release into a firmware stream.

    The tag suffix letter is authoritative (v4.45p / v4.13u / v4.46);
    the release name is the fallback. A bare substring match on "p"
    would misclassify tags like v4.50-patch1 as PRO.
    """
    tag = str(release.get("tag_name") or "").strip().lower()
    tag = tag.removeprefix("v")
    if tag.endswith("p"):
        return STREAM_PRO
    if tag.endswith("u"):
        return STREAM_USB
    name = str(release.get("name") or "").lower()
    if "pro" in name:
        return STREAM_PRO
    if "usb" in name:
        return STREAM_USB
    return STREAM_OBD


def _has_bin_asset(assets: list[Any], stream: str) -> bool:
    """True when the asset list carries a firmware .bin for the stream.

    Mirrors the device firmware naming: wican-fw_obd_pro_vXXXp.bin (PRO),
    wican-fw_usb_vXXXu.bin (USB), wican-fw_obd_vXXX.bin (OBD).
    """
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "").lower()
        if not name.endswith(".bin"):
            continue
        has_pro = "pro" in name
        has_usb = "usb" in name and not has_pro
        has_obd = "obd" in name and not has_usb and not has_pro
        if (
            (stream == STREAM_PRO and has_pro)
            or (stream == STREAM_USB and has_usb)
            or (stream == STREAM_OBD and has_obd)
        ):
            return True
    return False


def release_matches_stream(release: dict[str, Any], stream: str) -> bool:
    """True when a release can serve a device of the given stream.

    PRO firmware ships in dedicated releases (tag suffix p / PRO name).
    OBD and USB devices share the standard releases: a release like
    v4.13 carries both an OBD and a USB .bin, so their eligibility is
    decided by the assets, falling back to the tag/name stream when the
    release lists none. Offering a device a release with no installable
    asset would surface a permanently uninstallable update.
    """
    if stream == STREAM_PRO:
        return release_stream(release) == STREAM_PRO
    if release_stream(release) == STREAM_PRO:
        return False
    assets = release.get("assets")
    if isinstance(assets, list) and assets:
        return _has_bin_asset(assets, stream)
    return release_stream(release) == stream


class GitHubReleasesCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to fetch GitHub releases."""

    def __init__(self, hass: HomeAssistant, stream: str = STREAM_OBD) -> None:
        """Initialize the coordinator.

        Args:
            hass: Home Assistant instance.
            stream: The device's firmware stream ("pro", "usb", or "obd").

        """
        super().__init__(
            hass,
            _LOGGER,
            name="MeatPi firmware releases",
            update_interval=UPDATE_INTERVAL,
        )
        self._stream = stream

    def set_stream(self, stream: str) -> bool:
        """Switch firmware stream; returns True when it changed.

        A manually added entry has no hw_version until the device's first
        push — the caller re-derives the stream when it learns it and
        requests a refresh so the right release is offered without a
        reload.
        """
        if stream == self._stream:
            return False
        self._stream = stream
        return True

    async def _async_update_data(self) -> dict[str, Any]:
        """Return the latest installable release for this device's stream."""
        releases = await _get_shared_fetch(self.hass).async_get(self.hass)

        # Filter for latest non-prerelease
        stable_releases = [
            r
            for r in releases
            if isinstance(r, dict) and not r.get("prerelease", False)
        ]

        if not stable_releases:
            _LOGGER.warning("No stable releases found")
            return {}

        # Keep only the releases this device can actually install
        # (PRO / USB / OBD) so e.g. a USB device is never offered a
        # newer OBD-only release — which has no USB asset to install.
        device_specific_releases = [
            r
            for r in stable_releases
            if release_matches_stream(r, self._stream)
        ]

        if not device_specific_releases:
            _LOGGER.warning(
                "No stable %s releases found (found %d releases for other streams)",
                self._stream.upper(),
                len(stable_releases),
            )
            return {}

        latest = device_specific_releases[0]
        _LOGGER.debug(
            "Latest %s release: %s",
            self._stream.upper(),
            latest.get("tag_name"),
        )
        return latest


async def _async_fetch_releases(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Fetch the raw release list from GitHub. Raises UpdateFailed."""
    url = GITHUB_API_RELEASES_URL.format(owner=GITHUB_OWNER, repo=GITHUB_REPO)
    session = async_get_clientsession(hass)
    try:
        async with asyncio.timeout(GITHUB_API_TIMEOUT):
            response = await session.get(
                url,
                headers={"Accept": "application/vnd.github.v3+json"},
            )
            response.raise_for_status()
            releases = await response.json()
    except TimeoutError as err:
        raise UpdateFailed("Timeout fetching GitHub releases") from err
    except aiohttp.ClientError as err:
        raise UpdateFailed(f"Error fetching GitHub releases: {err}") from err
    except (ValueError, KeyError) as err:
        raise UpdateFailed(f"Invalid response from GitHub: {err}") from err

    # A proxy or captive portal can return 200 with a non-list body;
    # treat any unexpected shape as a failed update.
    if not isinstance(releases, list):
        raise UpdateFailed(
            f"Unexpected GitHub response shape: {type(releases).__name__}",
        )
    return cast("list[dict[str, Any]]", releases)


class _SharedReleasesFetch:
    """One GitHub fetch serving every config entry.

    Each entry's coordinator polls hourly; without sharing, N devices
    multiply against GitHub's 60/hour unauthenticated per-IP limit
    (which the params and catalog fetches also consume). The first
    coordinator to poll after expiry refreshes the cache; the rest are
    served from it. Failures are not cached, so a coordinator retries
    the real fetch on its next poll.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._releases: list[dict[str, Any]] | None = None
        self._fetched_at = 0.0

    async def async_get(self, hass: HomeAssistant) -> list[dict[str, Any]]:
        async with self._lock:
            now = time.monotonic()
            if (
                self._releases is not None
                and now - self._fetched_at < GITHUB_RELEASES_UPDATE_INTERVAL
            ):
                return self._releases
            releases = await _async_fetch_releases(hass)
            self._releases = releases
            self._fetched_at = time.monotonic()
            return releases


def _get_shared_fetch(hass: HomeAssistant) -> _SharedReleasesFetch:
    shared = hass.data.get(_SHARED_FETCH_KEY)
    if shared is None:
        shared = hass.data[_SHARED_FETCH_KEY] = _SharedReleasesFetch()
    return cast("_SharedReleasesFetch", shared)
