"""Installed package discovery and bounded runtime-family enforcement."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest

from sentinel.guardian_apps import desktop
from sentinel.daemon.operations import portal_ops, guardian_app_ops
from sentinel.daemon.supervision.state import _monitor_state
from sentinel.daemon.core.lock_state import LockMode, LockState
from sentinel.daemon.security import policy
from sentinel.app_blocker.process_monitor import monitor
from sentinel.app_blocker.process_monitor.policy import ProcessBlockPolicy, build_process_policy
from sentinel.app_blocker.AppArmor import block_manager


def test_snap_metadata_and_usr_games_fallback_visible_without_exported_launchers(tmp_path):
    mount = tmp_path / 'snap'
    current = mount / 'firefox/current'
    gui = current / 'meta/gui'
    gui.mkdir(parents=True)
    (current / 'meta/snap.yaml').write_text('name: firefox\n')
    (gui / 'firefox.desktop').write_text('[Desktop Entry]\nType=Application\nName=Firefox\n'
        'Exec=firefox %u\nNoDisplay=true\nIcon=${SNAP}/meta/gui/icon.png\n')
    with (patch.object(desktop, 'SNAP_MOUNT_ROOT', mount),
          patch.object(desktop, 'discover', return_value={}),
          patch.object(desktop.shutil, 'which', return_value='/usr/games/steam') as which,
          patch.object(desktop, 'protected_path', return_value=True),
          patch.object(_monitor_state, 'uid', 1000),
          patch.object(policy, 'get_lock_state', return_value=LockState(1, LockMode.UNLOCKED)),
          patch('subprocess.run') as run):
        output = portal_ops.installed_apps()['apps']
    assert [a['name'] for a in output] == ['Firefox', 'Steam']
    assert all(a['selectable'] for a in output)
    assert output[0]['snap_instance'] == 'firefox'
    assert output[0]['icon'] == str(current / 'meta/gui/icon.png')
    assert output[1]['executable'] == '/usr/games/steam'
    assert any('/usr/games' in call.kwargs.get('path', '') for call in which.call_args_list)
    run.assert_not_called()


def test_blocking_discovery_hidden_and_tombstone_does_not_change_guardian_menus(tmp_path):
    local, system = tmp_path/'local', tmp_path/'system'
    local.mkdir(); system.mkdir()
    (local/'steam.desktop').write_text('[Desktop Entry]\nType=Application\nHidden=true\n')
    (system/'steam.desktop').write_text('[Desktop Entry]\nType=Application\nName=Steam\nExec=/usr/games/steam %U\n')
    (system/'firefox.desktop').write_text('[Desktop Entry]\nType=Application\nName=Firefox\n'
        'Exec=/usr/bin/snap run firefox\nX-SnapInstanceName=firefox\nNoDisplay=true\n')
    with (patch.object(desktop, 'canonical', side_effect=lambda command: command if command.startswith('/') else '/usr/bin/'+command),
          patch.object(desktop, 'protected_path', return_value=True)):
        assert not desktop.discover([local, system])
        choices = desktop.discover([local, system], include_hidden=True)
    assert set(choices) == {'steam.desktop', 'firefox.desktop'}
    assert choices['firefox.desktop']['snap_instance'] == 'firefox'
    assert choices['steam.desktop']['executable'] == '/usr/games/steam'


def proc(pid, executable, uid=1000, args=None):
    p = Mock(pid=pid, info={'exe': executable, 'create_time': float(pid), 'uids': SimpleNamespace(real=uid)})
    p.cmdline.return_value = args or [executable]
    p.environ.return_value = {'SNAP_INSTANCE_NAME': 'firefox', 'STEAM': '1'}
    return p


def test_snap_runtime_matches_labels_revisions_and_parallel_instances_only_for_desktop_uid():
    procs = [proc(1, '/snap/firefox/123/usr/lib/firefox/firefox'),
             proc(2, '/snap/firefox/456/usr/lib/firefox/firefox'),
             proc(3, '/usr/bin/bash'), proc(4, '/snap/firefox_alt/123/bin/firefox'),
             proc(5, '/usr/bin/snap'), proc(6, '/usr/bin/python3'),
             proc(7, '/snap/firefox/123/bin/firefox', uid=0)]
    def label(path, *args, **kwargs):
        return 'snap.firefox.firefox (enforce)' if str(path) == '/proc/3/attr/current' else ''
    rules = ProcessBlockPolicy(frozenset(), frozenset(), frozenset({'firefox'}))
    with patch.object(monitor.psutil, 'process_iter', return_value=procs), patch.object(Path, 'read_text', new=label):
        assert monitor.scan_processes(rules, uid=1000) == 3
    for p in procs[:3]: p.kill.assert_called_once()
    for p in procs[3:]: p.kill.assert_not_called()
    with patch.object(monitor.psutil, 'process_iter', return_value=[procs[0]]):
        assert monitor.scan_processes(rules) == 0  # No guessed user when unconfigured.


def test_steam_runtime_matches_client_helpers_and_exact_script_without_other_shells(tmp_path):
    home = tmp_path/'home'
    root = home/'.local/share/Steam'
    procs = [proc(1, str(root/'ubuntu12_32/steam')),
             proc(2, str(root/'ubuntu12_64/steamwebhelper')),
             proc(3, '/usr/bin/bash', args=['bash', '/usr/games/steam']),
             proc(4, '/usr/bin/bash', args=['bash','-c','echo /usr/games/steam']),
             proc(5, '/usr/bin/bash', args=['bash','/tmp/unrelated','/usr/games/steam']),
             proc(6, '/usr/bin/python3', args=['python3','-m','sentinel.daemon.server']),
             proc(7, str(root/'ubuntu12_32/steam'), uid=0),
             proc(8, '/tmp/steamwebhelper')]
    with (patch.object(desktop, 'user_home', return_value=home),
          patch.object(desktop, 'protected_path', return_value=True),
          patch('sentinel.app_blocker.process_monitor.policy.resolve_executable', return_value='/usr/games/steam')):
        rules, errors = build_process_policy({'steam': {'type': 'native', 'executable': '/usr/games/steam'}})
    assert not errors and rules.steam_launchers == frozenset({'/usr/games/steam'})
    with patch.object(desktop, 'user_home', return_value=home), patch.object(monitor.psutil, 'process_iter', return_value=procs):
        assert monitor.scan_processes(rules, uid=1000) == 3
    for p in procs[:3]: p.kill.assert_called_once()
    for p in procs[3:]: p.kill.assert_not_called()
    with patch.object(monitor.psutil, 'process_iter', return_value=procs):
        assert monitor.scan_processes(ProcessBlockPolicy.empty(), uid=1000) == 0


def test_snap_never_installs_profile_for_shared_snap_runner(tmp_path):
    with (patch.object(block_manager, 'PROFILE_DIR', tmp_path/'profiles'),
          patch.object(block_manager, '_migrate_legacy_profiles', return_value=(0, {})),
          patch.object(block_manager, '_resolve_profile_executable') as resolve,
          patch.object(block_manager, 'block_application') as block):
        status = block_manager.sync_blocked_apps({'firefox': {'type': 'snap', 'snap_instance': 'firefox'}})
    assert status['desired_profiles'] == 0 and not status['unresolved'] and not status['errors']
    assert 'firefox' in status['process_only']
    resolve.assert_not_called(); block.assert_not_called()


def test_guardian_approved_snap_still_respects_explicit_block():
    entry = {'desktop_id': 'firefox.desktop', 'supported': True, 'executable': None,
             'flatpak_id': None, 'snap_instance': 'firefox', 'argv': ['/snap/bin/firefox'], 'type': 'snap'}
    with (patch.object(desktop, 'discover', return_value={'firefox.desktop': entry}),
          patch.object(guardian_app_ops, 'active', return_value=False),
          patch('sentinel.daemon.operations.app_blocker_ops.get_process_policy',
                return_value=ProcessBlockPolicy(frozenset(), frozenset(), frozenset({'firefox'})))):
        with pytest.raises(ValueError, match='blocked'):
            guardian_app_ops.launch_info('firefox.desktop')


@pytest.mark.parametrize('wrapper_name', ['bin_steam.sh', 'renamed-launcher'])
def test_steam_wrapper_alias_is_discovered_and_game_shortcut_cannot_replace_client(tmp_path, wrapper_name):
    wrapper = tmp_path/'usr/lib/steam'/wrapper_name
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text('#!/bin/sh\n')
    wrapper.chmod(0o755)
    alias = tmp_path/'usr/games/steam'
    alias.parent.mkdir(parents=True)
    alias.symlink_to(wrapper)
    menus = tmp_path/'applications'
    menus.mkdir()
    shortcut = menus/'my-game.desktop'
    shortcut.write_text('[Desktop Entry]\nType=Application\nName=A downloaded game\n'
                        f'Exec={alias} steam://rungameid/12345\nIcon=game\n')
    def which(command, path=None):
        if command == str(wrapper):
            return str(wrapper)
        return str(alias) if command in {'steam', str(alias)} else None
    real_discover = desktop.discover
    with (patch.object(desktop.shutil, 'which', side_effect=which),
          patch.object(desktop, 'protected_path', return_value=True),
          patch.object(desktop, 'SNAP_MOUNT_ROOT', tmp_path/'no-snaps'),
          patch.object(desktop, 'discover', side_effect=lambda **kw: real_discover([menus], **kw)),
          patch.object(_monitor_state, 'uid', 1000),
          patch.object(policy, 'get_lock_state', return_value=LockState(1, LockMode.UNLOCKED))):
        choices = portal_ops.installed_apps()['apps']
        assert desktop.parse_launcher(shortcut, 'my-game.desktop')['runtime_family'] == 'steam'
        assert len(choices) == 1 and choices[0]['name'] == 'Steam'
        assert choices[0]['icon'] == 'steam' and choices[0]['selectable']
        assert choices[0]['executable'] == str(wrapper)
        rules, errors = build_process_policy({'steam': choices[0]})
        assert not errors and rules.steam_launchers == frozenset({str(wrapper)})
        script_process = proc(90, '/usr/bin/bash', args=['bash', str(alias)])
        with patch.object(monitor.psutil, 'process_iter', return_value=[script_process]):
            assert monitor.scan_processes(rules, uid=1000) == 1
        script_process.kill.assert_called_once()


@pytest.mark.parametrize('shared', ['bash', 'snap', 'python3', 'plasmashell'])
def test_steam_alias_never_classifies_shared_or_session_runner_as_steam(shared):
    with (patch.object(desktop, 'protected_path', return_value=True),
          patch.object(desktop, 'canonical', return_value='/usr/bin/'+shared)):
        assert not desktop.is_system_steam_launcher('/usr/bin/'+shared)
