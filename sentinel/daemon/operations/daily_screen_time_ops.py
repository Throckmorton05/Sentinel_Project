"""Protected daily desktop allowance, using trusted active-session accounting."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import threading
from zoneinfo import ZoneInfo

from sentinel.activity.store import get_store
from sentinel.daemon.core.lock_state import LockMode, get_lock_state
from sentinel.daemon.security.policy import ChangeKind, PolicyDeniedError, require_policy
from sentinel.dns.storage import atomic_write, read_protected
from sentinel.settings.paths import STATE_DIR

LIMIT_ID = 'daily-screen-time'
_path = STATE_DIR / 'daily-screen-time.json'
_lock = threading.RLock()
_config = {'version': 1, 'enabled': False, 'minutes': 120, 'timezone': 'UTC',
           'revision': 0, 'day_state': {}}


def _validate(data):
    if (not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1
            or type(data.get('enabled')) is not bool
            or type(data.get('minutes')) is not int or not 1 <= data['minutes'] <= 1440
            or type(data.get('revision')) is not int or data['revision'] < 0):
        raise ValueError('Invalid daily screen-time configuration.')
    zone = data.get('timezone')
    if not isinstance(zone, str) or not 1 <= len(zone) <= 100:
        raise ValueError('Invalid daily screen-time timezone.')
    try:
        ZoneInfo(zone)
    except (KeyError, ValueError) as exc:
        raise ValueError('Invalid daily screen-time timezone.') from exc
    state = data.get('day_state')
    if not isinstance(state, dict) or set(state) - {'key', 'bypassed', 'bypass_mode', 'extension_used', 'grace_until'}:
        raise ValueError('Invalid daily screen-time bypass state.')
    if state:
        if (not isinstance(state.get('key'), str) or len(state['key']) > 120
                or type(state.get('bypassed')) is not bool
                or state.get('bypass_mode') not in ('', 'unlocked', 'guardian_lock')
                or type(state.get('extension_used')) is not bool
                or type(state.get('grace_until')) not in (int, float)
                or not math.isfinite(state['grace_until']) or state['grace_until'] < 0):
            raise ValueError('Invalid daily screen-time bypass state.')
    return {key: deepcopy(data[key]) for key in _config}


def initialize(path=None):
    global _path, _config
    with _lock:
        if path is not None:
            _path = Path(path)
        if _path.exists() or _path.is_symlink():
            _config = _validate(json.loads(read_protected(_path)))
            # Older versions kept a hidden daily bypass. Represent it as Off
            # so users can restore enforcement with the normal toggle.
            if _config['day_state'].get('bypassed'):
                new = deepcopy(_config)
                new['enabled'] = False
                new['day_state'].update(bypassed=False, bypass_mode='', grace_until=0)
                new['revision'] += 1
                _commit(new)
        else:
            try:
                zone = str(Path('/etc/localtime').resolve().relative_to('/usr/share/zoneinfo'))
                ZoneInfo(zone)
            except (OSError, ValueError, KeyError):
                zone = 'UTC'
            _commit({'version': 1, 'enabled': False, 'minutes': 120, 'timezone': zone,
                     'revision': 0, 'day_state': {}})


def _commit(data):
    global _config
    atomic_write(_path, json.dumps(data, indent=2, allow_nan=False) + '\n', mode=0o600)
    _config = data


def _today():
    store = get_store()
    now = store.clock()
    zone = ZoneInfo(_config['timezone'])
    day = now.astimezone(zone).date()
    start = datetime.combine(day, datetime.min.time(), zone)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), zone)
    key = day.isoformat() + '|' + _config['timezone']
    state = _config['day_state'] if _config['day_state'].get('key') == key else {
        'key': key, 'bypassed': False, 'bypass_mode': '', 'extension_used': False, 'grace_until': 0}
    mode = get_lock_state().mode
    grace = (mode is not LockMode.GUARDIAN_LOCK and _config['enabled']
             and state['extension_used'] and now.timestamp() < state['grace_until'])
    used = store.daily_screen_seconds(start, end)
    return store, now, start, end, state, mode, grace, used


def status():
    with _lock:
        store, now, start, end, state, mode, grace, used = _today()
        reached = _config['enabled'] and used >= _config['minutes'] * 60
        blocking = reached and not grace
        collection = store.metadata('source:screen_time', {'state': 'waiting'})
        return {'enabled': _config['enabled'], 'minutes': _config['minutes'],
                'timezone': _config['timezone'], 'revision': _config['revision'],
                'date': now.astimezone(ZoneInfo(_config['timezone'])).date().isoformat(),
                'used_seconds': round(used, 2),
                'remaining_seconds': max(0, _config['minutes'] * 60 - used),
                'reset_at': end.timestamp(), 'reached': reached, 'blocking': blocking,
                'bypassed_today': False,
                'grace_until': state['grace_until'] if grace else 0,
                'extension_available': mode is LockMode.SELF_LOCK and not state['extension_used'],
                'mode': mode.value, 'collection': collection,
                'permissions': {'guardian_pin_required': mode is LockMode.GUARDIAN_LOCK,
                                'can_weaken_without_pin': mode is LockMode.UNLOCKED or grace}}


def set_limit(enabled, minutes, timezone_name, *, expected_revision=None, guardian_authenticated=False):
    with _lock:
        new = deepcopy(_config)
        new.update(enabled=enabled, minutes=minutes, timezone=timezone_name)
        _validate(new)
        if expected_revision is not None and (type(expected_revision) is not int
                                              or expected_revision != _config['revision']):
            raise ValueError('Daily limit changed. Reload it before saving.')
        old = _config
        if old['enabled'] and (not enabled or minutes > old['minutes'] or timezone_name != old['timezone']):
            change = ChangeKind.WEAKEN
        elif enabled and (not old['enabled'] or minutes < old['minutes']):
            change = ChangeKind.STRENGTHEN
        else:
            change = ChangeKind.MAINTAIN
        # Match the scheduled screen-time exception: one two-minute chance per
        # day to disable/increase this allowance, without unlocking other policy.
        grace = old['enabled'] and _today()[6]
        if not (get_lock_state().mode is LockMode.SELF_LOCK and grace):
            require_policy('screenlimit.set', change, guardian_authenticated=guardian_authenticated)
        # Explicitly re-enabling restores enforcement, including after a daily
        # bypass. Keep usage and the consumed Self Lock extension for today.
        if enabled and not old['enabled'] and new['day_state']:
            new['day_state'].update(bypassed=False, bypass_mode='', grace_until=0)
        new['revision'] += 1
        _commit(new)
        return status()


def overlay_window():
    # Skip usage reads when the daily limit is off.
    with _lock:
        if not _config['enabled']:
            return None
        value = status()
        if not value['reached']:
            return None
        return {'id': LIMIT_ID, 'name': 'Daily screen-time limit', 'kind': 'daily_limit',
                'mode': value['mode'], 'end_at': value['reset_at'],
                'end_local': datetime.fromtimestamp(value['reset_at'], ZoneInfo(value['timezone'])).isoformat(),
                'blocking': value['blocking'], 'grace_until': value['grace_until'],
                'extension_available': value['extension_available'],
                'used_seconds': value['used_seconds'], 'limit_seconds': value['minutes'] * 60}


def bypass(*, guardian_authenticated=False):
    with _lock:
        _, now, _, end, state, mode, _, used = _today()
        if not _config['enabled'] or used < _config['minutes'] * 60:
            raise ValueError('The daily screen-time limit has not been reached.')
        state = deepcopy(state)
        new = deepcopy(_config)
        if mode is LockMode.SELF_LOCK:
            if state['extension_used']:
                raise PolicyDeniedError('The two-minute extension has already been used today.')
            state['extension_used'] = True
            state['grace_until'] = min(end.timestamp(), now.timestamp() + 120)
        else:
            require_policy('screenlimit.set', ChangeKind.WEAKEN,
                           guardian_authenticated=guardian_authenticated)
            new['enabled'] = False
            state.update(bypassed=False, bypass_mode='', grace_until=0)
        new['day_state'] = state
        new['revision'] += 1
        _commit(new)
