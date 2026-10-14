"""Sentinel-only updates have a fixed package scope and stop on index failure."""
import subprocess
import unittest
from unittest.mock import patch

from sentinel.daemon.operations import system_ops


class SentinelUpdateTests(unittest.TestCase):
    def inventory(self, packages):
        return ''.join(f'{name}\tinstalled\n' for name in packages)

    def test_only_installed_sentinel_components_are_requested(self):
        installed = ['sentinel', 'sentinel-core', 'sentinel-assets', 'sentinel-desktop',
                     'sentinel-models', 'sentinel-platform', 'kate', 'google-chrome-stable']
        with patch.object(system_ops.subprocess, 'check_output', return_value=self.inventory(installed)), \
             patch.object(system_ops, 'run_apt') as run, \
             patch.object(system_ops.Path, 'exists', return_value=True):
            result = system_ops._sentinel_update()
        self.assertEqual(result['packages'], list(system_ops.SENTINEL_PACKAGES))
        self.assertTrue(result['reboot_required'])
        self.assertEqual(run.call_args_list[0].args[0], ['update'])
        args = run.call_args_list[1].args[0]
        self.assertIn('--only-upgrade', args)
        self.assertNotIn('kate', args)
        self.assertNotIn('google-chrome-stable', args)
        self.assertEqual(args[args.index('--') + 1:], list(system_ops.SENTINEL_PACKAGES))

    def test_application_host_does_not_install_an_absent_platform(self):
        inventory = self.inventory(['sentinel', 'sentinel-core']) + 'sentinel-platform\tconfig-files\n'
        with patch.object(system_ops.subprocess, 'check_output', return_value=inventory), \
             patch.object(system_ops, 'run_apt') as run:
            system_ops._sentinel_update()
        self.assertNotIn('sentinel-platform', run.call_args.args[0])

    def test_failed_refresh_never_attempts_a_package_upgrade(self):
        with patch.object(system_ops.subprocess, 'check_output', return_value=self.inventory(['sentinel'])), \
             patch.object(system_ops, 'run_apt', side_effect=TimeoutError('index failed')) as run:
            with self.assertRaises(TimeoutError):
                system_ops._sentinel_update()
        self.assertEqual(run.call_count, 1)

    def test_missing_installation_or_inventory_failure_never_runs_apt(self):
        for value in (self.inventory(['kate']), subprocess.CalledProcessError(2, ['dpkg-query'])):
            with self.subTest(value=value), patch.object(system_ops.subprocess, 'check_output',
                    **({'side_effect': value} if isinstance(value, Exception) else {'return_value': value})), \
                 patch.object(system_ops, 'run_apt') as run:
                with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    system_ops._sentinel_update()
                run.assert_not_called()
