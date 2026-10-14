"""Guardian entry rotates credentials with real persistence and mocked OS effects."""
import os
import threading
from unittest.mock import patch

import pytest

from sentinel.daemon.core import lock_state
from sentinel.daemon.operations import guardian_website_ops
from sentinel.daemon.rpc import request_handler as rpc
from sentinel.daemon.security import guardian_auth as auth


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    for owner, field in ((lock_state, '_current_state'), (lock_state, '_current_path'),
                         (auth, '_current_path')):
        monkeypatch.setattr(owner, field, None)
    monkeypatch.setattr(lock_state, '_validate_parent', lambda path: None)
    monkeypatch.setattr(auth, '_validate_parent', lambda path: None)
    real_fstat = os.fstat
    def root_fstat(fd):
        values = list(real_fstat(fd))
        values[4] = values[5] = 0
        return os.stat_result(values)
    monkeypatch.setattr(os, 'fstat', root_fstat)
    lock_state.initialize_lock_state(tmp_path / 'lock.json')
    auth.initialize_guardian_auth(tmp_path / 'auth.json')
    monkeypatch.setattr(rpc.guardian_app_ops, 'reconcile', lambda: None)
    monkeypatch.setattr(rpc.browser_ops, 'apply_browser_policy', lambda: {})
    monkeypatch.setattr(guardian_website_ops, 'configured_enabled', lambda: False)
    return tmp_path


def request(action, params=None):
    return rpc._handle_request({'request_id': 'pin-setup', 'action': action,
                               'params': params or {}}, uid=1000, pid=222)


def test_first_entry_creates_pin_without_old_credentials_and_unlock_checks_it(fresh):
    assert request('guardian.pin_status')['result'] == {'configured': False, 'mode': 'unlocked'}
    result = request('state.enter_guardian_lock', {'new_pin': '4826', 'pin_confirmation': '4826'})
    assert result['ok'] and result['result']['mode'] == 'guardian_lock'
    assert request('guardian.pin_status')['result'] == {'configured': True, 'mode': 'guardian_lock'}
    assert not request('state.exit_guardian_lock', {'guardian_pin': '1234'})['ok']
    assert not request('state.exit_guardian_lock', {'guardian_authenticated': True})['ok']
    assert request('state.exit_guardian_lock', {'guardian_pin': '4826'})['ok']
    before = (fresh / 'auth.json').read_bytes()
    assert request('state.enter_guardian_lock', {'new_pin': '5678', 'pin_confirmation': '5678'})['ok']
    assert (fresh / 'auth.json').read_bytes() != before
    assert not request('state.exit_guardian_lock', {'guardian_pin': '4826'})['ok']
    assert request('state.exit_guardian_lock', {'guardian_pin': '5678'})['ok']


@pytest.mark.parametrize('configured', [False, True])
@pytest.mark.parametrize('params', [{}, {'guardian_pin': '1234'}, {'guardian_authenticated': True},
    {'new_pin': '4826'}, {'new_pin': '4826', 'pin_confirmation': '5678'},
    {'new_pin': 'abc', 'pin_confirmation': 'abc'}, {'new_pin': '12', 'pin_confirmation': '12'},
    {'new_pin': None, 'pin_confirmation': None}])
def test_invalid_setup_leaves_mode_and_credentials_untouched(fresh, params, configured):
    if configured:
        auth.setup_guardian_pin('9012', '9012')
    before = {name: (fresh / name).read_bytes() for name in ('lock.json', 'auth.json')}
    assert not request('state.enter_guardian_lock', params)['ok']
    assert lock_state.get_lock_state().mode is lock_state.LockMode.UNLOCKED
    assert auth.guardian_pin_configured() is configured
    assert before == {name: (fresh / name).read_bytes() for name in before}


def test_unlocked_entry_rotates_forgotten_pin_but_active_guardian_cannot_reset_it(fresh):
    auth.setup_guardian_pin('4826', '4826')
    result = request('state.enter_guardian_lock', {
        'new_pin': '5678', 'pin_confirmation': '5678',
    })
    assert result['ok']
    assert auth.verify_guardian_pin('5678') and not auth.verify_guardian_pin('4826')
    before = (fresh / 'auth.json').read_bytes()
    for extra in ({}, {'guardian_authenticated': True}, {'replace_existing': True},
                  {'guardian_pin': '5678'}):
        result = request('state.enter_guardian_lock', {
            'new_pin': '7890', 'pin_confirmation': '7890', **extra,
        })
        assert not result['ok'] and result['error']['code'] == 'permission_denied'
        assert (fresh / 'auth.json').read_bytes() == before
    assert not request('guardian.change_pin', {'new_pin': '9012'})['ok']
    assert request('guardian.change_pin', {'new_pin': '9012', 'guardian_pin': '5678'})['ok']
    assert auth.verify_guardian_pin('9012')


def test_self_lock_cannot_set_up_pin_or_enter_guardian(fresh):
    lock_state.enter_self_lock()
    before = (fresh / 'auth.json').read_bytes()
    assert not request('state.enter_guardian_lock', {
        'new_pin': '4826', 'pin_confirmation': '4826',
    })['ok']
    assert (fresh / 'auth.json').read_bytes() == before
    assert lock_state.get_lock_state().mode is lock_state.LockMode.SELF_LOCK


def test_credential_write_failure_never_enables_guardian(fresh):
    with patch.object(auth, '_write_auth', side_effect=OSError('disk full')):
        assert not request('state.enter_guardian_lock', {
            'new_pin': '4826', 'pin_confirmation': '4826',
        })['ok']
    assert not auth.guardian_pin_configured()
    assert lock_state.get_lock_state().mode is lock_state.LockMode.UNLOCKED


def test_mode_write_failure_retains_pin_and_retry_requires_confirmation_again(fresh):
    with patch.object(lock_state, '_write_state', side_effect=OSError('disk full')):
        assert not request('state.enter_guardian_lock', {
            'new_pin': '4826', 'pin_confirmation': '4826',
        })['ok']
    assert lock_state.get_lock_state().mode is lock_state.LockMode.UNLOCKED
    auth.initialize_guardian_auth(fresh / 'auth.json')
    lock_state.initialize_lock_state(fresh / 'lock.json')
    assert auth.verify_guardian_pin('4826')
    assert not request('state.enter_guardian_lock')['ok']
    assert request('state.enter_guardian_lock', {'new_pin': '2468', 'pin_confirmation': '2468'})['ok']
    assert auth.verify_guardian_pin('2468')


def test_entry_replaces_legacy_pin_without_authenticating_it(fresh):
    legacy = auth._build_auth_data('5678')
    legacy['version'] = 1
    del legacy['configured']
    auth._write_auth(legacy, fresh / 'auth.json')
    before = (fresh / 'auth.json').read_bytes()
    assert request('state.enter_guardian_lock', {'new_pin': '9876', 'pin_confirmation': '9876'})['ok']
    assert (fresh / 'auth.json').read_bytes() != before
    assert not request('state.exit_guardian_lock', {'guardian_pin': '5678'})['ok']
    assert not request('state.exit_guardian_lock', {'guardian_pin': '1234'})['ok']
    assert request('state.exit_guardian_lock', {'guardian_pin': '9876'})['ok']


def test_new_pin_authorizes_existing_guardian_owned_windows(fresh, monkeypatch):
    from sentinel.daemon.operations import block_window_ops as windows
    monkeypatch.setattr(windows, '_current_path', None)
    monkeypatch.setattr(windows, '_validate_parent', lambda path: None)
    monkeypatch.setattr(windows, '_reconcile_effective_policy', lambda *args, **kwargs: {})
    monkeypatch.setattr(windows.time, 'time', lambda: 1_800_000_000.0)
    windows.initialize_block_windows(fresh / 'windows.json')
    assert request('state.enter_guardian_lock', {'new_pin': '4826', 'pin_confirmation': '4826'})['ok']
    created = request('blockwindow.create', {
        'name': 'Study', 'start_at': 1_800_000_060.0, 'end_at': 1_800_003_600.0,
        'domains': ['youtube.com'], 'guardian_pin': '4826',
    })
    assert created['ok'], created
    window_id = created['result']['window']['id']
    assert created['result']['window']['lock_type'] == 'guardian_lock'
    assert request('state.exit_guardian_lock', {'guardian_pin': '4826'})['ok']
    assert request('state.enter_guardian_lock', {'new_pin': '9876', 'pin_confirmation': '9876'})['ok']
    saved = (fresh / 'windows.json').read_bytes()
    assert not request('blockwindow.delete', {'id': window_id, 'guardian_pin': '4826'})['ok']
    assert (fresh / 'windows.json').read_bytes() == saved
    assert request('blockwindow.delete', {'id': window_id, 'guardian_pin': '9876'})['ok']


def test_concurrent_entry_cannot_overwrite_the_pin_being_activated(fresh):
    writing, release = threading.Event(), threading.Event()
    real_write = auth._write_auth
    results = []
    def paused_write(data, path):
        writing.set()
        assert release.wait(3)
        real_write(data, path)
    def first_entry():
        results.append(request('state.enter_guardian_lock', {
            'new_pin': '4826', 'pin_confirmation': '4826',
        }))
    with patch.object(auth, '_write_auth', side_effect=paused_write):
        worker = threading.Thread(target=first_entry)
        worker.start()
        try:
            assert writing.wait(3)
            second = request('state.enter_guardian_lock', {
                'new_pin': '9876', 'pin_confirmation': '9876',
            })
            assert not second['ok'] and second['error']['code'] == 'daemon_busy'
        finally:
            release.set()
            worker.join(3)
    assert not worker.is_alive() and results[0]['ok']
    assert auth.verify_guardian_pin('4826') and not auth.verify_guardian_pin('9876')
