"""Constants for the MeatPi integration."""

# The Home Assistant domain is a permanent identifier: every existing config
# entry, entity-registry row, and device identifier is keyed by it, so it
# stays "wican" even though the integration is branded MeatPi (see
# notes/MEATPI_INTEGRATION_PLAN.md, decision D1).
DOMAIN = "wican"

MANUFACTURER = "MeatPi"

# Configuration
CONF_DEVICE_TYPE = "device_type"
CONF_POST_INTERVAL = "post_interval"
DEFAULT_POST_INTERVAL = 15  # seconds
MIN_POST_INTERVAL = 1
MAX_POST_INTERVAL = 3600

# Webhook Registration
WEBHOOK_REGISTRATION_TIMEOUT = 10  # seconds
WEBHOOK_RETRY_DELAY_BASE = 2  # seconds for exponential backoff
WEBHOOK_MAX_RETRIES = 3
PRO_DUAL_WEBHOOK_MIN_FW_VERSION = (4, 49)

# IP Caching
IP_CACHE_DURATION = 300  # 5 minutes in seconds

# mDNS Resolution
MDNS_RESOLUTION_TIMEOUT = 5  # seconds

# Device HTTP API (firmware V6+). Telemetry stays webhook-push on every
# firmware; these routes are the control/discovery surface of V6 devices.
API_STATUS_PATH = "/api/status"
API_SETTINGS_PATH = "/api/settings"
API_RESTART_PATH = "/api/restart"
API_RTC_SYNC_PATH = "/api/rtc/sync"
API_PROBE_TIMEOUT = 10  # seconds per probe request
API_REQUEST_TIMEOUT = 35  # seconds; /api/rtc/sync may block up to ~32 s

# Bounds on device API responses. A healthy device is far below these; a
# glitching or hostile one must not be able to balloon memory or the
# capability set.
MAX_API_RESPONSE_BYTES = 262144  # 256 KiB
MAX_API_COMPONENTS = 128
MAX_API_COMPONENT_NAME_LENGTH = 64

# When the capability probe has never succeeded (device asleep/unreachable at
# setup), retry it when telemetry pushes arrive — but at most this often.
PROBE_RETRY_INTERVAL = 300  # seconds

# Device API levels (capability probe result)
API_LEVEL_LEGACY = 0  # webhook-only firmware (pre-V6)
API_LEVEL_V6 = 6  # full /api/* HTTP surface

# Dispatcher signal fired when a capability probe finishes; suffixed with
# the entry_id so platforms of other entries never react.
SIGNAL_CAPABILITIES_UPDATED = f"{DOMAIN}_capabilities_updated"

# GPS / Location Tracking
GPS_ACCURACY_THRESHOLD = 200  # meters - filter out low accuracy GPS fixes
MIN_GPS_LATITUDE = -90.0
MAX_GPS_LATITUDE = 90.0
MIN_GPS_LONGITUDE = -180.0
MAX_GPS_LONGITUDE = 180.0

# GitHub API
GITHUB_API_RELEASES_URL = "https://api.github.com/repos/{owner}/{repo}/releases"
GITHUB_OWNER = "meatpiHQ"
GITHUB_REPO = "wican-fw"

# Firmware Update
FIRMWARE_DOWNLOAD_TIMEOUT = 120  # 2 minutes to download from GitHub
FIRMWARE_UPLOAD_TIMEOUT = 180  # 3 minutes to upload to device
GITHUB_API_TIMEOUT = 30  # seconds for GitHub API requests
FIRMWARE_UPDATE_REBOOT_DELAY = 2  # seconds to wait before refreshing after update
OTA_ENDPOINT = "/upload/ota.bin"
OTA_FORM_FIELD = "ota_file"

# Update Coordinator
GITHUB_RELEASES_UPDATE_INTERVAL = 3600  # 1 hour

# WiCAN Data Coordinator (push-based fallback polling)
WICAN_DATA_UPDATE_INTERVAL = 300  # seconds (5 minutes)

# Device availability: mark entities unavailable when the device has not pushed
# data within max(post_interval * DEVICE_STALE_FACTOR, MIN_DEVICE_STALE_SECONDS).
DEVICE_STALE_FACTOR = 5
MIN_DEVICE_STALE_SECONDS = 120

# Bounds on device-reported data. A healthy device never exceeds these; a
# misbehaving one (corrupted memory, hostile caller) must not be able to grow
# the entity registry or the config entry without limit.
MAX_DYNAMIC_PID_SENSORS = 1000
MAX_PID_KEY_LENGTH = 128
MAX_PID_CONFIG_FIELD_LENGTH = 64
MAX_DEVICE_INFO_FIELD_LENGTH = 255
