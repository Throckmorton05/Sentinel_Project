"""Persistent scheduled application/domain blocking owned by sentineld.

Block windows are temporary protection commitments.  Their own lock level is
separate from Sentinel's machine-wide lock mode:

* unlocked: freely editable/deletable.
* self_lock: may be maintained or strengthened, but not weakened before end.
* guardian_lock: every mutation requires guardian authentication.

When Sentinel itself is in Guardian Lock, newly-created windows are always
Guardian-locked.  Unlocked/Self-Locked machines may create only unlocked or
self-locked windows.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sentinel.daemon.core.lock_state import LockMode, get_lock_state
from sentinel.daemon.security.policy import PolicyDeniedError
from sentinel.settings.paths import BLOCK_WINDOWS_FILE
from sentinel.daemon.operations import recurrence as repeats


STATE_VERSION = 1
MAX_WINDOWS = 512
MAX_APPS_PER_WINDOW = 128
MAX_DOMAINS_PER_WINDOW = 512
MAX_NAME_LENGTH = 120
MAX_NOTES_LENGTH = 2000
MAX_TARGET_JSON_BYTES = 32 * 1024

_APP_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_FLATPAK_ID_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*){2,}"
)


class BlockWindowLock(str, Enum):
    UNLOCKED = "unlocked"
    SELF_LOCK = "self_lock"
    GUARDIAN_LOCK = "guardian_lock"


_lock = threading.RLock()
_current_path: Path | None = None


def _validate_parent(path: Path) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    info = path.parent.stat()
    if info.st_uid != 0:
        raise ValueError(f"{path.parent} must be owned by root.")
    if info.st_mode & 0o022:
        raise ValueError(f"{path.parent} must not be writable by group or others.")


def _default_store() -> dict[str, Any]:
    return {"version": STATE_VERSION, "revision": 0, "windows": []}


def _read_store(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"Cannot load Sentinel block windows from {path}: {exc}") from exc

    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{path} must be a regular file.")
        if info.st_uid != 0:
            raise ValueError(f"{path} must be owned by root.")
        if info.st_mode & 0o022:
            raise ValueError(f"{path} must not be writable by group or others.")
        try:
            data = json.load(stream)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {path}: {exc}") from exc

    return _validate_store(data)


def _write_store(data: dict[str, Any], path: Path) -> None:
    _validate_parent(path)
    rendered = json.dumps(data, indent=2, sort_keys=True) + "\n"
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(rendered)
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


def _require_initialized() -> Path:
    if _current_path is None:
        raise RuntimeError("Sentinel block windows have not been initialized.")
    return _current_path


def _finite_timestamp(value: object, label: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{label} must be a Unix timestamp or ISO-8601 date/time.")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"Invalid {label}.")
    return result


def _timezone_name(value: object) -> str:
    if value is None:
        return "UTC"
    if not isinstance(value, str):
        raise ValueError("timezone must be an IANA timezone name such as America/Chicago.")
    value = value.strip()
    if not value or len(value) > 128:
        raise ValueError("Invalid timezone.")
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {value}") from exc
    return value


def _parse_instant(value: object, *, timezone_name: str, label: str) -> float:
    if type(value) in (int, float):
        return _finite_timestamp(value, label)
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a Unix timestamp or ISO-8601 date/time.")
    text = value.strip()
    if not text:
        raise ValueError(f"Missing {label}.")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Invalid {label}; expected ISO-8601 date/time.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    result = parsed.timestamp()
    return _finite_timestamp(result, label)


def _iso_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_local(timestamp: float, timezone_name: str) -> str:
    return datetime.fromtimestamp(timestamp, tz=ZoneInfo(timezone_name)).isoformat()


def _require_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Block window name is required.")
    value = value.strip()
    if not value or len(value) > MAX_NAME_LENGTH or "\x00" in value or "\n" in value:
        raise ValueError(f"Block window name must be 1-{MAX_NAME_LENGTH} characters.")
    return value


def _notes(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > MAX_NOTES_LENGTH or "\x00" in value:
        raise ValueError(f"notes must be at most {MAX_NOTES_LENGTH} characters.")
    return value.strip()


def _enabled(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("enabled must be true or false.")
    return value


def _normalize_domain(value: object) -> str:
    from sentinel.dns.dns_parser import normalize_domain

    if not isinstance(value, str) or len(value) > 253:
        raise ValueError("Supply a domain name, such as example.com.")
    value = value.strip().lower().rstrip(".")
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Invalid domain name.") from exc
    if any(char in value for char in "/:*^|@$#"):
        raise ValueError("Use a domain name without a URL, wildcard, or filter syntax.")
    domain = normalize_domain(value)
    if domain is None:
        raise ValueError("Invalid domain name.")
    return domain


def _app_text(value: object, label: str, *, maximum: int = 255) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Missing or invalid {label}.")
    value = value.strip()
    if not value or len(value) > maximum or "\x00" in value or "\n" in value:
        raise ValueError(f"Missing or invalid {label}.")
    return value


def _normalize_app(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError("Each scheduled application must be an object.")

    key = _app_text(raw.get("key"), "application key", maximum=128)
    if not _APP_KEY_RE.fullmatch(key):
        raise ValueError(
            "Application keys may contain letters, numbers, '.', '_' and '-' only."
        )

    app_type = _app_text(raw.get("type"), "application type", maximum=16).lower()
    name = _app_text(raw.get("name", key), "application name")

    if app_type == "native":
        executable = _app_text(raw.get("executable"), "native executable", maximum=4096)
        return {"key": key, "name": name, "type": "native", "executable": executable}

    if app_type == "flatpak":
        flatpak_id = _app_text(raw.get("flatpak_id"), "Flatpak application ID")
        if not _FLATPAK_ID_RE.fullmatch(flatpak_id):
            raise ValueError("Expected a complete Flatpak application ID.")
        return {"key": key, "name": name, "type": "flatpak", "flatpak_id": flatpak_id}

    if app_type == 'snap':
        from sentinel.guardian_apps.desktop import SNAP_INSTANCE_RE
        instance = _app_text(raw.get('snap_instance'), 'Snap instance', maximum=128)
        if not SNAP_INSTANCE_RE.fullmatch(instance):
            raise ValueError('Expected a complete Snap instance name.')
        return {'key': key, 'name': name, 'type': 'snap', 'snap_instance': instance}

    raise ValueError("Application type must be 'native', 'flatpak' or 'snap'.")


def _normalize_apps(value: object) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("apps must be an array.")
    if len(value) > MAX_APPS_PER_WINDOW:
        raise ValueError(f"A block window may contain at most {MAX_APPS_PER_WINDOW} apps.")

    apps: dict[str, dict[str, str]] = {}
    for raw in value:
        normalized = _normalize_app(raw)
        key = normalized["key"]
        existing = apps.get(key)
        entry = {k: v for k, v in normalized.items() if k != "key"}
        if existing is not None and existing != entry:
            raise ValueError(f"Duplicate scheduled application key with different settings: {key}")
        apps[key] = entry

    return [{"key": key, **apps[key]} for key in sorted(apps)]


def _normalize_domains(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("domains must be an array.")
    if len(value) > MAX_DOMAINS_PER_WINDOW:
        raise ValueError(f"A block window may contain at most {MAX_DOMAINS_PER_WINDOW} domains.")
    return sorted({_normalize_domain(domain) for domain in value})


def _validate_target_payload(
    apps: list[dict[str, str]],
    domains: list[str],
) -> None:
    encoded = json.dumps(
        {"apps": apps, "domains": domains},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(encoded) > MAX_TARGET_JSON_BYTES:
        raise ValueError(
            f"Block-window targets are too large; keep the target payload under {MAX_TARGET_JSON_BYTES} bytes."
        )


def _normalize_lock(value: object) -> BlockWindowLock:
    if not isinstance(value, str):
        raise ValueError("lock_type must be 'unlocked' or 'self_lock'.")
    try:
        return BlockWindowLock(value)
    except ValueError as exc:
        raise ValueError(f"Invalid block-window lock type: {value!r}.") from exc


def _validate_window(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Invalid block-window entry.")

    window_id = raw.get("id")
    if not isinstance(window_id, str):
        raise ValueError("Block window is missing an ID.")
    try:
        uuid.UUID(window_id)
    except ValueError as exc:
        raise ValueError(f"Invalid block-window ID: {window_id!r}.") from exc

    timezone_name = _timezone_name(raw.get("timezone", "UTC"))
    start_at = _finite_timestamp(raw.get("start_at"), "start_at")
    end_at = _finite_timestamp(raw.get("end_at"), "end_at")
    if end_at <= start_at:
        raise ValueError("Block window end_at must be later than start_at.")

    lock_type = _normalize_lock(raw.get("lock_type"))
    created_under_mode = raw.get("created_under_mode")
    try:
        created_mode = LockMode(created_under_mode)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid created_under_mode in block-window state.") from exc

    created_at = _finite_timestamp(raw.get("created_at"), "created_at")
    updated_at = _finite_timestamp(raw.get("updated_at"), "updated_at")
    actor_uid = raw.get("created_by_uid")
    if type(actor_uid) is not int or actor_uid < 0:
        raise ValueError("Invalid created_by_uid in block-window state.")

    apps = _normalize_apps(raw.get("apps", []))
    domains = _normalize_domains(raw.get("domains", []))
    _validate_target_payload(apps, domains)
    block_type = raw.get('block_type', 'targets')
    if block_type not in ('targets', 'screen_time'):
        raise ValueError('block_type must be targets or screen_time.')
    if block_type == 'screen_time' and (apps or domains):
        raise ValueError('Screen time windows block the desktop, without app or domain targets.')
    if block_type == 'targets' and not apps and not domains:
        raise ValueError("A block window must block at least one app or domain.")
    repeats.validate(raw)
    screen_state = raw.get('screen_state', {})
    if not isinstance(screen_state, dict) or set(screen_state) - {'extension_start', 'grace_until', 'bypass_start'}:
        raise ValueError('Invalid screen time bypass state.')
    screen_state = {key: _finite_timestamp(value, key) for key, value in screen_state.items()}
    return {
        "id": window_id,
        "name": _require_name(raw.get("name")),
        "notes": _notes(raw.get("notes", "")),
        "enabled": _enabled(raw.get("enabled", True)),
        "lock_type": lock_type.value,
        "created_under_mode": created_mode.value,
        "created_by_uid": actor_uid,
        "created_at": created_at,
        "updated_at": updated_at,
        "start_at": start_at,
        "end_at": end_at,
        "timezone": timezone_name,
        "apps": apps,
        "domains": domains,
        "block_type": block_type,
        "recurrence": raw.get('recurrence', 'none'),
        "screen_state": screen_state,
    }


def _validate_store(data: object) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Sentinel block-window state must be a JSON object.")
    if data.get("version") != STATE_VERSION:
        raise ValueError(f"Unsupported block-window state version: {data.get('version')!r}.")
    revision = data.get("revision")
    if type(revision) is not int or revision < 0:
        raise ValueError("Invalid block-window revision.")
    raw_windows = data.get("windows")
    if not isinstance(raw_windows, list) or len(raw_windows) > MAX_WINDOWS:
        raise ValueError("Invalid block-window list.")

    windows = [_validate_window(item) for item in raw_windows]
    ids = [item["id"] for item in windows]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate block-window IDs in state.")

    return {"version": STATE_VERSION, "revision": revision, "windows": windows}


def initialize_block_windows(path: Path = BLOCK_WINDOWS_FILE) -> dict[str, Any]:
    global _current_path
    with _lock:
        _validate_parent(path)
        if path.exists():
            data = _read_store(path)
        else:
            data = _default_store()
            _write_store(data, path)
        _current_path = path
        return {"version": data["version"], "revision": data["revision"], "count": len(data["windows"])}


def _load_current() -> tuple[dict[str, Any], Path]:
    path = _require_initialized()
    return _read_store(path), path


def _window_state(window: dict[str, Any], now: float) -> str:
    if not window["enabled"]:
        return "disabled"
    window = repeats.current_or_next(window, now)
    if now < window["start_at"]:
        return "scheduled"
    if now < window["end_at"]:
        return "active"
    return "ended"


def _allowed_creation_locks(mode: LockMode) -> list[str]:
    if mode is LockMode.GUARDIAN_LOCK:
        return [BlockWindowLock.GUARDIAN_LOCK.value]
    return [BlockWindowLock.UNLOCKED.value, BlockWindowLock.SELF_LOCK.value]


def get_policy_info() -> dict[str, Any]:
    mode = get_lock_state().mode
    return {
        "system_lock_mode": mode.value,
        'block_types': ['targets', 'screen_time'],
        'recurrences': ['none', 'daily', 'weekly', 'monthly'],
        'screen_time': {'self_lock_extension_seconds': 120,
                        'extension_allows_schedule_disable': True,
                        'guardian_bypass_scope': 'current_occurrence'},
        "creation": {
            "allowed_lock_types": _allowed_creation_locks(mode),
            "forced_lock_type": (
                BlockWindowLock.GUARDIAN_LOCK.value
                if mode is LockMode.GUARDIAN_LOCK
                else None
            ),
            "guardian_pin_required": mode is LockMode.GUARDIAN_LOCK,
        },
        "lock_types": {
            BlockWindowLock.UNLOCKED.value: {
                "label": "Unlocked",
                "description": "The block window may be edited, disabled, or deleted freely.",
            },
            BlockWindowLock.SELF_LOCK.value: {
                "label": "Self Lock",
                "description": "Until the block window ends it may only be maintained or strengthened.",
            },
            BlockWindowLock.GUARDIAN_LOCK.value: {
                "label": "Guardian Lock",
                "description": "Mutations require Guardian authentication.",
            },
        },
        "limits": {
            "max_windows": MAX_WINDOWS,
            "max_apps_per_window": MAX_APPS_PER_WINDOW,
            "max_domains_per_window": MAX_DOMAINS_PER_WINDOW,
            "max_target_payload_bytes": MAX_TARGET_JSON_BYTES,
            "calendar_page_limit": 25,
            'recurring_calendar_range_days': 366,
        },
    }


def _permissions(window: dict[str, Any], now: float) -> dict[str, Any]:
    system_mode = get_lock_state().mode
    lock_type = BlockWindowLock(window["lock_type"])
    commitment_open = now < window["end_at"] or window.get('recurrence', 'none') != 'none'
    grace_open = _screen_grace_open(window, now)

    guardian_required = (
        system_mode is LockMode.GUARDIAN_LOCK
        or lock_type is BlockWindowLock.GUARDIAN_LOCK
    )

    if guardian_required:
        can_weaken_without_pin = False
        can_delete_without_pin = False
        can_edit_without_pin = False
    elif lock_type is BlockWindowLock.SELF_LOCK and commitment_open and not grace_open:
        can_weaken_without_pin = False
        can_delete_without_pin = False
        can_edit_without_pin = True
    else:
        can_weaken_without_pin = True
        can_delete_without_pin = True
        can_edit_without_pin = True

    return {
        "guardian_pin_required": guardian_required,
        "can_edit_without_pin": can_edit_without_pin,
        "can_delete_without_pin": can_delete_without_pin,
        "can_weaken_without_pin": can_weaken_without_pin,
        "can_strengthen_without_pin": not guardian_required,
        "self_lock_commitment_open": (
            lock_type is BlockWindowLock.SELF_LOCK and commitment_open and not grace_open
        ),
        "screen_time_disable_grace": grace_open and not guardian_required,
        "allowed_lock_types_now": _allowed_creation_locks(system_mode),
    }


def _screen_grace_open(window: dict[str, Any], now: float) -> bool:
    if window.get('block_type') != 'screen_time' or not window['enabled']:
        return False
    current = repeats.current_or_next(window, now)
    state = window.get('screen_state', {})
    return (current['start_at'] <= now < current['end_at']
            and state.get('extension_start') == current['start_at']
            and now < state.get('grace_until', 0))


def get_screen_time_status() -> dict[str, Any]:
    """All concurrent desktop blocks; grace/bypass never affects other windows."""
    with _lock:
        data, _ = _load_current()
        now = time.time()
        system_mode = get_lock_state().mode
        windows = []
        for schedule in data['windows']:
            if schedule.get('block_type') != 'screen_time' or _window_state(schedule, now) != 'active':
                continue
            current = repeats.current_or_next(schedule, now)
            state = schedule.get('screen_state', {})
            guardian = (system_mode is LockMode.GUARDIAN_LOCK
                        or schedule['lock_type'] == 'guardian_lock')
            mode = ('guardian_lock' if guardian else 'self_lock'
                    if system_mode is LockMode.SELF_LOCK or schedule['lock_type'] == 'self_lock'
                    else 'unlocked')
            bypassed = state.get('bypass_start') == current['start_at']
            grace = _screen_grace_open(schedule, now) and not guardian
            windows.append({
                'id': schedule['id'], 'name': schedule['name'], 'mode': mode,
                'end_at': current['end_at'], 'end_local': _iso_local(current['end_at'], current['timezone']),
                'blocking': not bypassed and not grace,
                'grace_until': state.get('grace_until', 0) if grace else 0,
                'extension_available': mode == 'self_lock' and state.get('extension_start') != current['start_at'],
            })
        windows.sort(key=lambda item: (not item['blocking'], item['end_at'], item['id']))
        from sentinel.daemon.operations.daily_screen_time_ops import overlay_window
        daily = overlay_window()
        if daily is not None:
            # Always include the allowance even when 25 scheduled blocks exist.
            windows.insert(0, daily)
        return {'blocking': any(item['blocking'] for item in windows), 'windows': windows[:25],
                'window_count': len(windows), 'windows_truncated': len(windows) > 25,
                'revision': data['revision']}


def bypass_screen_time(window_id: object, *, guardian_authenticated: bool = False) -> dict[str, Any]:
    from sentinel.daemon.operations import daily_screen_time_ops
    if window_id == daily_screen_time_ops.LIMIT_ID:
        daily_screen_time_ops.bypass(guardian_authenticated=guardian_authenticated)
        return get_screen_time_status()
    with _lock:
        data, path = _load_current()
        window = next((item for item in data['windows'] if item['id'] == window_id), None)
        now = time.time()
        if window is None or window.get('block_type') != 'screen_time' or _window_state(window, now) != 'active':
            raise ValueError('No active screen time window with this ID.')
        current = repeats.current_or_next(window, now)
        state = window.setdefault('screen_state', {})
        system_mode = get_lock_state().mode
        if system_mode is LockMode.GUARDIAN_LOCK or window['lock_type'] == 'guardian_lock':
            if not guardian_authenticated:
                raise PolicyDeniedError('Screen time bypass requires the Guardian PIN.')
            state['bypass_start'] = current['start_at']
        elif system_mode is LockMode.SELF_LOCK or window['lock_type'] == 'self_lock':
            if state.get('extension_start') == current['start_at']:
                raise PolicyDeniedError('The two-minute extension has already been used for this occurrence.')
            state['extension_start'] = current['start_at']
            state['grace_until'] = min(current['end_at'], now + 120)
        else:
            state['bypass_start'] = current['start_at']
        window['updated_at'] = now
        data['revision'] += 1
        _write_store(data, path)
    return get_screen_time_status()


def _present(
    window: dict[str, Any],
    *,
    now: float,
    include_targets: bool = True,
    schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    is_schedule = schedule is None
    schedule = schedule or window
    state = ('disabled' if not window['enabled'] else 'scheduled' if now < window['start_at']
             else 'active' if now < window['end_at'] else 'ended')
    timezone_name = window["timezone"]
    active = state == "active"
    next_transition = None
    if is_schedule:
        state = _window_state(schedule, now)
    transition_window = repeats.current_or_next(schedule, now) if is_schedule else window
    if window["enabled"]:
        if now < transition_window["start_at"]:
            next_transition = transition_window["start_at"]
        elif now < transition_window["end_at"]:
            next_transition = transition_window["end_at"]

    return {
        "id": window["id"],
        "kind": 'one_time' if schedule.get('recurrence', 'none') == 'none' else 'recurring',
        "block_type": schedule.get('block_type', 'targets'),
        "recurrence": schedule.get('recurrence', 'none'),
        "occurrence_id": f"{window['id']}:{window['start_at']}",
        "schedule_start_at": schedule['start_at'],
        "schedule_end_at": schedule['end_at'],
        "name": window["name"],
        "notes": window["notes"],
        "enabled": window["enabled"],
        "lock_type": window["lock_type"],
        "created_under_mode": window["created_under_mode"],
        "created_by_uid": window["created_by_uid"],
        "created_at": window["created_at"],
        "created_at_iso": _iso_utc(window["created_at"]),
        "updated_at": window["updated_at"],
        "updated_at_iso": _iso_utc(window["updated_at"]),
        "timezone": timezone_name,
        "start_at": window["start_at"],
        "start_iso": _iso_utc(window["start_at"]),
        "start_local": _iso_local(window["start_at"], timezone_name),
        "end_at": window["end_at"],
        "end_iso": _iso_utc(window["end_at"]),
        "end_local": _iso_local(window["end_at"], timezone_name),
        "duration_seconds": window["end_at"] - window["start_at"],
        "state": state,
        "active": active,
        "seconds_until_start": max(0, int(transition_window["start_at"] - now)),
        "seconds_until_end": max(0, int(transition_window["end_at"] - now)),
        "next_transition_at": next_transition,
        "next_transition_iso": _iso_utc(next_transition) if next_transition is not None else None,
        "targets": {
            **({
                "apps": [dict(app) for app in window["apps"]],
                "domains": list(window["domains"]),
            } if include_targets else {}),
            "app_count": len(window["apps"]),
            "domain_count": len(window["domains"]),
            "total_count": len(window["apps"]) + len(window["domains"]),
            "included": include_targets,
        },
        "calendar": {
            "all_day": False,
            "start": _iso_local(window["start_at"], timezone_name),
            "end": _iso_local(window["end_at"], timezone_name),
            "timezone": timezone_name,
            "status": state,
        },
        "permissions": _permissions(schedule, now),
    }


def _raw_effective(data: dict[str, Any], now: float) -> dict[str, Any]:
    active_windows = [
        repeats.current_or_next(item, now) for item in data["windows"] if _window_state(item, now) == "active"
    ]
    domains: set[str] = set()
    apps: dict[str, dict[str, str]] = {}
    next_transitions: list[float] = []

    for window in data["windows"]:
        window = repeats.current_or_next(window, now)
        if window["enabled"]:
            if now < window["start_at"]:
                next_transitions.append(window["start_at"])
            elif now < window["end_at"]:
                next_transitions.append(window["end_at"])

    for window in active_windows:
        domains.update(window["domains"])
        window_prefix = window["id"].replace("-", "")
        for app in window["apps"]:
            synthetic_key = f"window_{window_prefix}_{app['key']}"
            apps[synthetic_key] = {k: v for k, v in app.items() if k != "key"}

    return {
        "active_window_ids": tuple(sorted(item["id"] for item in active_windows)),
        "domains": frozenset(domains),
        "apps": apps,
        "next_transition_at": min(next_transitions) if next_transitions else None,
    }


def get_active_domains() -> frozenset[str]:
    with _lock:
        data, _ = _load_current()
        return _raw_effective(data, time.time())["domains"]


def get_active_apps() -> dict[str, dict[str, str]]:
    with _lock:
        data, _ = _load_current()
        return dict(_raw_effective(data, time.time())["apps"])


def get_effective_policy() -> dict[str, Any]:
    """Return a bounded, GUI-safe summary of schedule policy active now."""

    with _lock:
        data, _ = _load_current()
        now = time.time()
        effective = _raw_effective(data, now)
        all_domains = sorted(effective["domains"])
        all_apps = [
            {"key": key, **entry}
            for key, entry in sorted(effective["apps"].items())
        ]
        preview_limit = 100
        return {
            "generated_at": now,
            "generated_at_iso": _iso_utc(now),
            "active_window_ids": list(effective["active_window_ids"]),
            "active_window_count": len(effective["active_window_ids"]),
            "domains": all_domains[:preview_limit],
            "domain_count": len(all_domains),
            "apps": all_apps[:preview_limit],
            "app_count": len(all_apps),
            "targets_truncated": (
                len(all_domains) > preview_limit or len(all_apps) > preview_limit
            ),
            "target_preview_limit": preview_limit,
            "next_transition_at": effective["next_transition_at"],
            "next_transition_iso": (
                _iso_utc(effective["next_transition_at"])
                if effective["next_transition_at"] is not None
                else None
            ),
            "revision": data["revision"],
        }


def effective_signature() -> tuple[Any, ...]:
    with _lock:
        data, _ = _load_current()
        effective = _raw_effective(data, time.time())
        app_signature = tuple(
            sorted((key, tuple(sorted(entry.items()))) for key, entry in effective["apps"].items())
        )
        return (
            effective["active_window_ids"],
            tuple(sorted(effective["domains"])),
            app_signature,
        )


def list_block_windows(
    *,
    offset: object = 0,
    limit: object = 20,
    include_ended: object = True,
    include_targets: object = False,
    range_start: object = None,
    range_end: object = None,
) -> dict[str, Any]:
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a nonnegative integer.")
    if type(limit) is not int or not 1 <= limit <= 25:
        raise ValueError("limit must be between 1 and 25.")
    if type(include_ended) is not bool:
        raise ValueError("include_ended must be true or false.")
    if type(include_targets) is not bool:
        raise ValueError("include_targets must be true or false.")
    if include_targets and limit > 1:
        raise ValueError(
            "Use limit=1 when include_targets=true, or fetch targets with blockwindow.get."
        )

    with _lock:
        data, _ = _load_current()
        now = time.time()
        lower = None if range_start is None else _parse_instant(range_start, timezone_name="UTC", label="range_start")
        upper = None if range_end is None else _parse_instant(range_end, timezone_name="UTC", label="range_end")
        if lower is not None and upper is not None and upper <= lower:
            raise ValueError("range_end must be later than range_start.")

        windows = []
        for window in data["windows"]:
            state = _window_state(window, now)
            if not include_ended and state == "ended":
                continue
            instances = (repeats.in_range(window, lower, upper) if lower is not None and upper is not None
                         else [repeats.current_or_next(window, now)])
            for item in instances:
                if lower is not None and item['end_at'] <= lower:
                    continue
                if upper is not None and item['start_at'] >= upper:
                    continue
                windows.append((item, window))

        windows.sort(key=lambda pair: (pair[0]["start_at"], pair[0]["end_at"], pair[0]["id"]))
        page = windows[offset:offset + limit]
        presented = [_present(item, now=now, include_targets=include_targets, schedule=schedule)
                     for item, schedule in page]
        # Repeated notes and Unicode names can exceed the socket's 64 KiB frame.
        while len(presented) > 1 and len(json.dumps(presented, ensure_ascii=False).encode('utf-8')) > 48*1024:
            presented.pop()
        next_offset = offset + len(presented) if offset + len(presented) < len(windows) else None
        effective = _raw_effective(data, now)
        return {
            "windows": presented,
            "total": len(windows),
            "offset": offset,
            "limit": limit,
            "next_offset": next_offset,
            "revision": data["revision"],
            "generated_at": now,
            "generated_at_iso": _iso_utc(now),
            "active_window_ids": list(effective["active_window_ids"]),
            "next_transition_at": effective["next_transition_at"],
            "next_transition_iso": (
                _iso_utc(effective["next_transition_at"])
                if effective["next_transition_at"] is not None
                else None
            ),
            "range": {
                "start_at": lower,
                "start_iso": _iso_utc(lower) if lower is not None else None,
                "end_at": upper,
                "end_iso": _iso_utc(upper) if upper is not None else None,
            },
        }


def get_block_window(window_id: object) -> dict[str, Any]:
    if not isinstance(window_id, str):
        raise ValueError("Missing or invalid block-window ID.")
    with _lock:
        data, _ = _load_current()
        now = time.time()
        for window in data["windows"]:
            if window["id"] == window_id:
                return {"window": _present(window, now=now), "revision": data["revision"]}
    raise ValueError("Block window does not exist.")


def _resolve_create_lock(
    requested: object,
    *,
    mode: LockMode,
    guardian_authenticated: bool,
) -> BlockWindowLock:
    if mode is LockMode.GUARDIAN_LOCK:
        if not guardian_authenticated:
            raise PolicyDeniedError("Creating a block window requires guardian authentication.")
        return BlockWindowLock.GUARDIAN_LOCK

    lock_type = _normalize_lock(requested)
    if lock_type is BlockWindowLock.GUARDIAN_LOCK:
        raise ValueError(
            "Guardian-locked block windows can only be created while Sentinel is in Guardian Lock."
        )
    return lock_type


def _apps_map(apps: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {app["key"]: {k: v for k, v in app.items() if k != "key"} for app in apps}


def _classify_update(old: dict[str, Any], new: dict[str, Any]) -> str:
    weakened = False
    strengthened = False
    # Changing a repeating pattern or its timezone has no proven ordering.
    if old.get('recurrence', 'none') != new.get('recurrence', 'none'):
        if old.get('recurrence', 'none') == 'none':
            strengthened = True
        else:
            weakened = True
    if old['timezone'] != new['timezone']:
        weakened = True
    if old.get('recurrence', 'none') != 'none' and old['start_at'] != new['start_at']:
        weakened = True
    if old.get('block_type', 'targets') != new.get('block_type', 'targets'):
        if new.get('block_type') == 'screen_time':
            strengthened = True
        else:
            weakened = True

    if old["enabled"] and not new["enabled"]:
        weakened = True
    elif not old["enabled"] and new["enabled"]:
        strengthened = True

    if new["start_at"] > old["start_at"]:
        weakened = True
    elif new["start_at"] < old["start_at"]:
        strengthened = True

    if new["end_at"] < old["end_at"]:
        weakened = True
    elif new["end_at"] > old["end_at"]:
        strengthened = True

    old_domains = set(old["domains"])
    new_domains = set(new["domains"])
    if not old_domains <= new_domains:
        weakened = True
    if not new_domains <= old_domains:
        strengthened = True

    old_apps = _apps_map(old["apps"])
    new_apps = _apps_map(new["apps"])
    for key, entry in old_apps.items():
        if new_apps.get(key) != entry:
            weakened = True
    for key, entry in new_apps.items():
        if old_apps.get(key) != entry:
            strengthened = True

    old_lock = BlockWindowLock(old["lock_type"])
    new_lock = BlockWindowLock(new["lock_type"])
    rank = {
        BlockWindowLock.UNLOCKED: 0,
        BlockWindowLock.SELF_LOCK: 1,
        BlockWindowLock.GUARDIAN_LOCK: 2,
    }
    if rank[new_lock] < rank[old_lock]:
        weakened = True
    elif rank[new_lock] > rank[old_lock]:
        strengthened = True

    if weakened:
        return "weaken"
    if strengthened:
        return "strengthen"
    return "maintain"


def _authorize_existing_mutation(
    window: dict[str, Any],
    *,
    change: str,
    guardian_authenticated: bool,
    now: float,
    deleting: bool = False,
    disabling_screen_time: bool = False,
) -> None:
    system_mode = get_lock_state().mode
    lock_type = BlockWindowLock(window["lock_type"])

    if system_mode is LockMode.GUARDIAN_LOCK or lock_type is BlockWindowLock.GUARDIAN_LOCK:
        if not guardian_authenticated:
            raise PolicyDeniedError("This block window requires guardian authentication.")
        return

    # Deliberate exception to Sentinel's global Self Lock: an *unlocked*
    # block window remains freely editable, as requested by the user.  Its own
    # lock_type determines the commitment.
    if _screen_grace_open(window, now) and (deleting or disabling_screen_time):
        return
    if lock_type is BlockWindowLock.SELF_LOCK and (now < window["end_at"] or window.get('recurrence', 'none') != 'none'):
        if deleting or change == "weaken":
            raise PolicyDeniedError(
                "This Self-Locked block window cannot be weakened or deleted before it ends."
            )


def _reconcile_effective_policy(
    previous_apps: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Re-apply app and DNS enforcement after a schedule transition/mutation."""
    from sentinel.app_blocker.AppArmor.block_manager import managed_profile_name
    from sentinel.daemon.operations import app_blocker_ops, dns_ops

    force_remove_profiles: set[str] = set()
    if previous_apps:
        current_apps = get_active_apps()
        for key, entry in previous_apps.items():
            if key in current_apps:
                continue
            try:
                force_remove_profiles.add(managed_profile_name(entry))
            except (OSError, ValueError, subprocess.CalledProcessError):
                pass

    return {
        "app_blocker": app_blocker_ops.reconcile_app_blocker(
            force_remove_profiles=force_remove_profiles
        ),
        "dns": dns_ops.apply_dns(),
    }


def create_block_window(
    *,
    name: object,
    start_at: object,
    end_at: object,
    timezone_name: object = "UTC",
    lock_type: object = None,
    apps: object = None,
    domains: object = None,
    enabled: object = True,
    notes: object = "",
    block_type: object = 'targets',
    recurrence: object = 'none',
    guardian_authenticated: bool = False,
    actor_uid: int,
) -> dict[str, Any]:
    mode = get_lock_state().mode
    timezone_value = _timezone_name(timezone_name)
    resolved_lock = _resolve_create_lock(
        lock_type,
        mode=mode,
        guardian_authenticated=guardian_authenticated,
    )
    normalized_apps = _normalize_apps(apps)
    normalized_domains = _normalize_domains(domains)
    _validate_target_payload(normalized_apps, normalized_domains)
    if block_type == 'targets' and not normalized_apps and not normalized_domains:
        raise ValueError("A block window must block at least one app or domain.")
    start = _parse_instant(start_at, timezone_name=timezone_value, label="start_at")
    end = _parse_instant(end_at, timezone_name=timezone_value, label="end_at")
    if end <= start:
        raise ValueError("Block window end_at must be later than start_at.")

    now = time.time()
    window = {
        "id": str(uuid.uuid4()),
        "name": _require_name(name),
        "notes": _notes(notes),
        "enabled": _enabled(enabled),
        "lock_type": resolved_lock.value,
        "created_under_mode": mode.value,
        "created_by_uid": int(actor_uid),
        "created_at": now,
        "updated_at": now,
        "start_at": start,
        "end_at": end,
        "timezone": timezone_value,
        "apps": normalized_apps,
        "domains": normalized_domains,
        "block_type": block_type,
        "recurrence": recurrence,
        "screen_state": {},
    }
    window = _validate_window(window)

    with _lock:
        data, path = _load_current()
        if len(data["windows"]) >= MAX_WINDOWS:
            raise ValueError(f"Sentinel supports at most {MAX_WINDOWS} block windows.")
        before = _raw_effective(data, now)
        data["windows"].append(window)
        data["revision"] += 1
        _write_store(data, path)

    enforcement = _reconcile_effective_policy(dict(before["apps"]))
    return {
        "status": "complete",
        "change": "strengthen",
        "window": _present(window, now=time.time()),
        "revision": data["revision"],
        "enforcement": enforcement,
    }


def update_block_window(
    window_id: object,
    changes: object,
    *,
    guardian_authenticated: bool = False,
) -> dict[str, Any]:
    if not isinstance(window_id, str):
        raise ValueError("Missing or invalid block-window ID.")
    if not isinstance(changes, dict) or not changes:
        raise ValueError("No block-window changes were supplied.")

    allowed = {
        "name", "notes", "enabled", "lock_type", "start_at", "end_at",
        "timezone", "apps", "domains", "block_type", "recurrence",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"Unknown block-window fields: {', '.join(sorted(unknown))}")

    now = time.time()
    with _lock:
        data, path = _load_current()
        index = next((i for i, item in enumerate(data["windows"]) if item["id"] == window_id), None)
        if index is None:
            raise ValueError("Block window does not exist.")
        old = data["windows"][index]
        new = dict(old)

        timezone_value = (
            _timezone_name(changes["timezone"])
            if "timezone" in changes
            else old["timezone"]
        )
        new["timezone"] = timezone_value
        if "name" in changes:
            new["name"] = _require_name(changes["name"])
        if "notes" in changes:
            new["notes"] = _notes(changes["notes"])
        if "enabled" in changes:
            new["enabled"] = _enabled(changes["enabled"])
        if "start_at" in changes:
            new["start_at"] = _parse_instant(changes["start_at"], timezone_name=timezone_value, label="start_at")
        if "end_at" in changes:
            new["end_at"] = _parse_instant(changes["end_at"], timezone_name=timezone_value, label="end_at")
        if new["end_at"] <= new["start_at"]:
            raise ValueError("Block window end_at must be later than start_at.")
        if "apps" in changes:
            new["apps"] = _normalize_apps(changes["apps"])
        if "domains" in changes:
            new["domains"] = _normalize_domains(changes["domains"])
        _validate_target_payload(new["apps"], new["domains"])
        for field in ('block_type', 'recurrence'):
            if field in changes:
                new[field] = changes[field]
        if new.get('block_type', 'targets') == 'targets' and not new["apps"] and not new["domains"]:
            raise ValueError("A block window must block at least one app or domain.")
        new = _validate_window(new)

        system_mode = get_lock_state().mode
        if system_mode is LockMode.GUARDIAN_LOCK:
            # Any schedule edited by a Guardian becomes a Guardian-owned
            # commitment, preventing it from becoming freely mutable if the
            # machine later leaves Guardian mode.
            new["lock_type"] = BlockWindowLock.GUARDIAN_LOCK.value
        elif "lock_type" in changes:
            requested = _normalize_lock(changes["lock_type"])
            if requested is BlockWindowLock.GUARDIAN_LOCK:
                raise ValueError(
                    "Guardian-locked block windows can only be created or promoted while Sentinel is in Guardian Lock."
                )
            new["lock_type"] = requested.value

        change = _classify_update(old, new)
        _authorize_existing_mutation(
            old,
            change=change,
            guardian_authenticated=guardian_authenticated,
            now=now,
            disabling_screen_time=(old.get('block_type') == 'screen_time' and new['enabled'] is False
                                   and all(new.get(key) == value for key, value in old.items()
                                           if key not in ('enabled', 'updated_at'))),
        )
        new["updated_at"] = now
        before = _raw_effective(data, now)
        data["windows"][index] = new
        data["revision"] += 1
        _write_store(data, path)

    enforcement = _reconcile_effective_policy(dict(before["apps"]))
    return {
        "status": "complete",
        "change": change,
        "window": _present(new, now=time.time()),
        "revision": data["revision"],
        "enforcement": enforcement,
    }


def delete_block_window(
    window_id: object,
    *,
    guardian_authenticated: bool = False,
) -> dict[str, Any]:
    if not isinstance(window_id, str):
        raise ValueError("Missing or invalid block-window ID.")

    now = time.time()
    with _lock:
        data, path = _load_current()
        index = next((i for i, item in enumerate(data["windows"]) if item["id"] == window_id), None)
        if index is None:
            raise ValueError("Block window does not exist.")
        window = data["windows"][index]
        _authorize_existing_mutation(
            window,
            change="weaken",
            guardian_authenticated=guardian_authenticated,
            now=now,
            deleting=True,
        )
        before = _raw_effective(data, now)
        del data["windows"][index]
        data["revision"] += 1
        _write_store(data, path)

    enforcement = _reconcile_effective_policy(dict(before["apps"]))
    return {
        "status": "complete",
        "change": "weaken",
        "deleted": True,
        "id": window_id,
        "revision": data["revision"],
        "enforcement": enforcement,
    }


def reconcile_transition(previous_apps: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    """Internal scheduler hook used when wall clock crosses a window boundary."""
    return _reconcile_effective_policy(previous_apps)
