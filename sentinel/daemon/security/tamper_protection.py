"""Manage anti-tamper cooldowns, blackout UI and temporary KWin effects."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from sentinel.daemon.core.constants import (
    KWIN_EFFECTS_INTERFACE,
    KWIN_EFFECTS_OBJECT,
    OVERVIEW_EFFECT,
    QDBUS6,
    TAMPER_BLOCKER_MODULE,
    TAMPER_COOLDOWN_SECONDS,
)
from sentinel.daemon.core.session import _lock_graphical_session, _user_session_command
from sentinel.daemon.supervision.state import _monitor_state, _monitor_state_lock


def _tamper_blocker_process_matches(
    pid: int,
    uid: int,
) -> bool:
    """
    Verify that PID belongs to Sentinel's tamper blocker.
    """

    proc_dir = Path(f"/proc/{pid}")

    try:
        if proc_dir.stat().st_uid != uid:
            return False

        raw_cmdline = proc_dir.joinpath("cmdline").read_bytes()

    except (
        FileNotFoundError,
        PermissionError,
        ProcessLookupError,
        OSError,
    ):
        return False

    cmdline = raw_cmdline.replace(
        b"\x00",
        b" ",
    ).decode(
        "utf-8",
        errors="replace",
    )

    return TAMPER_BLOCKER_MODULE in cmdline


def _register_tamper_blocker(
    uid: int,
    pid: int,
) -> None:

    if not _tamper_blocker_process_matches(
        pid,
        uid,
    ):
        raise ValueError("Tamper blocker registration came from an unexpected process.")

    now = time.monotonic()

    with _monitor_state_lock:
        if (
            _monitor_state.tamper_cooldown_until is None
            or now >= _monitor_state.tamper_cooldown_until
        ):
            raise ValueError("No Sentinel tamper cooldown is active.")

        if _monitor_state.uid != uid:
            raise ValueError(
                "Tamper blocker UID does not match the monitored desktop user."
            )

        _monitor_state.tamper_blocker_pid = pid

    print(
        f"sentineld tamper protection: blackout registered uid={uid} pid={pid}",
        flush=True,
    )


def _launch_tamper_blocker(
    uid: int,
    remaining_seconds: float,
) -> None:

    seconds = max(1, int(remaining_seconds))
    command = _user_session_command(
        uid, sys.executable, "-I", "-B", "-m", TAMPER_BLOCKER_MODULE,
        "--seconds", str(seconds), wayland=True,
    )

    print(
        f"sentineld tamper protection: starting {seconds}-second blackout.",
        flush=True,
    )

    subprocess.Popen(
        command,
        start_new_session=True,
    )


def _terminate_tamper_blocker(
    pid: int,
    uid: int,
) -> None:

    if not _tamper_blocker_process_matches(
        pid,
        uid,
    ):
        return

    try:
        os.kill(
            pid,
            signal.SIGTERM,
        )

    except ProcessLookupError:
        return


def _enter_tamper_cooldown(
    uid: int,
) -> None:
    """
    Enter Sentinel's temporary anti-tamper blackout.
    """

    now = time.monotonic()

    deadline = now + TAMPER_COOLDOWN_SECONDS

    overview_was_loaded: bool | None = None

    try:
        overview_was_loaded = _kwin_effect_is_loaded(
            uid,
            OVERVIEW_EFFECT,
        )

        if overview_was_loaded:
            _kwin_unload_effect(
                uid,
                OVERVIEW_EFFECT,
            )

            print(
                "sentineld tamper protection: KWin Overview disabled.",
                flush=True,
            )

    except (
        RuntimeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(
            f"sentineld tamper protection: could not disable KWin Overview: {exc}",
            flush=True,
        )

    with _monitor_state_lock:
        _monitor_state.tamper_cooldown_until = deadline

        _monitor_state.tamper_blocker_pid = None

        _monitor_state.overview_was_loaded = overview_was_loaded

        #
        # Failures during the blackout do not extend it.
        #
        _monitor_state.failures.clear()

    print(
        "sentineld tamper protection: "
        f"entering {int(TAMPER_COOLDOWN_SECONDS)}-second cooldown.",
        flush=True,
    )

    #
    # Kill any Rofi launcher that was already open.
    #
    _terminate_rofi_processes(uid)

    try:
        _launch_tamper_blocker(
            uid,
            TAMPER_COOLDOWN_SECONDS,
        )

    except (
        RuntimeError,
        OSError,
    ) as exc:
        print(
            f"sentineld tamper protection: failed to start blackout: {exc}",
            flush=True,
        )

        #
        # Safe fallback if the anti-tamper UI cannot launch.
        #
        try:
            _lock_graphical_session(uid)

        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass


def _maintain_tamper_cooldown(
    uid: int,
    now: float,
) -> None:
    """
    Keep the blackout process alive until the cooldown expires.
    """

    with _monitor_state_lock:
        cooldown_until = _monitor_state.tamper_cooldown_until

        blocker_pid = _monitor_state.tamper_blocker_pid

    if cooldown_until is None:
        return

    if now < cooldown_until:
        _terminate_rofi_processes(uid)

    #
    # Cooldown finished.
    #

    if now >= cooldown_until:
        with _monitor_state_lock:
            overview_was_loaded = _monitor_state.overview_was_loaded

        if blocker_pid is not None:
            _terminate_tamper_blocker(
                blocker_pid,
                uid,
            )

        #
        # Restore KWin Overview if it was enabled
        # before Sentinel entered tamper cooldown.
        #
        if overview_was_loaded:
            try:
                _kwin_load_effect(
                    uid,
                    OVERVIEW_EFFECT,
                )

                print(
                    "sentineld tamper protection: KWin Overview restored.",
                    flush=True,
                )

            except (
                RuntimeError,
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
            ) as exc:
                print(
                    "sentineld tamper protection: "
                    f"could not restore KWin Overview: {exc}",
                    flush=True,
                )

        #
        # Clear cooldown state.
        #
        with _monitor_state_lock:
            _monitor_state.tamper_cooldown_until = None

            _monitor_state.tamper_blocker_pid = None

            _monitor_state.overview_was_loaded = None

            _monitor_state.failures.clear()

        print(
            "sentineld tamper protection: cooldown finished.",
            flush=True,
        )

        return

    #
    # Cooldown still active.
    #
    # If the user killed the blackout process, recreate it.
    #

    blocker_alive = blocker_pid is not None and _tamper_blocker_process_matches(
        blocker_pid,
        uid,
    )

    if blocker_alive:
        return

    remaining = cooldown_until - now

    with _monitor_state_lock:
        _monitor_state.tamper_blocker_pid = None

    print(
        "sentineld tamper protection: blackout process disappeared; restarting it.",
        flush=True,
    )

    try:
        _launch_tamper_blocker(
            uid,
            remaining,
        )

    except (
        RuntimeError,
        OSError,
    ) as exc:
        print(
            f"sentineld tamper protection: failed to restart blackout: {exc}",
            flush=True,
        )


def _run_kwin_effect_command(
    uid: int,
    method: str,
    effect: str,
) -> subprocess.CompletedProcess[str]:
    """
    Call KWin's Effects D-Bus interface inside the user's session.
    """

    command = _user_session_command(
        uid, QDBUS6, "org.kde.KWin", KWIN_EFFECTS_OBJECT,
        f"{KWIN_EFFECTS_INTERFACE}.{method}", effect,
    )

    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )


def _terminate_rofi_processes(
    uid: int,
) -> None:
    """
    Terminate Rofi instances owned by the monitored desktop user
    while Sentinel anti-tamper protection is active.
    """

    rofi_executables = {
        "rofi",
        "rofi-wayland",
    }

    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue

        try:
            if proc_dir.stat().st_uid != uid:
                continue

            exe_path = proc_dir.joinpath("exe").resolve()

        except (
            FileNotFoundError,
            PermissionError,
            ProcessLookupError,
            OSError,
        ):
            continue

        if exe_path.name not in rofi_executables:
            continue

        pid = int(proc_dir.name)

        try:
            os.kill(
                pid,
                signal.SIGTERM,
            )

            print(
                f"sentineld tamper protection: terminated Rofi pid={pid}.",
                flush=True,
            )

        except ProcessLookupError:
            continue


def _kwin_effect_is_loaded(
    uid: int,
    effect: str,
) -> bool:
    result = _run_kwin_effect_command(
        uid,
        "isEffectLoaded",
        effect,
    )

    return result.stdout.strip().lower() == "true"


def _kwin_unload_effect(
    uid: int,
    effect: str,
) -> None:
    _run_kwin_effect_command(
        uid,
        "unloadEffect",
        effect,
    )


def _kwin_load_effect(
    uid: int,
    effect: str,
) -> None:
    _run_kwin_effect_command(
        uid,
        "loadEffect",
        effect,
    )
