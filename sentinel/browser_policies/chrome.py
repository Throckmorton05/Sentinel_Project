import json
from sentinel.extension_catalog.catalog import catalog_extension_id, newtab_extension_id, load_inventory, required_extensions
from sentinel.extension_catalog.approvals import load_approvals
from sentinel.daemon.core.lock_state import LockMode, get_lock_state
from sentinel.browser_policies.restrictions import NON_GOOGLE_SEARCH_DOMAINS
from sentinel.dns.storage import atomic_write

from sentinel.settings.paths import CHROME_POLICY_FILE


def build_chrome_policy(settings: dict[str, bool]) -> dict:

    # Apply Sentinel Chrome enterprise policies.

    catalog_id = catalog_extension_id()
    approved = load_inventory()
    required = required_extensions(catalog_id)
    required_ids = {entry["id"] for entry in required}
    newtab_id = newtab_extension_id(catalog_id)
    reserved_ids = required_ids | ({newtab_id} if newtab_id else set())
    if any(entry["id"] in reserved_ids for entry in approved):
        raise ValueError("Optional inventory cannot override a required Sentinel extension")
    guardian = get_lock_state().mode is LockMode.GUARDIAN_LOCK
    approvals = load_approvals() if guardian else set()

    policy = {
        # Platform baseline stays managed independently of content toggles.
        "DefaultSearchProviderEnabled": True,
        "DefaultSearchProviderName": "Google",
        "DefaultSearchProviderKeyword": "google.com",
        "DefaultSearchProviderSearchURL": "https://www.google.com/search?q={searchTerms}",
        "DefaultSearchProviderSuggestURL": "https://www.google.com/complete/search?output=chrome&q={searchTerms}",
        "DefaultSearchProviderImageURL": "https://www.google.com/search?tbm=isch&q={searchTerms}",
        "URLBlocklist": list(NON_GOOGLE_SEARCH_DOMAINS),
        "ProxyMode": "direct",
        "ExtensionInstallSources": [],
        # Disallow unpacked extension loading and extension developer mode.
        "ExtensionDeveloperModeSettings": 1,
        "IncognitoModeAvailability": 1,
        "DnsOverHttpsMode": "off",
        "BrowserGuestModeEnabled": False,
        "ForceGoogleSafeSearch": True,
        "ForceYouTubeRestrict": 1,
        "ExtensionInstallBlocklist": ["*"],
    }

    # A single root-owned inventory feeds both installation policy and catalog.
    # Required extensions remain independent of content-filtering switches.
    allowed = [entry for entry in approved if not guardian or entry["id"] in approvals]
    policy["ExtensionInstallAllowlist"] = [entry["id"] for entry in required + allowed]
    policy["ExtensionInstallForcelist"] = [
        f'{entry["id"]};http://127.0.0.1:8765/extensions/{entry["endpoint"]}/updates.xml'
        for entry in required
    ]
    policy["ExtensionSettings"] = {"*": {"installation_mode": "blocked"}}
    for entry in required:
        policy["ExtensionSettings"][entry["id"]] = {
            "installation_mode": "force_installed",
            "update_url": f'http://127.0.0.1:8765/extensions/{entry["endpoint"]}/updates.xml',
            "override_update_url": True,
        }
    policy["ExtensionSettings"][catalog_id]["toolbar_pin"] = "force_pinned"
    if newtab_id:
        # This is appearance only, including in Guardian/Self Lock. A normal
        # install lets the user disable it independently of the pinned catalog.
        policy["ExtensionInstallAllowlist"].append(newtab_id)
        policy["ExtensionSettings"][newtab_id] = {
            "installation_mode": "normal_installed",
            "update_url": "http://127.0.0.1:8765/extensions/newtab/updates.xml",
            "override_update_url": True,
        }
    for entry in approved:
        if guardian and entry["id"] in approvals:
            update_url = "https://clients2.google.com/service/update2/crx"
            policy["ExtensionSettings"][entry["id"]] = {
                "installation_mode": "force_installed", "update_url": update_url,
            }
            policy["ExtensionInstallForcelist"].append(f'{entry["id"]};{update_url}')
        elif guardian:
            policy["ExtensionSettings"][entry["id"]] = {
                "installation_mode": "blocked",
                "blocked_install_message": "Open Sentinel Extensions and ask your guardian to approve this extension.",
            }
        else:
            policy["ExtensionSettings"][entry["id"]] = {"installation_mode": "allowed"}
    # Bookmarks, the bookmark bar, and home-page appearance are user choices.
    # The always-pinned catalog action remains the permanent access point.

    # The extensions stay installed when disabled: only sentineld's boolean
    # switches their filtering. Re-enabling does not wait for reinstallation.
    if not settings["safe_browsing"]:
        for key in ("ForceGoogleSafeSearch", "ForceYouTubeRestrict"):
            policy.pop(key)
    from sentinel.daemon.operations.guardian_website_ops import chrome_rules
    policy.update(chrome_rules())
    return policy


def apply_chrome_policy(settings: dict[str, bool]) -> None:
    """Called only by sentineld with a validated, authorized snapshot."""
    rendered = json.dumps(build_chrome_policy(settings), indent=4) + "\n"
    atomic_write(CHROME_POLICY_FILE, rendered)
