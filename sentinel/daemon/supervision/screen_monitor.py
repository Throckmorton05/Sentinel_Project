"""Bootstrap and supervise the protected desktop user's Wayland monitor."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

from sentinel.daemon.core.constants import (
    MONITOR_CHECK_INTERVAL,
    MONITOR_ADOPTION_GRACE,
    MONITOR_FAILURE_LIMIT,
    MONITOR_FAILURE_WINDOW,
    MONITOR_HEALTHY_RESET,
    MONITOR_HEARTBEAT_TIMEOUT,
    MONITOR_RESTART_GRACE,
    SCREEN_MONITOR_EXECUTABLE,
    SCREEN_MONITOR_MODULE,
)
from sentinel.daemon.supervision.readiness import _inference_apis_ready
from sentinel.daemon.core.session import (
    _get_graphical_session_id,
    _user_session_command,
    _wayland_session_ready,
)
from sentinel.daemon.supervision.state import (
    _monitor_state, _monitor_state_lock, _monitor_lifecycle_lock, _stop_event,
)
from sentinel.daemon.security.tamper_protection import (
    _enter_tamper_cooldown,
    _maintain_tamper_cooldown,
    _terminate_tamper_blocker,
)


def _monitor_process_matches(
    pid: int,
    uid: int,
) -> bool:
    """
    Verify that a heartbeat PID appears to be Sentinel's real
    screen-monitor process.

    In the final distro this becomes stronger because the runtime
    and monitor code will live in root-owned /usr/lib/sentinel.
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

    cmdline = raw_cmdline.replace(b"\x00", b" ").decode(
        "utf-8",
        errors="replace",
    )

    return SCREEN_MONITOR_MODULE in cmdline or "sentinel-screen-monitor" in cmdline


def _register_screen_monitor(
    uid: int,
    pid: int,
) -> None:

    if not _monitor_process_matches(
        pid,
        uid,
    ):
        raise ValueError(
            "Monitor registration did not come from the Sentinel screen monitor."
        )

    now = time.monotonic()

    with _monitor_state_lock:
        if not _monitor_state.enabled or _monitor_state.uid is None:
            raise ValueError("The protected desktop user has not been configured.")

        if _monitor_state.uid != uid:
            raise ValueError(
                "A different user already owns the screen-monitor watchdog."
            )

        existing_pid = _monitor_state.pid

        if (
            existing_pid is not None
            and existing_pid != pid
            and _monitor_state.last_heartbeat is not None
            and (now - _monitor_state.last_heartbeat < MONITOR_HEARTBEAT_TIMEOUT)
            and _monitor_process_matches(
                existing_pid,
                uid,
            )
        ):
            raise ValueError(
                "Another healthy Sentinel screen monitor is already registered."
            )

        _monitor_state.enabled = True

        _monitor_state.pid = pid

        _monitor_state.last_heartbeat = now

        _monitor_state.awaiting_registration_until = None

        _monitor_state.healthy_since = now

    print(
        f"sentineld watchdog: screen monitor registered uid={uid} pid={pid}",
        flush=True,
    )


def _screen_monitor_heartbeat(
    uid: int,
    pid: int,
) -> None:

    now = time.monotonic()

    with _monitor_state_lock:
        if not _monitor_state.enabled:
            raise ValueError("Screen monitor is not registered.")

        if _monitor_state.uid != uid:
            raise ValueError("Heartbeat UID does not match the registered monitor.")

        if _monitor_state.pid != pid:
            raise ValueError("Heartbeat PID does not match the registered monitor.")

        _monitor_state.last_heartbeat = now


def _terminate_screen_monitor(
    pid: int,
    uid: int,
) -> None:
    """
    Terminate only a process that still verifies as the real monitor.
    """

    if not _monitor_process_matches(
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

    deadline = time.monotonic() + 3.0

    while time.monotonic() < deadline:
        if not _monitor_process_matches(
            pid,
            uid,
        ):
            return

        time.sleep(0.1)

    if not _monitor_process_matches(
        pid,
        uid,
    ):
        return

    try:
        os.kill(
            pid,
            signal.SIGKILL,
        )

    except ProcessLookupError:
        pass


def _launch_screen_monitor(
    uid: int,
) -> None:
    """
    Relaunch the screen monitor inside the desktop user's
    graphical D-Bus session using Sentinel's authorized
    screen-monitor Python launcher.
    """

    if not SCREEN_MONITOR_EXECUTABLE.is_file():
        raise RuntimeError(
            f"Sentinel screen-monitor launcher does not exist: {SCREEN_MONITOR_EXECUTABLE}"
        )
    command = _user_session_command(
        uid, str(SCREEN_MONITOR_EXECUTABLE), "-I", "-B", "-m", SCREEN_MONITOR_MODULE, wayland=True,
    )
    print("sentineld watchdog: starting screen monitor.", flush=True)
    child = subprocess.Popen(command, start_new_session=True)
    with _monitor_state_lock:
        _monitor_state.monitor_child = child




def _record_monitor_failure(
    uid: int,
    reason: str,
) -> None:

    now = time.monotonic()

    should_blackout = False

    with _monitor_state_lock:
        cooldown_until = _monitor_state.tamper_cooldown_until

        if cooldown_until is not None and now < cooldown_until:
            print(
                "sentineld watchdog: "
                f"monitor failure during active "
                f"tamper cooldown: {reason}",
                flush=True,
            )

            return

        failures = _monitor_state.failures

        while failures and (now - failures[0] > MONITOR_FAILURE_WINDOW):
            failures.popleft()

        failures.append(now)

        count = len(failures)

        print(
            "sentineld watchdog: "
            f"monitor failure {count}/"
            f"{MONITOR_FAILURE_LIMIT}: "
            f"{reason}",
            flush=True,
        )

        if count >= MONITOR_FAILURE_LIMIT:
            failures.clear()

            should_blackout = True

    if should_blackout:
        _enter_tamper_cooldown(uid)


def _terminate_monitor_child() -> None:
    """Stop/reap a monitor launch, including one that never registered."""
    with _monitor_state_lock:
        child = _monitor_state.monitor_child
        _monitor_state.monitor_child = None
    if child is None or child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=3)
    except ProcessLookupError:
        child.poll()
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=3)


def initialize_screen_monitor(uid: int, *, enabled: bool = True) -> None:
    """Enable supervision before any monitor has connected."""
    if uid <= 0:
        raise ValueError("The protected desktop UID must not be root.")
    with _monitor_state_lock:
        _monitor_state.enabled = enabled
        _monitor_state.uid = uid
        _monitor_state.awaiting_registration_until = time.monotonic() + MONITOR_ADOPTION_GRACE
    print(f"sentineld watchdog: protecting desktop uid={uid}.", flush=True)


def _discover_screen_monitors(uid: int) -> list[int]:
    """Find an installed monitor left alive across a daemon restart.

    Require the protected UID, the actual interpreter executable, and an exact
    Python module operand. Never kill a process just because its name or an
    arbitrary argument mentions Sentinel.
    """
    interpreters = {Path(sys.executable).resolve(), SCREEN_MONITOR_EXECUTABLE.resolve()}
    found = []
    for process in psutil.process_iter(['pid', 'uids', 'exe', 'cmdline']):
        try:
            info = process.info
            args = info.get('cmdline') or []
            if not info.get('uids') or info['uids'].real != uid or not info.get('exe'):
                continue
            if Path(info['exe']).resolve() not in interpreters or '-m' not in args:
                continue
            index = args.index('-m')
            if '-c' in args[:index] or args[index + 1:] != [SCREEN_MONITOR_MODULE]:
                continue
            if _monitor_process_matches(info['pid'], uid):
                found.append(info['pid'])
        except (psutil.Error, OSError):
            continue
    return found


def set_screen_monitor_enabled(enabled: bool) -> None:
    """Apply an authorized lifecycle change without counting it as tampering."""
    with _monitor_lifecycle_lock:
        with _monitor_state_lock:
            uid = _monitor_state.uid
            if uid is None:
                raise RuntimeError('The protected desktop user has not been configured.')
            was_enabled = _monitor_state.enabled
            _monitor_state.enabled = enabled
        if enabled:
            if not was_enabled:
                with _monitor_state_lock:
                    # Keep the initial adoption grace when sentineld restarts;
                    # a prior monitor may reconnect before launching a new one.
                    _monitor_state.launch_attempted = False
                    _monitor_state.waiting_reason = None
            return
        # Disable registration/restarts BEFORE terminating any process. The
        # lifecycle lock also prevents a previously-started tick racing this.
        _reset_session(uid, None)
        for pid in _discover_screen_monitors(uid):
            _terminate_screen_monitor(pid, uid)
        with _monitor_state_lock:
            _monitor_state.waiting_reason = 'Image filtering is disabled.'


def _reset_session(uid: int, session_id: str | None) -> None:
    """Logout/session replacement is normal lifecycle, not a monitor failure."""
    with _monitor_state_lock:
        old_pid = _monitor_state.pid
        blocker_pid = _monitor_state.tamper_blocker_pid
        _monitor_state.pid = None
        _monitor_state.last_heartbeat = None
        _monitor_state.awaiting_registration_until = None
        _monitor_state.healthy_since = None
        _monitor_state.failures.clear()
        _monitor_state.tamper_cooldown_until = None
        _monitor_state.tamper_blocker_pid = None
        _monitor_state.overview_was_loaded = None
        _monitor_state.session_id = session_id
        _monitor_state.launch_attempted = False
    if old_pid is not None:
        _terminate_screen_monitor(old_pid, uid)
    _terminate_monitor_child()
    if blocker_pid is not None:
        _terminate_tamper_blocker(blocker_pid, uid)


def _restart_screen_monitor(
    uid: int,
    old_pid: int | None,
    *,
    reason: str,
    count_failure: bool,
) -> None:

    now = time.monotonic()

    #
    # Mark ourselves as waiting for a replacement BEFORE
    # launching it. That avoids registration races.
    #

    with _monitor_state_lock:
        # A monitor may have re-registered while logind was being queried.
        if _monitor_state.pid is not None and _monitor_state.pid != old_pid:
            return

        _monitor_state.pid = None

        _monitor_state.last_heartbeat = None

        _monitor_state.healthy_since = None

        _monitor_state.awaiting_registration_until = now + MONITOR_RESTART_GRACE
        _monitor_state.launch_attempted = True

    if count_failure:
        _record_monitor_failure(
            uid,
            reason,
        )

    if old_pid is not None:
        _terminate_screen_monitor(
            old_pid,
            uid,
        )

    _terminate_monitor_child()

    try:
        _launch_screen_monitor(uid)

    except (
        RuntimeError,
        OSError,
    ) as exc:
        print(
            f"sentineld watchdog: could not restart monitor: {exc}",
            flush=True,
        )


def _log_startup_wait(reason: str) -> None:
    with _monitor_state_lock:
        if _monitor_state.waiting_reason == reason:
            return
        _monitor_state.waiting_reason = reason
    print(f"sentineld watchdog: {reason}", flush=True)


def _initial_launch_ready(uid: int) -> bool:
    if not _wayland_session_ready(uid):
        _log_startup_wait("waiting for the user session bus, Wayland display and KWin.")
        return False
    if not _inference_apis_ready():
        _log_startup_wait("waiting for inference APIs on ports 8000-8003 to load their models.")
        return False
    with _monitor_state_lock:
        _monitor_state.waiting_reason = None
    return True


def _screen_monitor_watchdog_tick() -> None:
    with _monitor_lifecycle_lock:
        _screen_monitor_watchdog_tick_locked()


def _screen_monitor_watchdog_tick_locked() -> None:
    """Supervise one session/heartbeat observation."""
    now = time.monotonic()

    with _monitor_state_lock:
        if not _monitor_state.enabled:
            return

        uid = _monitor_state.uid

        pid = _monitor_state.pid

        last_heartbeat = _monitor_state.last_heartbeat

        awaiting_until = _monitor_state.awaiting_registration_until

        healthy_since = _monitor_state.healthy_since

    if uid is None:
        return

    #
    # No desktop session means:
    #
    #   boot before login
    #   user logged out
    #
    # Neither is tampering.
    #

    session_id = _get_graphical_session_id(uid)

    with _monitor_state_lock:
        previous_session = _monitor_state.session_id

    if session_id is None:
        _reset_session(uid, None)
        _log_startup_wait("waiting for the protected user's Wayland login.")
        return

    if previous_session is not None and previous_session != session_id:
        _reset_session(uid, session_id)
        return

    with _monitor_state_lock:
        _monitor_state.session_id = session_id

    _maintain_tamper_cooldown(
        uid,
        now,
    )

    #
    # Active session but no monitor.
    #

    if pid is None:
        if awaiting_until is None:
            if not _initial_launch_ready(uid):
                return
            _restart_screen_monitor(
                uid,
                None,
                reason=("screen monitor was not running"),
                count_failure=False,
            )

            return

        if now >= awaiting_until:
            with _monitor_state_lock:
                initial_start = not _monitor_state.launch_attempted
            if initial_start and not _initial_launch_ready(uid):
                return
            _restart_screen_monitor(
                uid,
                None,
                reason=("replacement monitor failed to register"),
                count_failure=not initial_start,
            )

        return

    #
    # Monitor disappeared.
    #

    if not _monitor_process_matches(
        pid,
        uid,
    ):
        _restart_screen_monitor(
            uid,
            pid,
            reason=("screen monitor process disappeared"),
            count_failure=True,
        )

        return

    #
    # Monitor process exists but stopped heartbeating.
    #

    if last_heartbeat is None or (now - last_heartbeat > MONITOR_HEARTBEAT_TIMEOUT):
        _restart_screen_monitor(
            uid,
            pid,
            reason=("screen monitor heartbeat timed out"),
            count_failure=True,
        )

        return

    #
    # Ten healthy minutes means old failures stop
    # contributing to tamper detection.
    #

    if healthy_since is not None and (now - healthy_since >= MONITOR_HEALTHY_RESET):
        with _monitor_state_lock:
            if _monitor_state.failures:
                print(
                    "sentineld watchdog: "
                    "healthy interval reached; "
                    "clearing old failures.",
                    flush=True,
                )

                _monitor_state.failures.clear()

            _monitor_state.healthy_since = now


def _screen_monitor_watchdog_loop() -> None:
    print("sentineld watchdog: started.", flush=True)
    while not _stop_event.wait(MONITOR_CHECK_INTERVAL):
        try:
            _screen_monitor_watchdog_tick()
        except Exception as exc:
            # logind/session teardown can race a check. Keep supervision alive.
            print(f"sentineld watchdog: check failed: {exc}", flush=True)
