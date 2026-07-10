# WiCAN Firmware Update Implementation Plan

> **HISTORICAL RECORD** — dated snapshot kept for reference; statistics and
> structure are superseded. Current documentation: `notes/README.md`.

## Overview
Add HomeAssistant `UpdateEntity` platform to enable OTA firmware updates directly from the HA UI, following core HA integration patterns (similar to WLED, ESPHome).

---

## 1. Requirements Summary

### Core Functionality
- **Update Entity**: `UpdateEntity` with `device_class=UpdateDeviceClass.FIRMWARE`
- **Version Tracking**:
  - `installed_version`: From device info (`fw_version` field in webhook data)
  - `latest_version`: From GitHub Releases API
- **Install Support**: 
  - `EntityFeature.INSTALL` for latest version
  - `EntityFeature.SPECIFIC_VERSION` to allow rollback/specific version selection
- **Update Flow**:
  1. Download firmware binary from GitHub Releases
  2. Upload to device OTA endpoint via multipart/form-data POST
  3. Refresh coordinator data after update initiated

### Technical Requirements
- **Async I/O**: All network operations must be non-blocking
- **Concurrency**: `PARALLEL_UPDATES = 1` (prevent multiple simultaneous updates)
- **Error Handling**: Map exceptions to `HomeAssistantError` with translations
- **Timeout**: Use proper timeouts for GitHub & device connections
- **Progress**: Optional progress reporting during download/upload

---

## 2. WiCAN-Specific Details

### Device Information
- **Domain**: `wican`
- **GitHub Repository**: `meatpiHQ/wican-fw`
- **Releases**: https://github.com/meatpiHQ/wican-fw/releases
- **Current Version Field**: `fw_version` (from webhook `status` dict)
- **Device Models**: Multiple hardware variants (WiCAN, WiCAN-PRO, WiCAN-USB)

### Firmware Binary Naming Convention
Based on GitHub releases analysis:
- **URL Pattern**: `https://github.com/meatpiHQ/wican-fw/releases/download/v{version}/{filename}`
- **Filename Logic**:
  ```
  Standard WiCAN:     wican_v{version}.bin
  WiCAN-PRO:         wican_v{version}p.bin  (note the 'p' suffix for PRO)
  ```
- **Version Format**: `v4.45` (standard), `v4.45p` (PRO)
- **Detection**: Use `hw_version` field from device info to determine model

### OTA Endpoint
- **Path**: `/update` (standard ESP32 OTA endpoint)
- **Method**: `POST`
- **Content-Type**: `multipart/form-data`
- **Form Field**: `update` or `file` (need to verify from ESP32 OTA handler)
- **Full URL**: `http://{device_host}/update`

### Device Connection Details
- **Host Discovery**: Use `entry.runtime_data.device_host` or `entry.data.get("host")`
- **Fallback**: Use `entry.runtime_data.device_ip` if host not available
- **mDNS**: Device may use mDNS hostname (stored in `entry.data.get("mdns")`)

---

## 3. File Structure & Changes

### New Files

#### `custom_components/wican/update.py`
Complete update entity platform implementation.

**Key Components**:
- `WiCANUpdateEntity(WiCANEntity, UpdateEntity)`
- `async_install(self, version: str | None, backup: bool, **kwargs)`
- `_download_firmware(version: str) -> bytes`
- `_upload_firmware_to_device(firmware: bytes) -> None`
- `_get_firmware_filename(version: str) -> str`
- Version comparison logic

#### `custom_components/wican/github_releases.py`
GitHub API client for fetching releases.

**Key Components**:
- `GitHubReleasesCoordinator(DataUpdateCoordinator)`
- `async def fetch_latest_release(owner: str, repo: str) -> dict`
- Release filtering (stable vs prerelease)
- Caching with 1-hour refresh interval

### Modified Files

#### `custom_components/wican/__init__.py`
```python
# Add Platform.UPDATE to PLATFORMS list
PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.DEVICE_TRACKER,
    Platform.UPDATE,  # NEW
]
```

#### `custom_components/wican/manifest.json`
```json
{
  "requirements": ["aiohttp>=3.8.0"],
  "version": "0.5.0"
}
```

#### `custom_components/wican/const.py`
```python
# GitHub API
GITHUB_API_RELEASES_URL = "https://api.github.com/repos/{owner}/{repo}/releases"
GITHUB_OWNER = "meatpiHQ"
GITHUB_REPO = "wican-fw"
GITHUB_RELEASE_URL_TEMPLATE = "https://github.com/{owner}/{repo}/releases/download/v{version}/{filename}"

# Firmware Update
FIRMWARE_UPDATE_TIMEOUT = 300  # 5 minutes for large firmware files
FIRMWARE_DOWNLOAD_TIMEOUT = 120  # 2 minutes to download from GitHub
FIRMWARE_UPLOAD_TIMEOUT = 180  # 3 minutes to upload to device
OTA_ENDPOINT = "/update"
OTA_FORM_FIELD = "update"  # or "file" - to be verified

# Update Coordinator
GITHUB_RELEASES_UPDATE_INTERVAL = 3600  # 1 hour
```

#### `custom_components/wican/strings.json`
Add translations for update entity and errors:
```json
{
  "entity": {
    "update": {
      "firmware": {
        "name": "Firmware"
      }
    }
  },
  "exceptions": {
    "firmware_download_failed": {
      "message": "Failed to download firmware from GitHub: {error}"
    },
    "firmware_upload_failed": {
      "message": "Failed to upload firmware to device: {error}"
    },
    "version_not_found": {
      "message": "Firmware version {version} not found in GitHub releases"
    },
    "device_unreachable": {
      "message": "Cannot reach device at {host} for firmware update"
    },
    "update_in_progress": {
      "message": "A firmware update is already in progress"
    }
  }
}
```

#### `custom_components/wican/exceptions.py`
```python
class WiCANFirmwareError(WiCANError):
    """Base exception for firmware update errors."""

class FirmwareDownloadError(WiCANFirmwareError):
    """Raised when firmware download from GitHub fails."""

class FirmwareUploadError(WiCANFirmwareError):
    """Raised when firmware upload to device fails."""

class FirmwareVersionNotFoundError(WiCANFirmwareError):
    """Raised when requested firmware version doesn't exist."""
```

#### `custom_components/wican/models.py`
```python
@dataclass
class WiCANRuntimeData:
    """Runtime data for WiCAN integration."""
    coordinator: WiCANDataUpdateCoordinator
    github_coordinator: GitHubReleasesCoordinator  # NEW
    webhook_id: str
    device_host: str | None = None
    device_ip: str | None = None
```

---

## 4. Implementation Details

### 4.1 Update Entity (`update.py`)

```python
"""Update platform for WiCAN integration."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

import aiohttp
import async_timeout

from homeassistant.components.update import (
    UpdateEntity,
    UpdateEntityFeature,
    UpdateDeviceClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WiCANConfigEntry
from .const import (
    DOMAIN,
    GITHUB_OWNER,
    GITHUB_REPO,
    GITHUB_RELEASE_URL_TEMPLATE,
    FIRMWARE_DOWNLOAD_TIMEOUT,
    FIRMWARE_UPLOAD_TIMEOUT,
    OTA_ENDPOINT,
    OTA_FORM_FIELD,
)
from .entity import WiCANEntity
from .exceptions import (
    FirmwareDownloadError,
    FirmwareUploadError,
    FirmwareVersionNotFoundError,
)

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1  # Only one update at a time


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WiCANConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up WiCAN update entity."""
    async_add_entities([WiCANUpdateEntity(entry)])


class WiCANUpdateEntity(WiCANEntity, UpdateEntity):
    """Representation of WiCAN firmware update entity."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.SPECIFIC_VERSION
        | UpdateEntityFeature.PROGRESS
    )

    def __init__(self, config_entry: WiCANConfigEntry) -> None:
        """Initialize the update entity."""
        # Create entity description for parent class
        from homeassistant.helpers.entity import EntityDescription
        entity_description = EntityDescription(
            key="firmware",
            name="Firmware",
        )
        super().__init__(config_entry, entity_description)
        self._attr_title = "WiCAN Firmware"
        self._github_coordinator = config_entry.runtime_data.github_coordinator
        self._update_in_progress = False

    @property
    def installed_version(self) -> str | None:
        """Return the installed firmware version."""
        # Get from coordinator data (from webhook status)
        fw_version = self.coordinator.data.get("status", {}).get("fw_version")
        if not fw_version:
            # Fallback to config entry data
            fw_version = self.config_entry.data.get("fw_version")
        return fw_version

    @property
    def latest_version(self) -> str | None:
        """Return the latest firmware version from GitHub."""
        if not self._github_coordinator.data:
            return None
        return self._github_coordinator.data.get("tag_name", "").lstrip("v")

    @property
    def release_url(self) -> str | None:
        """Return the URL for release notes."""
        if not self._github_coordinator.data:
            return None
        return self._github_coordinator.data.get("html_url")

    @property
    def release_summary(self) -> str | None:
        """Return the release notes summary."""
        if not self._github_coordinator.data:
            return None
        body = self._github_coordinator.data.get("body", "")
        # Truncate to first 500 chars
        return body[:500] + "..." if len(body) > 500 else body

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Install firmware update."""
        if self._update_in_progress:
            raise HomeAssistantError("update_in_progress")

        try:
            self._update_in_progress = True
            self._attr_in_progress = True
            self.async_write_ha_state()

            # Resolve version (use latest if not specified)
            target_version = version or self.latest_version
            if not target_version:
                raise HomeAssistantError("No firmware version available")

            _LOGGER.info("Starting firmware update to version %s", target_version)

            # Step 1: Download firmware from GitHub (50% of progress)
            self._attr_in_progress = 50
            self.async_write_ha_state()
            firmware_data = await self._download_firmware(target_version)

            # Step 2: Upload firmware to device (50% to 100%)
            self._attr_in_progress = 75
            self.async_write_ha_state()
            await self._upload_firmware_to_device(firmware_data)

            _LOGGER.info("Firmware update initiated successfully")

            # Update complete - device will reboot
            self._attr_in_progress = 100
            self.async_write_ha_state()

            # Wait a bit for device to start updating, then refresh
            await asyncio.sleep(2)
            await self.coordinator.async_request_refresh()

        except (FirmwareDownloadError, FirmwareUploadError) as err:
            _LOGGER.error("Firmware update failed: %s", err)
            raise HomeAssistantError(str(err)) from err
        finally:
            self._update_in_progress = False
            self._attr_in_progress = False
            self.async_write_ha_state()

    async def _download_firmware(self, version: str) -> bytes:
        """Download firmware binary from GitHub."""
        filename = self._get_firmware_filename(version)
        url = GITHUB_RELEASE_URL_TEMPLATE.format(
            owner=GITHUB_OWNER,
            repo=GITHUB_REPO,
            version=version,
            filename=filename,
        )

        _LOGGER.debug("Downloading firmware from %s", url)

        session = async_get_clientsession(self.hass)
        try:
            async with async_timeout.timeout(FIRMWARE_DOWNLOAD_TIMEOUT):
                response = await session.get(url)
                response.raise_for_status()
                firmware_data = await response.read()

            _LOGGER.debug("Downloaded %d bytes", len(firmware_data))
            return firmware_data

        except aiohttp.ClientResponseError as err:
            if err.status == 404:
                raise FirmwareVersionNotFoundError(
                    f"Firmware version {version} not found"
                ) from err
            raise FirmwareDownloadError(
                f"Failed to download firmware: HTTP {err.status}"
            ) from err
        except asyncio.TimeoutError as err:
            raise FirmwareDownloadError(
                "Timeout downloading firmware from GitHub"
            ) from err
        except aiohttp.ClientError as err:
            raise FirmwareDownloadError(
                f"Network error downloading firmware: {err}"
            ) from err

    async def _upload_firmware_to_device(self, firmware_data: bytes) -> None:
        """Upload firmware to device OTA endpoint."""
        # Get device connection info
        device_host = (
            self.config_entry.runtime_data.device_host
            or self.config_entry.data.get("host")
            or self.config_entry.data.get("mdns")
        )

        if not device_host:
            # Fallback to IP
            device_ip = self.config_entry.runtime_data.device_ip
            if device_ip:
                device_host = f"http://{device_ip}"

        if not device_host:
            raise FirmwareUploadError("Cannot determine device address")

        # Ensure http:// prefix
        if not device_host.startswith(("http://", "https://")):
            device_host = f"http://{device_host}"

        url = f"{device_host.rstrip('/')}{OTA_ENDPOINT}"
        _LOGGER.debug("Uploading firmware to %s", url)

        # Create multipart form data
        form_data = aiohttp.FormData()
        form_data.add_field(
            OTA_FORM_FIELD,
            firmware_data,
            filename="firmware.bin",
            content_type="application/octet-stream",
        )

        session = async_get_clientsession(self.hass)
        try:
            async with async_timeout.timeout(FIRMWARE_UPLOAD_TIMEOUT):
                response = await session.post(url, data=form_data)
                response.raise_for_status()

            _LOGGER.info("Firmware uploaded successfully to device")

        except asyncio.TimeoutError as err:
            raise FirmwareUploadError(
                "Timeout uploading firmware to device"
            ) from err
        except aiohttp.ClientError as err:
            raise FirmwareUploadError(
                f"Failed to upload firmware to device: {err}"
            ) from err

    def _get_firmware_filename(self, version: str) -> str:
        """Determine firmware filename based on device model."""
        hw_version = self.config_entry.data.get("hw_version", "").lower()

        # WiCAN-PRO uses 'p' suffix
        if "pro" in hw_version:
            return f"wican_v{version}p.bin"

        # Standard WiCAN
        return f"wican_v{version}.bin"

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        # Available if we have version info from device
        return self.installed_version is not None
```

### 4.2 GitHub Releases Coordinator (`github_releases.py`)

```python
"""GitHub Releases coordinator for WiCAN firmware updates."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

import aiohttp
import async_timeout

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    GITHUB_API_RELEASES_URL,
    GITHUB_OWNER,
    GITHUB_REPO,
    GITHUB_RELEASES_UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(seconds=GITHUB_RELEASES_UPDATE_INTERVAL)


class GitHubReleasesCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to fetch GitHub releases."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="WiCAN GitHub Releases",
            update_interval=UPDATE_INTERVAL,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch latest release from GitHub."""
        url = GITHUB_API_RELEASES_URL.format(
            owner=GITHUB_OWNER,
            repo=GITHUB_REPO,
        )

        session = async_get_clientsession(self.hass)

        try:
            async with async_timeout.timeout(30):
                response = await session.get(
                    url,
                    headers={"Accept": "application/vnd.github.v3+json"},
                )
                response.raise_for_status()
                releases = await response.json()

            # Filter for latest non-prerelease
            stable_releases = [r for r in releases if not r.get("prerelease", False)]

            if not stable_releases:
                _LOGGER.warning("No stable releases found")
                return {}

            latest = stable_releases[0]
            _LOGGER.debug("Latest release: %s", latest.get("tag_name"))
            return latest

        except asyncio.TimeoutError as err:
            raise UpdateFailed("Timeout fetching GitHub releases") from err
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Error fetching GitHub releases: {err}") from err
        except (ValueError, KeyError) as err:
            raise UpdateFailed(f"Invalid response from GitHub: {err}") from err
```

### 4.3 Update `__init__.py` Setup

```python
async def async_setup_entry(hass: HomeAssistant, entry: WiCANConfigEntry) -> bool:
    """Set up WiCAN from a config entry."""
    webhook_id = entry.data[CONF_WEBHOOK_ID]

    # Initialize coordinators
    coordinator = WiCANDataUpdateCoordinator(hass, entry)
    
    # Initialize GitHub releases coordinator (shared for all update entities)
    from .github_releases import GitHubReleasesCoordinator
    github_coordinator = GitHubReleasesCoordinator(hass)
    await github_coordinator.async_config_entry_first_refresh()

    # Store runtime data
    entry.runtime_data = WiCANRuntimeData(
        coordinator=coordinator,
        github_coordinator=github_coordinator,  # NEW
        webhook_id=webhook_id,
    )

    # ... rest of existing setup code ...
```

---

## 5. Error Handling

### Exception Mapping
```python
# In update.py
try:
    await self._download_firmware(version)
except FirmwareDownloadError as err:
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="firmware_download_failed",
        translation_placeholders={"error": str(err)},
    ) from err
```

### Common Error Scenarios
1. **GitHub API Rate Limit**: Catch 403 responses, show friendly error
2. **Network Timeout**: Use `async_timeout` for all HTTP calls
3. **Device Offline**: Catch connection refused/timeout during upload
4. **Invalid Version**: 404 from GitHub download URL
5. **OTA Endpoint Unavailable**: 404/500 from device endpoint

---

## 6. Testing Plan

### Unit Tests (`tests/test_update.py`)
- Test version comparison logic
- Test filename generation for different hw_versions
- Mock GitHub API responses
- Mock device OTA upload responses
- Test error handling paths

### Integration Tests
- Test full update flow with mock server
- Test coordinator refresh after update
- Test multiple devices (PRO vs standard)
- Test version rollback (specific version)

### Manual Testing Checklist
- [ ] Update entity appears in HA UI
- [ ] Correct installed version shown
- [ ] Latest version fetched from GitHub
- [ ] Install button works
- [ ] Progress indicator updates
- [ ] Device reboots after update
- [ ] Version updates after reboot
- [ ] Error messages display correctly
- [ ] Works with both WiCAN and WiCAN-PRO

---

## 7. Security Considerations

1. **HTTPS for GitHub**: Always use HTTPS for GitHub API/downloads
2. **Verify Binary Size**: Check firmware size is reasonable (< 10MB)
3. **Device Authentication**: Consider adding device authentication for OTA endpoint
4. **Rate Limiting**: Respect GitHub API rate limits (60/hour unauthenticated)
5. **Timeout Enforcement**: Prevent hanging connections

---

## 8. Future Enhancements

### Phase 2 (Optional)
- [ ] **Beta Channel**: Add config option to track prerelease versions
- [ ] **Backup/Restore**: Implement backup before update (if device supports)
- [ ] **Auto-Update**: Add config option for automatic updates
- [ ] **Release Notes**: Show formatted release notes in UI
- [ ] **Changelog**: Fetch and display changelog from GitHub
- [ ] **Update Scheduling**: Schedule updates during low-usage times
- [ ] **Rollback Protection**: Prevent rollback to versions older than X
- [ ] **GitHub Token**: Support personal access token for higher rate limits

---

## 9. Implementation Checklist

### Core Implementation
- [ ] Create `update.py` with `WiCANUpdateEntity`
- [ ] Create `github_releases.py` with `GitHubReleasesCoordinator`
- [ ] Update `__init__.py` to initialize GitHub coordinator
- [ ] Update `models.py` to include `github_coordinator`
- [ ] Add update exceptions to `exceptions.py`
- [ ] Add translations to `strings.json`
- [ ] Update constants in `const.py`
- [ ] Add `Platform.UPDATE` to `PLATFORMS` list

### Testing & Validation
- [ ] Write unit tests for update entity
- [ ] Write unit tests for GitHub coordinator
- [ ] Test with WiCAN standard device
- [ ] Test with WiCAN-PRO device
- [ ] Test error scenarios
- [ ] Test version rollback
- [ ] Verify progress indicator works
- [ ] Check HA logs for errors

### Documentation
- [ ] Update README.md with update instructions
- [ ] Add troubleshooting section for updates
- [ ] Document firmware naming conventions
- [ ] Add screenshots of update UI

### Release
- [ ] Bump version in `manifest.json`
- [ ] Update CHANGELOG.md
- [ ] Create GitHub release
- [ ] Test with HACS installation

---

## 10. Notes & Open Questions

### Questions to Resolve
1. **OTA Form Field Name**: Verify if ESP32 OTA uses `update` or `file` as form field name
   - *Action*: Test with actual device or check ESP32 OTA library source
   
2. **Device Response**: What does device return on successful OTA upload?
   - *Action*: Capture actual HTTP response during manual OTA update

3. **Reboot Behavior**: How long does device take to reboot and reconnect?
   - *Action*: Test and adjust coordinator refresh timing

4. **Version Format**: Are there any non-standard version formats to handle?
   - *Action*: Review all GitHub releases for version string variations

5. **Hardware Detection**: Is `hw_version` reliable for detecting PRO vs standard?
   - *Action*: Check webhook data from both device types

### References
- WLED Update Platform: `homeassistant/components/wled/update.py`
- ESPHome Update Platform: `homeassistant/components/esphome/update.py`
- HA Update Entity Docs: https://developers.home-assistant.io/docs/core/entity/update
- GitHub Releases API: https://docs.github.com/en/rest/releases
- ESP32 OTA: https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-reference/system/ota.html

---

## Summary

This plan provides a complete, production-ready implementation for OTA firmware updates in the WiCAN integration, following Home Assistant core integration patterns and best practices. The implementation prioritizes user safety, error handling, and seamless integration with the existing push-based architecture.
