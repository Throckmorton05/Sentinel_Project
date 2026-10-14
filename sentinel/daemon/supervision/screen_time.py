"""Retry the screen time overlay without locking or modifying the KDE session."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from sentinel.daemon.core.session import _user_session_command
from sentinel.daemon.operations.block_window_ops import get_screen_time_status

MODULE = 'sentinel.image_blocking.screen_blocker.screen_time_blocker'
_lock = threading.RLock()
_uid = None
_pid = None
_launcher = None
_retry_at = 0.0


def initialize(uid):
    global _uid
    _uid = uid


def _matches(pid):
    if pid is None:
        return False
    try:
        proc = Path(f'/proc/{pid}')
        args = proc.joinpath('cmdline').read_bytes().split(b'\0')
        return proc.stat().st_uid == _uid and any(
            args[i:i+2] == [b'-m', MODULE.encode()] for i in range(len(args)-1))
    except OSError:
        return False


def register_overlay(uid, pid):
    global _pid
    with _lock:
        if uid != _uid or not _matches(pid) or not get_screen_time_status()['blocking']:
            raise ValueError('Unexpected screen time overlay registration.')
        if _matches(_pid) and _pid != pid:
            raise ValueError('A screen time overlay is already registered.')
        _pid = pid


def stop():
    global _pid, _launcher, _retry_at
    with _lock:
        if _matches(_pid):
            try:
                os.kill(_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if _launcher is not None:
            if _launcher.poll() is None:
                try:
                    os.killpg(_launcher.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            _launcher = None
        _pid = None
        _retry_at = 0.0


def tick():
    global _launcher, _pid, _retry_at
    with _lock:
        if not get_screen_time_status()['blocking']:
            stop()
            return
        now = time.monotonic()
        if _matches(_pid):
            return
        vanished = _pid is not None
        if vanished:
            _pid = None
            _retry_at = now
        if now < _retry_at:
            return
        # Screen time is a break reminder, not anti-tamper enforcement. Keep
        # failures visible in the log and retry without locking the desktop,
        # terminating Rofi, or changing KWin's effects.
        if _launcher is not None:
            print('sentineld screen time: overlay stopped or failed to register; retrying.', flush=True)
            if _launcher.poll() is None:
                try:
                    os.killpg(_launcher.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        _pid = None
        _retry_at = now + 5
        try:
            _launcher = subprocess.Popen(_user_session_command(
                _uid, sys.executable, '-I', '-m', MODULE, wayland=True),
                cwd='/', start_new_session=True)
        except (OSError, RuntimeError) as exc:
            print(f'sentineld screen time: overlay pending: {exc}', flush=True)
