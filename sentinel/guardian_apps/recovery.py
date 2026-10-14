"""Disable-only, PIN-authenticated recovery independent of sentineld.

The systemd socket passes one connection as fd 0. There are no client-selected
paths, commands, lock-mode transitions or default credential initialization.
"""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import socket
import stat
import time

from sentinel.daemon.core.config import load_protected_uid
from sentinel.daemon.core.instance import instance_lock, protected_directory
from sentinel.daemon.core import lock_state
from sentinel.daemon.operations import guardian_app_ops as ops
from sentinel.daemon.security import guardian_auth
from sentinel.daemon.security.peer_auth import _get_peer_credentials
from sentinel.settings.paths import (
    RUN_DIR, GUARDIAN_APPS_FILE, GUARDIAN_AUTH_FILE, LOCK_STATE_FILE,
    GUARDIAN_DESKTOP_STATE_DIR,
)

RECOVERY_SOCKET = RUN_DIR / 'guardian-recovery.sock'
ATTEMPTS_FILE = GUARDIAN_DESKTOP_STATE_DIR / 'recovery-attempts.json'
MAX_BYTES = 2048


class RecoveryError(RuntimeError):
    pass


def _read_line(conn: socket.socket) -> dict:
    data = bytearray()
    while b'\n' not in data:
        block = conn.recv(min(512, MAX_BYTES + 1 - len(data)))
        if not block:
            raise RecoveryError('Incomplete recovery request.')
        data.extend(block)
        if len(data) > MAX_BYTES:
            raise RecoveryError('Recovery message is too large.')
    value = json.loads(bytes(data).split(b'\n', 1)[0])
    if not isinstance(value, dict):
        raise RecoveryError('Invalid recovery message.')
    return value


def _read_json(path: Path) -> dict:
    protected_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'r', encoding='utf-8') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RecoveryError('Recovery state must be a protected root-owned regular file.')
        return json.loads(stream.read(1024 * 1024))


def disable_offline(pin: str, uid: int) -> dict:
    if os.geteuid() != 0:
        raise RecoveryError('Recovery must be handled by the installed root service.')
    protected_uid = load_protected_uid()
    if uid not in {0, protected_uid}:
        raise RecoveryError('This user cannot recover Guardian App Access.')
    # A live daemon (including startup/shutdown) owns this lock. No recovery
    # writer can race its cached policy or a second daemon's initialization.
    with instance_lock():
        protected_directory(LOCK_STATE_FILE.parent)
        state = lock_state._read_state(LOCK_STATE_FILE)
        if state.mode is not lock_state.LockMode.GUARDIAN_LOCK:
            raise RecoveryError('Offline recovery is available only in Guardian Lock.')
        now = time.time()
        attempts = _read_json(ATTEMPTS_FILE) if ATTEMPTS_FILE.exists() else {}
        until = attempts.get('blocked_until', 0)
        if not isinstance(until, (int, float)):
            raise RecoveryError('Invalid recovery attempt state.')
        if until > now:
            raise RecoveryError('Too many incorrect PINs. Try again in five minutes.')
        start = attempts.get('window_start', now)
        failures = attempts.get('failures', 0)
        if (type(failures) is not int or failures < 0
                or not isinstance(start, (int, float))):
            raise RecoveryError('Invalid recovery attempt state.')
        if now - start >= 300:
            start, failures = now, 0
        # Read existing credentials only. An absent/corrupt store must never
        # create a default PIN or silently unlock a protected machine.
        protected_directory(GUARDIAN_AUTH_FILE.parent)
        auth = guardian_auth._read_auth(GUARDIAN_AUTH_FILE)
        if not auth.get('configured', True):
            raise RecoveryError('No Guardian PIN has been configured.')
        try:
            guardian_auth._validate_pin(pin)
            supplied = guardian_auth._hash_pin(pin, bytes.fromhex(auth['salt']))
            valid = hmac.compare_digest(supplied, bytes.fromhex(auth['hash']))
        except ValueError:
            valid = False
        if not valid:
            failures += 1
            ops._atomic(ATTEMPTS_FILE, json.dumps({'window_start': start,
                'failures': failures, 'blocked_until': now + 300 if failures >= 5 else 0}) + '\n')
            raise RecoveryError('Incorrect Guardian PIN.')
        config = ops._validate(_read_json(GUARDIAN_APPS_FILE))
        config['enabled'] = False
        # Persist first: even an interrupted cleanup cannot re-enable at boot.
        ops._atomic(GUARDIAN_APPS_FILE, json.dumps(config, indent=2) + '\n')
        ops._uid, ops._path, ops._config = protected_uid, GUARDIAN_APPS_FILE, config
        with lock_state._state_lock:
            lock_state._current_state = state
            lock_state._current_path = LOCK_STATE_FILE
        result = ops.reconcile()
        ops._atomic(ATTEMPTS_FILE, '{}\n')
        return {'enabled': False, 'errors': [str(error)[:200] for error in result['application']['errors'][:5]],
                'session_restart_required': True}


def handle_connection(conn: socket.socket) -> None:
    conn.settimeout(10)
    try:
        _, uid, _ = _get_peer_credentials(conn)
        # Authenticate the kernel peer before spending time reading its input.
        if uid not in {0, load_protected_uid()}:
            raise RecoveryError('This user cannot recover Guardian App Access.')
        request = _read_line(conn)
        if set(request) != {'version', 'guardian_pin'} or request['version'] != 1:
            raise RecoveryError('Invalid recovery request.')
        result = disable_offline(request['guardian_pin'], uid)
        response = {'ok': True, 'result': result}
    except (OSError, ValueError, RuntimeError) as exc:
        # Never log requests or PINs, including when parsing malformed input.
        response = {'ok': False, 'error': str(exc)}
    conn.sendall(json.dumps(response).encode('utf-8') + b'\n')


def request_disable(pin: str) -> dict:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(20)
            conn.connect(str(RECOVERY_SOCKET))
            conn.sendall(json.dumps({'version': 1, 'guardian_pin': pin}).encode('utf-8') + b'\n')
            response = _read_line(conn)
        if response.get('ok') is not True:
            raise RecoveryError(str(response.get('error', 'Recovery failed.')))
        return response['result']
    except (OSError, ValueError, KeyError) as exc:
        raise RecoveryError('Cannot contact Guardian recovery. Install/start the recovery socket service.') from exc


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit('The recovery helper must run through its installed root service.')
    # stdout belongs to this socket; no ordinary logging goes to it.
    with socket.socket(fileno=os.dup(0)) as conn:
        handle_connection(conn)


if __name__ == '__main__':
    main()
