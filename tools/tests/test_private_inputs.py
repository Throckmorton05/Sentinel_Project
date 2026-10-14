"""Private input relocation preserves originals and never rewrites history."""
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from relocate_extension_keys import relocate


class PrivateInputsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / 'project'
        self.repo.mkdir()
        self.private = self.base / 'private'

    def source(self, component='activity'):
        path = self.repo / 'sentinel' / component / 'browser_distribution/browser_extension.pem'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'disposable byte-preservation fixture')
        return path

    def test_move_verified_copy_permissions_and_idempotence(self):
        original = self.source()
        data = original.read_bytes()
        self.assertEqual(relocate(self.repo, self.private), 1)
        self.assertFalse(original.exists())
        self.assertEqual((self.private / 'activity.pem').read_bytes(), data)
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.private / 'activity.pem').stat().st_mode), 0o600)
        self.assertEqual(relocate(self.repo, self.private), 0)

    def test_collision_preserves_every_source_and_backup(self):
        first = self.source('activity')
        second = self.source('newtab')
        self.private.mkdir()
        backup = self.private / 'newtab.pem'
        backup.write_bytes(b'different original backup')
        with self.assertRaisesRegex(ValueError, 'Different'):
            relocate(self.repo, self.private)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(backup.read_bytes(), b'different original backup')
        self.assertFalse((self.private / 'activity.pem').exists())

    def test_inside_project_and_symlink_inputs_are_rejected(self):
        source = self.source()
        with self.assertRaisesRegex(ValueError, 'outside'):
            relocate(self.repo, self.repo / 'keys')
        target = self.base / 'actual'
        source.rename(target)
        source.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'symlinked'):
            relocate(self.repo, self.private)
        self.assertTrue(target.exists())

    def test_untrack_removes_only_legacy_key_and_keeps_other_index_entries(self):
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        source = self.source()
        other = self.repo / 'README.md'
        other.write_text('public fixture')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        relocate(self.repo, self.private, untrack=True)
        tracked = subprocess.check_output(['git', '-C', str(self.repo), 'ls-files'], text=True)
        self.assertEqual(tracked.strip(), 'README.md')
        self.assertTrue(other.exists())
        self.assertTrue((self.private / 'activity.pem').exists())


if __name__ == '__main__':
    unittest.main()
