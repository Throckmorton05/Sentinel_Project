"""VM policy integration; device/process effects are always isolated."""
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from sentinel import vm_protection as inventory
from sentinel.app_blocker.process_monitor import monitor
from sentinel.app_blocker.process_monitor.policy import ProcessBlockPolicy
from sentinel.daemon.core.lock_state import LockMode, LockState, STATE_VERSION
from sentinel.daemon.operations import (app_blocker_ops, appstore_ops, package_ops,
                                        settings_ops, vm_ops)
from sentinel.daemon.security import policy


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(vm_ops, 'UDEV_FILE', tmp_path / 'udev.rules')
    monkeypatch.setattr(vm_ops, 'POLKIT_FILE', tmp_path / 'polkit.rules')
    monkeypatch.setattr(vm_ops, 'DEV_DIR', tmp_path / 'dev')
    monkeypatch.setattr(vm_ops, '_enabled', False)
    monkeypatch.setattr(vm_ops, '_application', {'status': 'inactive', 'enabled': False, 'error': None})
    run, device = Mock(), Mock()
    monkeypatch.setattr(vm_ops, '_run', run)
    monkeypatch.setattr(vm_ops, '_restrict_device', device)
    return SimpleNamespace(run=run, device=device, root=tmp_path)


def test_default_and_invalid_values_leave_devices_alone(runtime):
    vm_ops.apply_vm_policy(settings={})
    assert not vm_ops.protection_enabled()
    runtime.run.assert_not_called()
    for value in ('true', 1, None):
        with pytest.raises(ValueError, match='boolean'):
            vm_ops.apply_vm_policy(settings={'vm_protection': value})
    assert not vm_ops.UDEV_FILE.exists()


def test_enable_repair_and_disable_only_managed_rules(runtime):
    unrelated = runtime.root / 'unrelated.rules'
    unrelated.write_text('Keep me')
    vm_ops.apply_vm_policy(settings={'vm_protection': True})
    assert vm_ops.protection_enabled()
    assert vm_ops.UDEV_FILE.read_text() == vm_ops.UDEV_RULE
    assert vm_ops.POLKIT_FILE.read_text() == vm_ops.POLKIT_RULE
    assert stat.S_IMODE(vm_ops.UDEV_FILE.stat().st_mode) == 0o644
    assert runtime.device.call_count == len(vm_ops.DEVICE_NAMES)
    vm_ops.apply_vm_policy(settings={'vm_protection': True})
    assert runtime.device.call_count == 2 * len(vm_ops.DEVICE_NAMES)
    vm_ops.apply_vm_policy(settings={'vm_protection': False})
    assert not vm_ops.protection_enabled()
    assert not vm_ops.UDEV_FILE.exists() and not vm_ops.POLKIT_FILE.exists()
    assert unrelated.read_text() == 'Keep me'
    for name in vm_ops.DEVICE_NAMES:
        runtime.run.assert_any_call('udevadm', 'trigger', '--action=add',
                                    '--subsystem-match=misc', f'--sysname-match={name}')
    assert not any('rmmod' in call.args or 'modprobe' in call.args for call in runtime.run.call_args_list)


def test_failed_enforcement_is_pending_and_retried(runtime):
    runtime.run.side_effect = ValueError('udev unavailable')
    with pytest.raises(ValueError, match='udev unavailable'):
        vm_ops.apply_vm_policy(settings={'vm_protection': True})
    assert vm_ops.application_status()['status'] == 'pending'
    assert vm_ops.protection_enabled()  # Name-based process barrier remains on.
    runtime.run.side_effect = None
    vm_ops.apply_vm_policy(settings={'vm_protection': True})
    assert vm_ops.application_status()['status'] == 'active'


def test_device_open_is_nofollow_and_requires_character_device(tmp_path):
    node = tmp_path / 'kvm'
    node.write_bytes(b'ordinary file')
    real_open = os.open
    with patch.object(vm_ops, '_run') as run, patch.object(vm_ops.os, 'open', wraps=real_open) as opened:
        with pytest.raises(ValueError, match='character device'):
            vm_ops._restrict_device(node)
        assert opened.call_args.args[1] & os.O_NOFOLLOW
        run.assert_not_called()
        link = tmp_path / 'symlink'; link.symlink_to(node)
        with pytest.raises(OSError):
            vm_ops._restrict_device(link)
        vm_ops._restrict_device(tmp_path / 'absent')
    assert node.read_bytes() == b'ordinary file'


def test_device_acl_and_permissions_use_the_same_open_descriptor(tmp_path):
    node = tmp_path / 'kvm'; node.write_bytes(b'fixture')
    with (patch.object(vm_ops.os, 'fstat', return_value=SimpleNamespace(st_mode=stat.S_IFCHR)),
          patch.object(vm_ops, '_run') as run, patch.object(vm_ops.os, 'fchown') as chown,
          patch.object(vm_ops.os, 'fchmod') as chmod):
        vm_ops._restrict_device(node)
    fd = chown.call_args.args[0]
    run.assert_called_once_with('setfacl', '-b', f'/proc/self/fd/{fd}', pass_fds=(fd,))
    chown.assert_called_once_with(fd, 0, 0)
    chmod.assert_called_once_with(fd, 0o600)
    with pytest.raises(OSError):
        os.fstat(fd)  # Descriptor is closed on completion.


@pytest.mark.parametrize('mode,auth,old,new,allowed', [
    (LockMode.UNLOCKED, False, False, True, True),
    (LockMode.UNLOCKED, False, True, False, True),
    (LockMode.SELF_LOCK, False, False, True, True),
    (LockMode.SELF_LOCK, False, True, False, False),
    (LockMode.GUARDIAN_LOCK, False, False, True, False),
    (LockMode.GUARDIAN_LOCK, True, False, True, True),
    (LockMode.GUARDIAN_LOCK, False, True, False, False),
    (LockMode.GUARDIAN_LOCK, True, True, False, True),
])
def test_real_lock_policy_applies_before_enforcement(runtime, mode, auth, old, new, allowed):
    file = runtime.root / 'settings.toml'
    file.write_text(f'vm_protection = {str(old).lower()}\n')
    with (patch.object(policy, 'get_lock_state', return_value=LockState(STATE_VERSION, mode)),
          patch.object(vm_ops, 'reconcile_tools') as reconcile):
        if allowed:
            settings_ops.set_setting('vm_protection', new, path=file, guardian_authenticated=auth)
            assert settings_ops._load_settings(file)['vm_protection'] is new
            reconcile.assert_called_once_with()
        else:
            with pytest.raises(policy.PolicyDeniedError):
                settings_ops.set_setting('vm_protection', new, path=file, guardian_authenticated=auth)
            reconcile.assert_not_called()
            runtime.run.assert_not_called()
            assert settings_ops._load_settings(file)['vm_protection'] is old


def test_enforcement_and_write_failures_preserve_settings(runtime):
    file = runtime.root / 'settings.toml'; file.write_text('vm_protection = false\n')
    with (patch.object(policy, 'get_lock_state', return_value=LockState(STATE_VERSION, LockMode.UNLOCKED)),
          patch.object(vm_ops, 'reconcile_tools') as reconcile):
        runtime.run.side_effect = ValueError('failure')
        with pytest.raises(ValueError):
            settings_ops.set_setting('vm_protection', True, path=file)
        assert settings_ops._load_settings(file)['vm_protection'] is False
        reconcile.assert_not_called()
        runtime.run.side_effect = None
        with patch.object(settings_ops, '_write_settings', side_effect=OSError('disk full')):
            with pytest.raises(OSError, match='disk full'):
                settings_ops.set_setting('vm_protection', True, path=file)
        assert not vm_ops.protection_enabled()
        assert not vm_ops.UDEV_FILE.exists()
        assert settings_ops._load_settings(file)['vm_protection'] is False


def test_noop_setting_retries_tool_enforcement(runtime):
    file = runtime.root / 'settings.toml'; file.write_text('vm_protection = true\n')
    with (patch.object(policy, 'get_lock_state', return_value=LockState(STATE_VERSION, LockMode.SELF_LOCK)),
          patch.object(vm_ops, 'reconcile_tools') as reconcile):
        settings_ops.set_setting('vm_protection', True, path=file)
        assert vm_ops.protection_enabled()
        reconcile.assert_called_once_with()


def test_write_error_after_replace_follows_the_value_actually_on_disk(runtime):
    file = runtime.root / 'settings.toml'; file.write_text('vm_protection = false\n')
    original = settings_ops._write_settings
    def replaced_then_failed(settings, path):
        original(settings, path)
        raise OSError('directory fsync failed')
    with (patch.object(policy, 'get_lock_state', return_value=LockState(STATE_VERSION, LockMode.UNLOCKED)),
          patch.object(settings_ops, '_write_settings', side_effect=replaced_then_failed)):
        with pytest.raises(OSError, match='fsync failed'):
            settings_ops.set_setting('vm_protection', True, path=file)
    assert settings_ops._load_settings(file)['vm_protection'] is True
    assert vm_ops.protection_enabled()
    assert vm_ops.UDEV_FILE.exists()


@pytest.mark.parametrize('enabled', [False, True])
def test_restart_loads_the_persisted_policy(runtime, enabled):
    with patch.object(settings_ops, '_load_settings', return_value={'vm_protection': enabled}):
        vm_ops.apply_vm_policy()
    assert vm_ops.protection_enabled() is enabled
    assert vm_ops.UDEV_FILE.exists() is enabled


def test_schema_defaults_do_not_require_a_config_migration(runtime):
    definition = next(entry for entry in settings_ops.get_schema()['settings']
                      if entry['name'] == 'vm_protection')
    assert definition['default'] is False
    assert definition['changes'] == {'enable': 'strengthen', 'disable': 'weaken'}
    file = runtime.root / 'settings.toml'; file.write_text('boot_media_protection = false\n')
    with patch.object(policy, 'get_lock_state', return_value=LockState(STATE_VERSION, LockMode.UNLOCKED)):
        assert settings_ops.get_snapshot(path=file)['settings']['vm_protection'] is False


@pytest.mark.parametrize('name', ['qemu-system-x86_64', 'qemu-system-aarch64', 'qemu-kvm',
                                 'VirtualBox', 'VBoxManage', 'VBoxHeadless', 'vmware-vmx',
                                 'virt-manager', 'gnome-boxes', 'qemu-img'])
def test_known_engines_match_downloaded_and_deleted_executables(name):
    assert inventory.is_vm_tool('/home/user/Downloads/' + name)
    assert inventory.is_vm_tool('/usr/bin/' + name + ' (deleted)')


@pytest.mark.parametrize('name', ['python3.14', 'bash', 'kate', 'qemu-x86_64',
                                 'virtualbox-guest-utils', 'retroarch'])
def test_unrelated_programs_and_guest_helpers_are_not_vm_engines(name):
    assert not inventory.is_vm_tool('/usr/bin/' + name)


def fake_process(exe, command=None, cwd='/home/user'):
    proc = Mock()
    proc.pid = 45
    proc.info = {'exe': exe, 'create_time': 1, 'uids': SimpleNamespace(real=1000)}
    proc.cmdline.return_value = command or [exe]
    proc.cwd.return_value = cwd
    proc.environ.return_value = {}
    return proc


def test_script_operand_matching_never_blocks_shared_python_or_argument_text():
    assert inventory.vm_process(fake_process('/usr/bin/python3.14',
        ['python3', '-I', '/usr/bin/virt-manager']))
    assert inventory.vm_process(fake_process('/usr/bin/python3.14',
        ['python3', './virt-manager']))
    assert inventory.vm_process(fake_process('/usr/bin/python3.14',
        ['python3', '-m', 'virtManager']))
    assert not inventory.vm_process(fake_process('/usr/bin/python3.14',
        ['python3', '/home/user/test.py', '/usr/bin/virt-manager']))
    assert not inventory.vm_process(fake_process('/usr/bin/bash',
        ['bash', '-c', 'echo /usr/bin/qemu-system-x86_64']))
    assert not inventory.vm_process(fake_process('/usr/bin/kate',
        ['kate', '/usr/bin/virt-manager']))


def test_process_watchdog_stops_existing_and_new_vm_tools_but_keeps_editor():
    engine = fake_process('/home/user/qemu-system-x86_64')
    script = fake_process('/usr/bin/python3.14', ['python3', '/usr/bin/virt-manager']); script.pid = 46
    editor = fake_process('/usr/bin/kate', ['kate', '/home/user/qemu-system-x86_64']); editor.pid = 47
    with patch.object(monitor.psutil, 'process_iter', return_value=[engine, script, editor]):
        assert monitor.scan_processes(ProcessBlockPolicy(frozenset(), frozenset(), vm_tools=True)) == 2
    engine.kill.assert_called_once_with(); script.kill.assert_called_once_with()
    editor.kill.assert_not_called()
    with patch.object(monitor.psutil, 'process_iter') as processes:
        assert monitor.scan_processes(ProcessBlockPolicy.empty()) == 0
        processes.assert_not_called()


def test_vm_baseline_cannot_be_removed_via_custom_block_list(runtime):
    entry = {'name': 'QEMU', 'type': 'native', 'executable': '/usr/bin/qemu-system-x86_64'}
    with (patch.object(app_blocker_ops, 'installed_browser_blocks', return_value={}),
          patch.object(app_blocker_ops, 'installed_vm_blocks', return_value={'vm_protection_0': entry}),
          patch('sentinel.daemon.operations.block_window_ops.get_active_apps', return_value={})):
        vm_ops._enabled = True
        assert app_blocker_ops._with_scheduled_apps({'vm_protection_0': {'name': 'fake'}})['vm_protection_0'] == entry
        assert app_blocker_ops.get_process_policy().vm_tools
        vm_ops._enabled = False
        assert app_blocker_ops._with_scheduled_apps({}) == {}
        assert not app_blocker_ops.get_process_policy().vm_tools


def test_vm_flatpaks_and_snap_instances_join_runtime_policy(runtime):
    file = runtime.root / 'apps.toml'; file.write_text('[apps]\n')
    vm_ops._enabled = True
    with (patch.object(app_blocker_ops, 'BLOCKED_APPS_FILE', file),
          patch.object(app_blocker_ops, 'installed_vm_blocks', return_value={}),
          patch.object(app_blocker_ops, 'installed_browser_blocks', return_value={}),
          patch.object(app_blocker_ops, '_runtime_policy', ProcessBlockPolicy.empty()),
          patch('sentinel.daemon.operations.block_window_ops.get_active_apps', return_value={})):
        result = app_blocker_ops.refresh_process_policy()
        assert inventory.VM_FLATPAK_IDS <= result.flatpak_ids
        assert inventory.VM_SNAP_INSTANCES <= result.snap_instances
        vm_ops._enabled = False
        result = app_blocker_ops.refresh_process_policy()
        assert not inventory.VM_FLATPAK_IDS & result.flatpak_ids
        assert not result.vm_tools


def test_disable_forces_only_vm_profile_removal(runtime):
    entry = {'name': 'QEMU', 'type': 'native', 'executable': '/usr/bin/qemu-system-x86_64'}
    with (patch.object(inventory, 'installed_vm_blocks', return_value={'vm': entry}),
          patch.object(app_blocker_ops, 'managed_profile_name', return_value='vm-profile'),
          patch.object(app_blocker_ops, 'reconcile_app_blocker') as reconcile):
        vm_ops.reconcile_tools()
        reconcile.assert_called_once_with(force_remove_profiles={'vm-profile'})


@pytest.mark.parametrize('package', ['qemu-kvm', 'qemu-system-x86:amd64', 'virtualbox-7.9',
                                    'libvirt-daemon-system', 'virt-manager', 'gnome-boxes'])
def test_install_guard_cannot_be_bypassed_by_unlocked_or_guardian(runtime, package):
    vm_ops._enabled = True
    with patch.object(package_ops, '_install_apt') as install:
        with pytest.raises(policy.PolicyDeniedError, match='Block virtual machines'):
            package_ops._install_package(package, 1000, guardian_authenticated=True)
        install.assert_not_called()


def test_resolved_alias_and_flatpak_guards_apply_before_installer(runtime):
    vm_ops._enabled = True
    with (patch.dict(package_ops.APPROVED_PACKAGES, {'test-vm': {'type': 'apt', 'packages': ['qemu-system-x86']}}),
          patch.object(package_ops, 'require_policy'), patch.object(package_ops, '_install_apt') as apt):
        with pytest.raises(policy.PolicyDeniedError):
            package_ops._install_package('test-vm', 1000)
        apt.assert_not_called()
    with patch.object(appstore_ops, '_run') as flatpak:
        with pytest.raises(policy.PolicyDeniedError):
            appstore_ops.install_app('org.gnome.Boxes', guardian_authenticated=True)
        flatpak.assert_not_called()
    vm_ops._enabled = False
    inventory.require_allowed_package('qemu-kvm')
    inventory.require_allowed_flatpak('org.gnome.Boxes')
    vm_ops._enabled = True
    inventory.require_allowed_package('virtualbox-guest-utils')
    inventory.require_allowed_package('python3')


def test_vm_inventory_never_attaches_to_shared_interpreters(tmp_path):
    python = tmp_path / 'python3'; python.write_text('fixture'); python.chmod(0o755)
    launcher = tmp_path / 'virt-manager'; launcher.symlink_to(python)
    qemu = tmp_path / 'qemu-system-x86_64'; qemu.write_text('fixture'); qemu.chmod(0o755)
    with patch.object(inventory, 'NATIVE_VM_PATTERNS', (str(launcher), str(qemu))):
        entries = inventory.installed_vm_blocks()
    assert [entry['executable'] for entry in entries.values()] == [str(qemu)]


@pytest.mark.skipif(not shutil.which('node'), reason='Node executes polkit JavaScript')
def test_polkit_denies_vm_authorization_without_affecting_mounts():
    script = 'var rule; var polkit={Result:{NO:"no"},addRule:function(r){rule=r;}};' + vm_ops.POLKIT_RULE
    for user, action, expected in [
        ('desktop', 'org.libvirt.unix.manage', 'no'),
        ('desktop', 'org.libvirt.api.domain.start', 'no'),
        ('desktop', 'org.virtualbox.manage', 'no'),
        ('desktop', 'com.vmware.manage', 'no'),
        ('desktop', 'org.freedesktop.udisks2.filesystem-mount', None),
        ('desktop', 'org.freedesktop.policykit.exec', None),
        ('root', 'org.libvirt.unix.manage', None),
    ]:
        invoke = 'console.log(JSON.stringify(rule({id:' + json.dumps(action) + '},{user:' + json.dumps(user) + '}) || null));'
        result = subprocess.run(['node', '-e', script + invoke], capture_output=True, text=True, check=True)
        assert json.loads(result.stdout) == expected


def test_command_failure_and_fd_inheritance_are_reported():
    with patch.object(vm_ops.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'denied')) as run:
        with pytest.raises(ValueError, match='denied'):
            vm_ops._run('setfacl', '-b', '/proc/self/fd/10', pass_fds=(10,))
        assert run.call_args.kwargs['pass_fds'] == (10,)
