"""
Persistent Sentinel lock-mode state.

This module owns Sentinel's authoritative operating mode:

    unlocked
    self_lock
    guardian_lock

Self Lock is intentionally permanent. There is no normal transition
from SELF_LOCK back to UNLOCKED.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from sentinel.settings.paths import LOCK_STATE_FILE

STATE_VERSION = 1


class LockMode(str, Enum):
    UNLOCKED = "unlocked"
    SELF_LOCK = "self_lock"
    GUARDIAN_LOCK = "guardian_lock"


@dataclass(frozen=True)
class LockState:
    version: int
    mode: LockMode

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "mode": self.mode.value,
        }


_state_lock = threading.RLock()

_current_state: LockState | None = None
_current_path: Path | None = None


def _default_state() -> LockState:
    return LockState(
        version=STATE_VERSION,
        mode=LockMode.UNLOCKED,
    )


def _validate_parent(
    path: Path,
) -> None:
    """
    Ensure the directory holding Sentinel state is protected.

    The protected desktop user must not be able to modify the
    directory containing the state file.
    """

    path.parent.mkdir(
        mode=0o755,
        parents=True,
        exist_ok=True,
    )

    info = path.parent.stat()

    if info.st_uid != 0:
        raise ValueError(f"{path.parent} must be owned by root.")

    if info.st_mode & 0o022:
        raise ValueError(f"{path.parent} must not be writable by group or others.")


def _state_from_data(
    data: object,
) -> LockState:
    """
    Validate state loaded from disk.
    """

    if not isinstance(
        data,
        dict,
    ):
        raise ValueError("Sentinel lock state must be a JSON object.")

    version = data.get("version")

    if version != STATE_VERSION:
        raise ValueError(f"Unsupported Sentinel lock-state version: {version!r}.")

    raw_mode = data.get("mode")

    try:
        mode = LockMode(raw_mode)

    except (
        TypeError,
        ValueError,
    ) as exc:
        raise ValueError(f"Invalid Sentinel lock mode: {raw_mode!r}.") from exc

    return LockState(
        version=STATE_VERSION,
        mode=mode,
    )


def _read_state(
    path: Path,
) -> LockState:
    """
    Safely read an existing root-owned state file.
    """

    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

    try:
        fd = os.open(
            path,
            flags,
        )

    except OSError as exc:
        raise ValueError(f"Cannot load Sentinel lock state from {path}: {exc}") from exc

    with os.fdopen(
        fd,
        "r",
        encoding="utf-8",
    ) as stream:
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

    return _state_from_data(data)


def _write_state(
    state: LockState,
    path: Path,
) -> None:
    """
    Atomically persist Sentinel's lock state.
    """

    _validate_parent(path)

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )

    temporary_path = Path(temporary_name)

    try:
        os.fchmod(
            fd,
            0o600,
        )

        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
        ) as stream:
            json.dump(
                state.as_dict(),
                stream,
                indent=2,
                sort_keys=True,
            )

            stream.write("\n")

            stream.flush()

            os.fsync(stream.fileno())

        os.replace(
            temporary_path,
            path,
        )

        directory_fd = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY,
        )

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


def initialize_lock_state(
    path: Path = LOCK_STATE_FILE,
) -> LockState:
    """
    Initialize Sentinel's authoritative lock state.

    A missing file means this is a fresh installation, so Sentinel
    begins unlocked.

    An existing malformed state file is never silently replaced
    with UNLOCKED.
    """

    global _current_state
    global _current_path

    with _state_lock:
        _validate_parent(path)

        if path.exists():
            state = _read_state(path)

        else:
            state = _default_state()

            _write_state(
                state,
                path,
            )

        _current_state = state
        _current_path = path

        return state


def _require_initialized() -> tuple[
    LockState,
    Path,
]:
    if _current_state is None or _current_path is None:
        raise RuntimeError("Sentinel lock state has not been initialized.")

    return (
        _current_state,
        _current_path,
    )


def get_lock_state() -> LockState:
    """
    Return Sentinel's authoritative current lock state.
    """

    with _state_lock:
        state, _ = _require_initialized()

        return state


def enter_self_lock() -> LockState:
    """
    Permanently enter Self Lock.

    Sentinel must be Unlocked first, including after exiting Guardian Lock.
    There is intentionally no matching exit_self_lock() function.
    """

    global _current_state

    with _state_lock:
        state, path = _require_initialized()

        if state.mode is not LockMode.UNLOCKED:
            raise ValueError("Sentinel must be unlocked before entering Self Lock.")

        new_state = LockState(
            version=STATE_VERSION,
            mode=LockMode.SELF_LOCK,
        )

        _write_state(
            new_state,
            path,
        )

        _current_state = new_state

        return new_state


def enter_guardian_lock() -> LockState:
    """
    Enter Guardian Lock.

    Sentinel must currently be unlocked.
    """

    global _current_state

    with _state_lock:
        state, path = _require_initialized()

        if state.mode is not LockMode.UNLOCKED:
            raise ValueError("Sentinel must be unlocked before entering Guardian Lock.")

        new_state = LockState(
            version=STATE_VERSION,
            mode=LockMode.GUARDIAN_LOCK,
        )

        _write_state(
            new_state,
            path,
        )

        _current_state = new_state

        return new_state


def exit_guardian_lock() -> LockState:
    """
    Exit Guardian Lock and return to Unlocked mode.

    Authentication must be performed by the caller before this
    transition is allowed.
    """

    global _current_state

    with _state_lock:
        state, path = _require_initialized()

        if state.mode is not LockMode.GUARDIAN_LOCK:
            raise ValueError(
                "Sentinel must be in Guardian Lock before it can be unlocked."
            )

        new_state = LockState(
            version=STATE_VERSION,
            mode=LockMode.UNLOCKED,
        )

        _write_state(
            new_state,
            path,
        )

        _current_state = new_state

        return new_state
