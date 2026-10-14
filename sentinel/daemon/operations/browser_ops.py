"""Daemon-owned browser settings and repeatable Chrome policy application."""
from __future__ import annotations

import time
import tomllib
from typing import Any

from sentinel.browser_policies.chrome import apply_chrome_policy
from sentinel.daemon.operations import settings_ops
from sentinel.dns.storage import read_protected
from sentinel.settings.paths import SETTINGS_FILE

_safe_steam_enabled = True

_application = {"status": "pending", "last_applied_at": None, "error": None}


def get_settings() -> dict[str, bool]:
    with settings_ops._settings_lock:
        return settings_ops.browser_snapshot(tomllib.loads(read_protected(SETTINGS_FILE)))


def initialize_browser_policy() -> None:
    get_settings()  # Invalid/unprotected settings prevent serving an unsafe API.
    from sentinel.daemon.operations.guardian_website_ops import initialize_guardian_websites
    initialize_guardian_websites()
    apply_browser_policy()


def application_status() -> dict:
    from sentinel.safe_steam.status import get_status
    result = dict(_application)
    result['safe_steam'] = get_status(_safe_steam_enabled)
    return result


def apply_browser_policy(*, settings: dict[str, Any] | None = None) -> dict:
    global _application, _safe_steam_enabled
    try:
        snapshot = get_settings() if settings is None else settings_ops.browser_snapshot(settings)
        _safe_steam_enabled = snapshot['safe_steam']
        from sentinel.safe_steam.assets import install_chrome_assets
        from sentinel.safe_steam.status import set_status
        try:
            install_chrome_assets()
        except (OSError, ValueError) as exc:
            set_status('chrome', 'pending', str(exc))
            raise
        apply_chrome_policy(snapshot)
        set_status('chrome', 'applied')
    except (OSError, ValueError) as exc:
        from sentinel.safe_steam.status import set_status
        set_status('chrome', 'pending', str(exc))
        _application = {"status": "pending", "last_applied_at": _application["last_applied_at"],
                        "error": str(exc)}
    else:
        _application = {"status": "applied", "last_applied_at": time.time(), "error": None}
    return application_status()


def get_status() -> dict:
    return {"settings": get_settings(), "application": application_status(),
            "supported_browser": "native Google Chrome Stable", "search_provider": "Google"}
