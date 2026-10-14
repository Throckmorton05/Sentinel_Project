from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Any

from sentinel.app_blocker.resolver import resolve_executable


@dataclass(frozen=True)
class ProcessBlockPolicy:
    """Effective runtime process policy used by sentineld's app blocker."""

    native_executables: frozenset[str]
    flatpak_ids: frozenset[str]
    snap_instances: frozenset[str] = frozenset()
    steam_launchers: frozenset[str] = frozenset()
    vm_tools: bool = False

    @classmethod
    def empty(cls) -> "ProcessBlockPolicy":
        return cls(frozenset(), frozenset())


def build_process_policy(
    apps: Mapping[str, Mapping[str, Any]],
) -> tuple[ProcessBlockPolicy, dict[str, str]]:
    """Resolve a protected app policy into process matching rules.

    Native entries are matched by their resolved host executable path.
    Flatpak entries are matched by the FLATPAK_ID inherited by sandboxed
    processes. Uninstalled native applications remain persisted in policy and
    are reported as unresolved; sentineld's reconciliation loop will resolve
    them automatically if they are installed later.
    """

    native: set[str] = set()
    flatpaks: set[str] = set()
    snaps: set[str] = set()
    steam: set[str] = set()
    unresolved: dict[str, str] = {}

    for key, app_data in apps.items():
        app_type = app_data.get("type")

        if app_type == "native":
            executable = app_data.get("executable")
            if not isinstance(executable, str) or not executable:
                unresolved[key] = "native app is missing executable"
                continue
            try:
                resolved = resolve_executable(executable)
                native.add(resolved)
                from sentinel.guardian_apps import desktop
                path = Path(resolved).resolve()
                if desktop.is_system_steam_launcher(path):
                    steam.add(str(path))
            except (OSError, ValueError) as exc:
                unresolved[key] = str(exc)

        elif app_type == "flatpak":
            flatpak_id = app_data.get("flatpak_id")
            if not isinstance(flatpak_id, str) or not flatpak_id:
                unresolved[key] = "flatpak app is missing flatpak_id"
                continue
            flatpaks.add(flatpak_id)

        elif app_type == 'snap':
            from sentinel.guardian_apps.desktop import SNAP_INSTANCE_RE
            instance = app_data.get('snap_instance')
            if isinstance(instance, str) and SNAP_INSTANCE_RE.fullmatch(instance):
                snaps.add(instance)
            else:
                unresolved[key] = 'snap app is missing a valid snap_instance'
        else:
            unresolved[key] = f"unsupported app type: {app_type!r}"

    return ProcessBlockPolicy(frozenset(native), frozenset(flatpaks),
                              frozenset(snaps), frozenset(steam)), unresolved
