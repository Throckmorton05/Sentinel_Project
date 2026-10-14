"""Daily trusted DNS source downloads, independent of the RPC mutation lock."""

from __future__ import annotations

import json
import math
import threading
import time
from typing import Any

import requests

from sentinel.dns.storage import atomic_write, read_protected
from sentinel.settings.paths import DNS_DATA_DIR

SOURCE_STATE_FILE = DNS_DATA_DIR / "source-refresh.json"
REFRESH_SECONDS = 24 * 60 * 60
RETRY_SECONDS = 15 * 60
# Manual source updates share this lock. Normal settings/list writes do not.
sources_lock = threading.Lock()
_state_lock = threading.RLock()
_state: dict[str, Any] = {
    "version": 1, "last_attempt_at": None, "last_completed_at": None,
    "error": None, "pending_apply": False,
}
_updating = False


def _read_state() -> dict[str, Any]:
    try:
        data = json.loads(read_protected(SOURCE_STATE_FILE, private=True))
    except FileNotFoundError:
        return {"version": 1, "last_attempt_at": None, "last_completed_at": None,
                "error": None, "pending_apply": False}
    if (not isinstance(data, dict) or type(data.get("version")) is not int
            or data["version"] != 1 or type(data.get("pending_apply")) is not bool
            or (data.get("error") is not None and not isinstance(data["error"], str))):
        raise ValueError("Invalid DNS source refresh state.")
    for name in ("last_attempt_at", "last_completed_at"):
        value = data.get(name)
        if value is not None and (type(value) not in (int, float)
                or not math.isfinite(value) or value < 0):
            raise ValueError("Invalid DNS source refresh timestamp.")
        data[name] = value
    return data


def _save_state(data: dict[str, Any]) -> None:
    global _state
    with _state_lock:
        atomic_write(SOURCE_STATE_FILE, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=0o600)
        _state = dict(data)


def _next_attempt(data: dict[str, Any]) -> float:
    attempted = data["last_attempt_at"]
    completed = data["last_completed_at"]
    if attempted is None:
        return 0.0
    return attempted + (REFRESH_SECONDS if completed is not None and completed >= attempted
                        else RETRY_SECONDS)


def source_status() -> dict[str, Any]:
    with _state_lock:
        data = dict(_state)
        data["status"] = "updating" if _updating else "pending" if data["error"] else "automatic"
        data["next_attempt_at"] = _next_attempt(data)
        return data


def needs_apply() -> bool:
    with _state_lock:
        return _state["pending_apply"]


def mark_applied() -> None:
    # DNS's own application status/reconciler owns backend failure retries.
    with _state_lock:
        if _state["pending_apply"]:
            _save_state({**_state, "pending_apply": False})


def refresh_if_due() -> bool:
    """Download configured sources when due; retain cached data on outages.

    Persist the attempt before downloading, so a daemon restart does not create
    a tight retry loop and partially updated category files get reconciled.
    The generator owns per-source fallback and atomic category replacement.
    """
    global _state, _updating
    if not sources_lock.acquire(blocking=False):
        return False
    try:
        data = _read_state()
        with _state_lock:
            _state = dict(data)
        if time.time() < _next_attempt(data):
            return False
        data = {**data, "last_attempt_at": time.time(), "pending_apply": True, "error": None}
        _save_state(data)
        with _state_lock:
            _updating = True
        from sentinel.daemon.operations import dns_ops
        try:
            failures = dns_ops.refresh_category_sources()
            if failures:
                raise ValueError("DNS sources retained cached data: " + "; ".join(failures))
        except (OSError, ValueError, requests.RequestException) as exc:
            data["error"] = str(exc)
        else:
            data["last_completed_at"] = max(time.time(), data["last_attempt_at"])
        _save_state(data)
        return True
    except (OSError, ValueError, requests.RequestException) as exc:
        # A damaged/unwritable state file stays intact and visible to clients.
        with _state_lock:
            _state = {**_state, "error": str(exc)}
        raise
    finally:
        with _state_lock:
            _updating = False
        sources_lock.release()
