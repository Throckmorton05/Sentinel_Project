"""Settings GUI revocation across launcher, direct and alternate host routes."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sentinel.daemon.operations import guardian_app_ops as ops
from sentinel.daemon.tests import test_guardian_apps as fixtures
from sentinel.guardian_apps import desktop


class GuardianSettingsAccessTests(unittest.TestCase):
    setUp = fixtures.GuardianAppTests.setUp
    launcher = fixtures.GuardianAppTests.launcher

    def processes(self, executables):
        return [Mock(pid=90000+i, info={'exe': exe, 'uids': SimpleNamespace(real=1000)})
                for i, exe in enumerate(executables)]

    def scan(self, processes):
        with patch.object(ops.psutil, 'process_iter', return_value=processes), \
             patch.object(Path, 'read_text', side_effect=FileNotFoundError):
            return ops.scan_processes()

    def test_hidden_settings_launcher_does_not_exempt_direct_or_panel_hosts(self):
        self.launcher('systemsettings', '/usr/bin/systemsettings', 'NoDisplay=true\n')
        ops.replace_selection(['kate.desktop'], True, guardian_authenticated=True)
        self.assertNotIn('systemsettings.desktop', ops._inventory)
        processes = self.processes(['/usr/bin/systemsettings', '/usr/bin/kcmshell6',
                                    '/usr/bin/kate', '/usr/bin/nm-applet', '/usr/bin/plasmashell'])
        root = Mock(pid=99000, info={'exe': '/usr/bin/systemsettings', 'uids': SimpleNamespace(real=0)})
        self.assertEqual(self.scan([*processes, root]), 2)
        for proc in processes[:2]:
            proc.kill.assert_called_once()
        for proc in [*processes[2:], root]:
            proc.kill.assert_not_called()

    def test_revoking_settings_stops_all_installed_versions_and_kcm_hosts(self):
        self.launcher('org.kde.systemsettings', '/usr/bin/systemsettings')
        ops.replace_selection(['org.kde.systemsettings.desktop'], True, guardian_authenticated=True)
        processes = self.processes(['/usr/bin/' + name for name in sorted(
            desktop.SETTINGS_PROGRAMS | desktop.SETTINGS_HOST_PROGRAMS)])
        self.assertEqual(self.scan(processes), 0)
        ops.revoke('org.kde.systemsettings.desktop', guardian_authenticated=True)
        self.assertEqual(self.scan(processes), len(processes))
        for proc in processes:
            proc.kill.assert_called_once()
        with self.assertRaises(ValueError):
            ops.launch_info('org.kde.systemsettings.desktop')
        ops.approve('org.kde.systemsettings.desktop', guardian_authenticated=True)
        for proc in processes:
            proc.reset_mock()
        self.assertEqual(self.scan(processes), 0)
        for proc in processes:
            proc.kill.assert_not_called()

    def test_libexec_settings_is_a_gui_app_and_background_libexec_is_preserved(self):
        executable = '/usr/lib/x86_64-linux-gnu/libexec/systemsettings'
        self.launcher('org.kde.systemsettings', executable)
        self.assertIn('org.kde.systemsettings.desktop', desktop.discover())
        ops.replace_selection([], True, guardian_authenticated=True)
        processes = self.processes([executable, '/usr/lib/libexec/kcmshell6',
                                    '/usr/lib/libexec/plasma-helper'])
        # Resolve the installed host to its protected libexec path.
        with patch.object(desktop, 'canonical', side_effect=lambda name:
                          '/usr/lib/libexec/kcmshell6' if name == 'kcmshell6' else '/usr/bin/' + name):
            self.assertEqual(self.scan(processes), 2)
        processes[2].kill.assert_not_called()

    def test_cold_start_with_legacy_kcm_approval_cannot_allow_settings_hosts(self):
        self.launcher('kcm_network', '/usr/bin/kcmshell6 kcm_network', 'NoDisplay=true\n')
        self.path.write_text(json.dumps({'version': 1, 'enabled': True, 'defaults_seeded': True,
            'apps': {'kcm_network.desktop': {'name': 'Network', 'type': 'native',
                                            'executable': '/usr/bin/kcmshell6'}}}))
        ops.initialize_guardian_apps(1000, self.path)
        self.assertEqual(self.scan(self.processes(['/usr/bin/systemsettings', '/usr/bin/kcmshell6'])), 2)
        ops.set_enabled(False, guardian_authenticated=True)
        self.assertEqual(self.scan(self.processes(['/usr/bin/systemsettings', '/usr/bin/kcmshell6'])), 0)

    def test_settings_resolution_rejects_unprotected_paths_and_unrelated_programs(self):
        def resolve(name):
            return {'systemsettings': '/tmp/systemsettings', 'kcmshell6': '/usr/bin/python3',
                    'kcmshell5': '/usr/bin/kcmshell5'}.get(name)
        with patch.object(desktop, 'canonical', side_effect=resolve), \
             patch.object(desktop, 'protected_path', side_effect=lambda p: str(p).startswith('/usr/')):
            self.assertEqual(desktop.settings_executables(), {'/usr/bin/kcmshell5'})

    def test_main_settings_label_is_preferred_over_info_center_alias(self):
        self.launcher('org.kde.kinfocenter', '/usr/bin/systemsettings --info')
        self.launcher('org.kde.systemsettings', '/usr/bin/systemsettings')
        ops.approve('org.kde.kinfocenter.desktop', guardian_authenticated=True)
        ops.set_enabled(True, guardian_authenticated=True)
        choices = [row for row in ops.list_apps()['apps']
                   if row.get('executable') == '/usr/bin/systemsettings']
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0]['desktop_id'], 'org.kde.systemsettings.desktop')
        self.assertTrue(choices[0]['approved'])
        ops.revoke(choices[0]['desktop_id'], guardian_authenticated=True)
        self.assertEqual(self.scan(self.processes(['/usr/bin/systemsettings'])), 1)


if __name__ == '__main__':
    unittest.main()
