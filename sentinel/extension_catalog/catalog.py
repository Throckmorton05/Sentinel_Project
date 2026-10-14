"""Validate packaged inventory and expose only entries in the written policy."""
from __future__ import annotations

import json
import re
import tomllib
from urllib.parse import urlsplit

from sentinel.activity.constants import ACTIVITY_EXTENSION_ID
from sentinel.dns.storage import read_protected
from sentinel.settings.paths import APPROVED_EXTENSIONS_FILE, CATALOG_DISTRIBUTION_DIR, CHROME_POLICY_FILE
from sentinel.daemon.core.lock_state import LockMode, get_lock_state
from sentinel.extension_catalog.approvals import load_approvals

ID_PATTERN = re.compile(r"[a-p]{32}")
CATALOG_ENDPOINT = "/api/browser/extensions"


def catalog_extension_id() -> str:
    """The public ID belongs to the locally retained build signing key."""
    metadata = json.loads(read_protected(CATALOG_DISTRIBUTION_DIR / "metadata.json"))
    extension_id = metadata.get("id") if isinstance(metadata, dict) else None
    if not isinstance(extension_id, str) or not ID_PATTERN.fullmatch(extension_id):
        raise ValueError("Invalid Sentinel Extensions installation metadata")
    if extension_id in {ACTIVITY_EXTENSION_ID, "jdfobdpedcmlbmolbdfaoaidldkjjfnh", "njcmpekfomicmkpnaihnkohgpoeooeoa"}:
        raise ValueError("Sentinel Extensions identity collides with a required extension")
    return extension_id


def required_extensions(catalog_id: str) -> list[dict]:
    return [
        {"id": ACTIVITY_EXTENSION_ID, "name": "Sentinel Activity", "description": "Records supported browser activity for Sentinel reports.", "endpoint": "activity"},
        {"id": "jdfobdpedcmlbmolbdfaoaidldkjjfnh", "name": "Safe Steam", "description": "Applies your Sentinel filtering settings on Steam.", "endpoint": "safe-steam"},
        {"id": "njcmpekfomicmkpnaihnkohgpoeooeoa", "name": "Safe Crunchyroll", "description": "Applies your Sentinel filtering settings on Crunchyroll.", "endpoint": "safe-crunchyroll"},
        {"id": catalog_id, "name": "Sentinel Extensions", "description": "Your always-available approved extension catalog.", "endpoint": "catalog"},
    ]


def newtab_extension_id(catalog_id: str) -> str | None:
    """The optional appearance companion has its own locally retained key.

    Old installations have no newtab_id and continue to apply their security
    policies until both public extension packages are rebuilt and installed.
    """
    try:
        metadata = json.loads(read_protected(CATALOG_DISTRIBUTION_DIR / "metadata.json"))
    except FileNotFoundError:
        return None
    if not isinstance(metadata, dict):
        raise ValueError("Invalid Sentinel New Tab installation metadata")
    extension_id = metadata.get("newtab_id")
    if extension_id is None:
        return None
    if (not isinstance(extension_id, str) or not ID_PATTERN.fullmatch(extension_id)
            or extension_id in {entry['id'] for entry in required_extensions(catalog_id)}):
        raise ValueError("Invalid or colliding Sentinel New Tab identity")
    return extension_id


def parse_inventory(text: str) -> list[dict[str, str]]:
    data = tomllib.loads(text)
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("Unsupported approved-extension schema")
    entries = data.get("extensions", [])
    if not isinstance(entries, list) or len(entries) > 500:
        raise ValueError("Expected at most 500 approved extensions")
    seen = set()
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid approved-extension entry")
        clean = {}
        for field in ("id", "name", "developer", "category", "description", "permissions_note", "store_url"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > 1000:
                raise ValueError(f"Invalid approved-extension {field}")
            clean[field] = value.strip()
        extension_id = clean["id"]
        if not ID_PATTERN.fullmatch(extension_id) or extension_id in seen:
            raise ValueError("Invalid or duplicate approved-extension ID")
        url = urlsplit(clean["store_url"])
        if (url.scheme != "https" or url.netloc != "chromewebstore.google.com"
                or url.query or url.fragment or not url.path.startswith("/detail/")
                or url.path.rsplit("/", 1)[-1] != extension_id):
            raise ValueError("Approved extensions must link to their matching Chrome Web Store page")
        seen.add(extension_id)
        result.append(clean)
    return result


def load_inventory() -> list[dict[str, str]]:
    return parse_inventory(read_protected(APPROVED_EXTENSIONS_FILE))


def applied_catalog() -> dict:
    """Intersect inventory with the last successfully written Chrome policy.

    Newly added items never appear as installable before policy application.
    Removed items disappear even if a subsequent policy write is pending.
    In Guardian mode, blocked entries remain visible for authenticated approval.
    This endpoint is read-only; it never changes approval or lock state.
    """
    entries = load_inventory()
    catalog_id = catalog_extension_id()
    policy = json.loads(read_protected(CHROME_POLICY_FILE))
    rules = policy.get("ExtensionSettings", {})
    if not isinstance(rules, dict) or rules.get("*", {}).get("installation_mode") != "blocked":
        raise ValueError("Sentinel extension policy is unavailable")
    guardian = get_lock_state().mode is LockMode.GUARDIAN_LOCK
    approvals = load_approvals() if guardian else set()
    optional = []
    for entry in entries:
        mode = rules.get(entry["id"], {}).get("installation_mode")
        if not guardian and mode != "allowed":
            continue
        if guardian and mode not in {"blocked", "force_installed", "allowed"}:
            continue
        granted = entry["id"] in approvals
        optional.append({**entry, "guardian_approved": granted,
                         "policy_pending": guardian and mode != ("force_installed" if granted else "blocked")})
    required = [entry for entry in required_extensions(catalog_id)
                if rules.get(entry["id"], {}).get("installation_mode") == "force_installed"]
    return {"schema_version": 1, "guardian_required": guardian,
            "extensions": optional, "required_extensions": required}
