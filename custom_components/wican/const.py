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

# History backfill (SD-card data logger → HA long-term statistics).
# See notes/HISTORICAL_DATA_SYNC.md for the design.
CONF_HISTORY_SYNC = "history_sync"
DEFAULT_HISTORY_SYNC = True
COMPONENT_DATA_LOGGER = "data_logger"
LOGGER_STATUS_PATH = "/api/logger"
LOGGER_EXPORT_PATH = "/api/logger/export"
FS_LIST_PATH = "/api/fs/list"
FS_DOWNLOAD_PATH = "/api/fs/download"
HISTORY_STORAGE_VERSION = 1
# Bounds: a healthy device is far below these; a glitching one must not be
# able to stall the event loop or balloon memory/statistics.
HISTORY_MAX_AGE_DAYS = 30  # never import rows older than this
HISTORY_FUTURE_SKEW = 300  # seconds of clock skew tolerated into the future
HISTORY_MAX_ROWS_PER_SYNC = 200_000
HISTORY_MAX_FILE_BYTES = 8 * 1024 * 1024
HISTORY_MAX_FILES_PER_SYNC = 48
HISTORY_MAX_PIDS_PER_SYNC = 200
HISTORY_EXPORT_PAGE_ROWS = 2000  # rows requested per export page
HISTORY_EXPORT_MAX_PAGES = 200
HISTORY_EXPORT_PAGE_BYTES = 2 * 1024 * 1024  # size cap per export page
HISTORY_SYNC_MIN_INTERVAL = 300  # seconds between sync runs per entry

# Remote device catalog: declarative device-type definitions pulled from
# GitHub (like params.json), so a new MeatPi product gets first-class
# support without an integration release. Design: notes/DEVICE_CATALOG.md.
# TODO(meatpi): placeholder — point at the real repo once it exists.
DEVICE_CATALOG_URL = (
    "https://raw.githubusercontent.com/meatpiHQ/meatpi-devices/main/"
    "device_catalog.json"
)
CATALOG_SCHEMA_VERSION = 1
CATALOG_STORAGE_KEY = f"{DOMAIN}.device_catalog"
CATALOG_STORAGE_VERSION = 1
CATALOG_REFRESH_INTERVAL = 86400  # seconds between remote fetch attempts
CATALOG_FETCH_TIMEOUT = 15  # seconds
CATALOG_MAX_BYTES = 262144  # 256 KiB fetch cap
# Bounds on catalog content — a compromised or corrupt catalog must not be
# able to balloon the registry or entity set.
CATALOG_MAX_DEVICE_TYPES = 64
CATALOG_MAX_KEYWORDS = 8
CATALOG_MAX_KEYWORD_LENGTH = 32
CATALOG_MAX_MODEL_LENGTH = 64
CATALOG_MAX_SENSORS = 32
CATALOG_MAX_FIELD_LENGTH = 128

# PID parameter definitions (params.json). Fetched from the wican-fw
# vehicle-profiles pipeline on every entry setup/reload (the "reload the
# integration to get your new parameter" support flow), coalesced across
# concurrent setups, persisted in .storage; the bundled file is the
# offline fallback.
PARAMS_STORAGE_KEY = f"{DOMAIN}.params"
PARAMS_STORAGE_VERSION = 1
PARAMS_FETCH_DEDUPE_WINDOW = 30  # seconds; folds concurrent setups into one fetch
PARAMS_MAX_BYTES = 1048576  # 1 MiB fetch cap (file is ~55 KiB today)
PARAMS_MAX_ENTRIES = 5000
PARAMS_MAX_KEY_LENGTH = 128
PARAMS_MAX_FIELD_LENGTH = 256

# Webhook pushes may arrive gzip-compressed (LTE data saving). aiohttp
# inflates transparently but only bounds the *compressed* size, so the
# handler reads at most this many decompressed bytes — a compression bomb
# stops inflating at the cap instead of ballooning memory. A healthy
# payload is a few KiB.
MAX_WEBHOOK_BODY_BYTES = 2 * 1024 * 1024

# Cap on distinct top-level payload keys retained by the coordinator: a
# healthy device sends a handful (status, autopid_data, config, gps, ...);
# a buggy firmware emitting rotating keys must not grow memory forever.
MAX_COORDINATOR_KEYS = 32

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
