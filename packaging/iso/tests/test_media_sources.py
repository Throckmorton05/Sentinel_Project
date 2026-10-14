"""Media-source handling and real cleanup commands, all in temporary directories."""
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apt_sources import disable_media_source, media_cleanup_config, media_only_source

SOURCE = '''Types: deb
URIs: file:///cdrom
Suites: resolute
Components: main restricted
Check-Date: no
Signed-By:
 -----BEGIN PGP PUBLIC KEY BLOCK-----
 .
 test-fixture
 -----END PGP PUBLIC KEY BLOCK-----
'''


class MediaSourcesTests(unittest.TestCase):
    def test_reported_media_source_with_inline_key(self):
        self.assertTrue(media_only_source(SOURCE))
        self.assertTrue(media_only_source(SOURCE.replace('file:///cdrom', 'file:/cdrom/')))

    def test_online_sources_in_same_or_folded_or_additional_stanza_rejected(self):
        for text in (SOURCE.replace('file:///cdrom', 'file:///cdrom https://archive.ubuntu.com/ubuntu'),
                     SOURCE.replace('file:///cdrom', 'file:///cdrom\n https://archive.ubuntu.com/ubuntu'),
                     SOURCE + '\nTypes: deb\nURIs: https://archive.ubuntu.com/ubuntu\nSuites: resolute\n',
                     SOURCE.replace('file:///cdrom', 'file:///srv/other-repository'),
                     SOURCE.replace('file:///cdrom', 'file://otherhost/cdrom'),
                     SOURCE.replace('URIs:', '# URIs:')):
            with self.subTest(text=text):
                self.assertFalse(media_only_source(text))

    def test_disable_preserves_bytes_and_online_file_then_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            media = root / 'cdrom.sources'
            online = root / 'ubuntu.sources'
            media.write_text(SOURCE)
            online.write_text('Types: deb\nURIs: https://archive.ubuntu.com/ubuntu\n')
            original_online = online.read_bytes()
            disable_media_source(media)
            self.assertFalse(media.exists())
            self.assertEqual((root / 'cdrom.sources.disabled').read_text(), SOURCE)
            self.assertEqual(online.read_bytes(), original_online)
            disable_media_source(media)

    def test_mixed_source_and_conflicting_backup_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'cdrom.sources'
            mixed = SOURCE.replace('file:///cdrom', 'https://archive.ubuntu.com/ubuntu')
            source.write_text(mixed)
            with self.assertRaisesRegex(ValueError, 'non-media'):
                disable_media_source(source)
            self.assertEqual(source.read_text(), mixed)
            source.write_text(SOURCE)
            backup = source.with_name(source.name + '.disabled')
            backup.write_text('existing saved source')
            with self.assertRaisesRegex(ValueError, 'different saved'):
                disable_media_source(source)
            self.assertEqual(source.read_text(), SOURCE)
            self.assertEqual(backup.read_text(), 'existing saved source')

    def test_cleanup_succeeds_with_present_or_missing_files(self):
        config = media_cleanup_config()
        self.assertIs(config['dontChroot'], False)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            unrelated = root / 'ubuntu.sources'
            unrelated.write_text('keep online repositories')
            for name in ('cdrom.sources', 'cdrom.sources.disabled'):
                (root / name).write_text(SOURCE)
            for repetition in range(2):
                for command in config['script']:
                    args = shlex.split(command)
                    self.assertEqual(len(args), 4)
                    self.assertEqual(args[:3], ['/bin/rm', '-f', '--'])
                    destination = Path(args[3])
                    self.assertEqual(destination.parent, Path('/etc/apt/sources.list.d'))
                    self.assertIn(destination.name, {'cdrom.sources', 'cdrom.sources.disabled'})
                    result = subprocess.run([*args[:3], str(root / destination.name)], capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(unrelated.read_text(), 'keep online repositories')


if __name__ == '__main__':
    unittest.main()
