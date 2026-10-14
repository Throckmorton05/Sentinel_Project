"""Exact legacy bytes can migrate despite root-group write bits; custom state cannot."""
import hashlib
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import platform_postinst


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'sentinel-dns.service'
        self.path.write_bytes(b'known old service bytes')
        self.path.chmod(0o664)
        self.expected = hashlib.sha256(self.path.read_bytes()).hexdigest()

    def remove(self, *, uid=0, gid=0):
        original = os.fstat
        def metadata(fd):
            info = original(fd)
            return SimpleNamespace(st_uid=uid, st_gid=gid, st_mode=info.st_mode,
                                   st_ino=info.st_ino, st_dev=info.st_dev)
        # Content behavior uses a disposable /tmp file. The protected-parent
        # boundary is tested separately; production may not use /tmp parents.
        with patch.object(platform_postinst, 'protected_legacy_parent'), \
             patch.object(platform_postinst.os, 'fstat', side_effect=metadata):
            return platform_postinst.remove_known_legacy(self.path, self.expected)

    def test_exact_root_group_writable_old_copy_is_removed(self):
        self.assertTrue(self.remove())
        self.assertFalse(self.path.exists())

    def test_modified_group_writable_copy_is_not_accepted(self):
        self.path.write_bytes(b'custom service')
        with self.assertRaisesRegex(ValueError, 'unprotected'):
            self.remove()
        self.assertEqual(self.path.read_bytes(), b'custom service')

    def test_custom_read_only_override_is_retained(self):
        self.path.write_bytes(b'custom service')
        self.path.chmod(0o644)
        with patch.object(platform_postinst, 'protected', return_value=self.path) as guard:
            self.assertFalse(self.remove())
        guard.assert_called_once_with(self.path)
        self.assertTrue(self.path.exists())

    def test_world_write_nonroot_owner_and_nonroot_writable_group_rejected(self):
        for mode, uid, gid in ((0o666, 0, 0), (0o664, 1000, 0), (0o664, 0, 1000)):
            with self.subTest(mode=mode, uid=uid, gid=gid):
                self.path.chmod(mode)
                with self.assertRaisesRegex(ValueError, 'unprotected'):
                    self.remove(uid=uid, gid=gid)
                self.assertTrue(self.path.exists())

    def test_symlink_override_is_preserved_without_reading_target(self):
        target = self.path.with_name('target')
        self.path.rename(target)
        self.path.symlink_to(target)
        self.assertFalse(self.remove())
        self.assertTrue(self.path.is_symlink())
        self.assertTrue(target.exists())

    def test_unsafe_parent_directory_rejected(self):
        for mode, uid in ((stat.S_IFDIR | 0o775, 0),
                          (stat.S_IFDIR | 0o755, 1000),
                          (stat.S_IFLNK | 0o755, 0)):
            with self.subTest(mode=mode, uid=uid):
                with patch.object(platform_postinst.Path, 'lstat', return_value=SimpleNamespace(
                    st_mode=mode, st_uid=uid)):
                    with self.assertRaisesRegex(ValueError, 'unprotected'):
                        platform_postinst.protected_legacy_parent(self.path)

    def test_cubic_migration_repairs_boot_links_without_starting_services(self):
        with patch.object(platform_postinst.os, 'geteuid', return_value=0), \
             patch.object(platform_postinst, 'repository'), \
             patch.object(platform_postinst, 'migrate_units', return_value=True), \
             patch.object(platform_postinst, 'configure_splash'), \
             patch.object(platform_postinst.Path, 'exists', return_value=False), \
             patch.object(platform_postinst.Path, 'is_dir', return_value=False), \
             patch.object(platform_postinst.subprocess, 'run') as run, \
             patch.object(platform_postinst, 'write') as write:
            platform_postinst.main()
        self.assertEqual(run.call_args_list[0].args[0],
                         ['systemctl', 'reenable', *platform_postinst.UNITS])
        self.assertFalse(any('start' in call.args[0] for call in run.call_args_list))
        write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
