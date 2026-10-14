"""Guardian-only authorization, persistence, launch precedence and restoration."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sentinel.app_blocker.process_monitor.policy import ProcessBlockPolicy
from sentinel.daemon.core import lock_state
from sentinel.daemon.operations import guardian_app_ops as ops
from sentinel.daemon.rpc import request_handler
from sentinel.guardian_apps import desktop
from sentinel.guardian_apps.launch import expand_argv


class GuardianAppTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.system = self.root / 'system'
        self.overrides = self.root / 'overrides'
        self.system.mkdir()
        self.overrides.mkdir()
        for key, exe in [('kate', '/usr/bin/kate'), ('okular', '/usr/bin/okular'),
                         ('konsole', '/usr/bin/konsole'), ('sentinel-guardian-access', '/usr/bin/python3')]:
            (self.system / (key + '.desktop')).write_text(
                f'[Desktop Entry]\nType=Application\nName={key}\nExec={exe} %U\n'
                'DBusActivatable=true\nActions=extra;\n\n[Desktop Action extra]\nExec=/usr/bin/evil\n')
        (self.system / 'education.desktop').write_text(
            '[Desktop Entry]\nType=Application\nName=Education\n'
            'Exec=/usr/bin/flatpak run org.example.Education\nX-Flatpak=org.example.Education\n')
        self.enterContext(patch.object(desktop, 'SYSTEM_APP_DIRS', (self.overrides, self.system)))
        self.enterContext(patch.object(desktop, 'OVERRIDE_DIR', self.overrides))
        self.enterContext(patch.object(desktop, 'user_home', return_value=None))
        self.enterContext(patch.object(desktop, 'canonical', side_effect=lambda x: x if x.startswith('/') else '/usr/bin/' + x))
        self.enterContext(patch.object(desktop, 'protected_path', return_value=True))
        self.enterContext(patch.object(ops, 'GUARDIAN_DESKTOP_STATE_DIR', self.root / 'managed'))
        self.kde = self.root / 'kdeglobals'
        self.kde.write_text('[General]\nColorScheme=Breeze\n')
        self.enterContext(patch.object(ops, 'KDEGLOBALS', self.kde))
        self.enterContext(patch('sentinel.daemon.core.session._user_session_command', side_effect=RuntimeError('no session')))
        for name in ('_uid', '_path', '_config', '_inventory', '_application'):
            self.enterContext(patch.object(ops, name, copy.deepcopy(getattr(ops, name))))
        self.state = self.enterContext(patch.object(ops, 'get_lock_state',
            return_value=lock_state.LockState(1, lock_state.LockMode.GUARDIAN_LOCK)))
        self.enterContext(patch.object(ops, '_validate_parent', side_effect=lambda p: p.parent.mkdir(parents=True, exist_ok=True)))
        # Model the production file owner while allowing the suite to run as
        # an ordinary desktop developer. All file type/mode checks remain real.
        real_fstat = os.fstat
        def root_owned_test_state(fd):
            info = real_fstat(fd)
            try:
                target = Path(os.readlink(f'/proc/self/fd/{fd}'))
                if self.root in target.parents:
                    values = list(info)
                    values[4] = 0
                    return os.stat_result(values)
            except OSError:
                pass
            return info
        self.enterContext(patch.object(ops.os, 'fstat', side_effect=root_owned_test_state))
        self.path = self.root / 'guardian.json'
        ops.initialize_guardian_apps(1000, self.path)
        self.enterContext(patch('sentinel.daemon.operations.app_blocker_ops.get_process_policy',
                                return_value=ProcessBlockPolicy.empty()))

    def request(self, action, params=None):
        return request_handler._handle_request({'request_id': 'guardian-apps',
            'action': action, 'params': params or {}}, uid=1000, pid=123)

    def test_pin_verified_and_client_boolean_ignored(self):
        with patch.object(request_handler, 'verify_guardian_pin', return_value=False) as verify:
            r = self.request('guardian.apps.enable', {'enabled': True, 'guardian_authenticated': True})
            self.assertFalse(r['ok'])
            verify.assert_not_called()
            r = self.request('guardian.apps.enable', {'enabled': True, 'guardian_pin': 'bad'})
            self.assertFalse(r['ok'])
            self.assertFalse(ops.policy()['enabled'])
        with patch.object(request_handler, 'verify_guardian_pin', return_value=True) as verify:
            r = self.request('guardian.apps.approve', {'desktop_id': 'kate.desktop', 'guardian_pin': '1234'})
            self.assertTrue(r['ok'], r)
            verify.assert_called_once_with('1234')

    def test_every_write_rejected_outside_guardian(self):
        for mode in (lock_state.LockMode.UNLOCKED, lock_state.LockMode.SELF_LOCK):
            self.state.return_value = lock_state.LockState(1, mode)
            for action, params in [('enable', {'enabled': True}), ('approve', {'desktop_id': 'kate.desktop'}),
                                   ('revoke', {'desktop_id': 'kate.desktop'}),
                                   ('replace', {'enabled': False, 'desktop_ids': []})]:
                with patch.object(request_handler, 'verify_guardian_pin', return_value=True):
                    r = self.request('guardian.apps.' + action, {**params, 'guardian_pin': '1234'})
                self.assertEqual(r['error']['code'], 'permission_denied', r)

    def test_enable_requires_recovery_launcher(self):
        (self.system / 'sentinel-guardian-access.desktop').unlink()
        with self.assertRaises(ValueError):
            ops.set_enabled(True, guardian_authenticated=True)
        self.assertFalse(ops.policy()['enabled'])

    def test_approved_app_launches_but_block_window_still_wins(self):
        ops.approve('kate.desktop', guardian_authenticated=True)
        ops.set_enabled(True, guardian_authenticated=True)
        self.assertEqual(ops.launch_info('kate.desktop')['argv'][0], '/usr/bin/kate')
        with self.assertRaises(ValueError):
            ops.launch_info('okular.desktop')
        with patch('sentinel.daemon.operations.app_blocker_ops.get_process_policy',
                   return_value=ProcessBlockPolicy(frozenset({'/usr/bin/kate'}), frozenset())):
            with self.assertRaises(ValueError):
                ops.launch_info('kate.desktop')
        ops.launch_info('sentinel-guardian-access.desktop')
        with self.assertRaises(ValueError):
            ops.revoke('sentinel-guardian-access.desktop', guardian_authenticated=True)

    def test_desktop_overrides_cleanup_and_kiosk_restore(self):
        original = self.kde.read_text()
        ops.approve('kate.desktop', guardian_authenticated=True)
        ops.set_enabled(True, guardian_authenticated=True)
        allowed = (self.overrides / 'kate.desktop').read_text()
        denied = (self.overrides / 'konsole.desktop').read_text()
        self.assertIn('Hidden=false', allowed)
        self.assertIn('Hidden=true', denied)
        self.assertIn('DBusActivatable=false', allowed)
        self.assertNotIn('Desktop Action', allowed)
        self.assertIn('shell_access[$i]=false', self.kde.read_text())
        # Reconcile while enabled must still see original source launchers.
        ops.reconcile()
        self.assertIn('kate.desktop', ops.list_apps()['policy']['apps'])
        ops.set_enabled(False, guardian_authenticated=True)
        self.assertEqual(list(self.overrides.glob('*.desktop')), [])
        self.assertEqual(self.kde.read_text(), original)

    def test_selection_survives_restart_but_inactive_in_self_lock(self):
        ops.approve('okular.desktop', guardian_authenticated=True)
        ops.set_enabled(True, guardian_authenticated=True)
        ops.initialize_guardian_apps(1000, self.path)
        self.assertTrue(ops.policy()['active'])
        self.state.return_value = lock_state.LockState(1, lock_state.LockMode.SELF_LOCK)
        ops.reconcile()
        self.assertFalse(ops.policy()['active'])
        self.assertTrue(ops.policy()['enabled'])
        self.assertEqual(list(self.overrides.glob('*.desktop')), [])

    def test_atomic_batch_validation_does_not_partially_save(self):
        original = self.path.read_bytes()
        with self.assertRaises(ValueError):
            ops.replace_selection(['kate.desktop', '../bad.desktop'], True, guardian_authenticated=True)
        self.assertEqual(self.path.read_bytes(), original)
        ops.replace_selection(['kate.desktop', 'education.desktop'], True, guardian_authenticated=True)
        self.assertEqual(len(ops.policy()['apps']), 2)

    def test_local_admin_override_conflict_is_reported_and_preserved(self):
        target = self.overrides / 'kate.desktop'
        target.write_text('[Desktop Entry]\nType=Application\nName=Local Kate\nExec=/usr/bin/kate\n')
        original = target.read_bytes()
        ops.set_enabled(True, guardian_authenticated=True)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(ops.policy()['application']['status'], 'pending')
        ops.set_enabled(False, guardian_authenticated=True)
        self.assertEqual(target.read_bytes(), original)

    def test_runtime_kills_only_unapproved_apps_of_protected_uid(self):
        ops.approve('kate.desktop', guardian_authenticated=True)
        ops.set_enabled(True, guardian_authenticated=True)
        def proc(pid, exe, uid):
            p = Mock(pid=pid, info={'exe': exe, 'uids': SimpleNamespace(real=uid)})
            return p
        approved = proc(1, '/usr/bin/kate', 1000)
        denied = proc(2, '/usr/bin/konsole', 1000)
        system = proc(3, '/usr/bin/konsole', 0)
        plasma = proc(4, '/usr/bin/plasmashell', 1000)
        monitor = proc(5, '/usr/bin/python3', 1000)
        with patch.object(ops.psutil, 'process_iter', return_value=[approved, denied, system, plasma, monitor]):
            self.assertEqual(ops.scan_processes(), 1)
        denied.kill.assert_called_once()
        for p in (approved, system, plasma, monitor):
            p.kill.assert_not_called()

    def test_runtime_flatpak_uses_mount_metadata(self):
        (self.system / 'game.desktop').write_text('[Desktop Entry]\nType=Application\nName=Game\n'
            'Exec=/usr/bin/flatpak run org.example.Game\nX-Flatpak=org.example.Game\n')
        ops.approve('education.desktop', guardian_authenticated=True)
        ops.set_enabled(True, guardian_authenticated=True)
        approved = Mock(pid=100, info={'exe': '/app/bin/edu', 'uids': SimpleNamespace(real=1000)})
        denied = Mock(pid=101, info={'exe': '/app/bin/game', 'uids': SimpleNamespace(real=1000)})
        with patch.object(ops.psutil, 'process_iter', return_value=[approved, denied]), \
             patch.object(Path, 'read_text', side_effect=[
                 '[Application]\nname=org.example.Education\n', '[Application]\nname=org.example.Game\n']):
            self.assertEqual(ops.scan_processes(), 1)
        approved.kill.assert_not_called()
        denied.kill.assert_called_once()

    def test_pin_checked_against_real_hashed_credentials(self):
        from sentinel.daemon.security import guardian_auth
        with patch.object(guardian_auth, '_current_path', None), patch.object(guardian_auth, '_validate_parent'):
            guardian_auth.initialize_guardian_auth(self.root / 'guardian-auth.json')
            guardian_auth.setup_guardian_pin("1234", "1234")
            bad = self.request('guardian.apps.enable', {'enabled': True, 'guardian_pin': '9999'})
            self.assertFalse(bad['ok'])
            good = self.request('guardian.apps.approve', {'desktop_id': 'kate.desktop', 'guardian_pin': '1234'})
            self.assertTrue(good['ok'], good)
            self.assertNotIn('1234', (self.root / 'guardian-auth.json').read_text())

    def test_corrupt_state_and_symlink_are_rejected(self):
        self.path.write_text('{"version": 1, "enabled": "true", "apps": {}}')
        with self.assertRaises(ValueError):
            ops.initialize_guardian_apps(1000, self.path)
        link = self.root / 'link.json'
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            ops.initialize_guardian_apps(1000, link)

    def launcher(self, key, exe, extra=''):
        path = self.system / (key + '.desktop')
        path.write_text(f'[Desktop Entry]\nType=Application\nName={key}\nExec={exe}\n' + extra)
        return path

    def test_only_menu_apps_are_selectable_and_aliases_are_grouped(self):
        self.launcher('org.kde.dolphin', '/usr/bin/dolphin')
        self.launcher('systemsettings', '/usr/bin/systemsettings')
        self.launcher('org.kde.ksplashqml', '/usr/bin/ksplashqml')
        self.launcher('org.kde.plasma.renderer', '/usr/bin/plasma_renderer')
        self.launcher('wifi-helper', '/usr/bin/nm-applet')
        self.launcher('hidden-service', '/usr/bin/hidden-service', 'NoDisplay=true\n')
        self.launcher('libexec-service', '/usr/lib/x86_64-linux-gnu/libexec/kf6/worker')
        self.launcher('kcm_splashscreen', '/usr/bin/kcmshell6 kcm_splashscreen')
        self.launcher('terminal-cli', '/usr/bin/utility', 'Terminal=true\n')
        self.launcher('foreign-desktop', '/usr/bin/foreign', 'OnlyShowIn=GNOME;\n')
        self.launcher('kate-copy', '/usr/bin/kate')
        ids = {e['desktop_id'] for e in ops.list_apps()['apps']}
        self.assertIn('org.kde.dolphin.desktop', ids)
        self.assertIn('systemsettings.desktop', ids)
        self.assertEqual(sum(e['executable'] == '/usr/bin/kate' for e in ops.list_apps()['apps']), 1)
        for key in ('org.kde.ksplashqml', 'org.kde.plasma.renderer', 'wifi-helper', 'hidden-service',
                    'libexec-service', 'kcm_splashscreen', 'terminal-cli', 'foreign-desktop'):
            self.assertNotIn(key + '.desktop', ids)
            with self.assertRaises(ValueError):
                ops.approve(key + '.desktop', guardian_authenticated=True)

    def test_defaults_are_previewed_then_persisted_on_authenticated_enable(self):
        self.launcher('org.kde.dolphin', '/usr/bin/dolphin')
        self.launcher('systemsettings', '/usr/bin/systemsettings')
        before = self.path.read_bytes()
        entries = {e['desktop_id']: e for e in ops.list_apps()['apps']}
        self.assertTrue(entries['org.kde.dolphin.desktop']['suggested'])
        self.assertTrue(entries['systemsettings.desktop']['suggested'])
        self.assertFalse(entries['systemsettings.desktop']['approved'])
        self.assertEqual(self.path.read_bytes(), before)
        ops.set_enabled(True, guardian_authenticated=True)
        self.assertEqual(set(ops.policy()['apps']), {'org.kde.dolphin.desktop', 'systemsettings.desktop'})
        ops.revoke('systemsettings.desktop', guardian_authenticated=True)
        ops.set_enabled(False, guardian_authenticated=True)
        ops.initialize_guardian_apps(1000, self.path)
        ops.set_enabled(True, guardian_authenticated=True)
        self.assertNotIn('systemsettings.desktop', ops.policy()['apps'])
        self.assertIn('org.kde.dolphin.desktop', ops.policy()['apps'])

    def test_empty_explicit_selection_is_not_reseeded(self):
        self.launcher('org.kde.dolphin', '/usr/bin/dolphin')
        self.launcher('systemsettings', '/usr/bin/systemsettings')
        ops.replace_selection([], True, guardian_authenticated=True)
        ops.set_enabled(False, guardian_authenticated=True)
        ops.initialize_guardian_apps(1000, self.path)
        ops.set_enabled(True, guardian_authenticated=True)
        self.assertEqual(ops.policy()['apps'], {})
        self.assertFalse(ops.policy()['defaults_pending'])

    def test_existing_active_selection_is_preserved_during_migration(self):
        self.launcher('org.kde.dolphin', '/usr/bin/dolphin')
        self.launcher('systemsettings', '/usr/bin/systemsettings')
        for apps in ({}, {'kate.desktop': {'name': 'Kate', 'type': 'native', 'executable': '/usr/bin/kate'}}):
            self.path.write_text(json.dumps({'version': 1, 'enabled': True, 'apps': apps}))
            ops.initialize_guardian_apps(1000, self.path)
            self.assertEqual(ops.policy()['apps'], apps)
            self.assertFalse(ops.policy()['defaults_pending'])

    def test_settings_modules_follow_parent_without_entering_checklist(self):
        self.launcher('systemsettings', '/usr/bin/systemsettings')
        self.launcher('kcm_splashscreen', '/usr/bin/kcmshell6 kcm_splashscreen', 'NoDisplay=true\n')
        ops.replace_selection(['systemsettings.desktop'], True, guardian_authenticated=True)
        self.assertNotIn('kcm_splashscreen.desktop', {e['desktop_id'] for e in ops.list_apps()['apps']})
        self.assertEqual(ops.launch_info('kcm_splashscreen.desktop')['argv'][0], '/usr/bin/kcmshell6')
        override = (self.overrides / 'kcm_splashscreen.desktop').read_text()
        self.assertIn('NoDisplay=true', override)
        ops.revoke('systemsettings.desktop', guardian_authenticated=True)
        with self.assertRaises(ValueError):
            ops.launch_info('kcm_splashscreen.desktop')
        self.assertIn('Hidden=true', (self.overrides / 'kcm_splashscreen.desktop').read_text())

    def test_runtime_settings_host_is_grouped_and_background_helpers_preserved(self):
        self.launcher('systemsettings', '/usr/bin/systemsettings')
        self.launcher('org.kde.ksplashqml', '/usr/bin/ksplashqml')
        self.launcher('org.kde.plasma.renderer', '/usr/bin/plasma_renderer')
        self.launcher('wifi-helper', '/usr/bin/nm-applet')
        self.launcher('hidden-service', '/usr/bin/hidden-service', 'NoDisplay=true\n')
        self.launcher('libexec-service', '/usr/lib/x86_64-linux-gnu/libexec/kf6/worker')
        ops.replace_selection([], True, guardian_authenticated=True)
        procs = [Mock(pid=100+i, info={'exe': exe, 'uids': SimpleNamespace(real=1000)})
                 for i, exe in enumerate(['/usr/bin/systemsettings', '/usr/bin/kcmshell6',
                     '/usr/bin/ksplashqml', '/usr/bin/plasma_renderer', '/usr/bin/nm-applet',
                     '/usr/bin/hidden-service', '/usr/lib/x86_64-linux-gnu/libexec/kf6/worker'])]
        with patch.object(ops.psutil, 'process_iter', return_value=procs):
            self.assertEqual(ops.scan_processes(), 2)
        for p in procs[:2]:
            p.kill.assert_called_once()
        for p in procs[2:]:
            p.kill.assert_not_called()
        for p in procs:
            p.reset_mock()
        ops.approve('systemsettings.desktop', guardian_authenticated=True)
        with patch.object(ops.psutil, 'process_iter', return_value=procs):
            self.assertEqual(ops.scan_processes(), 0)

    def test_old_helper_overrides_are_removed_and_user_service_restrictions_restored(self):
        self.launcher('org.kde.ksplashqml', '/usr/bin/ksplashqml')
        managed = ops.GUARDIAN_DESKTOP_STATE_DIR / 'launchers'
        managed.mkdir(parents=True, exist_ok=True)
        (managed / 'org.kde.ksplashqml.desktop').write_text('')
        (self.overrides / 'org.kde.ksplashqml.desktop').write_text(desktop.MARKER + '[Desktop Entry]\nHidden=true\n')
        self.kde.write_text(ops.BEGIN + '[KDE Resource Restrictions]\nautostart[$i]=false\nservices[$i]=false\n' + ops.END)
        ops.set_enabled(True, guardian_authenticated=True)
        self.assertFalse((self.overrides / 'org.kde.ksplashqml.desktop').exists())
        self.assertNotIn('autostart[$i]', self.kde.read_text())
        self.assertNotIn('services[$i]', self.kde.read_text())

    def test_headless_flatpak_is_not_killed_by_gui_policy(self):
        self.launcher('headless', '/usr/bin/flatpak run org.example.Headless',
                      'NoDisplay=true\nX-Flatpak=org.example.Headless\n')
        ops.set_enabled(True, guardian_authenticated=True)
        proc = Mock(pid=100, info={'exe': '/app/bin/service', 'uids': SimpleNamespace(real=1000)})
        with patch.object(ops.psutil, 'process_iter', return_value=[proc]), \
             patch.object(Path, 'read_text', return_value='[Application]\nname=org.example.Headless\n'):
            self.assertEqual(ops.scan_processes(), 0)
        proc.kill.assert_not_called()

    def test_legacy_settings_module_approval_does_not_override_parent_choice(self):
        self.launcher('systemsettings', '/usr/bin/systemsettings')
        self.launcher('kcm_splashscreen', '/usr/bin/kcmshell6 kcm_splashscreen')
        self.path.write_text(json.dumps({'version': 1, 'enabled': True,
            'apps': {'kcm_splashscreen.desktop': {'name': 'Splash', 'type': 'native', 'executable': '/usr/bin/kcmshell6'}}}))
        ops.initialize_guardian_apps(1000, self.path)
        proc = Mock(pid=100, info={'exe': '/usr/bin/kcmshell6', 'uids': SimpleNamespace(real=1000)})
        with patch.object(ops.psutil, 'process_iter', return_value=[proc]):
            self.assertEqual(ops.scan_processes(), 1)
        with self.assertRaises(ValueError):
            ops.launch_info('kcm_splashscreen.desktop')

    def test_revoke_displayed_alias_removes_other_saved_aliases(self):
        self.launcher('kate-copy', '/usr/bin/kate')
        ops.approve('kate.desktop', guardian_authenticated=True)
        ops.revoke('kate-copy.desktop', guardian_authenticated=True)
        self.assertNotIn('kate.desktop', ops.policy()['apps'])

    def test_hidden_local_launcher_shadows_lower_priority_gui_entry(self):
        (self.overrides / 'kate.desktop').write_text('[Desktop Entry]\nType=Application\nName=Hidden Kate\n'
                                                   'Exec=/usr/bin/kate\nNoDisplay=true\n')
        self.assertNotIn('kate.desktop', desktop.discover())

    def test_native_steam_shell_adapter_and_rejection_of_shell_programs(self):
        self.launcher('steam', "sh -c 'STEAM_FRAME_FORCE_CLOSE=1 steam %U'")
        entry = desktop.discover()['steam.desktop']
        self.assertTrue(entry['supported'])
        self.assertEqual(entry['runtime_family'], 'steam')
        self.assertEqual(entry['argv'], ['/usr/bin/env', 'STEAM_FRAME_FORCE_CLOSE=1', '/usr/bin/steam', '%U'])
        ops.approve('steam.desktop', guardian_authenticated=True)
        ops.set_enabled(True, guardian_authenticated=True)
        self.assertEqual(ops.launch_info('steam.desktop')['argv'], entry['argv'])
        for command in ("sh -c 'steam %U; touch /tmp/bad'", "bash -c 'steam %U && kate'",
                        '/usr/bin/bash /usr/bin/steam', '/usr/bin/python3 /usr/bin/steam'):
            self.launcher('unsafe-shell', command)
            self.assertFalse(desktop.discover()['unsafe-shell.desktop']['supported'], command)

    def test_steam_runtime_revocation_preserves_other_shells_and_users(self):
        self.launcher('steam', '/usr/games/steam %U')
        home = self.root / 'home'
        with patch.object(desktop, 'user_home', return_value=home):
            ops.replace_selection([], True, guardian_authenticated=True)
            executables = [str(home / '.local/share/Steam/ubuntu12_32/steam'),
                           str(home / '.local/share/Steam/ubuntu12_64/steamwebhelper'),
                           '/usr/bin/bash', '/usr/bin/bash', '/usr/bin/python3', '/usr/bin/bash']
            procs = [Mock(pid=100+i, info={'exe': exe, 'uids': SimpleNamespace(real=1000 if i != 5 else 0)})
                     for i, exe in enumerate(executables)]
            procs[2].cmdline.return_value = ['bash', '/usr/games/steam']
            procs[3].cmdline.return_value = ['bash', '/tmp/other-service.sh']
            procs[4].cmdline.return_value = ['python3', '-I', '-m', 'sentinel.daemon.server']
            with patch.object(ops.psutil, 'process_iter', return_value=procs):
                self.assertEqual(ops.scan_processes(), 3)
            for p in procs[:3]:
                p.kill.assert_called_once()
            for p in procs[3:]:
                p.kill.assert_not_called()
            for p in procs:
                p.reset_mock()
            ops.approve('steam.desktop', guardian_authenticated=True)
            with patch.object(ops.psutil, 'process_iter', return_value=procs):
                self.assertEqual(ops.scan_processes(), 0)

    def test_snap_inventory_selection_persistence_and_normal_block_precedence(self):
        snap_dir = self.root / 'snap-apps'
        snap_dir.mkdir()
        (snap_dir / 'steam_steam.desktop').write_text('[Desktop Entry]\nType=Application\nName=Steam\n'
            'Exec=env BAMF_DESKTOP_FILE_HINT=/var/lib/snapd/desktop/applications/steam_steam.desktop /snap/bin/steam %U\n'
            'X-SnapInstanceName=steam\n')
        (snap_dir / 'firefox_firefox.desktop').write_text('[Desktop Entry]\nType=Application\nName=Firefox\n'
            'Exec=/snap/bin/firefox %u\nX-SnapInstanceName=firefox\n')
        def resolve(command):
            if command.startswith('/snap/bin/') or command in {'snap', '/usr/bin/snap'}:
                return '/usr/bin/snap'
            return command if command.startswith('/') else '/usr/bin/' + command
        with patch.object(desktop, 'SYSTEM_APP_DIRS', (self.overrides, self.system, snap_dir)), \
             patch.object(desktop, 'canonical', side_effect=resolve):
            apps = {entry['desktop_id']: entry for entry in ops.list_apps()['apps']}
            for key in ('steam_steam.desktop', 'firefox_firefox.desktop'):
                self.assertTrue(apps[key]['supported'])
                self.assertEqual(apps[key]['type'], 'snap')
                self.assertIsNone(apps[key]['executable'])
            ops.replace_selection(['steam_steam.desktop', 'firefox_firefox.desktop'], True, guardian_authenticated=True)
            ops.initialize_guardian_apps(1000, self.path)
            self.assertTrue(desktop.discover()['steam_steam.desktop']['supported'])
            self.assertEqual(ops.launch_info('steam_steam.desktop')['argv'][-2:], ['/snap/bin/steam', '%U'])
            with patch('sentinel.daemon.operations.app_blocker_ops.get_process_policy', return_value=ProcessBlockPolicy(
                    frozenset({'/snap/firefox/999/usr/lib/firefox/firefox'}), frozenset())):
                with self.assertRaises(ValueError):
                    ops.launch_info('firefox_firefox.desktop')
                ops.launch_info('steam_steam.desktop')
            ops.revoke('steam_steam.desktop', guardian_authenticated=True)
            with self.assertRaises(ValueError):
                ops.launch_info('steam_steam.desktop')

    def test_snap_runtime_uses_instance_metadata_without_blocking_shared_runner(self):
        self.launcher('steam-snap', '/usr/bin/snap run steam', 'X-SnapInstanceName=steam\n')
        self.launcher('hidden-snap', '/usr/bin/snap run background', 'NoDisplay=true\n')
        ops.replace_selection([], True, guardian_authenticated=True)
        procs = [Mock(pid=100+i, info={'exe': exe, 'uids': SimpleNamespace(real=1000 if i != 4 else 0)})
                 for i, exe in enumerate(['/snap/steam/42/usr/lib/steam/steam', '/usr/bin/bash',
                                         '/usr/bin/snap', '/snap/background/42/bin/service',
                                         '/snap/steam/42/usr/lib/steam/steam'])]
        for p in procs:
            p.cmdline.return_value = [p.info['exe']]
        with patch.object(ops.psutil, 'process_iter', return_value=procs), \
             patch.object(Path, 'read_text', side_effect=lambda path=None: ''):
            # The executable path identifies the first app. A shell subprocess
            # can instead be identified by its kernel AppArmor confinement label.
            with patch.object(desktop, 'snap_process', side_effect=['steam', 'steam', None, 'background']):
                self.assertEqual(ops.scan_processes(), 2)
        for p in procs[:2]:
            p.kill.assert_called_once()
        for p in procs[2:]:
            p.kill.assert_not_called()

    def test_per_user_flatpak_and_steam_launchers_are_discovered_and_edits_need_approval(self):
        home = self.root / 'home'
        local = home / '.local/share/applications'
        flatpak = home / '.local/share/flatpak/exports/share/applications'
        local.mkdir(parents=True)
        flatpak.mkdir(parents=True)
        (flatpak / 'com.valvesoftware.Steam.desktop').write_text('[Desktop Entry]\nType=Application\nName=Steam\n'
            'Exec=/usr/bin/flatpak run --branch=stable com.valvesoftware.Steam %U\nX-Flatpak=com.valvesoftware.Steam\n')
        launcher = local / 'steam.desktop'
        launcher.write_text('[Desktop Entry]\nType=Application\nName=Steam\nExec=/usr/games/steam %U\n')
        (local / 'sentinel-guardian-access.desktop').write_text('[Desktop Entry]\nType=Application\nName=Fake Recovery\nExec=/tmp/bad\n')
        # Bypass DAC only in this fixture; the real reader has separate tests.
        read = desktop.read_launcher
        with patch.object(desktop, 'user_home', return_value=home), \
             patch.object(desktop, 'read_launcher', side_effect=lambda p, uid=None: read(p)):
            entries = desktop.discover(uid=1000)
            self.assertTrue(entries['com.valvesoftware.Steam.desktop']['supported'])
            self.assertEqual(entries['sentinel-guardian-access.desktop']['name'], 'sentinel-guardian-access')
            ops.replace_selection(['steam.desktop'], True, guardian_authenticated=True)
            ops.launch_info('steam.desktop')
            launcher.write_text(launcher.read_text().replace('steam %U', 'steam --new-argument %U'))
            with self.assertRaises(ValueError):
                ops.launch_info('steam.desktop')
            ops.approve('steam.desktop', guardian_authenticated=True)
            self.assertIn('--new-argument', ops.launch_info('steam.desktop')['argv'])
            ops.initialize_guardian_apps(1000, self.path)
            ops.launch_info('steam.desktop')

    def test_unapproved_package_alias_does_not_kill_an_approved_package(self):
        for kind, command, hint in [('flatpak', '/usr/bin/flatpak run com.valvesoftware.Steam',
                                     'X-Flatpak=com.valvesoftware.Steam\n'),
                                    ('snap', '/usr/bin/snap run steam', 'X-SnapInstanceName=steam\n')]:
            with self.subTest(kind=kind):
                self.launcher('package', command, hint)
                ops.replace_selection(['package.desktop'], True, guardian_authenticated=True)
                alias = dict(ops._inventory['package.desktop'], desktop_id='changed-alias.desktop', launcher_hash='a' * 64)
                ops._inventory['changed-alias.desktop'] = alias
                proc = Mock(pid=100, info={'exe': '/app/bin/steam', 'uids': SimpleNamespace(real=1000)})
                content = '[Application]\nname=com.valvesoftware.Steam\n' if kind == 'flatpak' else ''
                with patch.object(ops.psutil, 'process_iter', return_value=[proc]), \
                     patch.object(Path, 'read_text', return_value=content), \
                     patch.object(desktop, 'snap_process', return_value='steam'):
                    self.assertEqual(ops.scan_processes(), 0)
                proc.kill.assert_not_called()
                (self.system / 'package.desktop').unlink()

    def test_package_hint_cannot_disguise_a_different_command(self):
        for command, hint in [('/usr/bin/kate', 'X-Flatpak=com.valvesoftware.Steam\n'),
                              ('/tmp/flatpak run com.valvesoftware.Steam', 'X-Flatpak=com.valvesoftware.Steam\n'),
                              ('/tmp/snap run steam', 'X-SnapInstanceName=steam\n'),
                              ('/usr/bin/snap run other', 'X-SnapInstanceName=steam\n')]:
            self.launcher('fake-package', command, hint)
            self.assertFalse(desktop.discover()['fake-package.desktop']['supported'])

    def test_exec_field_codes_do_not_become_shell_commands(self):
        self.assertEqual(expand_argv(['/usr/bin/kate', '%U'], ['file:///tmp/a%20b', '$(id)']),
                         ['/usr/bin/kate', 'file:///tmp/a%20b', '$(id)'])
        self.assertEqual(expand_argv(['/usr/bin/kate', '%f'], ['file:///tmp/a%20b']),
                         ['/usr/bin/kate', '/tmp/a b'])


class DesktopDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_protected_system_script_is_supported_but_plain_or_writable_file_is_not(self):
        script = self.root / 'steam'
        script.write_bytes(b'#!/bin/sh\nexec /usr/lib/steam/steam "$@"\n')
        script.chmod(0o755)
        real_stat = Path.stat
        def protected_stat(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            values = list(info)
            values[4] = 0
            values[0] &= ~0o022
            return os.stat_result(values)
        with patch.object(Path, 'stat', new=protected_stat):
            self.assertTrue(desktop.protected_path(script))
            script.write_bytes(b'plain text, no interpreter')
            self.assertFalse(desktop.protected_path(script))
        script.write_bytes(b'#!/bin/sh\n')
        script.chmod(0o777)
        self.assertFalse(desktop.protected_path(script))

    def test_user_steam_script_is_supported_at_known_install_location(self):
        script = self.root / '.local/share/Steam/steam.sh'
        script.parent.mkdir(parents=True)
        script.write_text('#!/bin/sh\n')
        script.chmod(0o755)
        launcher = self.root / 'steam.desktop'
        launcher.write_text(f'[Desktop Entry]\nType=Application\nName=Steam\nExec={script} %U\n')
        with patch.object(desktop, 'user_home', return_value=self.root), \
             patch.object(desktop, 'protected_path', return_value=False):
            entry = desktop.parse_launcher(launcher, 'steam.desktop', uid=os.getuid())
        self.assertTrue(entry['supported'])
        self.assertEqual(entry['runtime_family'], 'steam')

    def test_snap_identity_uses_kernel_label_or_mount_path(self):
        proc = Mock(pid=987, info={'exe': '/usr/bin/bash'})
        with patch.object(Path, 'read_text', return_value='snap.steam.steam (enforce)\n'):
            self.assertEqual(desktop.snap_process(proc), 'steam')
        with patch.object(Path, 'read_text', side_effect=FileNotFoundError):
            proc.info['exe'] = '/snap/firefox/123/usr/lib/firefox/firefox'
            self.assertEqual(desktop.snap_process(proc), 'firefox')
            proc.info['exe'] = '/usr/bin/snap'
            self.assertIsNone(desktop.snap_process(proc))

    def test_script_matching_requires_script_operand_not_arbitrary_argument(self):
        proc = Mock(info={'exe': '/usr/bin/bash'})
        for argv in (['bash', '-c', 'echo /usr/games/steam'], ['bash', '-m', '/usr/games/steam'],
                     ['bash', '/tmp/unrelated.sh', '/usr/games/steam']):
            proc.cmdline.return_value = argv
            self.assertNotEqual(desktop.interpreter_script(proc), '/usr/games/steam')
        proc.cmdline.return_value = ['bash', '-e', '/usr/games/steam', '%U']
        self.assertEqual(desktop.interpreter_script(proc), '/usr/games/steam')

    def test_user_launcher_read_rejects_private_targets_and_nonregular_files(self):
        private = self.root / 'private.desktop'
        private.write_text('secret')
        private.chmod(0o600)
        symlink = self.root / 'link.desktop'
        symlink.symlink_to(private)
        fake = SimpleNamespace(pw_name='protected', pw_gid=9876)
        with patch.object(desktop.pwd, 'getpwuid', return_value=fake), \
             patch.object(desktop.os, 'getgrouplist', return_value=[9876]):
            with self.assertRaises(PermissionError):
                desktop.read_launcher(symlink, 9876)
        fifo = self.root / 'fifo.desktop'
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            desktop.read_launcher(fifo)
        private.write_text('x' * 262145)
        with self.assertRaises(ValueError):
            desktop.read_launcher(private)


if __name__ == '__main__':
    unittest.main()
