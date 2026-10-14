"""Client discovery, stale-write protection and isolated device authorization."""
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import patch

import pytest

from sentinel.daemon import client
from sentinel.daemon.core.lock_state import LockMode, LockState, STATE_VERSION
from sentinel.daemon.operations import settings_ops
from sentinel.daemon.rpc import request_handler
from sentinel.daemon.rpc.protocol import encode_message
from sentinel.daemon.security import policy
from sentinel.daemon.security.caller_context import CallerContext, current_caller


def rpc(action, params=None, caller=None):
    return request_handler._handle_request(
        {"request_id": "test", "action": action, "params": params or {}},
        uid=1000, pid=222, caller=caller,
    )


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('safe_installs = true\n')
    original_set = settings_ops.set_setting
    original_snapshot = settings_ops.get_snapshot
    with (
        patch.object(request_handler, "_set_setting",
                     side_effect=lambda *a, **k: original_set(*a, **k, path=path)),
        patch.object(request_handler, "_get_settings_snapshot",
                     side_effect=lambda: original_snapshot(path=path)),
        patch.object(request_handler, "verify_guardian_pin", return_value=False),
        patch.object(policy, "get_lock_state") as state,
        patch.object(request_handler, "get_lock_state", side_effect=lambda: state()),
    ):
        state.return_value = LockState(version=STATE_VERSION, mode=LockMode.UNLOCKED)
        yield path, state


def test_schema_snapshot_and_aliases(settings):
    path, _ = settings
    schema = rpc('settings.schema')['result']
    snapshot = rpc('settings.snapshot')['result']
    entries = {entry['name']: entry for entry in schema['settings']}
    assert set(entries) == set(snapshot['settings'])
    assert 'dns.browsers_sans_safesearch' not in entries
    assert entries['dns.browsers_sans_safe_search']['aliases'] == ['dns.browsers_sans_safesearch']
    assert all(entry['description'] and entry['type'] == 'boolean' for entry in entries.values())
    assert snapshot['settings']['safe_installs'] is True
    assert snapshot['settings']['dns.enabled'] is True
    assert snapshot['settings']['dns.adult'] is False
    assert len(snapshot['revision']) == 64
    assert settings_ops.get_snapshot(path=path)['revision'] == snapshot['revision']
    encode_message(rpc('settings.schema'))


def test_revision_conflict_preserves_settings(settings):
    path, _ = settings
    revision = rpc('settings.snapshot')['result']['revision']
    assert rpc('settings.set', {'name': 'safe_installs', 'value': False,
                               'expected_revision': revision})['ok']
    contents = path.read_bytes()
    response = rpc('settings.set', {'name': 'safe_installs', 'value': True,
                                    'expected_revision': revision})
    assert response['error']['code'] == 'revision_conflict'
    assert path.read_bytes() == contents
    assert rpc('settings.snapshot')['result']['revision'] != revision
    # Existing clients still work without a revision.
    assert rpc('settings.set', {'name': 'safe_installs', 'value': True})['ok']


def test_revision_noop_and_manual_changes(settings):
    path, _ = settings
    revision = rpc('settings.snapshot')['result']['revision']
    assert rpc('settings.set', {'name': 'safe_installs', 'value': True,
                               'expected_revision': revision})['ok']
    assert rpc('settings.snapshot')['result']['revision'] == revision
    path.write_text('safe_installs = false\n')
    assert rpc('settings.set', {'name': 'safe_installs', 'value': True,
                               'expected_revision': revision})['error']['code'] == 'revision_conflict'


@pytest.mark.parametrize('revision', [True, 1, {}, 'bad', 'A' * 64])
def test_invalid_revision_does_not_write(settings, revision):
    path, _ = settings
    before = path.read_bytes()
    assert not rpc('settings.set', {'name': 'safe_installs', 'value': False,
                                   'expected_revision': revision})['ok']
    assert path.read_bytes() == before


def test_two_clients_cannot_apply_same_stale_revision(settings):
    path, _ = settings
    revision = rpc('settings.snapshot')['result']['revision']
    barrier = threading.Barrier(2)
    def write():
        barrier.wait()
        try:
            settings_ops.set_setting('safe_installs', False, path=path,
                                     expected_revision=revision)
            return 'saved'
        except settings_ops.SettingsRevisionConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: write(), range(2)))
    assert sorted(results) == ['conflict', 'saved']


def test_readonly_device_cannot_write_even_when_unlocked(settings):
    path, _ = settings
    device = CallerContext('device', 'guardian-phone', frozenset({'settings.read'}))
    before = path.read_bytes()
    assert rpc('settings.snapshot', caller=device)['ok']
    with patch.object(request_handler, '_set_setting') as setter:
        result = rpc('settings.set', {'name': 'safe_installs', 'value': False}, device)
    assert result['error']['code'] == 'permission_denied'
    setter.assert_not_called()
    assert path.read_bytes() == before


def test_device_guardian_authorization_and_self_lock(settings):
    path, state = settings
    device = CallerContext('device', 'guardian-phone', frozenset({'settings.write'}))
    state.return_value = LockState(version=STATE_VERSION, mode=LockMode.GUARDIAN_LOCK)
    assert rpc('settings.set', {'name': 'safe_installs', 'value': False}, device)['ok']
    assert settings_ops.get_snapshot(path=path)['settings']['safe_installs'] is False
    state.return_value = LockState(version=STATE_VERSION, mode=LockMode.SELF_LOCK)
    assert rpc('settings.set', {'name': 'safe_installs', 'value': True}, device)['ok']
    assert rpc('settings.set', {'name': 'safe_installs', 'value': False}, device)['error']['code'] == 'permission_denied'


@pytest.mark.parametrize('action', [
    'system.shutdown', 'package.install', 'service.add', 'monitor.register',
    'monitor.heartbeat', 'tamper.blocker.register', 'activity.monitor_block',
    'guardian.change_pin', 'state.exit_guardian_lock', 'guardian.apps.launch',
    'browser.extension.approve', 'unknown.future.action',
])
def test_device_action_allowlist_is_closed(settings, action):
    from sentinel.daemon.security.caller_context import DEVICE_PERMISSIONS
    device = CallerContext('device', 'guardian-phone', DEVICE_PERMISSIONS)
    assert rpc(action, caller=device)['error']['code'] == 'permission_denied'


def test_wire_identity_claims_never_authenticate(settings):
    path, state = settings
    state.return_value = LockState(version=STATE_VERSION, mode=LockMode.GUARDIAN_LOCK)
    before = path.read_bytes()
    assert not rpc('settings.set', {
        'name': 'safe_installs', 'value': False, 'guardian_authenticated': True,
        'caller': {'kind': 'device', 'permissions': ['settings.write']},
        'device_id': 'guardian-phone', 'permissions': ['settings.write'],
    })['ok']
    assert path.read_bytes() == before
    device = CallerContext('device', 'guardian-phone', frozenset({'settings.write'}))
    assert not rpc('settings.set', {'name': 'safe_installs', 'value': False,
                                   'guardian_pin': '1234'}, device)['ok']


def test_device_context_cannot_leak_to_local_request(settings):
    _, state = settings
    state.return_value = LockState(version=STATE_VERSION, mode=LockMode.GUARDIAN_LOCK)
    device = CallerContext('device', 'guardian-phone', frozenset({'settings.write'}))
    assert rpc('settings.set', {'name': 'safe_installs', 'value': False}, device)['ok']
    assert current_caller.get().kind == 'local'
    assert not rpc('settings.set', {'name': 'safe_installs', 'value': True})['ok']


def test_parallel_device_contexts_are_isolated():
    barrier = threading.Barrier(2)
    def capabilities():
        barrier.wait()
        return {'device_id': current_caller.get().device_id}
    def read(device_id):
        device = CallerContext('device', device_id, frozenset({'api.read'}))
        return rpc('api.capabilities', caller=device)['result']['device_id']
    with patch.object(request_handler, 'get_capabilities', side_effect=capabilities):
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(read, ['first', 'second'])) == ['first', 'second']
    assert current_caller.get().kind == 'local'


def test_capabilities_explain_scope_and_no_network_listener():
    device = CallerContext('device', 'guardian-phone', frozenset({'api.read', 'activity.read'}))
    result = rpc('api.capabilities', caller=device)['result']
    assert result['protocol_version'] == 1
    assert result['transport'] == {'unix_socket': True, 'lan_listener': False, 'device_pairing': False}
    assert 'activity.sync' in result['available_companion_actions']
    assert 'settings.set' not in result['available_companion_actions']
    assert result['authorization']['self_lock_is_permanent']
    encode_message(rpc('api.capabilities'))


def test_invalid_internal_context_and_exception_cleanup(settings):
    with pytest.raises(ValueError):
        CallerContext('device', 'phone', frozenset({'arbitrary.root.access'}))
    device = CallerContext('device', 'phone', frozenset({'settings.read'}))
    with patch.object(request_handler, '_get_settings_snapshot', side_effect=RuntimeError('test')):
        with pytest.raises(RuntimeError):
            rpc('settings.snapshot', caller=device)
    assert current_caller.get().kind == 'local'


def test_client_helpers_and_conditional_write_parameters():
    with patch.object(client, 'request', return_value={}) as request:
        client.get_api_capabilities()
        request.assert_called_with('api.capabilities', timeout=3.0)
        client.get_settings_schema()
        request.assert_called_with('settings.schema', timeout=3.0)
        client.get_settings_snapshot()
        request.assert_called_with('settings.snapshot', timeout=3.0)
        client.set_setting('safe_installs', False, expected_revision='a' * 64)
        assert request.call_args.args[1]['expected_revision'] == 'a' * 64
        client.set_setting('safe_installs', True)
        assert 'expected_revision' not in request.call_args.args[1]
        client.sync_activity_events(cursor=None, limit=5)
        request.assert_called_with('activity.sync', {'cursor': None, 'limit': 5})


def test_client_preserves_error_code():
    response = {'version': 1, 'request_id': 'fixed', 'ok': False,
                'error': {'code': 'revision_conflict', 'message': 'Reload settings.'}}
    with (
        patch.object(client.uuid, 'uuid4') as uuid,
        patch.object(client.socket, 'socket'),
        patch.object(client, '_read_message', return_value=encode_message(response).rstrip(b'\n')),
    ):
        uuid.return_value.hex = 'fixed'
        with pytest.raises(client.SentineldError) as error:
            client.set_setting('safe_installs', False)
    assert error.value.code == 'revision_conflict'
