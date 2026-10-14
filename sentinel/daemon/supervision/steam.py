"""Deploy both Safe Steam variants and repair Steam as its desktop user."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from sentinel.daemon.core.session import _get_graphical_session_id, _user_session_command
from sentinel.daemon.operations import browser_ops
from sentinel.daemon.supervision.state import _stop_event
from sentinel.safe_steam import assets, status
from sentinel.safe_steam.runtime import running_extension_path, validate_load_path
from sentinel.safe_steam.installation import native_steam_installed


def protection_enabled():
    try:
        return browser_ops.get_settings()['safe_steam'] is not False
    except (OSError, ValueError):
        return True


def run_user_check(uid, option='--once', *, extension_path=None):
    args = [option]
    if extension_path is not None:
        args.extend(['--extension-path', str(validate_load_path(extension_path))])
    command = _user_session_command(uid, sys.executable, '-I', '-m', 'sentinel.safe_steam.watchdog', *args)
    child = subprocess.Popen(command, cwd='/', start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic()+35.0
        while child.poll() is None and not _stop_event.wait(.25):
            if time.monotonic() >= deadline:
                break
    finally:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        child.wait(timeout=3.0)
    return child.returncode


def check_steam(uid):
    if not native_steam_installed():
        status.set_status('steam', 'not_installed', 'Install native Steam with sen install steam.')
        return
    if _get_graphical_session_id(uid) is None:
        status.set_status('steam', 'waiting', 'Waiting for your desktop login.')
        return
    update_needed = assets.native_update_needed()
    # Observe before any stop-for-update: stopping Steam removes its mount
    # namespace, but the subsequent repair still needs this exact load path.
    extension_path = running_extension_path(uid)
    if update_needed:
        status.set_status('steam', 'pending', 'Updating the Steam extension; Steam may close for repair.')
        result = run_user_check(uid, '--stop-for-update')
        if result != 0:
            status.set_status('steam', 'pending', 'Steam could not be stopped for the extension update.')
            return
        if _stop_event.is_set():
            return
        assets.install_native_assets()
    result = (run_user_check(uid, extension_path=extension_path)
              if extension_path is not None else run_user_check(uid))
    if result == 0:
        status.set_status('steam', 'applied')
    elif result == 2:
        status.set_status('steam', 'waiting', 'Open Steam once to initialize its profile.')
    elif result == 4:
        status.set_status('steam', 'not_installed', 'Install native Steam with sen install steam.')
    else:
        status.set_status('steam', 'pending', 'Steam extension verification or repair failed; retrying automatically.')
        if not _stop_event.is_set():
            print(f'sentineld Steam protection check failed (exit {result}); retrying.', flush=True)


def steam_watchdog_loop(uid):
    while not _stop_event.is_set():
        status._wake.clear()
        try:
            # Chrome keeps the extension installed independently of filtering.
            # Failed asset/policy writes retry instead of relying on another click.
            try:
                assets.install_chrome_assets()
            except (OSError, ValueError) as exc:
                status.set_status('chrome', 'pending', str(exc))
            if status.get_status(protection_enabled())['chrome']['status'] != 'applied':
                browser_ops.apply_browser_policy()
            check_steam(uid)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            status.set_status('steam', 'pending', str(exc))
            print(f'sentineld Steam check failed: {exc}', flush=True)
        if not _stop_event.is_set():
            status._wake.wait(2.0)
