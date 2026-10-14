"""Supervise a separate foreground collector, independent of image filtering."""
import json
import logging
import os
import selectors
import signal
import subprocess
import sys
import time
from datetime import timedelta
from .store import get_store, timestamp
from sentinel.daemon.core.constants import LOGINCTL
from sentinel.daemon.core.session import _get_graphical_session_id, _user_session_command
from sentinel.daemon.supervision.state import _stop_event

_log = logging.getLogger(__name__)
BROWSERS = {'google-chrome', 'google-chrome-stable', 'chrome', 'chromium', 'chromium-browser'}


class ScreenTimeAccumulator:
    def __init__(self, store):
        self.store = store
        self.previous = None

    def reset(self):
        self.previous = None

    def sample(self, sample, *, session_active=True, monotonic=None):
        tick = time.monotonic() if monotonic is None else monotonic
        now = self.store.clock()
        app = sample.get('app', '')
        if not isinstance(app, str) or len(app.encode()) > 256 or type(sample.get('active')) is not bool:
            raise ValueError('Invalid foreground sample.')
        active = sample['active'] and session_active
        domain = ''
        if active and app.lower() in BROWSERS:
            focus = self.store.metadata('browser_focus', {})
            if focus.get('observed_at') and 0 <= (now-timestamp(focus['observed_at'])).total_seconds() <= 40:
                domain = focus.get('domain', '')
        previous = self.previous
        self.previous = (tick, now, active, app, domain)
        if previous:
            old_tick, old_time, old_active, old_app, old_domain = previous
            elapsed, wall = tick-old_tick, (now-old_time).total_seconds()
            # Never bridge worker restarts, logout, lock, suspend or clock jumps.
            if old_active and active and 0 < elapsed <= 15 and abs(wall-elapsed) < 2:
                self.store.record_usage(old_time, now, old_app, old_domain)
        self.store.source_status('screen_time', 'active' if active else 'paused')


def session_in_use(session):
    result = subprocess.run([LOGINCTL, 'show-session', session, '-p', 'Active',
        '-p', 'LockedHint', '-p', 'IdleHint'], capture_output=True, text=True, timeout=3, check=False)
    properties = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    return (result.returncode == 0 and properties.get('Active') == 'yes'
            and properties.get('LockedHint') == 'no' and properties.get('IdleHint') == 'no')


def stop_worker(child):
    if child is not None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=1)
        except ProcessLookupError:
            pass
        finally:
            if child.stdout:
                child.stdout.close()


def screen_time_loop(uid):
    child = None
    try:
        while not _stop_event.is_set():
            try:
                session = _get_graphical_session_id(uid)
                if not session:
                    get_store().source_status('screen_time', 'waiting', reason='Waiting for your desktop session.')
                    _stop_event.wait(5)
                    continue
                command = _user_session_command(uid, sys.executable, '-I', '-m',
                    'sentinel.activity.session_worker', wayland=True)
                child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    start_new_session=True)
                accumulator = ScreenTimeAccumulator(get_store())
                # Nonblocking bytes avoid hanging on a partial/broken stdout line.
                os.set_blocking(child.stdout.fileno(), False)
                buffer = b''
                last = time.monotonic()
                with selectors.DefaultSelector() as selector:
                    selector.register(child.stdout, selectors.EVENT_READ)
                    while not _stop_event.is_set():
                        if child.poll() is not None or time.monotonic()-last > 20:
                            raise RuntimeError('Desktop collector stopped responding.')
                        if not selector.select(timeout=1):
                            continue
                        chunk = os.read(child.stdout.fileno(), 4096)
                        if not chunk:
                            raise RuntimeError('Desktop collector disconnected.')
                        buffer += chunk
                        if len(buffer) > 8192:
                            raise ValueError('Invalid desktop collector output.')
                        while b'\n' in buffer:
                            line, buffer = buffer.split(b'\n', 1)
                            if _get_graphical_session_id(uid) != session:
                                raise RuntimeError('Desktop session changed.')
                            from sentinel.daemon.operations.block_window_ops import get_screen_time_status
                            active = session_in_use(session) and not get_screen_time_status()['blocking']
                            accumulator.sample(json.loads(line), session_active=active)
                            last = time.monotonic()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                _log.warning('Screen-time collector unavailable (%s); retrying.', type(exc).__name__)
                try:
                    get_store().source_status('screen_time', 'unavailable', reason='Waiting for the KDE desktop collector.')
                except (OSError, ValueError):
                    pass
            finally:
                stop_worker(child)
                child = None
            _stop_event.wait(5)
    finally:
        stop_worker(child)
