"""Portal entry verifies the root-owned PIN without policy side effects."""
import pytest

from sentinel.daemon.core import lock_state
from sentinel.daemon.rpc import request_handler as rpc
from sentinel.daemon.security import guardian_auth as auth
from sentinel.daemon.security.caller_context import CallerContext, DEVICE_PERMISSIONS
from sentinel.daemon.tests.test_guardian_pin_setup import fresh, request


def enable():
    assert request('state.enter_guardian_lock',
                   {'new_pin': '4826', 'pin_confirmation': '4826'})['ok']


@pytest.mark.parametrize('params', [{}, {'guardian_pin': '1234'}, {'guardian_authenticated': True},
    {'guardian_pin': None}, {'guardian_pin': 4826}, {'guardian_pin': 'abcd'},
    {'guardian_pin': '12'}, {'guardian_pin': '４８２６'}])
def test_missing_wrong_and_forged_pin_never_authenticate(fresh, params):
    enable()
    before = {name: (fresh / name).read_bytes() for name in ('auth.json', 'lock.json')}
    response = request('guardian.authenticate', params)
    assert not response['ok'] and response['error']['code'] == 'permission_denied'
    assert 'result' not in response
    assert before == {name: (fresh / name).read_bytes() for name in before}


def test_correct_pin_is_read_only_and_cannot_authorize_later_mutation(fresh):
    enable()
    before = {name: (fresh / name).read_bytes() for name in ('auth.json', 'lock.json')}
    assert request('guardian.authenticate', {'guardian_pin': '4826'})['result'] == {'authenticated': True}
    assert before == {name: (fresh / name).read_bytes() for name in before}
    assert not request('state.exit_guardian_lock')['ok']
    assert not request('state.exit_guardian_lock', {'guardian_authenticated': True})['ok']
    assert lock_state.get_lock_state().mode is lock_state.LockMode.GUARDIAN_LOCK
    auth.change_guardian_pin('4826', '5678')
    assert not request('guardian.authenticate', {'guardian_pin': '4826'})['ok']
    assert request('guardian.authenticate', {'guardian_pin': '5678'})['ok']


@pytest.mark.parametrize('mode', ['unlocked', 'self_lock'])
def test_authentication_never_enables_or_changes_lock_mode(fresh, mode):
    auth.setup_guardian_pin('4826', '4826')
    if mode == 'self_lock':
        lock_state.enter_self_lock()
    assert not request('guardian.authenticate', {'guardian_pin': '4826'})['ok']
    assert lock_state.get_lock_state().mode.value == mode


def test_device_grants_do_not_expose_local_pin_login(fresh):
    enable()
    device = CallerContext('device', 'guardian-phone', DEVICE_PERMISSIONS)
    response = rpc._handle_request({'request_id': 'login', 'action': 'guardian.authenticate',
        'params': {}}, uid=1000, pid=222, caller=device)
    assert not response['ok'] and response['error']['code'] == 'permission_denied'


def test_login_serializes_with_pin_and_mode_changes(fresh):
    enable()
    with rpc._mutation_lock:
        response = request('guardian.authenticate', {'guardian_pin': '4826'})
    assert not response['ok'] and response['error']['code'] == 'daemon_busy'
