from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from sentinel.safe_steam.provision import SAFE_STEAM_PATH, provision, verify_source, verify_integrity
from sentinel.safe_steam.runtime import CONTAINER_EXTENSION_PATH, LOAD_PATHS, validate_load_path
from sentinel.safe_steam.installation import native_steam_installed, native_steam_launcher

SAFE_STEAM_ID = "fbgjjhaoclncakjkdebkllglkgpffkfb"

EXPECTED_SAFE_STEAM_PATH = CONTAINER_EXTENSION_PATH

CHECK_INTERVAL = 0.5
_last_waiting = False
_last_absent = False
SHUTDOWN_TIMEOUT = 15.0


def steam_root() -> Path:
    home = Path.home()

    debian_path = home / ".steam" / "debian-installation"

    if debian_path.exists():
        return debian_path.resolve()

    return home / ".local" / "share" / "Steam"


def preferences_path() -> Path:
    return steam_root() / "config" / "htmlcache" / "Default" / "Preferences"


def load_preferences(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def safe_steam_is_valid(path: Path, extension_path: Path | None = None) -> bool:
    prefs = load_preferences(path)

    if not isinstance(prefs, dict):
        return False

    extensions = prefs.get("extensions", {})
    if not isinstance(extensions, dict) or not isinstance(extensions.get("settings", {}), dict):
        return False
    entry = extensions.get("settings", {}).get(SAFE_STEAM_ID)

    if not isinstance(entry, dict):
        return False

    if entry.get("state") != 1:
        return False

    if entry.get("location") != 4:
        return False

    expected = validate_load_path(extension_path or EXPECTED_SAFE_STEAM_PATH)
    if entry.get("path") != str(expected):
        return False

    return True


def steam_is_running() -> bool:
    result = subprocess.run(
        [
            "pgrep",
            "-u",
            str(os.getuid()),
            "-f",
            "steamwebhelper|ubuntu12_32/steam",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )

    return result.returncode == 0


def shutdown_steam() -> None:
    print("[Safe Steam Watchdog] Shutting Steam down.")

    try:
        # The daemon's explicit user PATH does not necessarily include /usr/games.
        # Use the protected native launcher, rather than a user PATH override.
        launcher = native_steam_launcher()
        if launcher is not None:
            subprocess.run(
                [str(launcher), "-shutdown"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5.0,
            )
    except (OSError, subprocess.TimeoutExpired):
        # Still reach the UID-scoped fallback if Steam's launcher is broken.
        pass

    deadline = time.monotonic() + SHUTDOWN_TIMEOUT

    while time.monotonic() < deadline:
        if not steam_is_running():
            print("[Safe Steam Watchdog] Steam stopped.")
            return

        time.sleep(0.25)

    print("[Safe Steam Watchdog] Steam did not exit normally; sending SIGTERM.")

    uid = str(os.getuid())

    subprocess.run(
        [
            "pkill",
            "-TERM",
            "-u",
            uid,
            "-f",
            "steamwebhelper|ubuntu12_32/steam",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )

    time.sleep(1.0)


def run_provisioner(extension_path: Path = CONTAINER_EXTENSION_PATH) -> bool:
    print("[Safe Steam Watchdog] Running provisioner.")

    try:
        provision(extension_path)
        return True
    except SystemExit as exc:
        print(f"[Safe Steam Watchdog] ERROR: Provisioner exited with code {exc.code}.")
        return False
    except Exception as exc:
        print(
            "[Safe Steam Watchdog] ERROR: "
            f"Provisioner raised an unexpected error: {exc}"
        )
        return False


def repair_safe_steam(path: Path, extension_path: Path = CONTAINER_EXTENSION_PATH) -> None:
    print("[Safe Steam Watchdog] Safe Steam registration is missing or invalid.")

    if steam_is_running():
        shutdown_steam()

    if steam_is_running():
        print("[Safe Steam Watchdog] ERROR: Unable to stop Steam. Repair aborted.")
        return

    if not run_provisioner(extension_path):
        print("[Safe Steam Watchdog] ERROR: Provisioner failed.")
        return

    if not safe_steam_is_valid(path, extension_path):
        print(
            "[Safe Steam Watchdog] ERROR: "
            "Safe Steam is still invalid after provisioning."
        )
        return

    print("[Safe Steam Watchdog] Safe Steam successfully restored.")


def check_once(extension_path: Path | None = None) -> bool:
    """Run as the desktop user. Never let root edit Steam's user profile."""
    global _last_waiting, _last_absent
    _last_waiting = False
    _last_absent = not native_steam_installed()
    if _last_absent:
        return True
    prefs = preferences_path()
    if not prefs.exists():
        _last_waiting = True
        # Let the first launch create its profile before closing Steam for
        # provisioning. Never create a fake profile or repeatedly abort setup.
        return True
    if extension_path is None:
        # Retain a supported saved path while Steam is stopped. sentineld
        # supplies the observed path whenever the main webhelper is running.
        data = load_preferences(prefs)
        try:
            saved = data['extensions']['settings'][SAFE_STEAM_ID]['path']
            extension_path = validate_load_path(saved)
        except (KeyError, TypeError, ValueError):
            extension_path = EXPECTED_SAFE_STEAM_PATH
    extension_path = validate_load_path(extension_path)
    print(f'[Safe Steam Watchdog] Steam load path: {extension_path}')
    try:
        verify_source()
        verify_integrity()
    except (SystemExit, OSError, ValueError):
        if steam_is_running():
            shutdown_steam()
        return False
    if not safe_steam_is_valid(prefs, extension_path):
        repair_safe_steam(prefs, extension_path)
    return safe_steam_is_valid(prefs, extension_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="One check for sentineld supervision")
    parser.add_argument("--stop-for-update", action="store_true", help="Stop Steam before the daemon updates its protected extension assets")
    parser.add_argument('--extension-path', choices=[str(path) for path in LOAD_PATHS],
                        help='Protected extension path observed in Steam\'s filesystem')
    args = parser.parse_args()
    if os.geteuid() == 0:
        raise SystemExit("Run the Steam watchdog as the protected desktop user, not root.")
    if args.stop_for_update:
        if steam_is_running():
            shutdown_steam()
        raise SystemExit(1 if steam_is_running() else 0)
    if args.once:
        valid = check_once(Path(args.extension_path) if args.extension_path else None)
        raise SystemExit(4 if _last_absent else 2 if valid and _last_waiting else 0 if valid else 1)
    print("[Safe Steam Watchdog] Starting.")
    while True:
        check_once(Path(args.extension_path) if args.extension_path else None)
        time.sleep(2.0)


if __name__ == "__main__":
    main()
