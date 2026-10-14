"""Installation state and recent policy-read observations, owned by sentineld."""
import threading
import time

_lock = threading.RLock()
_wake = threading.Event()
_native = {'status': 'pending', 'error': None, 'checked_at': None}
_chrome = {'status': 'pending', 'error': None, 'checked_at': None}
_seen = {}


def set_status(target, status, error=None):
    with _lock:
        value = {'status': status, 'error': error, 'checked_at': time.time()}
        if target == 'steam':
            _native.update(value)
        elif target == 'chrome':
            _chrome.update(value)
        else:
            raise ValueError('Invalid Safe Steam target.')


def request_reconcile(enabled=None):
    # Filtering and installation are separate. Even switching filtering off
    # leaves installation/repair active, and package changes can wake it too.
    set_status('steam', 'pending')
    _wake.set()


def observe(origin):
    identities = {'chrome-extension://fbgjjhaoclncakjkdebkllglkgpffkfb': 'steam',
                  'chrome-extension://jdfobdpedcmlbmolbdfaoaidldkjjfnh': 'chrome'}
    target = identities.get(origin)
    if target:
        with _lock:
            _seen[target] = time.time()


def get_status(enabled):
    now = time.time()
    with _lock:
        result = {}
        for name, values in (('steam', _native), ('chrome', _chrome)):
            last = _seen.get(name)
            result[name] = dict(values,
                runtime='observed' if last and 0 <= now-last <= 20 else 'waiting',
                last_policy_read_at=last)
        return {'enabled': enabled, **result}
