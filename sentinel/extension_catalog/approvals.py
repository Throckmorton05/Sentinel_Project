"""Root-owned Guardian installation receipts, separate from packaged approval."""
import json
import re

from sentinel.dns.storage import read_protected, atomic_write
from sentinel.settings.paths import GUARDIAN_EXTENSIONS_FILE


def load_approvals() -> set[str]:
    try:
        data = json.loads(read_protected(GUARDIAN_EXTENSIONS_FILE, private=True))
    except FileNotFoundError:
        return set()
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("Invalid Guardian extension approval state")
    entries = data.get("extension_ids")
    if (not isinstance(entries, list) or len(entries) > 500
            or any(not isinstance(value, str) or not re.fullmatch(r"[a-p]{32}", value) for value in entries)
            or len(set(entries)) != len(entries)):
        raise ValueError("Invalid Guardian extension approvals")
    return set(entries)


def save_approvals(extension_ids: set[str]) -> None:
    atomic_write(GUARDIAN_EXTENSIONS_FILE, json.dumps({
        "schema_version": 1, "extension_ids": sorted(extension_ids),
    }, indent=2) + "\n", mode=0o600)
