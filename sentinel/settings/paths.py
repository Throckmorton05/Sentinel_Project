from pathlib import Path


# ============================================================
# Sentinel filesystem layout
# ============================================================

CONFIG_DIR = Path("/etc/sentinel")

SHARE_DIR = Path("/usr/share/sentinel")

STATE_DIR = Path("/var/lib/sentinel")

CACHE_DIR = Path("/var/cache/sentinel")

LOG_DIR = Path("/var/log/sentinel")

RUN_DIR = Path("/run/sentinel")


# ============================================================
# Configuration
# ============================================================

SETTINGS_FILE = CONFIG_DIR / "settings.toml"

BLOCKED_APPS_FILE = CONFIG_DIR / "blocked_apps.toml"

GUARDIAN_APPS_FILE = CONFIG_DIR / "guardian-apps.json"
GUARDIAN_WEBSITES_FILE = CONFIG_DIR / "guardian-websites.json"
GUARDIAN_DESKTOP_STATE_DIR = STATE_DIR / "guardian-desktop"

DNS_CONFIG_DIR = CONFIG_DIR / "dns"

USER_ALLOWLIST_FILE = DNS_CONFIG_DIR / "user_allowlist.txt"

USER_BLOCKLIST_FILE = DNS_CONFIG_DIR / "user_blocklist.txt"


# ============================================================
# Read-only packaged data
# ============================================================

APPROVED_APPS_FILE = SHARE_DIR / "approved_apps.toml"

APPROVED_EXTENSIONS_FILE = SHARE_DIR / "browser" / "approved_extensions.toml"

GUARDIAN_EXTENSIONS_FILE = STATE_DIR / "browser" / "guardian-extensions.json"

INFO_FILE = SHARE_DIR / "info.toml"

ADGUARD_CREDENTIALS_FILE = CONFIG_DIR / "adguard.toml"

ASSETS_DIR = SHARE_DIR / "assets"

SENTINEL_LOGO = ASSETS_DIR / "Sentinel_Shield.svg"

APPSTORE_ICON = ASSETS_DIR / "sentinel-appstore.png"


# ============================================================
# DNS
# ============================================================

DNS_STATIC_DIR = SHARE_DIR / "dns"

DNS_DATA_DIR = STATE_DIR / "dns"

DNS_POLICY_FILE = DNS_DATA_DIR / "user-policy.json"

DNS_CACHE_DIR = CACHE_DIR / "dns"

MERGED_BLOCKLIST_FILE = DNS_DATA_DIR / "merged.txt"

ADULT_HEAVY_SITES_FILE = DNS_STATIC_DIR / "adult_heavy_sites.txt"

UNSAFE_SEARCH_ENGINES_FILE = DNS_STATIC_DIR / "unsafe_search_engines.txt"


# AdGuard is an external program, so its own paths can remain
# wherever AdGuard is installed.
ADGUARD_FILTER_FILE = Path("/opt/AdGuardHome/userfilters/sentinel.txt")

ADGUARD_CONFIG_FILE = Path("/opt/AdGuardHome/AdGuardHome.yaml")


# ============================================================
# AppArmor
# ============================================================

PROFILE_DIR = STATE_DIR / "apparmor" / "profiles"


# ============================================================
# Image filtering
# ============================================================

IMAGE_MODEL_DIR = SHARE_DIR / "models"

ANIME_REAL_MODEL = (
    IMAGE_MODEL_DIR / "anime_real_cls" / "mobilenetv3_v1.4_dist" / "model.onnx"
)

OPENNSFW2_ONNX_MODEL = IMAGE_MODEL_DIR / "openNSFW2_ONNX_Model" / "open_nsfw.onnx"

HENTAI_MODEL_DIR = IMAGE_MODEL_DIR / "hentai-vit"

HENTAI_ONNX_MODEL = HENTAI_MODEL_DIR / "model.onnx"


# ============================================================
# Safe Steam / browser extensions
# ============================================================

SAFE_STEAM_DIR = SHARE_DIR / "safe_steam"

SAFE_STEAM_EXTENSION_DIR = SAFE_STEAM_DIR / "steam_extension"

SAFE_STEAM_PROVISIONING_DIR = SAFE_STEAM_DIR / "provisioning"

EXTENSIONS_DIR = SHARE_DIR / "extensions"

CATALOG_DISTRIBUTION_DIR = EXTENSIONS_DIR / "catalog"
NEWTAB_DISTRIBUTION_DIR = EXTENSIONS_DIR / "newtab"

SAFE_STEAM_DISTRIBUTION_DIR = EXTENSIONS_DIR / "safe-steam"

SAFE_CRUNCHYROLL_DISTRIBUTION_DIR = EXTENSIONS_DIR / "safe-crunchyroll"


# ============================================================
# Logging
# ============================================================

SCREEN_BLOCK_LOG = LOG_DIR / "blocked.jsonl"


# ============================================================
# Runtime
# ============================================================

SENTINELD_SOCKET = RUN_DIR / "sentineld.sock"


LOCK_STATE_FILE = STATE_DIR / "lock-state.json"

BLOCK_WINDOWS_FILE = STATE_DIR / "block-windows.json"

GUARDIAN_AUTH_FILE = STATE_DIR / "guardian-auth.json"

# ============================================================
# System integration
# ============================================================

MANAGED_SERVICE_DIR = CONFIG_DIR / "services"

SYSTEMD_SYSTEM_DIR = Path("/etc/systemd/system")

SDDM_AUTOLOGIN_CONFIG = Path("/etc/sddm.conf.d/sentinel-autologin.conf")

CHROME_POLICY_DIR = Path("/etc/opt/chrome/policies/managed")

CHROME_POLICY_FILE = CHROME_POLICY_DIR / "sentinel.json"
