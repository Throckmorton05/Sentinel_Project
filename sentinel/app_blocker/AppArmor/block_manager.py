from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Mapping

from sentinel.settings.paths import PROFILE_DIR
from sentinel.app_blocker.resolver import resolve_executable, resolve_flatpak_executable
from sentinel.app_blocker.AppArmor.blocker import (
    block_app,
    executable_to_profile_name,
    get_profile_path,
)
from sentinel.app_blocker.AppArmor.profile_loader import load_profile, unload_profile
from sentinel.app_blocker.AppArmor.profile_generator import legacy_blocked_executable


LEGACY_PROFILE_DIR = Path("/etc/apparmor.d")


def _migrate_legacy_profiles() -> tuple[int, dict[str, str]]:
    """Unload and remove app-block profiles created by pre-sentineld Sentinel.

    Older Sentinel releases wrote deny-all profiles directly into
    /etc/apparmor.d. Those files are outside the current PROFILE_DIR, so a
    policy removal could otherwise leave an application blocked forever.

    Detection intentionally requires both the exact old Sentinel profile shape
    and the exact filename Sentinel generated from the attached executable.
    Distribution/user AppArmor profiles with additional policy are ignored.
    """

    migrated = 0
    errors: dict[str, str] = {}
    try:
        candidates = list(LEGACY_PROFILE_DIR.iterdir())
    except FileNotFoundError:
        return migrated, errors
    except OSError as exc:
        return migrated, {str(LEGACY_PROFILE_DIR): str(exc)}

    for profile_path in candidates:
        if not profile_path.is_file():
            continue

        try:
            profile_text = profile_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue

        executable = legacy_blocked_executable(profile_text)
        if executable is None:
            continue
        if profile_path.name != executable_to_profile_name(executable):
            continue

        managed_profile_path = PROFILE_DIR / profile_path.name
        try:
            if managed_profile_path.is_file():
                # Both files describe the same AppArmor attachment/profile
                # identity. Do not unload through the legacy copy first: that
                # would also unload the current managed profile and make the
                # normal reconciliation unload fail immediately afterward.
                profile_path.unlink()
            else:
                unload_profile(profile_path)
                profile_path.unlink()
            migrated += 1
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            errors[str(profile_path)] = str(exc)

    return migrated, errors


def _resolve_profile_executable(app_data: Mapping[str, Any]) -> str:
    app_type = app_data.get("type")

    if app_type == "native":
        executable = app_data.get("executable")
        if not isinstance(executable, str) or not executable:
            raise ValueError("Native app missing executable")
        return resolve_executable(executable)

    if app_type == "flatpak":
        flatpak_id = app_data.get("flatpak_id")
        if not isinstance(flatpak_id, str) or not flatpak_id:
            raise ValueError("Flatpak app missing flatpak_id")
        return resolve_flatpak_executable(flatpak_id)

    raise ValueError(f"Unsupported app type: {app_type}")


def managed_profile_name(app_data: Mapping[str, Any]) -> str:
    """Return the managed AppArmor filename for a configured application."""
    full_path = _resolve_profile_executable(app_data)
    return get_profile_path(full_path).name


def block_application(app_data: Mapping[str, Any]) -> Path:
    full_path = _resolve_profile_executable(app_data)
    profile_path = block_app(full_path)
    try:
        load_profile(profile_path)
    except Exception:
        # A profile file is our retry marker. Do not leave one behind if the
        # kernel rejected it, otherwise a later reconciliation would mistake
        # the file for an active profile and skip the retry.
        profile_path.unlink(missing_ok=True)
        raise
    return profile_path


def sync_blocked_apps(
    apps: Mapping[str, Mapping[str, Any]],
    *,
    force_remove_profiles: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Synchronize AppArmor profiles with daemon-owned application policy.

    Every resolved desired profile is reloaded on reconciliation. Profile files
    persist across reboot but kernel AppArmor state does not, so treating a file
    on disk as proof that the profile is active would silently disable the
    AppArmor layer after a reboot.

    Resolution/application errors are reported per app instead of discarding
    the protected policy. If any configured app is temporarily unresolved,
    existing extra profiles are retained and reloaded rather than risk
    weakening protection because of a transient resolver failure.
    """

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    # One-time/upgrade-safe migration: old Sentinel releases placed generated
    # deny-all profiles directly in /etc/apparmor.d.  Remove only files that
    # exactly match that legacy Sentinel template before reconciling the new
    # daemon-owned profile directory.
    migrated_legacy_profiles, migration_errors = _migrate_legacy_profiles()

    desired_profiles: set[str] = set()
    profile_to_app: dict[str, Mapping[str, Any]] = {}
    unresolved: dict[str, str] = {}
    process_only: dict[str, str] = {}

    for key, app_data in apps.items():
        if app_data.get('type') == 'snap':
            # Snap already owns its confinement profiles. Runtime enforcement
            # uses its kernel label/mount identity across revision updates.
            process_only[key] = 'Snap instance blocking uses the daemon process watchdog.'
            continue
        try:
            full_path = _resolve_profile_executable(app_data)
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            unresolved[key] = str(exc)
            continue

        profile_path = get_profile_path(full_path)
        desired_profiles.add(profile_path.name)
        profile_to_app[profile_path.name] = app_data

    current_profiles = {
        profile.name for profile in PROFILE_DIR.iterdir() if profile.is_file()
    }

    errors: dict[str, str] = dict(migration_errors)

    extra_profiles = current_profiles - desired_profiles

    # A user-authorized removal is stronger evidence than an unrelated resolver
    # failure. If remove_app() tells us exactly which managed profile belongs to
    # the removed entry, unload that stale profile even when another configured
    # application is temporarily unresolved. Without this exception, one broken
    # Flatpak resolver could keep an entirely unrelated removed app blocked.
    forced_extra_profiles = extra_profiles & set(force_remove_profiles)
    for profile_name in forced_extra_profiles:
        profile_path = PROFILE_DIR / profile_name
        try:
            unload_profile(profile_path)
            profile_path.unlink(missing_ok=True)
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            errors[profile_name] = str(exc)

    remaining_extra_profiles = extra_profiles - forced_extra_profiles
    if unresolved:
        # We cannot prove which remaining stale-looking profile belongs to an
        # unresolved configured app. Keep and reload those until resolution
        # works, but never resurrect an explicitly removed application's profile.
        for profile_name in remaining_extra_profiles:
            profile_path = PROFILE_DIR / profile_name
            try:
                load_profile(profile_path)
            except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
                errors[profile_name] = str(exc)
    else:
        for profile_name in remaining_extra_profiles:
            profile_path = PROFILE_DIR / profile_name
            try:
                unload_profile(profile_path)
                profile_path.unlink(missing_ok=True)
            except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
                errors[profile_name] = str(exc)

    # Always regenerate + reload desired profiles. This restores kernel policy
    # after boot and makes an AppArmor parser failure retryable.
    for profile_name in desired_profiles:
        try:
            block_application(profile_to_app[profile_name])
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            errors[profile_name] = str(exc)

    return {
        "desired_profiles": len(desired_profiles),
        "migrated_legacy_profiles": migrated_legacy_profiles,
        "active_profiles": len(
            [profile for profile in PROFILE_DIR.iterdir() if profile.is_file()]
        ),
        "unresolved": unresolved,
        "process_only": process_only,
        "errors": errors,
    }
