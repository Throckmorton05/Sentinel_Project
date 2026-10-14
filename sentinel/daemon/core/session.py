"""Discover logind Wayland sessions and construct desktop-user commands."""
from __future__ import annotations

import pwd
import stat
import subprocess
from pathlib import Path

from sentinel.daemon.core.constants import ENV, LOGINCTL, QDBUS6, RUNUSER


def _peer_user(uid: int) -> pwd.struct_passwd:
    try:
        return pwd.getpwuid(uid)
    except KeyError as exc:
        raise ValueError(f"Unknown user ID: {uid}") from exc


def _find_wayland_display(uid: int) -> str | None:
    runtime_dir = Path(f"/run/user/{uid}")
    if not runtime_dir.is_dir():
        return None
    for candidate in sorted(runtime_dir.glob("wayland-*")):
        try:
            info = candidate.stat()
        except OSError:
            continue
        if info.st_uid == uid and stat.S_ISSOCK(info.st_mode):
            return candidate.name
    return None


def _get_graphical_session_id(uid: int) -> str | None:
    """Find a logged-in Wayland session, excluding TTY, greeter and logout."""
    result = subprocess.run(
        [LOGINCTL, "show-user", str(uid), "-p", "Sessions", "--value"],
        capture_output=True, text=True, check=False, timeout=5,
    )
    if result.returncode != 0:
        return None
    online = None
    for session_id in result.stdout.split():
        result = subprocess.run(
            [LOGINCTL, "show-session", session_id,
             "-p", "User", "-p", "Type", "-p", "Class", "-p", "State"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        if result.returncode != 0:
            continue
        properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if (properties.get("User") != str(uid)
                or properties.get("Type") != "wayland"
                or properties.get("Class") not in {"user", "user-early"}):
            continue
        if properties.get("State") == "active":
            return session_id
        if properties.get("State") == "online":
            online = session_id
    return online


def _user_session_command(uid: int, *args: str, wayland: bool = False) -> list[str]:
    """Use an explicit environment rather than inheriting the root daemon's."""
    user = _peer_user(uid)
    runtime_dir = Path(f"/run/user/{uid}")
    bus_path = runtime_dir / "bus"
    try:
        info = bus_path.stat()
    except OSError as exc:
        raise RuntimeError("The user's D-Bus session is not available.") from exc
    if info.st_uid != uid or not stat.S_ISSOCK(info.st_mode):
        raise RuntimeError("The user's D-Bus session is not available.")
    command = [
        RUNUSER, "-u", user.pw_name, "--", ENV, "-i",
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        f"HOME={user.pw_dir}", f"USER={user.pw_name}", f"LOGNAME={user.pw_name}",
        f"XDG_RUNTIME_DIR={runtime_dir}", f"DBUS_SESSION_BUS_ADDRESS=unix:path={bus_path}",
        "PYTHONDONTWRITEBYTECODE=1", "PYTHONUNBUFFERED=1",
    ]
    if wayland:
        display = _find_wayland_display(uid)
        if display is None:
            raise RuntimeError("The user's Wayland display is not available.")
        command.extend([
            f"WAYLAND_DISPLAY={display}", "QT_QPA_PLATFORM=wayland",
            "XDG_SESSION_TYPE=wayland", "XDG_CURRENT_DESKTOP=KDE",
        ])
    return [*command, *args]


def _wayland_session_ready(uid: int) -> bool:
    """Wait for the session bus, Wayland socket and KWin's D-Bus service."""
    try:
        result = subprocess.run(
            _user_session_command(
                uid, QDBUS6, "org.freedesktop.DBus", "/org/freedesktop/DBus",
                "org.freedesktop.DBus.NameHasOwner", "org.kde.KWin", wayland=True,
            ),
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def _lock_graphical_session(uid: int) -> None:
    session_id = _get_graphical_session_id(uid)
    if session_id is None:
        print("sentineld watchdog: no graphical session to lock.", flush=True)
        return
    print(f"sentineld watchdog: locking session {session_id}.", flush=True)
    subprocess.run([LOGINCTL, "lock-session", session_id], check=True, timeout=5)
