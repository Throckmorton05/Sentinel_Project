"""Read-only and temporary-directory tests; never customize the host image."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ISO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ISO))
import boot_security
import common
import image
import prepare
import protected_apt
import provision_install
import session_guard


def transaction(*rows, command='apt-get remove chrome'):
    return 'VERSION 2\nCommandLine::AsString=' + command + '\n\n' + '\n'.join(rows) + '\n'


class KitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'input').write_text('original')
        self.manifest = {'format_version': 1, 'target': 'kubuntu-26.04-amd64',
                         'sha256': {'input': common.digest(self.root / 'input')}}
        self.save()

    def save(self):
        (self.root / 'kit-manifest.json').write_text(json.dumps(self.manifest))

    def test_complete_manifest_and_tamper(self):
        common.verify_kit(self.root)
        (self.root / 'input').write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            common.verify_kit(self.root)

    def test_extra_file_rejected(self):
        (self.root / 'extra').write_text('unlisted')
        with self.assertRaisesRegex(ValueError, 'unlisted'):
            common.verify_kit(self.root)

    def test_traversal_rejected(self):
        self.manifest['sha256'] = {'../outside': '0' * 64}
        self.save()
        with self.assertRaisesRegex(ValueError, 'Invalid'):
            common.verify_kit(self.root)

    def test_symlink_rejected_even_with_correct_bytes(self):
        (self.root / 'alias').symlink_to(self.root / 'input')
        self.manifest['sha256']['alias'] = common.digest(self.root / 'input')
        self.save()
        with self.assertRaisesRegex(ValueError, 'symlink'):
            common.verify_kit(self.root)

    def test_host_root_rejected_before_changes(self):
        stat = os.stat('/')
        with patch.object(common.os, 'geteuid', return_value=0), patch.object(common.os, 'stat', return_value=stat):
            with self.assertRaisesRegex(ValueError, 'host root'):
                common.require_image_environment()

    def test_nonroot_rejected(self):
        with patch.object(common.os, 'geteuid', return_value=1000):
            with self.assertRaisesRegex(ValueError, 'INSIDE Cubic'):
                common.require_image_environment()

    def test_atomic_write_rejects_symlink_ancestor(self):
        (self.root / 'link').symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlinked'):
            common.write(self.root / 'link/new', 'unsafe')
        self.assertFalse((self.root / 'new').exists())

    def test_tar_prefix_and_ambiguous_members(self):
        archive = self.root / 'source.tar'
        with tarfile.open(archive, 'w') as stream:
            info = tarfile.TarInfo('./AdGuardHome/AdGuardHome')
            info.size = 6
            stream.addfile(info, io.BytesIO(b'binary'))
        prepare.extract_one(archive, 'AdGuardHome/AdGuardHome', self.root / 'binary')
        self.assertEqual((self.root / 'binary').read_bytes(), b'binary')
        with tarfile.open(archive, 'w') as stream:
            for name in ('./AdGuardHome/AdGuardHome', 'AdGuardHome/AdGuardHome'):
                info = tarfile.TarInfo(name)
                info.size = 1
                stream.addfile(info, io.BytesIO(b'x'))
        with self.assertRaisesRegex(ValueError, 'regular archive member'):
            prepare.extract_one(archive, 'AdGuardHome/AdGuardHome', self.root / 'binary')


class DebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = []
        for name in sorted(common.PACKAGES):
            root = self.root / name
            (root / 'DEBIAN').mkdir(parents=True)
            (root / 'DEBIAN/control').write_text(f'Package: {name}\nVersion: 0.1.0-1\nArchitecture: all\nMaintainer: ISO Test <test@example.invalid>\nDescription: disposable metadata fixture\n')
            target = self.root / (name + '.deb')
            subprocess.run(['dpkg-deb', '--build', str(root), str(target)], check=True, stdout=subprocess.DEVNULL)
            self.paths.append(target)
        (self.root / 'SHA256SUMS').write_text(''.join(common.digest(p) + '  ' + p.name + '\n' for p in self.paths))

    def test_real_deb_metadata_and_successful_build_checksums(self):
        packages, version = prepare.validate_debs(self.root)
        self.assertEqual(set(packages), common.PACKAGES)
        self.assertEqual(version, '0.1.0-1')

    def test_failed_or_modified_build_rejected(self):
        (self.root / 'VALIDATION_FAILED.txt').write_text('failed')
        with self.assertRaisesRegex(ValueError, 'failed validation'):
            prepare.validate_debs(self.root)
        (self.root / 'VALIDATION_FAILED.txt').unlink()
        with self.paths[0].open('ab') as stream:
            stream.write(b'tampered')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            prepare.validate_debs(self.root)

    def test_missing_package_rejected(self):
        self.paths[0].unlink()
        with self.assertRaisesRegex(ValueError, 'all five'):
            prepare.validate_debs(self.root)

    def test_mixed_version_rejected(self):
        original = prepare.deb_fields
        def mixed(path):
            fields = original(path)
            if path == self.paths[0]:
                fields['Version'] = '0.1.1-1'
            return fields
        with patch.object(prepare, 'deb_fields', side_effect=mixed):
            with self.assertRaisesRegex(ValueError, 'same version'):
                prepare.validate_debs(self.root)


class CalamaresTests(unittest.TestCase):
    def setUp(self):
        jobs = ['partition', 'mount', 'unpackfs', 'machineid', 'fstab', 'locale',
                'keyboard', 'localecfg', 'luksbootkeyfile', 'users', 'displaymanager',
                'networkcfg', 'hwclock', 'shellprocess@before_bootloader_kerncopy',
                'initramfscfg', 'initramfs', 'grubcfg', 'contextualprocess@before_bootloader',
                'bootloader', 'shellprocess@add386arch', 'automirror', 'packages',
                'contextualprocess@pkgselect_action', 'contextualprocess@pkgselect_snap_action',
                'shellprocess@rmcdrom', 'shellprocess@logs', 'umount']
        self.settings = {'dont-chroot': False, 'oem-setup': False, 'branding': 'kubuntu',
                         'instances': [{'id': 'logs', 'module': 'shellprocess', 'config': 'logs.conf'}],
                         'sequence': [{'show': ['welcome', 'pkgselect', 'users', 'summary']}, {'exec': jobs}, {'show': ['finished']}]}
        self.users = {'sudoersGroup': 'sudo', 'setRootPassword': False, 'doAutologin': False,
                      'defaultGroups': ['sudo', 'adm', 'dip', 'lpadmin', 'cdrom', 'plugdev', {'name': 'sambashare'}],
                      'passwordRequirements': {'minLength': 8}, 'user': {'shell': '/bin/bash', 'forbidden_names': ['root']}}

    def test_finalizer_order_and_no_admin_grant(self):
        settings, users = image.calamares_config(self.settings, self.users)
        self.assertEqual(settings['branding'], 'sentinel')
        jobs = settings['sequence'][1]['exec']
        expected = [job for job in self.settings['sequence'][1]['exec']
                    if job not in {'contextualprocess@pkgselect_action', 'contextualprocess@pkgselect_snap_action'}]
        self.assertEqual([job for job in jobs if job != 'shellprocess@sentinel_provision'], expected)
        self.assertEqual(jobs[-2:], ['shellprocess@sentinel_provision', 'umount'])
        self.assertEqual(users['sudoersGroup'], '')
        self.assertEqual(users['defaultGroups'], ['cdrom', 'plugdev', {'name': 'sambashare'}])
        self.assertEqual(users['passwordRequirements'], self.users['passwordRequirements'])
        self.assertIn('sentinel-dns', users['user']['forbidden_names'])
        self.assertEqual(self.users['sudoersGroup'], 'sudo')
        self.assertEqual(image.calamares_config(settings, users), (settings, users))

    def test_unsupported_install_order_and_oem_rejected(self):
        self.settings['oem-setup'] = True
        with self.assertRaisesRegex(ValueError, 'OEM'):
            image.calamares_config(self.settings, self.users)
        self.settings['oem-setup'] = False
        self.settings['sequence'][1]['exec'].remove('users')
        with self.assertRaisesRegex(ValueError, 'required installer jobs'):
            image.calamares_config(self.settings, self.users)


class AptPolicyTests(unittest.TestCase):
    policy = {'protected': ['google-chrome-stable', 'sentinel-core'],
              'allowed_install': ['google-chrome-stable', 'sentinel-core', 'git', 'steam-installer'],
              'blocked_install': ['firefox', 'xrdp']}

    def test_direct_and_dependency_removal_rejected(self):
        for command in ('apt-get remove google-chrome-stable', 'apt-get remove libgtk-3-0', 'apt-get autoremove'):
            with self.assertRaisesRegex(ValueError, 'cannot be removed'):
                protected_apt.check_plan(transaction('google-chrome-stable 140 > - **REMOVE**', command=command), self.policy)

    def test_chrome_update_allowed_downgrade_rejected(self):
        protected_apt.check_plan(transaction('google-chrome-stable 140 < 141 /var/cache/chrome.deb'), self.policy, compare=lambda new, old: False)
        with self.assertRaisesRegex(ValueError, 'Downgrading'):
            protected_apt.check_plan(transaction('google-chrome-stable 140 > 139 /var/cache/chrome.deb'), self.policy, compare=lambda new, old: True)

    def test_approved_request_dependencies_allowed(self):
        protected_apt.check_plan(transaction('git - < 1 /cache/git.deb', 'libgit-test - < 1 /cache/lib.deb',
                                            command='apt-get install -y -- git'), self.policy)
        protected_apt.check_plan(transaction('steam-installer - < 1 /cache/steam.deb', 'steam-libs-i386:i386 - < 1 /cache/libs.deb',
                                            command='apt-get install -y -- steam-installer'), self.policy)

    def test_unapproved_requests_and_blocked_dependencies_rejected(self):
        with self.assertRaisesRegex(ValueError, 'not an approved'):
            protected_apt.check_plan(transaction('unreviewed-server - < 1 /cache/x.deb', command='apt-get install unreviewed-server'), self.policy)
        with self.assertRaisesRegex(ValueError, 'outside the supported'):
            protected_apt.check_plan(transaction('firefox - < 1 /cache/x.deb', command='apt-get install git'), self.policy)
        with self.assertRaisesRegex(ValueError, 'not an approved'):
            protected_apt.check_plan(transaction('libunknown - < 1 /cache/x.deb', command='apt-get install git xrdp'), self.policy)

    def test_existing_updates_and_kernel_abi_allowed(self):
        protected_apt.check_plan(transaction('libc6 1 < 2 /cache/libc.deb', 'linux-image-6.20-generic - < 1 /cache/kernel.deb',
                                            command='apt-get upgrade -y'), self.policy)

    def test_malformed_protocol_rejected(self):
        for text in ('VERSION 1\n\n', 'VERSION 2\nIncompleteHeader', transaction('truncated 1 2')):
            with self.assertRaises(ValueError):
                protected_apt.check_plan(text, self.policy)


class SessionAndBootTests(unittest.TestCase):
    def test_bridge_setting_only_frozen_after_user_setup(self):
        source = '[Other]\nkeep=original\n[Plugins]\nwobblywindowsEnabled=true\n'
        staged = common.kwin_defaults(source)
        self.assertIn('sentinel-window-bridgeEnabled=true', staged)
        self.assertNotIn('sentinel-window-bridgeEnabled[$i]', staged)
        installed = common.kwin_defaults(staged, lock_bridge=True)
        self.assertIn('sentinel-window-bridgeEnabled[$i]=true', installed)
        self.assertIn('wobblywindowsEnabled[$i]=false', installed)
        self.assertIn('keep=original', installed)
        self.assertEqual(common.kwin_defaults(installed, lock_bridge=True), installed)

    def test_keep_oldest_valid_wayland_session(self):
        current = [('new', {'Type': 'wayland', 'Service': 'sddm', 'TimestampMonotonic': '20'}),
                   ('old', {'Type': 'wayland', 'Service': 'sddm', 'TimestampMonotonic': '10'}),
                   ('x', {'Type': 'x11', 'Service': 'sddm'}), ('console', {'Type': 'tty', 'Service': 'login'})]
        self.assertEqual(session_guard.extra_sessions(current), ['new', 'x', 'console'])

    def test_session_lookup_handles_logout_race(self):
        success = subprocess.CompletedProcess([], 0, 'User=1000\nClass=user\nType=wayland\nService=sddm\nTimestampMonotonic=10\n')
        expired = subprocess.CompletedProcess([], 1, '')
        with patch.object(session_guard.subprocess, 'check_output', return_value='4 user\n5 user\n'), patch.object(session_guard.subprocess, 'run', side_effect=[success, expired]):
            self.assertEqual([sid for sid, data in session_guard.sessions(1000)], ['4'])

    def test_grub_regular_boot_unrestricted_idempotent(self):
        text = '#!/bin/sh\nCLASS="--class gnu-linux --class gnu --class os"\n'
        modified = boot_security.normal_boot_script(text)
        self.assertIn(boot_security.MARK, modified)
        self.assertEqual(boot_security.normal_boot_script(modified), modified)
        self.assertEqual(subprocess.run(['sh', '-n'], input=modified, text=True).returncode, 0)
        with self.assertRaisesRegex(ValueError, 'Unrecognized'):
            boot_security.normal_boot_script('#!/bin/sh\nunknown=1\n')

    def test_generated_polkit_has_no_general_authentication(self):
        rules = provision_install.polkit_rules('test_user')
        self.assertIn('subject.user !== "test_user"', rules)
        self.assertIn('return polkit.Result.NO;', rules)
        self.assertNotIn('AUTH_ADMIN', rules)
        self.assertNotIn('settings.modify.system', rules)


class CubicNamespaceTests(unittest.TestCase):
    mount = '101 80 259:2 /home/test_user/Sentinel_ISO_Build/custom-root / rw,relatime - ext4 /dev/nvme0n1p2 rw\n'

    def test_reported_cubic_namespace_accepted(self):
        self.assertTrue(common.cubic_namespace_root(self.mount, 'bash\n', 'cubic', False))

    def test_ordinary_host_rejected_even_with_cubic_hostname(self):
        host = self.mount.replace('/home/test_user/Sentinel_ISO_Build/custom-root', '/')
        self.assertFalse(common.cubic_namespace_root(host, 'bash', 'cubic', False))

    def test_wrong_mount_shell_hostname_or_running_system_rejected(self):
        for mount, pid1, hostname, running in (
            (self.mount.replace('/custom-root', '/ordinary-folder'), 'bash', 'cubic', False),
            (self.mount, 'systemd', 'cubic', False),
            (self.mount, 'bash', 'development-host', False),
            (self.mount, 'bash', 'cubic', True),
            ('', 'bash', 'cubic', False),
            (self.mount + self.mount, 'bash', 'cubic', False),
        ):
            with self.subTest(mount=mount, pid1=pid1, hostname=hostname, running=running):
                self.assertFalse(common.cubic_namespace_root(mount, pid1, hostname, running))

    def test_kernel_escaped_project_path(self):
        mount = self.mount.replace('Sentinel_ISO_Build', r'Sentinel\040ISO\040Build')
        self.assertTrue(common.cubic_namespace_root(mount, 'bash', 'cubic', False))

    def test_full_environment_guard_accepts_namespace_then_checks_target(self):
        from types import SimpleNamespace
        stat = SimpleNamespace(st_dev=1, st_ino=2)
        files = {'/proc/self/mountinfo': self.mount, '/proc/1/comm': 'bash\n',
                 '/etc/os-release': 'ID=ubuntu\nVERSION_ID="26.04"\n'}
        def read(path, *args, **kwargs):
            return files[str(path)]
        with patch.object(common.os, 'geteuid', return_value=0), \
             patch.object(common.os, 'stat', return_value=stat), \
             patch.object(common.os, 'uname', return_value=SimpleNamespace(nodename='cubic')), \
             patch.object(common.Path, 'read_text', read), \
             patch.object(common.Path, 'exists', return_value=False), \
             patch.object(common.Path, 'is_file', return_value=True), \
             patch.object(common.subprocess, 'check_output', return_value='amd64\n'):
            common.require_image_environment()
            files['/etc/os-release'] = 'ID=ubuntu\nVERSION_ID="24.04"\n'
            with self.assertRaisesRegex(ValueError, '26.04'):
                common.require_image_environment()


if __name__ == '__main__':
    unittest.main()
