"""Isolated enforcement and real lock-policy tests; never touch host devices."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sentinel.daemon.operations import boot_media_ops as boot, settings_ops
from sentinel.daemon.core.lock_state import LockMode, LockState, STATE_VERSION
from sentinel.daemon.security import policy


class BootMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.tmp)
        self.udev = self.root / 'udev.rules'
        self.polkit = self.root / 'polkit.rules'
        self.enterContext(patch.object(boot, 'UDEV_FILE', self.udev))
        self.enterContext(patch.object(boot, 'POLKIT_FILE', self.polkit))
        self.run = self.enterContext(patch.object(boot, '_run'))
        self.enterContext(patch.object(boot, 'usb_nodes', return_value=[Path('/dev/fake-usb')]))
        self.chown = self.enterContext(patch.object(boot.os, 'chown'))
        self.chmod = self.enterContext(patch.object(boot.os, 'chmod'))

    def test_enable_repair_disable(self):
        boot.apply_boot_media_policy(settings={'boot_media_protection': True})
        self.assertEqual(self.udev.read_text(), boot.UDEV_RULE)
        self.assertEqual(self.polkit.read_text(), boot.POLKIT_RULE)
        self.run.assert_any_call('setfacl', '-b', '/dev/fake-usb')
        self.chmod.assert_called_with(Path('/dev/fake-usb'), 0o600)
        boot.apply_boot_media_policy(settings={'boot_media_protection': True})
        self.assertEqual(self.chmod.call_count, 2)
        boot.apply_boot_media_policy(settings={'boot_media_protection': False})
        self.assertFalse(self.udev.exists())
        self.assertFalse(self.polkit.exists())
        self.run.assert_any_call('udevadm', 'trigger', '--action=add', '--subsystem-match=block')

    def test_default_disabled_does_nothing(self):
        boot.apply_boot_media_policy(settings={})
        self.run.assert_not_called()

    def test_invalid_value_fails_before_changes(self):
        with self.assertRaises(ValueError):
            boot.apply_boot_media_policy(settings={'boot_media_protection': 'true'})
        self.assertFalse(self.udev.exists())

    def test_usb_ssd_partition_detection_excludes_internal(self):
        sys = self.root / 'sys'; sys.mkdir()
        usb = self.root / 'devices/usb1/port/target/block/sda/sda1'; usb.mkdir(parents=True)
        internal = self.root / 'devices/pci/block/nvme0n1'; internal.mkdir(parents=True)
        (sys / 'sda1').symlink_to(usb)
        (sys / 'nvme0n1').symlink_to(internal)
        self.assertEqual(ORIGINAL_USB_NODES(sys, Path('/dev')), [Path('/dev/sda1')])

    def test_lock_matrix_and_failed_enable_not_persisted(self):
        file = self.root / 'settings.toml'
        for mode, auth, old, new, allowed in [
            (LockMode.UNLOCKED, False, False, True, True),
            (LockMode.UNLOCKED, False, True, False, True),
            (LockMode.SELF_LOCK, False, False, True, True),
            (LockMode.SELF_LOCK, False, True, False, False),
            (LockMode.GUARDIAN_LOCK, False, False, True, False),
            (LockMode.GUARDIAN_LOCK, True, False, True, True),
            (LockMode.GUARDIAN_LOCK, False, True, False, False),
            (LockMode.GUARDIAN_LOCK, True, True, False, True),
        ]:
            with self.subTest(mode=mode, auth=auth, old=old, new=new):
                file.write_text(f'boot_media_protection = {str(old).lower()}\n')
                with patch.object(policy, 'get_lock_state', return_value=LockState(version=STATE_VERSION, mode=mode)), patch.object(boot, 'apply_boot_media_policy') as apply:
                    if allowed:
                        settings_ops.set_setting('boot_media_protection', new, path=file, guardian_authenticated=auth)
                        apply.assert_called_once()
                    else:
                        with self.assertRaises(policy.PolicyDeniedError):
                            settings_ops.set_setting('boot_media_protection', new, path=file, guardian_authenticated=auth)
                        apply.assert_not_called()
                        self.assertEqual(settings_ops.get_setting('boot_media_protection', path=file), old)
        file.write_text('boot_media_protection = false\n')
        with patch.object(policy, 'get_lock_state', return_value=LockState(version=STATE_VERSION, mode=LockMode.UNLOCKED)), patch.object(boot, 'apply_boot_media_policy', side_effect=ValueError('failed')):
            with self.assertRaises(ValueError):
                settings_ops.set_setting('boot_media_protection', True, path=file)
        self.assertIn('false', file.read_text())

    def test_polkit_rule_executes_allow_and_deny_matrix(self):
        import json
        import shutil
        import subprocess
        if not shutil.which("node"):
            self.skipTest("Node needed to execute polkit JavaScript")
        script = "var rule; var polkit={Result:{NO: 'no'},addRule:function(r){rule=r;}};" + boot.POLKIT_RULE
        for user, action, expected in [
            ("test_user", "org.freedesktop.policykit.exec", "no"),
            ("test_user", "org.freedesktop.udisks2.modify-device-system", "no"),
            ("test_user", "org.freedesktop.udisks2.open-device", "no"),
            ("test_user", "org.freedesktop.udisks2.filesystem-mount", None),
            ("test_user", "org.freedesktop.udisks2.filesystem-unmount-others", None),
            ("test_user", "org.freedesktop.udisks2.encrypted-unlock", None),
            ("test_user", "org.freedesktop.udisks2.eject-media", None),
            ("root", "org.freedesktop.policykit.exec", None),
        ]:
            invocation = "console.log(JSON.stringify(rule({id:" + json.dumps(action) + "},{user:" + json.dumps(user) + "}) || null));"
            result = subprocess.run(["node", "-e", script + invocation], capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), expected)

    def test_command_errors_report_failure(self):
        import subprocess
        with patch.object(boot, '_run', wraps=ORIGINAL_RUN), patch.object(boot.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'failure')):
            with self.assertRaisesRegex(ValueError, 'failure'):
                boot.apply_boot_media_policy(settings={'boot_media_protection': True})


ORIGINAL_RUN = boot._run
ORIGINAL_USB_NODES = boot.usb_nodes
