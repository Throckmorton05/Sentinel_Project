from __future__ import annotations

import json
import re
import socket
import uuid
from typing import Any

from sentinel.daemon.rpc.protocol import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    ProtocolError,
    decode_message,
    encode_message,
)
from sentinel.settings.paths import SENTINELD_SOCKET


def user_message(message: object) -> str:
    """Keep internal service names out of messages shown by desktop clients."""
    text = re.sub(r'\bsentineld(?:\.service)?\b', 'Sentinel backend', str(message), flags=re.IGNORECASE)
    return re.sub(r'\bdaemon\b', 'backend', text, flags=re.IGNORECASE)


class SentineldError(RuntimeError):
    """Raised when sentineld rejects or cannot process a request."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(user_message(message))
        self.code = code


# ============================================================
# Socket communication
# ============================================================


def _read_message(
    sock: socket.socket,
) -> bytes:
    """
    Read one newline-terminated message from sentineld.
    """

    data = bytearray()

    while True:
        chunk = sock.recv(4096)

        if not chunk:
            break

        data.extend(chunk)

        if len(data) > MAX_MESSAGE_BYTES + 1:
            raise SentineldError("Response from sentineld was too large.")

        if b"\n" in chunk:
            break

    if not data:
        raise SentineldError("sentineld closed the connection without responding.")

    line, _, _ = bytes(data).partition(b"\n")

    return line


# ============================================================
# Generic request
# ============================================================


def request(
    action: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = 5.0,
    guardian_pin: str | None = None,
) -> dict[str, Any]:
    """
    Send one request to sentineld and return its result.

    Parameters
    ----------
    action
        Explicit sentineld action, such as:

            system.restart
            package.install

    params
        Optional parameters for the action.

    timeout
        Maximum number of seconds to wait for sentineld.

        Short actions normally use the default 5-second timeout.
        Package installation and system updates use longer timeouts.
    """

    request_id = uuid.uuid4().hex

    payload = {
        "version": PROTOCOL_VERSION,
        "request_id": request_id,
        "action": action,
        "params": {**(params or {}), **({"guardian_pin": guardian_pin} if guardian_pin is not None else {})},
    }

    try:
        with socket.socket(
            socket.AF_UNIX,
            socket.SOCK_STREAM,
        ) as sock:
            sock.settimeout(timeout)

            sock.connect(str(SENTINELD_SOCKET))

            sock.sendall(encode_message(payload))

            raw_response = _read_message(sock)

    except FileNotFoundError as exc:
        raise SentineldError("Cannot connect to Sentinel backend.") from exc

    except PermissionError as exc:
        raise SentineldError("Cannot connect to Sentinel backend.") from exc

    except TimeoutError as exc:
        raise SentineldError(
            f"Timed out waiting for sentineld while performing '{action}'."
        ) from exc

    except (
        ConnectionRefusedError,
        OSError,
    ) as exc:
        raise SentineldError("Cannot connect to Sentinel backend.") from exc

    try:
        response = decode_message(raw_response)

    except ProtocolError as exc:
        raise SentineldError(f"Invalid response from sentineld: {exc}") from exc

    if response.get("request_id") != request_id:
        raise SentineldError("sentineld returned the wrong request ID.")

    if not response.get("ok"):
        error = response.get(
            "error",
            {},
        )

        if not isinstance(
            error,
            dict,
        ):
            raise SentineldError("sentineld returned an invalid error response.")

        message = error.get(
            "message",
            "sentineld rejected the request.",
        )

        raise SentineldError(str(message), code=error.get("code"))

    result = response.get(
        "result",
        {},
    )

    if not isinstance(
        result,
        dict,
    ):
        raise SentineldError("sentineld returned an invalid result.")

    return result


# ============================================================
# Diagnostic operations
# ============================================================


def ping() -> dict[str, Any]:
    """
    Verify that sentineld is running and responding.
    """

    return request("ping")


# ============================================================
# System operations
# ============================================================


def update_system(*, guardian_pin: str | None = None) -> dict[str, Any]:
    """
    Ask sentineld to update the operating system.

    A long timeout is used because apt update/upgrade may take
    several minutes.
    """

    return request(
        "system.update",
        guardian_pin=guardian_pin,
        timeout=4500.0,
    )


def update_sentinel(*, guardian_pin: str | None = None) -> dict[str, Any]:
    """Upgrade the installed Sentinel packages from the configured signed channel."""
    return request("sentinel.update", guardian_pin=guardian_pin, timeout=4500.0)


def restart_system(*, guardian_pin: str | None = None) -> dict[str, Any]:
    """
    Ask sentineld to restart the system.
    """

    return request(
        "system.restart",
        guardian_pin=guardian_pin,
        timeout=10.0,
    )


def shutdown_system(*, guardian_pin: str | None = None) -> dict[str, Any]:
    """
    Ask sentineld to shut down the system.
    """

    return request(
        "system.shutdown",
        guardian_pin=guardian_pin,
        timeout=10.0,
    )


# ============================================================
# Package operations
# ============================================================


def install_package(
    name: str,
    *,
    guardian_pin: str | None = None,
) -> dict[str, Any]:
    """
    Ask sentineld to install a package or approved Sentinel alias.

    Examples
    --------

        install_package("git")
        install_package("neovim")
        install_package("poetry")
    """

    return request(
        "package.install",
        {
            "name": name,
        },
        guardian_pin=guardian_pin,
        timeout=1800.0,
    )


def update_package(
    name: str,
    *,
    guardian_pin: str | None = None,
) -> dict[str, Any]:
    """Update Neovim to the latest verified official stable release."""
    return request(
        "package.update", {"name": name},
        guardian_pin=guardian_pin, timeout=1800.0,
    )


def uninstall_package(
    name: str,
    *,
    guardian_pin: str | None = None,
) -> dict[str, Any]:
    """
    Ask sentineld to uninstall a package or approved Sentinel alias.
    """

    return request(
        "package.uninstall",
        {
            "name": name,
        },
        guardian_pin=guardian_pin,
        timeout=1800.0,
    )


# ============================================================
# Sentinel-managed services
# ============================================================


def add_service(
    name: str,
    path: str,
    *,
    guardian_pin: str | None = None,
) -> dict[str, Any]:
    """
    Register a user program as a Sentinel-managed service.
    """

    return request(
        "service.add",
        {
            "name": name,
            "path": path,
        },
        guardian_pin=guardian_pin,
        timeout=30.0,
    )


def start_service(
    name: str,
    *,
    guardian_pin: str | None = None,
) -> dict[str, Any]:

    return request(
        "service.start",
        {
            "name": name,
        },
        guardian_pin=guardian_pin,
        timeout=30.0,
    )


def stop_service(
    name: str,
    *,
    guardian_pin: str | None = None,
) -> dict[str, Any]:

    return request(
        "service.stop",
        {
            "name": name,
        },
        guardian_pin=guardian_pin,
        timeout=30.0,
    )


def enable_service(
    name: str,
    *,
    guardian_pin: str | None = None,
) -> dict[str, Any]:

    return request(
        "service.enable",
        {
            "name": name,
        },
        guardian_pin=guardian_pin,
        timeout=30.0,
    )


def disable_service(
    name: str,
    *,
    guardian_pin: str | None = None,
) -> dict[str, Any]:

    return request(
        "service.disable",
        {
            "name": name,
        },
        guardian_pin=guardian_pin,
        timeout=30.0,
    )


def delete_service(
    name: str,
    *,
    guardian_pin: str | None = None,
) -> dict[str, Any]:

    return request(
        "service.delete",
        {
            "name": name,
        },
        guardian_pin=guardian_pin,
        timeout=30.0,
    )


# ============================================================
# Sentinel settings
# ============================================================


def get_setting(
    name: str,
) -> dict[str, Any]:
    """
    Read one registered Sentinel setting.
    """

    return request(
        "settings.get",
        {
            "name": name,
        },
        timeout=3.0,
    )


def get_api_capabilities() -> dict[str, Any]:
    return request("api.capabilities", timeout=3.0)


def get_settings_schema() -> dict[str, Any]:
    return request("settings.schema", timeout=3.0)


def get_settings_snapshot() -> dict[str, Any]:
    return request("settings.snapshot", timeout=3.0)


def set_setting(
    name: str,
    value: Any,
    *,
    guardian_pin: str | None = None,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    """
    Request a change to one registered Sentinel setting.

    sentineld determines whether the requested change strengthens
    or weakens protection and applies the active lock-state policy.
    """

    return request(
        "settings.set",
        {
            "name": name,
            "value": value,
            **({"expected_revision": expected_revision} if expected_revision is not None else {}),
        },
        guardian_pin=guardian_pin,
        timeout=60.0 if name == "image_filtering" else (300.0 if name.startswith("dns.") else 3.0),
    )


# ============================================================
# Sentinel lock state
# ============================================================


def get_lock_state() -> dict[str, Any]:
    """
    Return sentineld's authoritative lock state.
    """

    return request(
        "state.get",
        timeout=3.0,
    )


def enter_self_lock(*, guardian_pin: str | None = None) -> dict[str, Any]:
    """
    Permanently place Sentinel into Self Lock.
    """

    return request(
        "state.enter_self_lock",
        guardian_pin=guardian_pin,
        timeout=3.0,
    )



def get_guardian_pin_status() -> dict[str, Any]:
    """Read PIN setup status without requesting or exposing credentials."""
    return request("guardian.pin_status")


def authenticate_guardian(*, guardian_pin: str) -> dict[str, Any]:
    """Verify Portal entry without changing policy or creating a saved session."""
    return request("guardian.authenticate", guardian_pin=guardian_pin, timeout=3.0)


def enter_guardian_lock(*, new_pin: str | None = None,
                        pin_confirmation: str | None = None,
                        guardian_pin: str | None = None) -> dict[str, Any]:
    """Enter from Unlocked with a confirmed PIN, replacing the saved PIN."""
    params = {}
    if new_pin is not None:
        params["new_pin"] = new_pin
    if pin_confirmation is not None:
        params["pin_confirmation"] = pin_confirmation
    return request("state.enter_guardian_lock", params, guardian_pin=guardian_pin, timeout=3.0)


def exit_guardian_lock(*, guardian_pin: str) -> dict[str, Any]:
    return request("state.exit_guardian_lock", guardian_pin=guardian_pin, timeout=3.0)


def change_guardian_pin(*, guardian_pin: str, new_pin: str) -> dict[str, Any]:
    return request("guardian.change_pin", {"new_pin": new_pin},
                   guardian_pin=guardian_pin, timeout=3.0)


def register_tamper_blocker() -> dict[str, Any]:
    """
    Register this process as Sentinel's active tamper blocker.
    """

    return request(
        "tamper.blocker.register",
        timeout=3.0,
    )


# ============================================================
# Screen monitor watchdog
# ============================================================


def register_screen_monitor() -> dict[str, Any]:
    """
    Register this process as Sentinel's active screen monitor.
    """

    return request(
        "monitor.register",
        timeout=3.0,
    )


def screen_monitor_heartbeat() -> dict[str, Any]:
    """
    Report that the registered screen monitor is still healthy.
    """

    return request(
        "monitor.heartbeat",
        timeout=3.0,
    )


# ============================================================
# Standalone diagnostic entry point
# ============================================================


def main() -> None:
    """
    Ping sentineld and print the result.

    This allows:

        python -m sentinel.daemon.client
    """

    result = ping()

    print(
        json.dumps(
            result,
            indent=2,
        )
    )


# App-store requests deliberately expose no shell flags or installation scope.
def get_appstore_policy() -> dict[str, Any]:
    return request("appstore.policy", timeout=3.0)


def get_appstore_installed() -> dict[str, Any]:
    return request("appstore.installed", timeout=60.0)


def get_appstore_updates() -> dict[str, Any]:
    return request("appstore.updates", timeout=180.0)


def install_flatpak(app_id: str, *, guardian_pin: str | None = None) -> dict[str, Any]:
    return request("appstore.install", {"app_id": app_id},
                   guardian_pin=guardian_pin, timeout=7200.0)


def uninstall_flatpak(app_id: str, *, guardian_pin: str | None = None) -> dict[str, Any]:
    return request("appstore.uninstall", {"app_id": app_id},
                   guardian_pin=guardian_pin, timeout=7200.0)


def update_flatpak(ref: str | None = None, *, guardian_pin: str | None = None) -> dict[str, Any]:
    return request("appstore.update_flatpak", {"ref": ref},
                   guardian_pin=guardian_pin, timeout=7200.0)


def update_system_package(name: str, *, guardian_pin: str | None = None) -> dict[str, Any]:
    return request("appstore.update_system_package", {"name": name},
                   guardian_pin=guardian_pin, timeout=7200.0)


def update_appstore_all(*, guardian_pin: str | None = None) -> dict[str, Any]:
    return request("appstore.update_all", guardian_pin=guardian_pin, timeout=14400.0)


if __name__ == "__main__":
    main()


# ============================================================
# Scheduled block windows
# ============================================================


def get_block_window_policy() -> dict[str, Any]:
    """Return creation rules, lock choices, and block-window limits."""

    return request("blockwindow.policy", timeout=3.0)


def list_block_windows(
    *,
    offset: int = 0,
    limit: int = 20,
    include_ended: bool = True,
    include_targets: bool = False,
    range_start: str | float | None = None,
    range_end: str | float | None = None,
) -> dict[str, Any]:
    """List calendar-ready block windows, optionally constrained to a range."""

    return request(
        "blockwindow.list",
        {
            "offset": offset,
            "limit": limit,
            "include_ended": include_ended,
            "include_targets": include_targets,
            "range_start": range_start,
            "range_end": range_end,
        },
        timeout=3.0,
    )


def get_block_window(window_id: str) -> dict[str, Any]:
    return request("blockwindow.get", {"id": window_id}, timeout=3.0)


def get_effective_block_windows() -> dict[str, Any]:
    """Return the app/domain restrictions active from schedules right now."""

    return request("blockwindow.effective", timeout=3.0)


def create_block_window(
    *,
    name: str,
    start_at: str | float,
    end_at: str | float,
    timezone: str = "UTC",
    lock_type: str | None = None,
    apps: list[dict[str, Any]] | None = None,
    domains: list[str] | None = None,
    enabled: bool = True,
    notes: str = "",
    block_type: str = 'targets',
    recurrence: str = 'none',
    guardian_pin: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "name": name,
        "start_at": start_at,
        "end_at": end_at,
        "timezone": timezone,
        "apps": apps or [],
        "domains": domains or [],
        "enabled": enabled,
        "notes": notes,
        "block_type": block_type,
        "recurrence": recurrence,
    }
    if lock_type is not None:
        params["lock_type"] = lock_type
    return request(
        "blockwindow.create",
        params,
        guardian_pin=guardian_pin,
        timeout=30.0,
    )


def get_screen_time_blocks() -> dict[str, Any]:
    return request('screenblock.status', timeout=3.0)

def get_daily_screen_time_limit() -> dict[str, Any]:
    return request('screenlimit.status', timeout=5.0)


def set_daily_screen_time_limit(enabled: bool, minutes: int, timezone: str, *,
                               expected_revision=None, guardian_pin=None) -> dict[str, Any]:
    return request('screenlimit.set', {'enabled': enabled, 'minutes': minutes,
                   'timezone': timezone, 'expected_revision': expected_revision},
                   guardian_pin=guardian_pin, timeout=5.0)


def bypass_screen_time_block(window_id: str, *, guardian_pin: str | None = None) -> dict[str, Any]:
    return request('screenblock.bypass', {'id': window_id}, guardian_pin=guardian_pin, timeout=3.0)


def register_screen_time_overlay() -> dict[str, Any]:
    return request('screenblock.register', timeout=3.0)


def update_block_window(
    window_id: str,
    changes: dict[str, Any],
    *,
    guardian_pin: str | None = None,
) -> dict[str, Any]:
    return request(
        "blockwindow.update",
        {"id": window_id, **changes},
        guardian_pin=guardian_pin,
        timeout=30.0,
    )


def delete_block_window(
    window_id: str,
    *,
    guardian_pin: str | None = None,
) -> dict[str, Any]:
    return request(
        "blockwindow.delete",
        {"id": window_id},
        guardian_pin=guardian_pin,
        timeout=30.0,
    )


# ============================================================
# Application blocker
# ============================================================


def list_installed_gui_apps(*, offset: int = 0, limit: int = 25) -> dict[str, Any]:
    return request("appblock.catalog", {"offset": offset, "limit": limit}, timeout=30.0)


def list_blocked_apps() -> dict[str, Any]:
    return request("appblock.list", timeout=3.0)


def get_appblock_status() -> dict[str, Any]:
    return request("appblock.status", timeout=3.0)


def add_blocked_native_app(
    key: str,
    name: str,
    executable: str,
    *,
    guardian_pin: str | None = None,
) -> dict[str, Any]:
    return request(
        "appblock.add",
        {
            "key": key,
            "name": name,
            "type": "native",
            "executable": executable,
        },
        guardian_pin=guardian_pin,
        timeout=30.0,
    )


def add_blocked_flatpak_app(
    key: str,
    name: str,
    flatpak_id: str,
    *,
    guardian_pin: str | None = None,
) -> dict[str, Any]:
    return request(
        "appblock.add",
        {
            "key": key,
            "name": name,
            "type": "flatpak",
            "flatpak_id": flatpak_id,
        },
        guardian_pin=guardian_pin,
        timeout=30.0,
    )


def add_blocked_snap_app(key: str, name: str, snap_instance: str, *, guardian_pin: str | None = None) -> dict[str, Any]:
    return request('appblock.add', {'key': key, 'name': name, 'type': 'snap',
                   'snap_instance': snap_instance}, guardian_pin=guardian_pin, timeout=30.0)


def remove_blocked_app(
    key: str,
    *,
    guardian_pin: str | None = None,
) -> dict[str, Any]:
    return request(
        "appblock.remove",
        {"key": key},
        guardian_pin=guardian_pin,
        timeout=30.0,
    )


def sync_app_blocker(*, guardian_pin: str | None = None) -> dict[str, Any]:
    return request(
        "appblock.sync",
        guardian_pin=guardian_pin,
        timeout=30.0,
    )


# DNS clients submit intent only. Deadlines and policy live in sentineld.
def get_dns_status() -> dict[str, Any]:
    return request("dns.status", timeout=300.0)


def list_dns_domains(list_name: str, *, offset: int = 0, limit: int = 50) -> dict[str, Any]:
    return request(f"dns.{list_name}.list", {"offset": offset, "limit": limit}, timeout=300.0)


def add_dns_domain(list_name: str, domain: str, *, guardian_pin: str | None = None) -> dict[str, Any]:
    return request(f"dns.{list_name}.add", {"domain": domain}, guardian_pin=guardian_pin, timeout=300.0)


def remove_dns_domain(list_name: str, domain: str, *, guardian_pin: str | None = None) -> dict[str, Any]:
    return request(f"dns.{list_name}.remove", {"domain": domain}, guardian_pin=guardian_pin, timeout=300.0)


def regenerate_dns(*, update_sources: bool = False, guardian_pin: str | None = None) -> dict[str, Any]:
    return request("dns.regenerate", {"update_sources": update_sources},
                   guardian_pin=guardian_pin, timeout=1800.0 if update_sources else 300.0)


def get_browser_status() -> dict[str, Any]:
    return request("browser.status", timeout=3.0)


def sync_browser_policy(*, guardian_pin: str | None = None) -> dict[str, Any]:
    return request("browser.sync", guardian_pin=guardian_pin, timeout=10.0)


# Activity APIs are reads; deletion/retention changes are intentionally absent.
def get_activity_status() -> dict[str, Any]:
    return request("activity.status")


def list_activity_events(*, start=None, end=None, kind=None, before_id=None, limit=10, query=None) -> dict[str, Any]:
    return request("activity.events", {"start": start, "end": end, "kind": kind,
                                      "before_id": before_id, "limit": limit, "query": query})


def sync_activity_events(*, cursor=None, limit=20) -> dict[str, Any]:
    """Save next_cursor after persisting events; deduplicate by (store_id, id)."""
    return request("activity.sync", {"cursor": cursor, "limit": limit})


def get_activity_report(*, period="week", date=None, timezone="UTC") -> dict[str, Any]:
    return request("activity.report", {"period": period, "date": date, "timezone": timezone})


def record_screen_block(details: dict[str, Any]) -> dict[str, Any]:
    return request("activity.monitor_block", details, timeout=1.0)


# Guardian-selected desktop apps. Safe Installs remains a separate catalog.
def get_guardian_app_policy() -> dict:
    return request("guardian.apps.policy")


def list_guardian_apps() -> dict:
    return request("guardian.apps.list")


def approve_guardian_app(desktop_id: str, *, guardian_pin: str | None = None) -> dict:
    return request("guardian.apps.approve", {"desktop_id": desktop_id}, guardian_pin=guardian_pin, timeout=30)


def revoke_guardian_app(desktop_id: str, *, guardian_pin: str | None = None) -> dict:
    return request("guardian.apps.revoke", {"desktop_id": desktop_id}, guardian_pin=guardian_pin, timeout=30)


def set_guardian_apps_enabled(enabled: bool, *, guardian_pin: str | None = None) -> dict:
    return request("guardian.apps.enable", {"enabled": enabled}, guardian_pin=guardian_pin, timeout=30)


def replace_guardian_apps(desktop_ids: list[str], enabled: bool, *, guardian_pin: str | None = None) -> dict:
    return request("guardian.apps.replace", {"desktop_ids": desktop_ids, "enabled": enabled},
                   guardian_pin=guardian_pin, timeout=30)


# Guardian-selected websites. DNS allowlist exceptions remain separate.
def get_guardian_website_policy() -> dict:
    return request("guardian.websites.policy")


def approve_guardian_website(domain: str, *, guardian_pin: str | None = None) -> dict:
    return request("guardian.websites.approve", {"domain": domain}, guardian_pin=guardian_pin, timeout=30)


def revoke_guardian_website(domain: str, *, guardian_pin: str | None = None) -> dict:
    return request("guardian.websites.revoke", {"domain": domain}, guardian_pin=guardian_pin, timeout=30)


def set_guardian_websites_enabled(enabled: bool, *, guardian_pin: str | None = None) -> dict:
    return request("guardian.websites.enable", {"enabled": enabled}, guardian_pin=guardian_pin, timeout=30)


def replace_guardian_websites(domains: list[str], enabled: bool, *, guardian_pin: str | None = None) -> dict:
    return request("guardian.websites.replace", {"domains": domains, "enabled": enabled},
                   guardian_pin=guardian_pin, timeout=30)


def get_image_filtering_status() -> dict[str, Any]:
    return request("imagefilter.status", timeout=3.0)


def get_screen_time_report(*, date=None, timezone="UTC") -> dict[str, Any]:
    return request("activity.usage", {"date": date, "timezone": timezone})
