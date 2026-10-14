"""Policy-enforced application blocking owned by sentineld.

The daemon is the sole writer of blocked_apps.toml and the sole privileged
owner of AppArmor synchronization. Runtime process enforcement consumes a
cached, validated policy snapshot from this module.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
import tomllib
from pathlib import Path
from typing import Any

import tomli_w
from dataclasses import replace
from sentinel.daemon.operations.vm_ops import protection_enabled
from sentinel.vm_protection import installed_vm_blocks, VM_FLATPAK_IDS, VM_SNAP_INSTANCES

from sentinel.app_blocker.AppArmor.block_manager import (
    managed_profile_name,
    sync_blocked_apps,
)
from sentinel.app_blocker.process_monitor.policy import (
    ProcessBlockPolicy,
    build_process_policy,
)
from sentinel.daemon.security.policy import ChangeKind, require_policy
from sentinel.browser_policies.restrictions import (
    installed_browser_blocks, runtime_browser_flatpak_ids, dev_allow_firefox,
)
from sentinel.settings.paths import BLOCKED_APPS_FILE


_APP_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_FLATPAK_ID_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*){2,}"
)

_lock = threading.RLock()
_runtime_policy = ProcessBlockPolicy.empty()
_application: dict[str, Any] = {
    "status": "pending",
    "error": None,
    "unresolved": {},
    "apparmor": {},
}


def _require_text(value: object, label: str, *, maximum: int = 255) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Missing or invalid {label}.")
    value = value.strip()
    if not value or len(value) > maximum or "\x00" in value or "\n" in value:
        raise ValueError(f"Missing or invalid {label}.")
    return value


def _require_key(value: object) -> str:
    key = _require_text(value, "application key", maximum=128)
    if not _APP_KEY_RE.fullmatch(key):
        raise ValueError(
            "Application keys may contain letters, numbers, '.', '_' and '-' only."
        )
    return key


def _require_flatpak_id(value: object) -> str:
    flatpak_id = _require_text(value, "Flatpak application ID")
    if not _FLATPAK_ID_RE.fullmatch(flatpak_id):
        raise ValueError("Expected a complete Flatpak application ID.")
    return flatpak_id


def _normalize_entry(
    *,
    app_type: object,
    name: object,
    executable: object = None,
    flatpak_id: object = None,
    snap_instance: object = None,
) -> dict[str, str]:
    app_type = _require_text(app_type, "application type", maximum=16).lower()
    name = _require_text(name, "application name")

    if app_type == "native":
        executable = _require_text(executable, "native executable", maximum=4096)
        return {"name": name, "type": "native", "executable": executable}

    if app_type == "flatpak":
        return {
            "name": name,
            "type": "flatpak",
            "flatpak_id": _require_flatpak_id(flatpak_id),
        }

    if app_type == 'snap':
        from sentinel.guardian_apps.desktop import SNAP_INSTANCE_RE
        instance = _require_text(snap_instance, 'Snap instance', maximum=128)
        if not SNAP_INSTANCE_RE.fullmatch(instance):
            raise ValueError('Expected a complete Snap instance name.')
        return {'name': name, 'type': 'snap', 'snap_instance': instance}

    raise ValueError("Application type must be 'native', 'flatpak' or 'snap'.")


def _validate_apps(data: object) -> dict[str, dict[str, str]]:
    if not isinstance(data, dict):
        raise ValueError("blocked_apps.toml must contain a TOML table.")

    raw_apps = data.get("apps", {})
    if not isinstance(raw_apps, dict):
        raise ValueError("blocked_apps.toml [apps] must be a TOML table.")

    apps: dict[str, dict[str, str]] = {}
    for raw_key, raw_entry in raw_apps.items():
        key = _require_key(raw_key)
        if not isinstance(raw_entry, dict):
            raise ValueError(f"Invalid blocked application entry: {key}")
        apps[key] = _normalize_entry(
            app_type=raw_entry.get("type"),
            name=raw_entry.get("name", key),
            executable=raw_entry.get("executable"),
            flatpak_id=raw_entry.get("flatpak_id"),
            snap_instance=raw_entry.get("snap_instance"),
        )
    return apps


def _load_apps(path: Path | None = None) -> dict[str, dict[str, str]]:
    if path is None:
        path = BLOCKED_APPS_FILE
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except FileNotFoundError as exc:
        raise ValueError(f"Sentinel blocked-app policy does not exist: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid blocked-app TOML in {path}: {exc}") from exc
    return _validate_apps(data)


def _write_apps(
    apps: dict[str, dict[str, str]],
    path: Path | None = None,
) -> None:
    if path is None:
        path = BLOCKED_APPS_FILE
    rendered = tomli_w.dumps({"apps": apps})
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    temporary_path = Path(temporary_name)

    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(rendered)
            if not rendered.endswith("\n"):
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise




def _with_scheduled_apps(
    apps: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    """Merge currently-active block-window apps into durable app policy.

    Scheduled entries use daemon-generated synthetic keys and never mutate
    blocked_apps.toml.  Import lazily to avoid an operations-module cycle.
    """

    effective = dict(apps)
    try:
        from sentinel.daemon.operations import block_window_ops

        scheduled = block_window_ops.get_active_apps()
    except RuntimeError:
        # Unit tests and migration helpers may exercise app blocking before the
        # schedule store has been initialized. Production sentineld initializes
        # block windows before applying app policy.
        scheduled = {}

    for key, entry in scheduled.items():
        effective[key] = dict(entry)

    # Add last so a custom/scheduled key cannot override a baseline entry.
    effective.update(installed_browser_blocks())
    if protection_enabled():
        effective.update(installed_vm_blocks())
    return effective

def _apply_apps(
    apps: dict[str, dict[str, str]],
    *,
    force_remove_profiles: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    global _runtime_policy, _application

    effective_apps = _with_scheduled_apps(apps)
    runtime_policy, unresolved = build_process_policy(effective_apps)
    removals = set(force_remove_profiles)
    if dev_allow_firefox():
        # Explicitly unload old baseline Firefox profiles even if unrelated
        # custom app entries are unresolved. A currently desired custom or
        # scheduled Firefox block remains protected by sync_blocked_apps.
        all_baseline = installed_browser_blocks(include_dev_exempt=True)
        current_paths = {entry["executable"] for entry in installed_browser_blocks().values()}
        for entry in all_baseline.values():
            if entry["executable"] not in current_paths:
                try:
                    removals.add(managed_profile_name(entry))
                except (OSError, ValueError, subprocess.CalledProcessError):
                    pass
    apparmor = sync_blocked_apps(
        effective_apps,
        force_remove_profiles=removals,
    )

    combined_unresolved = dict(unresolved)
    for key, detail in apparmor.get("unresolved", {}).items():
        combined_unresolved.setdefault(key, detail)

    errors = apparmor.get("errors", {})
    status = "active" if not errors and not combined_unresolved else "pending"
    error = None
    if errors:
        error = "One or more AppArmor profiles could not be applied; retry scheduled."
    elif combined_unresolved:
        error = "One or more blocked applications could not be fully resolved; retry scheduled."

    _runtime_policy = ProcessBlockPolicy(
        runtime_policy.native_executables, runtime_policy.flatpak_ids | runtime_browser_flatpak_ids()
        | (VM_FLATPAK_IDS if protection_enabled() else frozenset()),
        runtime_policy.snap_instances | (VM_SNAP_INSTANCES if protection_enabled() else frozenset()),
        runtime_policy.steam_launchers, vm_tools=protection_enabled(),
    )
    _application = {
        "status": status,
        "error": error,
        "unresolved": combined_unresolved,
        "apparmor": apparmor,
        "native_executables": len(runtime_policy.native_executables),
        "flatpak_ids": len(_runtime_policy.flatpak_ids),
        "snap_instances": len(_runtime_policy.snap_instances),
        "steam_launchers": len(_runtime_policy.steam_launchers),
    }
    return application_status()


def initialize_app_blocker() -> dict[str, Any]:
    """Load protected policy and apply it before the daemon begins serving RPCs."""
    with _lock:
        apps = _load_apps()
        return _apply_apps(apps)


def reconcile_app_blocker(
    *,
    force_remove_profiles: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Re-resolve durable + scheduled apps and retry AppArmor application."""
    with _lock:
        apps = _load_apps()
        return _apply_apps(apps, force_remove_profiles=force_remove_profiles)


def refresh_process_policy() -> ProcessBlockPolicy:
    """Cheaply re-resolve runtime matches without touching AppArmor.

    The watchdog uses this between full reconciliations so a native executable
    that is installed after its block was created becomes kill-enforced within
    a few seconds rather than waiting for the 30-second AppArmor retry loop.
    """
    global _runtime_policy
    with _lock:
        apps = _load_apps()
        runtime_policy, _ = build_process_policy(_with_scheduled_apps(apps))
        _runtime_policy = ProcessBlockPolicy(
            runtime_policy.native_executables, runtime_policy.flatpak_ids | runtime_browser_flatpak_ids()
            | (VM_FLATPAK_IDS if protection_enabled() else frozenset()),
            runtime_policy.snap_instances | (VM_SNAP_INSTANCES if protection_enabled() else frozenset()),
            runtime_policy.steam_launchers, vm_tools=protection_enabled(),
        )
        return _runtime_policy


def get_process_policy() -> ProcessBlockPolicy:
    with _lock:
        # Start name-based enforcement immediately on a setting change, even
        # before full AppArmor reconciliation has finished.
        return replace(_runtime_policy, vm_tools=protection_enabled())


def application_status() -> dict[str, Any]:
    with _lock:
        return {
            **_application,
            "unresolved": dict(_application.get("unresolved", {})),
            "apparmor": dict(_application.get("apparmor", {})),
        }


def list_apps() -> dict[str, Any]:
    with _lock:
        apps = _load_apps()
        return {
            "apps": [
                {"key": key, **entry}
                for key, entry in sorted(apps.items(), key=lambda item: item[0])
            ],
            "application": application_status(),
        }


def get_status() -> dict[str, Any]:
    with _lock:
        apps = _load_apps()
        return {
            "blocked_count": len(apps),
            "browser_baseline": {
                "native": list(installed_browser_blocks().values()),
                "flatpak_ids": sorted(runtime_browser_flatpak_ids()),
                "removable": False,
                "dev_allow_firefox": dev_allow_firefox(),
            },
            "application": application_status(),
        }


def add_app(
    key: object,
    *,
    app_type: object,
    name: object,
    executable: object = None,
    flatpak_id: object = None,
    snap_instance: object = None,
    guardian_authenticated: bool = False,
) -> dict[str, Any]:
    key = _require_key(key)
    entry = _normalize_entry(
        app_type=app_type,
        name=name,
        executable=executable,
        flatpak_id=flatpak_id,
        snap_instance=snap_instance,
    )

    with _lock:
        apps = _load_apps()
        existing = apps.get(key)
        if existing is not None and existing != entry:
            raise ValueError(
                f"Blocked application '{key}' already exists with different settings; "
                "remove it before replacing it."
            )

        change = ChangeKind.MAINTAIN if existing == entry else ChangeKind.STRENGTHEN
        require_policy(
            "appblock.add",
            change,
            guardian_authenticated=guardian_authenticated,
        )

        if existing is None:
            apps[key] = entry
            _write_apps(apps)

        application = _apply_apps(apps)
        return {
            "status": "complete",
            "change": change.value,
            "app": {"key": key, **entry},
            "application": application,
        }


def remove_app(
    key: object,
    *,
    guardian_authenticated: bool = False,
) -> dict[str, Any]:
    key = _require_key(key)

    with _lock:
        apps = _load_apps()
        existing = apps.get(key)
        change = ChangeKind.WEAKEN if existing is not None else ChangeKind.MAINTAIN
        require_policy(
            "appblock.remove",
            change,
            guardian_authenticated=guardian_authenticated,
        )

        force_remove_profiles: set[str] = set()
        if existing is not None:
            # Resolve the exact managed profile while the old entry is still
            # available. This lets reconciliation remove this application's
            # AppArmor profile even if some unrelated configured app is
            # temporarily unresolved.
            try:
                force_remove_profiles.add(managed_profile_name(existing))
            except (OSError, ValueError, subprocess.CalledProcessError):
                # The normal reconciliation path remains fail-closed if this
                # particular target cannot currently be resolved.
                pass

            del apps[key]
            _write_apps(apps)

        application = _apply_apps(
            apps,
            force_remove_profiles=force_remove_profiles,
        )
        return {
            "status": "complete",
            "change": change.value,
            "key": key,
            "removed": existing is not None,
            "application": application,
        }


def sync(*, guardian_authenticated: bool = False) -> dict[str, Any]:
    require_policy(
        "appblock.sync",
        ChangeKind.MAINTAIN,
        guardian_authenticated=guardian_authenticated,
    )
    return {"status": "complete", "application": reconcile_app_blocker()}
