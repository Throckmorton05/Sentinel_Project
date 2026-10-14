"""Reproduce post-login lockout safely: temp files, fake RPC/processes only.

Regression coverage includes independent recovery and KDE authorization.
No test enables the host policy, starts a desktop app or changes a real service.
"""
from __future__ import annotations

import configparser
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from sentinel.daemon import client
from sentinel.daemon.operations import guardian_app_ops as ops
from sentinel.daemon.tests import test_guardian_apps as fixtures
from sentinel.guardian_apps import desktop, launch

spec = importlib.util.spec_from_file_location('guardian_diagnostics',
    Path(__file__).resolve().parents[3] / 'tools/guardian_app_diagnostics.py')
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)

try:
    from PyQt6 import QtWidgets
    from sentinel.guardian_apps import manager
except ImportError:
    QtWidgets = manager = None


SAVED_APPS = {
    'org.kde.dolphin.desktop': {'name': 'Dolphin', 'type': 'native', 'executable': '/usr/bin/dolphin'},
    'org.kde.kinfocenter.desktop': {'name': 'Info Center', 'type': 'native', 'executable': '/usr/bin/systemsettings'},
    'firefox_firefox.desktop': {'name': 'Firefox', 'type': 'snap', 'snap_instance': 'firefox'},
    'google-chrome.desktop': {'name': 'Google Chrome', 'type': 'native', 'executable': '/opt/google/chrome/chrome'},
    'rofi.desktop': {'name': 'Rofi', 'type': 'native', 'executable': '/usr/bin/rofi'},
    'org.kde.konsole.desktop': {'name': 'Konsole', 'type': 'native', 'executable': '/usr/bin/konsole'},
}


class SavedApprovalRestartTests(unittest.TestCase):
    # Reuse the existing isolated fixture, without inheriting/rerunning its tests.
    setUp = fixtures.GuardianAppTests.setUp
    launcher = fixtures.GuardianAppTests.launcher

    def save_snapshot(self):
        for key, entry in SAVED_APPS.items():
            command = entry.get('executable') or '/usr/bin/snap run firefox'
            extra = 'X-SnapInstanceName=firefox\n' if entry['type'] == 'snap' else ''
            if key == 'org.kde.konsole.desktop':
                extra += 'X-KDE-AuthorizeAction=shell_access\n'
            self.launcher(key.removesuffix('.desktop'), command, extra)
        self.path.write_text(json.dumps({'version': 1, 'enabled': True,
            'apps': SAVED_APPS, 'defaults_seeded': True}))
        # Model a fresh daemon interpreter, not just another reconcile call.
        ops._inventory = {}
        ops._config = {'version': 1, 'enabled': False, 'apps': {}, 'defaults_seeded': False}
        return ops.initialize_guardian_apps(1000, self.path)

    def test_cold_start_preserves_all_six_approvals_and_launch_authorization(self):
        result = self.save_snapshot()
        self.assertEqual(result['apps'], SAVED_APPS)
        self.assertTrue(result['active'])
        for key in (*SAVED_APPS, 'sentinel-guardian-access.desktop'):
            with self.subTest(key=key):
                entry = desktop.discover(uid=1000)[key]
                self.assertTrue(ops.selected(entry))
                self.assertTrue(ops.launch_info(key)['argv'])
                parser = configparser.ConfigParser(interpolation=None)
                parser.read_string((self.overrides / key).read_text())
                self.assertEqual(parser['Desktop Entry']['Hidden'], 'false')

    def test_runtime_after_restart_keeps_approved_apps_and_stops_unapproved_gui(self):
        self.save_snapshot()
        procs = [Mock(pid=500+i, info={'exe': exe, 'uids': SimpleNamespace(real=1000)})
                 for i, exe in enumerate(['/usr/bin/dolphin', '/usr/bin/systemsettings',
                     '/opt/google/chrome/chrome', '/usr/bin/rofi', '/usr/bin/konsole',
                     '/snap/firefox/999/usr/lib/firefox/firefox', '/usr/bin/kate', '/usr/bin/plasmashell'])]
        with patch.object(ops.psutil, 'process_iter', return_value=procs), \
             patch.object(Path, 'read_text', side_effect=FileNotFoundError):
            self.assertEqual(ops.scan_processes(), 1)
        procs[6].kill.assert_called_once()
        for p in procs[:6] + procs[7:]:
            p.kill.assert_not_called()

    def test_offline_disable_and_cold_start_remove_overrides_and_kiosk(self):
        self.save_snapshot()
        data = json.loads(self.path.read_text())
        data['enabled'] = False
        self.path.write_text(json.dumps(data))
        result = ops.initialize_guardian_apps(1000, self.path)
        self.assertFalse(result['enabled'])
        self.assertFalse(result['active'])
        self.assertEqual(result['apps'], SAVED_APPS)
        self.assertEqual(result['application']['errors'], [])
        self.assertEqual(list(self.overrides.glob('*.desktop')), [])
        self.assertNotIn(ops.BEGIN, self.kde.read_text())
        self.assertFalse(ops.initialize_guardian_apps(1000, self.path)['enabled'])

    def test_builtin_recovery_launcher_should_not_use_daemon_dependent_launch_gate(self):
        self.save_snapshot()
        rendered = (self.overrides / 'sentinel-guardian-access.desktop').read_text()
        self.assertNotIn('sentinel.guardian_apps.launch', rendered,
            'Recovery is always selected, but its generated launcher still requires a live daemon.')
        self.assertIn('NoDisplay=true', rendered)

    def test_approved_konsole_should_not_conflict_with_kde_shell_authorization(self):
        self.save_snapshot()
        source = (self.overrides / 'org.kde.konsole.desktop').read_text()
        denied = diagnostics.launcher_authorizations(source,
            diagnostics.read_restrictions(self.kde.read_text()))
        self.assertEqual(denied, [], 'KDE authorization can reject Konsole before the Sentinel gate runs.')

    def test_cold_start_allows_kio_launch_for_approved_apps_and_recovery(self):
        self.save_snapshot()
        actions = diagnostics.read_restrictions(self.kde.read_text())
        # KIO's global check applies even without X-KDE-AuthorizeAction.
        self.assertNotIn(actions.get('run_desktop_files'), {'false', '0', 'no'})
        for key in (*SAVED_APPS, 'sentinel-guardian-access.desktop'):
            with self.subTest(desktop_id=key):
                self.assertEqual(diagnostics.launcher_authorizations(
                    (self.overrides / key).read_text(), actions), [])

    def test_cold_start_migrates_old_global_launch_deny_preserving_admin_config(self):
        self.save_snapshot()
        admin = '[General]\nColorScheme=Breeze\n'
        old = (ops.BEGIN + '[KDE Action Restrictions]\n'
               'run_desktop_files[$i]=false\nshell_access[$i]=false\n' + ops.END)
        self.kde.write_text(admin + old)
        ops.initialize_guardian_apps(1000, self.path)
        self.assertTrue(self.kde.read_text().startswith(admin))
        actions = diagnostics.read_restrictions(self.kde.read_text())
        self.assertNotIn('run_desktop_files', actions)
        self.assertEqual(ops.policy()['apps'], SAVED_APPS)
        self.assertTrue(ops.policy()['active'])
        ops.set_enabled(False, guardian_authenticated=True)
        self.assertEqual(self.kde.read_text(), admin)

    def test_shell_restriction_returns_when_konsole_approval_is_revoked(self):
        self.save_snapshot()
        self.assertNotIn('shell_access[$i]=false', self.kde.read_text())
        ops.revoke('org.kde.konsole.desktop', guardian_authenticated=True)
        self.assertIn('shell_access[$i]=false', self.kde.read_text())
        self.assertIn('Hidden=true', (self.overrides / 'org.kde.konsole.desktop').read_text())

    def test_diagnostics_do_not_write_reconcile_or_spawn_apps(self):
        self.save_snapshot()
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        read = Path.read_text
        def read_text(path, *args, **kwargs):
            if path == Path('/etc/sentinel/daemon.toml'):
                return 'protected_user = "test-desktop"\n'
            return read(path, *args, **kwargs)
        from sentinel.settings import paths
        with patch.object(paths, 'GUARDIAN_APPS_FILE', self.path), \
             patch.object(paths, 'GUARDIAN_DESKTOP_STATE_DIR', ops.GUARDIAN_DESKTOP_STATE_DIR), \
             patch.object(Path, 'read_text', new=read_text), \
             patch.object(diagnostics.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=1000)), \
             patch.object(client, 'get_guardian_app_policy', side_effect=client.SentineldError('sentineld is not running.')), \
             patch.object(diagnostics, 'service_status', return_value={}), \
             patch.object(diagnostics, 'daemon_processes', return_value=[]), \
             patch.object(diagnostics, 'daemon_lifecycle', return_value={}), \
             patch.object(diagnostics, 'api_health', return_value={}), \
             patch.object(diagnostics, 'package_hashes', return_value={}), \
             patch.object(Path, 'is_socket') as socket_probe, \
             patch.object(ops, 'reconcile') as reconcile, patch.object(ops, '_atomic') as atomic, \
             patch.object(ops, 'initialize_guardian_apps') as initialize, \
             patch('subprocess.Popen') as spawn:
            # Test both installations without inspecting the host's live socket.
            results = []
            for available in (False, True):
                socket_probe.return_value = available
                results.append((available, diagnostics.collect_report(self.root)))
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        for mutation in (reconcile, atomic, initialize, spawn):
            mutation.assert_not_called()
        for available, result in results:
            with self.subTest(recovery_socket_available=available):
                self.assertNotIn('offline_probe_error', result)
                self.assertTrue(all(row['approved'] for row in result['launchers']))
                self.assertTrue(all(row['offline_recovery_socket_available'] == available
                                    for row in result['launchers']))
                codes = {item['code'] for item in result['findings']}
                self.assertIn('DAEMON_DOWN_WITH_GATED_LAUNCHERS', codes)
                self.assertNotIn('RECOVERY_USES_SAME_RPC_GATE', codes)
                self.assertNotIn('APPROVAL_CONFLICTS_WITH_KDE_AUTHORIZATION', codes)
                self.assertEqual('OFFLINE_RECOVERY_SOCKET_MISSING' in codes, not available)


@unittest.skipIf(QtWidgets is None, 'PyQt6 required to exercise launch failure dialogs')
class DaemonOutageLaunchTests(unittest.TestCase):
    def test_daemon_outage_rejects_normal_approved_launchers(self):
        for key in SAVED_APPS:
            with self.subTest(desktop_id=key):
                sock = MagicMock()
                sock.__enter__.return_value.connect.side_effect = FileNotFoundError()
                with patch.object(client.socket, 'socket', return_value=sock), \
                     patch.object(launch.os, 'geteuid', return_value=1000), \
                     patch.object(launch.sys, 'argv', ['launcher', key]), \
                     patch.object(launch.subprocess, 'Popen') as spawn, \
                     patch.object(QtWidgets, 'QApplication'), \
                     patch.object(QtWidgets.QMessageBox, 'information') as message:
                    with self.assertRaises(SystemExit) as failure:
                        launch.main()
                self.assertEqual(failure.exception.code, 1)
                spawn.assert_not_called()
                self.assertIn('Cannot connect to Sentinel backend.', message.call_args.args[-1])

    def test_cached_recovery_gate_starts_only_fixed_unprivileged_manager_without_rpc(self):
        with patch.object(launch.os, 'geteuid', return_value=1000), \
             patch.object(launch.sys, 'argv', ['launcher', 'sentinel-guardian-access.desktop', '/tmp/evil']), \
             patch.object(launch, 'request') as request, \
             patch.object(launch.subprocess, 'Popen') as spawn:
            launch.main()
        request.assert_not_called()
        spawn.assert_called_once_with(['/usr/lib/sentinel/venv/bin/python', '-I', '-m',
                                      'sentinel.guardian_apps.manager'], start_new_session=True)

    def test_recovery_manager_opens_offline_window_without_daemon(self):
        with patch.object(manager, 'QApplication'), \
             patch.object(manager.client, 'get_lock_state', side_effect=client.SentineldError('sentineld is not running.')), \
             patch.object(manager, 'Manager') as window, \
             patch.object(manager, 'OfflineRecovery') as recovery_window, \
             patch.object(manager, 'finish_gui'):
            manager.main()
        window.assert_not_called()
        recovery_window.return_value.show.assert_called_once()

    def test_cached_portal_gate_can_open_recovery_without_backend_or_arbitrary_argv(self):
        with patch.object(launch.os, 'geteuid', return_value=1000), \
             patch.object(launch.sys, 'argv', ['launcher', 'sentinel-portal.desktop', '/tmp/evil']), \
             patch.object(launch, 'request') as request, \
             patch.object(launch.subprocess, 'Popen') as spawn:
            launch.main()
        request.assert_not_called()
        spawn.assert_called_once_with(['/usr/lib/sentinel/venv/bin/python', '-I', '-m',
                                      'sentinel.portal.main'], start_new_session=True)


class DiagnosticClassificationTests(unittest.TestCase):
    def test_global_launch_deny_blocks_even_launcher_without_authorization_metadata(self):
        source = '[Desktop Entry]\nType=Application\nName=Dolphin\nExec=/usr/bin/dolphin\n'
        for denied in ('false', '0', 'no'):
            with self.subTest(value=denied):
                self.assertEqual(diagnostics.launcher_authorizations(
                    source, {'run_desktop_files': denied}), ['run_desktop_files'])
        self.assertEqual(diagnostics.launcher_authorizations(source, {}), [])
        self.assertEqual(diagnostics.launcher_authorizations(
            source, {'run_desktop_files': 'true'}), [])

    def test_global_launch_deny_reported_on_disk_and_predicted_while_disabled(self):
        base = {'persisted_policy': {'enabled': False}, 'rpc': {'reachable': True},
                'services': {'sentineld.service': {'UnitFileState': 'enabled'}}}
        for field, expected in (
                ('kde_action_restrictions_on_disk', 'KDE_BLOCKS_ALL_DESKTOP_LAUNCHES'),
                ('kde_action_restrictions_if_enabled', 'KDE_WOULD_BLOCK_ALL_DESKTOP_LAUNCHES')):
            with self.subTest(field=field):
                report = {**base, field: {'run_desktop_files': 'false'}}
                self.assertIn(expected, {row['code'] for row in diagnostics.findings(report)})

    def test_disabled_clean_state_does_not_claim_historical_cause(self):
        report = {'persisted_policy': {'enabled': False}, 'rpc': {'reachable': True},
                  'services': {'sentineld.service': {'UnitFileState': 'enabled'}}, 'launchers': []}
        codes = {item['code'] for item in diagnostics.findings(report)}
        self.assertEqual(codes, {'POLICY_CURRENTLY_DISABLED'})

    def test_stale_runtime_transient_startup_and_api_wait_are_distinct(self):
        report = {'rpc': {'reachable': True, 'policy': {'application': {'session_restart_required': True}}},
                  'services': {'sentineld.service': {'UnitFileState': 'disabled'},
                               'sentineld-dev.service': {'Transient': 'yes'}},
                  'api_health': {'8000': {'ready': False}},
                  'package_hashes': {'module': {'same_bytes': False}}}
        codes = {item['code'] for item in diagnostics.findings(report)}
        self.assertTrue({'DAEMON_NOT_ENABLED_AT_BOOT', 'TRANSIENT_DEVELOPMENT_DAEMON',
                         'CHECKOUT_AND_RUNTIME_DIFFER', 'MONITOR_WAITING_FOR_MODEL_APIS',
                         'SESSION_RESTART_FLAG_SET'} <= codes)
        self.assertNotIn('DAEMON_DOWN_WITH_GATED_LAUNCHERS', codes)

    def test_kde_authorization_respects_key_and_group_immutability_syntax(self):
        actions = diagnostics.read_restrictions('[KDE Action Restrictions][$i]\nshell_access[$i]=false\nother=true\n')
        source = '[Desktop Entry]\nX-KDE-AuthorizeAction=shell_access,other\n'
        self.assertEqual(diagnostics.launcher_authorizations(source, actions), ['shell_access'])

    def test_service_probe_does_not_expose_other_environment_values(self):
        with patch.object(diagnostics, '_command', return_value=(0,
                'ActiveState=active\nEnvironment=SECRET=private SENTINEL_DEV_ALLOW_FIREFOX=1\n', '')):
            result = diagnostics.service_status('sentineld.service')
        self.assertTrue(result['firefox_dev_exemption'])
        self.assertNotIn('private', json.dumps(result))

    def test_runtime_enablement_is_not_reported_as_persistent_boot_startup(self):
        report = {'rpc': {'reachable': True},
                  'services': {'sentineld.service': {'UnitFileState': 'enabled-runtime'}}}
        self.assertIn('DAEMON_NOT_ENABLED_AT_BOOT',
                      {item['code'] for item in diagnostics.findings(report)})

    def test_disabled_report_predicts_recovery_and_kde_conflicts_without_enabling(self):
        report = {'rpc': {'reachable': False}, 'persisted_policy': {'enabled': False},
                  'services': {'sentineld.service': {'UnitFileState': 'enabled'}},
                  'launchers': [
                      {'desktop_id': 'sentinel-guardian-access.desktop', 'gate_when_enabled': True,
                       'manager_requires_rpc': True, 'generated_gate': False},
                      {'desktop_id': 'org.kde.konsole.desktop', 'approved': True,
                       'denied_kde_actions': [], 'denied_kde_actions_if_enabled': ['shell_access']} ]}
        codes = {item['code'] for item in diagnostics.findings(report)}
        self.assertIn('RECOVERY_GATE_WHEN_ENABLED', codes)
        self.assertIn('APPROVED_APP_WOULD_CONFLICT_WITH_KDE_AUTHORIZATION', codes)
        self.assertNotIn('DAEMON_DOWN_WITH_GATED_LAUNCHERS', codes)

    def test_mismatched_saved_identity_is_reported(self):
        report = {'services': {'sentineld.service': {'UnitFileState': 'enabled'}},
                  'rpc': {'reachable': True}, 'launchers': [
                      {'desktop_id': 'kate.desktop', 'saved': True, 'installed': True, 'approved': False}]}
        self.assertIn('SAVED_APPROVAL_NO_LONGER_MATCHES_LAUNCHER',
                      {item['code'] for item in diagnostics.findings(report)})

    def test_lifecycle_probe_filters_logs_and_keeps_only_lifecycle_fields(self):
        with patch.object(diagnostics, '_command', return_value=(0, json.dumps({
                'UNIT': 'sentineld-dev.service', '__REALTIME_TIMESTAMP': '1000',
                'MESSAGE': 'Main process exited, code=exited, status=1/FAILURE',
                'UNRELATED_PRIVATE_FIELD': 'private'}) + '\n', '')) as command:
            result = diagnostics.daemon_lifecycle()
        self.assertNotIn('private', json.dumps(result))
        self.assertEqual(len(result['events']), 1)
        self.assertTrue(any(arg.startswith('--grep=') for arg in command.call_args.args[0]))

    def test_api_probe_checks_all_ports_without_starting_services(self):
        opener = Mock()
        response = MagicMock(status=200)
        response.read.return_value = b'{"status":"ok","model_loaded":true}'
        response.__enter__.return_value = response
        opener.open.side_effect = [response, OSError('connection refused'), response, response]
        with patch.object(diagnostics.urllib.request, 'build_opener', return_value=opener):
            result = diagnostics.api_health()
        self.assertEqual(len(result), 4)
        self.assertTrue(result['8000']['ready'])
        self.assertFalse(result['8001']['ready'])
        self.assertEqual(opener.open.call_count, 4)


if __name__ == '__main__':
    unittest.main()
