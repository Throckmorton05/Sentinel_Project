"""Reproduce Steam's host /usr being available only below /run/host."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from sentinel.safe_steam import provision, runtime, watchdog
from sentinel.daemon.supervision import steam
from sentinel.safe_steam import assets, status


@pytest.fixture(autouse=True)
def installed_native_steam():
    steam._stop_event.clear()
    with (patch.object(watchdog, 'native_steam_installed', return_value=True),
          patch.object(steam, 'native_steam_installed', return_value=True)):
        yield


def fake_helper(proc, uid=1000, *, kind=None, executable='steamwebhelper', name='100'):
    process = proc / name
    process.mkdir(parents=True)
    (process / 'comm').write_text('steamwebhelper\n')
    (process / 'exe').symlink_to('/fake/bin/' + executable)
    (process / 'status').write_text(f'Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n')
    args = b'steamwebhelper\0' + (f'--type={kind}\0'.encode() if kind else b'')
    (process / 'cmdline').write_bytes(args)
    (process / 'root').mkdir()
    return process


def expose(process, path, source):
    visible = process / 'root' / path.relative_to('/')
    visible.parent.mkdir(parents=True, exist_ok=True)
    visible.symlink_to(source, target_is_directory=True)


@pytest.mark.parametrize('visible_path', runtime.LOAD_PATHS)
def test_detects_exact_host_assets_in_helper_namespace(tmp_path, visible_path):
    source = tmp_path / 'protected'; source.mkdir()
    proc = tmp_path / 'proc'
    process = fake_helper(proc)
    expose(process, visible_path, source)
    with patch.object(runtime, 'HOST_EXTENSION_PATH', source):
        assert runtime.running_extension_path(1000, proc=proc) == visible_path


def test_directory_with_same_name_but_different_assets_is_rejected(tmp_path):
    source = tmp_path / 'protected'; source.mkdir()
    unrelated = tmp_path / 'unrelated'; unrelated.mkdir()
    proc = tmp_path / 'proc'
    process = fake_helper(proc)
    expose(process, runtime.HOST_EXTENSION_PATH, unrelated)
    with patch.object(runtime, 'HOST_EXTENSION_PATH', source):
        with pytest.raises(RuntimeError, match='cannot see'):
            runtime.running_extension_path(1000, proc=proc)


def test_ignores_other_users_renderer_processes_and_renamed_programs(tmp_path):
    fake_helper(tmp_path, uid=1001, name='1')
    fake_helper(tmp_path, kind='renderer', name='2')
    fake_helper(tmp_path, executable='unrelated-program', name='3')
    assert runtime.running_extension_path(1000, proc=tmp_path) is None


@pytest.mark.parametrize('path', ['/home/test_user/custom-extension', '/run/host/tmp/extension',
                                 '/usr/share/sentinel/safe_steam/steam_extension/../other'])
def test_arbitrary_load_paths_are_rejected(path):
    with pytest.raises(ValueError):
        runtime.validate_load_path(path)


def write_registration(path, extension_path):
    path.write_text(json.dumps({'extensions': {'settings': {provision.SAFE_STEAM_ID:
        {'path': str(extension_path), 'state': 1, 'location': 4}}}}))


def test_old_host_registration_is_invalid_for_container(tmp_path):
    prefs = tmp_path / 'Preferences'
    write_registration(prefs, runtime.HOST_EXTENSION_PATH)
    assert not watchdog.safe_steam_is_valid(prefs, runtime.CONTAINER_EXTENSION_PATH)
    assert watchdog.safe_steam_is_valid(prefs, runtime.HOST_EXTENSION_PATH)
    write_registration(prefs, runtime.CONTAINER_EXTENSION_PATH)
    assert watchdog.safe_steam_is_valid(prefs, runtime.CONTAINER_EXTENSION_PATH)


def test_observed_container_path_repairs_old_registration(tmp_path):
    prefs = tmp_path / 'Preferences'
    write_registration(prefs, runtime.HOST_EXTENSION_PATH)
    def repair(path, extension_path):
        write_registration(path, extension_path)
    with (patch.object(watchdog, 'preferences_path', return_value=prefs),
          patch.object(watchdog, 'verify_source'), patch.object(watchdog, 'verify_integrity'),
          patch.object(watchdog, 'repair_safe_steam', side_effect=repair) as fixed):
        assert watchdog.check_once(runtime.CONTAINER_EXTENSION_PATH)
    fixed.assert_called_once_with(prefs, runtime.CONTAINER_EXTENSION_PATH)


@pytest.mark.parametrize('load_path', runtime.LOAD_PATHS)
def test_stopped_steam_retains_supported_registration(tmp_path, load_path):
    prefs = tmp_path / 'Preferences'
    write_registration(prefs, load_path)
    with (patch.object(watchdog, 'preferences_path', return_value=prefs),
          patch.object(watchdog, 'verify_source'), patch.object(watchdog, 'verify_integrity'),
          patch.object(watchdog, 'repair_safe_steam') as repair):
        assert watchdog.check_once()
    repair.assert_not_called()


def test_provisioner_writes_runtime_path_but_verifies_host_assets(tmp_path):
    prefs = tmp_path / 'config/htmlcache/Default/Preferences'
    prefs.parent.mkdir(parents=True)
    prefs.write_text('{"other_setting": true}')
    template = tmp_path / 'template.json'
    template.write_text('{"path":"@SAFE_STEAM_PATH@","state":1,"location":4}')
    with (patch.object(provision.os, 'geteuid', return_value=1000),
          patch.object(provision, 'steam_is_running', return_value=False),
          patch.object(provision, 'steam_root', return_value=tmp_path),
          patch.object(provision, 'PREF_TEMPLATE', template),
          patch.object(provision, 'verify_source') as source,
          patch.object(provision, 'verify_integrity') as integrity):
        provision.provision()
    source.assert_called_once(); integrity.assert_called_once()
    assert json.loads(prefs.read_text())['other_setting'] is True
    assert watchdog.safe_steam_is_valid(prefs, runtime.CONTAINER_EXTENSION_PATH)
    assert json.loads(prefs.with_name('Preferences.safesteam-backup').read_text()) == {'other_setting': True}


def test_provisioner_rejects_root_before_touching_profile():
    with patch.object(provision.os, 'geteuid', return_value=0), patch.object(provision, 'steam_root') as home:
        with pytest.raises(SystemExit):
            provision.provision()
    home.assert_not_called()


def test_repair_keeps_observed_path_across_steam_shutdown(tmp_path):
    prefs = tmp_path / 'Preferences'; write_registration(prefs, runtime.HOST_EXTENSION_PATH)
    def install(load_path):
        write_registration(prefs, load_path)
        return True
    with (patch.object(watchdog, 'steam_is_running', side_effect=[True, False]),
          patch.object(watchdog, 'shutdown_steam') as stop,
          patch.object(watchdog, 'run_provisioner', side_effect=install) as installer):
        watchdog.repair_safe_steam(prefs, runtime.CONTAINER_EXTENSION_PATH)
    stop.assert_called_once()
    installer.assert_called_once_with(runtime.CONTAINER_EXTENSION_PATH)
    assert watchdog.safe_steam_is_valid(prefs, runtime.CONTAINER_EXTENSION_PATH)


@pytest.mark.parametrize('load_path', runtime.LOAD_PATHS)
def test_daemon_preserves_observed_path_before_stopping_for_update(load_path):
    calls = []
    def observe(uid):
        calls.append('observe')
        return load_path
    def run(uid, option='--once', **kwargs):
        calls.append((option, kwargs))
        return 0
    with (patch.object(steam, 'protection_enabled', return_value=True),
          patch.object(steam, '_get_graphical_session_id', return_value='3'),
          patch.object(assets, 'native_update_needed', return_value=True),
          patch.object(assets, 'install_native_assets', side_effect=lambda: calls.append('install')),
          patch.object(steam, 'running_extension_path', side_effect=observe),
          patch.object(steam, 'run_user_check', side_effect=run)):
        steam.check_steam(1000)
    assert calls == ['observe', ('--stop-for-update', {}), 'install',
                     ('--once', {'extension_path': load_path})]
    assert status.get_status(True)['steam']['status'] == 'applied'


def test_hidden_assets_prevent_false_success():
    with (patch.object(steam, 'protection_enabled', return_value=True),
          patch.object(steam, '_get_graphical_session_id', return_value='3'),
          patch.object(assets, 'native_update_needed', return_value=False),
          patch.object(steam, 'running_extension_path', side_effect=RuntimeError('hidden assets')),
          patch.object(steam, 'run_user_check') as run):
        with pytest.raises(RuntimeError, match='hidden assets'):
            steam.check_steam(1000)
    run.assert_not_called()
