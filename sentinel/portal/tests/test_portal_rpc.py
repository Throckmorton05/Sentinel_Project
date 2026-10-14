from unittest.mock import patch
import pytest
from sentinel.daemon.core.lock_state import LockMode, LockState
from sentinel.daemon.operations import portal_ops
from sentinel.daemon.rpc import request_handler
from sentinel.daemon.rpc.protocol import encode_message
from sentinel.daemon.security import policy
from sentinel.daemon.security.caller_context import CallerContext


def rpc(action, params=None, caller=None):
    return request_handler._handle_request({'request_id': 'portal', 'action': action, 'params': params or {}},
                                          uid=1000, pid=222, caller=caller)


@pytest.mark.parametrize('mode', list(LockMode))
def test_desktop_status_is_read_only_pin_free_and_bounded(tmp_path, mode):
    services = tmp_path / 'services'
    services.mkdir()
    (services / 'sen-managed-demo.service').write_text('test')
    (services / 'unrelated.service').write_text('test')
    login = tmp_path / 'autologin.conf'
    config = {'enabled': False, 'auto_restart': False, 'cadence': 'weekly', 'time': '03:00', 'last_success': None}
    with (patch.object(portal_ops, 'MANAGED_SERVICE_DIR', services),
          patch.object(portal_ops, 'SDDM_AUTOLOGIN_CONFIG', login),
          patch.object(portal_ops, '_load_config', return_value=config),
          patch.object(policy, 'get_lock_state', return_value=LockState(1, mode))):
        response = rpc('desktop.status')
    assert response['ok']
    assert response['result']['managed_services'] == ['demo']
    assert response['result']['autologin_enabled'] is False
    encode_message(response)


def test_extension_list_is_paginated_and_transport_safe():
    entries = [{'id': str(i), 'description': '長'*1000, 'name': 'x'*1000} for i in range(30)]
    with (patch('sentinel.extension_catalog.catalog.applied_catalog', return_value={
            'extensions': entries, 'required_extensions': [], 'guardian_required': True}),
          patch.object(policy, 'get_lock_state', return_value=LockState(1, LockMode.GUARDIAN_LOCK))):
        output = []
        offset = 0
        while offset is not None:
            response = rpc('browser.extensions.list', {'offset': offset, 'limit': 25})
            assert response['ok']
            encode_message(response)
            output.extend(response['result']['extensions'])
            offset = response['result']['next_offset']
        assert output == entries
        for params in [{'offset': -1}, {'limit': 26}, {'limit': True}]:
            assert not rpc('browser.extensions.list', params)['ok']


@pytest.mark.parametrize('action', ['desktop.status', 'browser.extensions.list'])
def test_new_local_reads_are_not_exposed_to_unpaired_devices(action):
    caller = CallerContext('device', 'test-device', frozenset({'settings.read'}))
    with patch.object(request_handler, 'desktop_status') as desktop, patch.object(request_handler, 'extension_catalog') as catalog:
        response = rpc(action, caller=caller)
    assert not response['ok']
    desktop.assert_not_called()
    catalog.assert_not_called()


def test_extension_reads_are_available_during_admin_mutations():
    with request_handler._mutation_lock, patch.object(request_handler, 'extension_catalog', return_value={'extensions': []}):
        assert rpc('browser.extensions.list')['ok']


def launcher(name, target, **changes):
    result = {'desktop_id': name + '.desktop', 'name': name, 'type': 'native',
              'executable': target, 'supported': True, 'runtime_family': None,
              'source': '[Desktop Entry]\nType=Application\nName=' + name + '\nIcon=kate\n'}
    result.update(changes)
    return result


@pytest.mark.parametrize('mode', list(LockMode))
def test_app_catalog_groups_identities_and_shows_unsupported(mode):
    from sentinel.daemon.supervision.state import _monitor_state
    entries = [launcher('Kate', '/usr/bin/kate'), launcher('Kate session', '/usr/bin/kate'),
               launcher('Flatpak Game', None, type='flatpak', flatpak_id='com.example.Game'),
               launcher('Snap Game', None, type='snap', snap_instance='game'),
               launcher('Script', '/usr/bin/python3', supported=False),
               launcher('Steam', '/usr/bin/steam', runtime_family='steam'),
               launcher('Portal', '/usr/bin/python3', desktop_id='sentinel-portal.desktop')]
    with (patch.object(_monitor_state, 'uid', 1000),
          patch('sentinel.guardian_apps.desktop.discover', return_value={e['desktop_id']: e for e in entries}) as discover,
          patch('sentinel.guardian_apps.desktop.protected_path', return_value=True),
          patch.object(policy, 'get_lock_state', return_value=LockState(1, mode))):
        response = rpc('appblock.catalog')
    assert response['ok']
    discover.assert_called_once_with(uid=1000, include_hidden=True)
    apps = response['result']['apps']
    assert [e['name'] for e in apps] == ['Flatpak Game', 'Kate', 'Portal', 'Snap Game', 'Steam']
    assert {e['name'] for e in apps if e['selectable']} == {'Kate', 'Flatpak Game', 'Snap Game', 'Steam'}
    assert all(e['reason'] for e in apps if not e['selectable'])
    assert all(e['icon'] == 'kate' for e in apps)
    assert all(e['key'].startswith('app_') for e in apps)
    assert not any('source' in e or 'argv' in e for e in apps)
    encode_message(response)


def test_app_catalog_real_discovery_icons_no_launcher_execution(tmp_path):
    from sentinel.daemon.supervision.state import _monitor_state
    from sentinel.guardian_apps import desktop
    (tmp_path / 'kate.desktop').write_text('[Desktop Entry]\nType=Application\nName=Kate\nExec=kate %U\nIcon=/tmp/my-kate.svg\n')
    (tmp_path / 'hidden.desktop').write_text('[Desktop Entry]\nType=Application\nName=Hidden\nExec=kate\nNoDisplay=true\n')
    (tmp_path / 'terminal.desktop').write_text('[Desktop Entry]\nType=Application\nName=Terminal command\nExec=kate\nTerminal=true\n')
    real_discover = desktop.discover
    with (patch.object(_monitor_state, 'uid', 1000),
          patch.object(policy, 'get_lock_state', return_value=LockState(1, LockMode.UNLOCKED)),
          patch.object(desktop, 'canonical', side_effect=lambda command: '/usr/bin/kate' if command == 'kate' else None),
          patch.object(desktop, 'protected_path', return_value=True),
          patch.object(desktop, 'discover', side_effect=lambda uid, **kwargs: real_discover([tmp_path], uid=uid, **kwargs)),
          patch('subprocess.run') as run):
        result = rpc('appblock.catalog')
    assert result['ok']
    assert result['result']['apps'][0]['icon'] == '/tmp/my-kate.svg'
    assert result['result']['total'] == 1
    run.assert_not_called()


def test_app_catalog_pagination_byte_bounds_and_stable_keys():
    from sentinel.daemon.supervision.state import _monitor_state
    entries = [launcher('App' + str(i) + '長'*100, '/usr/bin/app' + str(i),
                        source='[Desktop Entry]\nIcon=' + '長'*3000 + '\n') for i in range(35)]
    inventory = {e['desktop_id']: e for e in entries}
    with (patch.object(_monitor_state, 'uid', 1000),
          patch.object(policy, 'get_lock_state', return_value=LockState(1, LockMode.UNLOCKED)),
          patch('sentinel.guardian_apps.desktop.discover', return_value=inventory)):
        apps = []
        offset = 0
        while offset is not None:
            response = rpc('appblock.catalog', {'offset': offset, 'limit': 25})
            assert response['ok']
            encode_message(response)
            apps.extend(response['result']['apps'])
            offset = response['result']['next_offset']
        assert len(apps) == 35 and len({e['key'] for e in apps}) == 35
        renamed = dict(entries[0], name='Renamed')
        with patch('sentinel.guardian_apps.desktop.discover', return_value={'renamed': renamed}):
            output = rpc('appblock.catalog')['result']['apps'][0]
        assert output['key'] == next(e['key'] for e in apps if e['executable'] == renamed['executable'])
        for params in [{'offset': -1}, {'offset': True}, {'limit': True}, {'limit': 0}, {'limit': 26}]:
            assert not rpc('appblock.catalog', params)['ok']
        assert rpc('appblock.catalog', {'offset': 999})['result']['apps'] == []


def test_app_catalog_concurrent_local_read_only_and_configured_uid():
    from sentinel.daemon.supervision.state import _monitor_state
    with request_handler._mutation_lock, patch.object(request_handler, 'installed_apps', return_value={'apps': []}) as catalog:
        assert rpc('appblock.catalog')['ok']
        device = CallerContext('device', 'test-device', frozenset({'settings.read'}))
        assert not rpc('appblock.catalog', caller=device)['ok']
        assert catalog.call_count == 1
    with (patch.object(_monitor_state, 'uid', None),
          patch.object(policy, 'get_lock_state', return_value=LockState(1, LockMode.UNLOCKED)),
          patch('sentinel.guardian_apps.desktop.discover') as discover):
        assert not rpc('appblock.catalog')['ok']
        discover.assert_not_called()
