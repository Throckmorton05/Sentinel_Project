"""Policy-enforced system Flatpak and app-store update operations.

Only the default system installation and its configured Flathub remote are
managed here. Clients cannot select a remote, scope, executable or CLI flags.
"""
from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path

from sentinel.browser_policies.restrictions import require_supported_browser_flatpak
from sentinel.vm_protection import require_allowed_flatpak
from sentinel.daemon.core.constants import APT_GET
from sentinel.daemon.core.lock_state import LockMode, get_lock_state
from sentinel.daemon.operations.package_ops import _require_package_name
from sentinel.daemon.operations.settings_ops import get_setting
from sentinel.daemon.operations.system_ops import _system_update
from sentinel.daemon.operations.apt import run_apt
from sentinel.daemon.security.policy import ChangeKind, PolicyDeniedError, require_policy
from sentinel.settings.paths import APPROVED_APPS_FILE, BLOCKED_APPS_FILE
from sentinel.safe_steam.installation import (
    UNSUPPORTED_FLATPAK_IDS, NATIVE_APP_ID, native_steam_installed,
)

FLATPAK = "/usr/bin/flatpak"
_ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*){2,}")
_REF_PART_RE = re.compile(r"[A-Za-z0-9_.-]+")


def _require_id(value: object) -> str:
    if not isinstance(value, str) or len(value) > 255 or not _ID_RE.fullmatch(value):
        raise ValueError("Expected a complete Flatpak application ID.")
    return value


def _require_ref(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected a complete installed Flatpak ref.")
    parts = value.split("/")
    if (len(parts) != 4 or parts[0] not in ("app", "runtime")
            or any(part in (".", "..") or not _REF_PART_RE.fullmatch(part) for part in parts[2:])):
        raise ValueError("Expected a complete installed Flatpak ref.")
    _require_id(parts[1])
    return value


def _cli_ref(value: str, *, kind: str) -> str:
    """Normalize CLI display refs; RPC callers still require complete refs.

    List/remote-ls can omit the app/runtime prefix from their ref column.
    The caller supplies the kind from its explicit --app/--runtime query.
    """
    value = value.strip()
    if len(value.split("/")) == 3:
        value = f"{kind}/{value}"
    ref = _require_ref(value)
    if ref.split("/", 1)[0] != kind:
        raise ValueError("Flatpak returned a ref with an unexpected kind.")
    return ref


def _cli_entries(output: str, *, kind: str) -> list[dict]:
    entries = []
    for line in output.splitlines():
        fields = line.split("\t")
        if not fields[0].strip():
            continue
        ref = _cli_ref(fields[0], kind=kind)
        entries.append({"ref": ref, "id": ref.split("/")[1],
                        "name": fields[1].strip() if len(fields) > 1 else ref.split("/")[1]})
    return entries


def _run(args: list[str], *, timeout: float = 3600) -> str:
    result = subprocess.run(
        args, check=True, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "LC_ALL": "C"},
    )
    return result.stdout


def _catalog_ids(path: Path, *, enabled_only: bool = False) -> set[str]:
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    apps = data.get("apps", {})
    if not isinstance(apps, dict):
        raise ValueError(f"Invalid application policy in {path}.")
    result = set()
    for entry in apps.values():
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid application policy in {path}.")
        if enabled_only and entry.get("enabled") is not True:
            continue
        if "flatpak_id" in entry:
            result.add(_require_id(entry["flatpak_id"]))
    return result


def get_appstore_policy() -> dict:
    mode = get_lock_state().mode
    return {
        "mode": mode.value,
        "approved_only": get_setting("safe_installs"),
        "can_uninstall": mode is not LockMode.SELF_LOCK,
        "guardian_required": mode is LockMode.GUARDIAN_LOCK,
    }


def install_app(app_id: object, *, guardian_authenticated: bool = False) -> None:
    app_id = _require_id(app_id)
    if app_id in UNSUPPORTED_FLATPAK_IDS:
        raise PolicyDeniedError('Flatpak Steam is unsupported. Install native Steam with sen install steam.')
    require_supported_browser_flatpak(app_id)
    require_allowed_flatpak(app_id)
    # Installing within the current settings maintains policy. Lock mode
    # governs authentication; safe_installs independently governs approval.
    require_policy("appstore.install", ChangeKind.MAINTAIN,
                   guardian_authenticated=guardian_authenticated)
    # Re-read protected settings/catalogs for every request. A stale UI or
    # a verified Guardian PIN cannot bypass an enabled safe_installs setting.
    if get_setting("safe_installs"):
        approved = _catalog_ids(APPROVED_APPS_FILE, enabled_only=True)
        blocked = _catalog_ids(BLOCKED_APPS_FILE)
        if app_id not in approved - blocked:
            raise PolicyDeniedError(
                "Safe Installs is enabled. Only enabled approved applications "
                "that are not blocked may be installed."
            )
    ref = _run([FLATPAK, "remote-info", "--system", "--show-ref", "flathub", app_id], timeout=60).strip()
    ref = _cli_ref(ref, kind="app")
    if ref.split("/")[:2] != ["app", app_id]:
        raise ValueError("Flathub did not resolve the exact requested application.")
    _run([FLATPAK, "install", "--system", "--noninteractive", "-y", "flathub", ref])


def _installed_refs(*, apps_only: bool = False) -> list[dict]:
    entries = []
    for kind in (("app",) if apps_only else ("app", "runtime")):
        output = _run([FLATPAK, "list", "--system", f"--{kind}",
                       "--columns=ref:f,name:f"], timeout=30)
        entries.extend(_cli_entries(output, kind=kind))
    return entries


def get_installed_apps() -> list[dict]:
    entries = list(_installed_refs(apps_only=True))
    if native_steam_installed():
        entries.append({"id": NATIVE_APP_ID, "name": "Steam", "installer": "native_steam"})
    return entries


def uninstall_app(app_id: object, *, guardian_authenticated: bool = False) -> None:
    app_id = _require_id(app_id)
    require_policy("appstore.uninstall", ChangeKind.WEAKEN,
                   guardian_authenticated=guardian_authenticated)
    refs = [app["ref"] for app in _installed_refs(apps_only=True) if app["id"] == app_id]
    if not refs:
        raise ValueError("This application is not installed in the system Flatpak installation.")
    _run([FLATPAK, "uninstall", "--system", "--noninteractive", "-y", "--app", *refs])


def get_updates() -> list[dict]:
    installed = {app["ref"] for app in _installed_refs()}
    updates = []
    seen = set()
    for kind in ("app", "runtime"):
        output = _run([FLATPAK, "remote-ls", "--system", "--updates", f"--{kind}",
                       "--columns=ref:f,name:f"], timeout=60)
        for entry in _cli_entries(output, kind=kind):
            ref = entry["ref"]
            if ref not in installed or ref in seen:
                continue
            seen.add(ref)
            updates.append({"id": ref, "name": entry["name"],
                            "type": "Flatpak", "description": "Flatpak update available"})
    output = _run([APT_GET, "-s", "upgrade"], timeout=60)
    for line in output.splitlines():
        if line.startswith("Inst "):
            name = _require_package_name({"name": line.split()[1]})
            updates.append({"id": name, "name": name, "type": "System",
                            "description": "System package update available"})
    return updates


def update_flatpak(ref: object = None, *, guardian_authenticated: bool = False) -> None:
    require_policy("appstore.update_flatpak", ChangeKind.MAINTAIN,
                   guardian_authenticated=guardian_authenticated)
    args = [FLATPAK, "update", "--system", "--noninteractive", "-y"]
    if ref is not None:
        ref = _require_ref(ref)
        if ref not in {entry["ref"] for entry in _installed_refs()}:
            raise ValueError("Only an installed Flatpak ref may be updated.")
        args.append(ref)
    _run(args)


def update_system_package(name: object, *, guardian_authenticated: bool = False) -> None:
    name = _require_package_name({"name": name})
    require_policy("appstore.update_system_package", ChangeKind.MAINTAIN,
                   guardian_authenticated=guardian_authenticated)
    run_apt(["install", "--only-upgrade", "--no-remove", "-y", "--", name])


def update_all(*, guardian_authenticated: bool = False) -> None:
    require_policy("appstore.update_all", ChangeKind.MAINTAIN,
                   guardian_authenticated=guardian_authenticated)
    update_flatpak(guardian_authenticated=guardian_authenticated)
    _system_update()
