"""
Policy-enforced Sentinel settings operations.

Normal Sentinel settings are registered in SETTING_DEFINITIONS and are
accessed through get_setting() and set_setting().

Clients request a desired value. sentineld itself determines whether the
change strengthens or weakens Sentinel and applies lock-state policy
before writing anything.
"""

from __future__ import annotations

import os
import hashlib
import json
import re
import tempfile
import threading
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import tomli_w

from sentinel.daemon.security.policy import (
    ChangeKind,
    classify_boolean_change,
    require_policy,
)
from sentinel.settings.paths import SETTINGS_FILE


_settings_lock = threading.RLock()


class SettingKind(str, Enum):
    """
    Describes how a setting affects Sentinel protection.
    """

    BOOLEAN_PROTECTION = "boolean_protection"


@dataclass(frozen=True)
class SettingDefinition:
    """
    Definition of one setting managed by sentineld.
    """

    keys: tuple[str, ...]
    kind: SettingKind
    default: Any


SETTING_DEFINITIONS: dict[str, SettingDefinition] = {
    "image_filtering": SettingDefinition(
        keys=("image_filtering",), kind=SettingKind.BOOLEAN_PROTECTION, default=True,
    ),
    "boot_media_protection": SettingDefinition(
        keys=("boot_media_protection",),
        kind=SettingKind.BOOLEAN_PROTECTION, default=False,
    ),
    "vm_protection": SettingDefinition(
        keys=("vm_protection",), kind=SettingKind.BOOLEAN_PROTECTION, default=False,
    ),
    "safe_installs": SettingDefinition(
        keys=("safe_installs",),
        kind=SettingKind.BOOLEAN_PROTECTION,
        default=True,
    ),
    "safe_browsing": SettingDefinition(
        keys=("safe_browsing",),
        kind=SettingKind.BOOLEAN_PROTECTION,
        default=True,
    ),
}

BROWSER_SETTINGS = ("safe_browsing", "safe_steam", "safe_crunchyroll")
for _name in BROWSER_SETTINGS:
    SETTING_DEFINITIONS[_name] = SettingDefinition(
        keys=(_name,), kind=SettingKind.BOOLEAN_PROTECTION, default=True,
    )


def browser_snapshot(settings: dict[str, Any]) -> dict[str, bool]:
    """Validate the complete browser policy before applying any part of it."""
    snapshot = {}
    for name in BROWSER_SETTINGS:
        definition = SETTING_DEFINITIONS[name]
        value = _read_nested_value(settings, definition.keys, default=definition.default)
        _validate_value(definition, value)
        snapshot[name] = value
    return snapshot

DNS_CATEGORIES = (
    "adult", "adult_heavy_sites", "gambling", "social_media",
    "unsafe_search_engines", "browsers_sans_safe_search",
)
for _name in ("enabled", *DNS_CATEGORIES):
    SETTING_DEFINITIONS[f"dns.{_name}"] = SettingDefinition(
        keys=("dns", _name), kind=SettingKind.BOOLEAN_PROTECTION,
        default=_name == "enabled",
    )

# Older installations used both spellings. They address one protection.
SETTING_DEFINITIONS["dns.browsers_sans_safesearch"] = SETTING_DEFINITIONS[
    "dns.browsers_sans_safe_search"
]

SETTING_ALIASES = {"dns.browsers_sans_safesearch": "dns.browsers_sans_safe_search"}
SETTING_DESCRIPTIONS = {
    "image_filtering": "Scan visible windows for explicit images. Starts or stops the monitor and its filtering services.",
    "boot_media_protection": "Restrict creation of bootable removable media.",
    "vm_protection": "Block KVM access and common VM tools, including QEMU, VirtualBox, VMware and Boxes. Enabling closes running VM tools; shut down your VMs first.",
    "safe_installs": "Limit software installation to approved applications.",
    "safe_browsing": "Enable managed browser safety protections.",
    "safe_steam": "Enable Safe Steam content filtering.",
    "safe_crunchyroll": "Enable Safe Crunchyroll content filtering.",
    "dns.enabled": "Enable Sentinel DNS filtering.",
    "dns.adult": "Block domains in the adult content category.",
    "dns.adult_heavy_sites": "Block sites with substantial adult content.",
    "dns.gambling": "Block domains in the gambling category.",
    "dns.social_media": "Block domains in the social media category.",
    "dns.unsafe_search_engines": "Block search engines without enforced safe search.",
    "dns.browsers_sans_safe_search": "Block browser-related domains without safe search.",
}


class SettingsRevisionConflict(ValueError):
    """The client loaded an older settings snapshot."""


def get_schema() -> dict[str, Any]:
    return {"schema_version": 1, "settings": [
        {"name": name, "type": "boolean", "default": definition.default,
         "description": SETTING_DESCRIPTIONS[name], "kind": definition.kind.value,
         "aliases": sorted(alias for alias, target in SETTING_ALIASES.items() if target == name),
         "changes": {"enable": "strengthen", "disable": "weaken"}}
        for name, definition in sorted(SETTING_DEFINITIONS.items())
        if name not in SETTING_ALIASES
    ]}


def _registered_values(settings: dict[str, Any]) -> dict[str, Any]:
    values = {}
    for name, definition in sorted(SETTING_DEFINITIONS.items()):
        if name in SETTING_ALIASES:
            continue
        value = _read_nested_value(settings, definition.keys, default=definition.default)
        _validate_value(definition, value)
        values[name] = value
    return values


def _revision(settings: dict[str, Any]) -> str:
    # Opaque content revision survives daemon restarts and notices manual edits.
    content = json.dumps(_registered_values(settings), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def get_snapshot(*, path: Path = SETTINGS_FILE) -> dict[str, Any]:
    require_policy("settings.snapshot", ChangeKind.READ)
    with _settings_lock:
        settings = _load_settings(path)
        return {"schema_version": 1, "revision": _revision(settings),
                "settings": _registered_values(settings)}


def _get_definition(
    name: str,
) -> SettingDefinition:
    """
    Return the registered definition for a Sentinel setting.
    """

    try:
        return SETTING_DEFINITIONS[name]

    except KeyError as exc:
        raise ValueError(f"Unknown Sentinel setting: {name}") from exc


def _load_settings(
    path: Path = SETTINGS_FILE,
) -> dict[str, Any]:
    """
    Load settings.toml.
    """

    try:
        with path.open("rb") as stream:
            settings = tomllib.load(stream)

    except FileNotFoundError as exc:
        raise ValueError(f"Sentinel settings file does not exist: {path}") from exc

    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid Sentinel settings TOML in {path}: {exc}") from exc

    if not isinstance(
        settings,
        dict,
    ):
        raise ValueError("Sentinel settings file must contain a TOML table.")

    dns = settings.get("dns", {})
    if not isinstance(dns, dict):
        raise ValueError("The dns setting must be a TOML table.")
    if "browsers_sans_safesearch" in dns:
        legacy = dns.pop("browsers_sans_safesearch")
        if "browsers_sans_safe_search" in dns and dns["browsers_sans_safe_search"] != legacy:
            raise ValueError("Conflicting SafeSearch browser DNS settings.")
        dns["browsers_sans_safe_search"] = legacy
    return settings


def dns_snapshot(settings: dict[str, Any]) -> dict[str, bool]:
    """One validated snapshot, supplied by the daemon to DNS enforcement."""
    snapshot = {"safe_browsing": browser_snapshot(settings)["safe_browsing"]}
    for name in ("enabled", *DNS_CATEGORIES):
        definition = SETTING_DEFINITIONS[f"dns.{name}"]
        value = _read_nested_value(settings, definition.keys, default=definition.default)
        _validate_value(definition, value)
        snapshot[name] = value
    return snapshot


def _write_settings(
    settings: dict[str, Any],
    path: Path = SETTINGS_FILE,
) -> None:
    """
    Atomically replace settings.toml.
    """

    rendered = tomli_w.dumps(settings)

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )

    temporary_path = Path(
        temporary_name,
    )

    try:
        os.fchmod(
            fd,
            0o644,
        )

        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
        ) as stream:
            stream.write(
                rendered,
            )

            if not rendered.endswith("\n"):
                stream.write("\n")

            stream.flush()
            os.fsync(
                stream.fileno(),
            )

        os.replace(
            temporary_path,
            path,
        )

        directory_fd = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY,
        )

        try:
            os.fsync(
                directory_fd,
            )

        finally:
            os.close(
                directory_fd,
            )

    except Exception:
        try:
            os.close(
                fd,
            )

        except OSError:
            pass

        temporary_path.unlink(
            missing_ok=True,
        )

        raise


def _read_nested_value(
    settings: dict[str, Any],
    keys: tuple[str, ...],
    *,
    default: Any,
) -> Any:
    """
    Read a nested setting.

    Missing settings return their registered default.
    """

    current: Any = settings

    for key in keys:
        if not isinstance(
            current,
            dict,
        ):
            raise ValueError(f"Invalid settings structure at {'.'.join(keys)}.")

        if key not in current:
            return default

        current = current[key]

    return current


def _write_nested_value(
    settings: dict[str, Any],
    keys: tuple[str, ...],
    value: Any,
) -> None:
    """
    Write a nested setting.
    """

    if not keys:
        raise ValueError("Setting path cannot be empty.")

    current = settings

    for key in keys[:-1]:
        existing = current.get(
            key,
        )

        if existing is None:
            existing = {}
            current[key] = existing

        if not isinstance(
            existing,
            dict,
        ):
            raise ValueError(f"Invalid settings structure at {'.'.join(keys)}.")

        current = existing

    current[keys[-1]] = value


def _validate_value(
    definition: SettingDefinition,
    value: Any,
) -> None:
    """
    Validate a requested setting value.
    """

    if definition.kind is SettingKind.BOOLEAN_PROTECTION:
        if type(value) is not bool:
            raise ValueError("Boolean protection settings require true or false.")

        return

    raise ValueError(f"Unsupported setting kind: {definition.kind}")


def _classify_change(
    definition: SettingDefinition,
    *,
    old: Any,
    new: Any,
) -> ChangeKind:
    """
    Determine the security meaning of a settings change.
    """

    if definition.kind is SettingKind.BOOLEAN_PROTECTION:
        if type(old) is not bool:
            raise ValueError("Stored boolean protection setting is not a boolean.")

        return classify_boolean_change(
            old=old,
            new=new,
        )

    raise ValueError(f"Unsupported setting kind: {definition.kind}")


def get_setting(
    name: str,
    *,
    path: Path = SETTINGS_FILE,
) -> Any:
    """
    Read one registered Sentinel setting.
    """

    definition = _get_definition(
        name,
    )

    with _settings_lock:
        settings = _load_settings(
            path,
        )

        value = _read_nested_value(
            settings,
            definition.keys,
            default=definition.default,
        )

        _validate_value(
            definition,
            value,
        )

        require_policy(
            f"settings.{name}.get",
            ChangeKind.READ,
        )

        return value


def set_setting(
    name: str,
    value: Any,
    *,
    guardian_authenticated: bool = False,
    expected_revision: str | None = None,
    path: Path = SETTINGS_FILE,
) -> ChangeKind:
    """
    Change one registered Sentinel setting.

    sentineld, not the client, determines whether the requested
    change strengthens or weakens protection.
    """

    definition = _get_definition(
        name,
    )

    _validate_value(
        definition,
        value,
    )

    if expected_revision is not None and (
        not isinstance(expected_revision, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_revision)
    ):
        raise ValueError("expected_revision must be a settings snapshot revision.")

    with _settings_lock:
        settings = _load_settings(
            path,
        )

        if expected_revision is not None and expected_revision != _revision(settings):
            raise SettingsRevisionConflict("Settings changed. Reload the snapshot before retrying.")

        old_value = _read_nested_value(
            settings,
            definition.keys,
            default=definition.default,
        )

        _validate_value(
            definition,
            old_value,
        )

        change = _classify_change(
            definition,
            old=old_value,
            new=value,
        )

        require_policy(
            f"settings.{name}.set",
            change,
            guardian_authenticated=guardian_authenticated,
        )

        if name == "vm_protection":
            from sentinel.daemon.operations.vm_ops import apply_vm_policy
            previous_vm_settings = {**settings, name: old_value}
            try:
                apply_vm_policy(settings={**settings, name: value})
            except (OSError, ValueError):
                # Failed changes do not commit. Try to restore the old policy;
                # if rollback also fails, reconciliation retries durable intent.
                try:
                    apply_vm_policy(settings=previous_vm_settings)
                except (OSError, ValueError) as rollback_error:
                    print(f"sentineld VM policy rollback pending: {rollback_error}", flush=True)
                raise

        if name == "boot_media_protection" and value:
            from sentinel.daemon.operations.boot_media_ops import apply_boot_media_policy
            apply_boot_media_policy(settings={**settings, name: value})

        if old_value == value:
            if name == "boot_media_protection" and not value:
                from sentinel.daemon.operations.boot_media_ops import apply_boot_media_policy
                apply_boot_media_policy(settings=settings)
            if name not in {"image_filtering", "safe_steam", "vm_protection"}:
                return change

        if old_value != value:
            _write_nested_value(
                settings,
                definition.keys,
                value,
            )

            if definition.keys[0] == "dns":
                dns_snapshot(settings)
            if name in BROWSER_SETTINGS:
                browser_snapshot(settings)

            try:
                _write_settings(settings, path)
            except OSError:
                if name == "vm_protection":
                    # A directory-fsync error can occur after atomic replace.
                    # Restore the policy actually on disk, not stale intent.
                    try:
                        durable_vm_settings = _load_settings(path)
                    except (OSError, ValueError):
                        durable_vm_settings = previous_vm_settings
                    try:
                        apply_vm_policy(settings=durable_vm_settings)
                    except (OSError, ValueError) as rollback_error:
                        print(f"sentineld VM policy rollback pending: {rollback_error}", flush=True)
                raise

    if name == "vm_protection":
        from sentinel.daemon.operations.vm_ops import reconcile_tools
        reconcile_tools()

    if name == "image_filtering":
        from sentinel.daemon.operations.image_filter_ops import apply_image_filtering
        apply_image_filtering(settings=settings)

    if name == "boot_media_protection" and not value:
        from sentinel.daemon.operations.boot_media_ops import apply_boot_media_policy
        apply_boot_media_policy(settings=settings)

    if definition.keys[0] == "dns" or name == "safe_browsing":
        # The RPC mutation lock serializes policy changes. Do not keep the
        # settings read lock across network calls: other clients still need
        # to read settings and application status during a backend outage.
        from sentinel.daemon.operations.dns_ops import apply_dns
        apply_dns(settings=settings)

    if name == "safe_steam":
        from sentinel.safe_steam.status import request_reconcile
        request_reconcile(value)

    if name in BROWSER_SETTINGS:
        from sentinel.daemon.operations.browser_ops import apply_browser_policy
        apply_browser_policy(settings=settings)

    return change
