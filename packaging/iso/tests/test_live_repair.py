"""Real file and shell checks for installer-media and live-desktop regressions."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apt_sources import disable_media_source, stage_installer_media_source, media_only_source
from boot_branding import branded_text, brand_boot_tree
from live_branding import install_live_branding

PROJECT = Path(__file__).resolve().parents[3]


class InstallerMediaTests(unittest.TestCase):
    def test_missing_input_becomes_valid_disabled_copyable_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'cdrom.sources'
            stage_installer_media_source(source)
            text = source.read_text()
            self.assertTrue(media_only_source(text))
            self.assertIn('Enabled: no\n', text)
            self.assertIn('Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg', text)
            copy = Path(directory) / 'copy'; copy.mkdir()
            subprocess.run(['cp', str(source), str(copy)], check=True)
            self.assertEqual((copy / source.name).read_bytes(), source.read_bytes())

    def test_every_stanza_disabled_and_original_key_preserved_across_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'cdrom.sources'
            text = ('Types: deb\nURIs: file:///cdrom\nSuites: resolute\n'
                    'Enabled: yes\nSigned-By:\n -----BEGIN PGP PUBLIC KEY BLOCK-----\n .\n key\n'
                    ' -----END PGP PUBLIC KEY BLOCK-----\n\n'
                    'Types: deb\nURIs: file:/cdrom/\nSuites: resolute\n')
            source.write_text(text)
            for _ in range(2):
                disable_media_source(source)
                stage_installer_media_source(source)
                self.assertEqual(source.read_text().count('Enabled: no'), 2)
                self.assertNotIn('Enabled: yes', source.read_text())
                self.assertIn(' .\n key\n', source.read_text())
                self.assertEqual(source.with_name('cdrom.sources.disabled').read_text(), text)

    def test_mixed_online_input_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'cdrom.sources'
            text = 'Types: deb\nURIs: https://example.org/ubuntu\nSuites: resolute\n'
            source.write_text(text)
            with self.assertRaises(ValueError):
                stage_installer_media_source(source)
            self.assertEqual(source.read_text(), text)


class DesktopHandoffTests(unittest.TestCase):
    def test_stock_and_legacy_wrapper_reach_plasma_without_temporary_compositor(self):
        for prompt in ('kubuntu-installer-prompt', '/usr/bin/sentinel-live-welcome'):
            with self.subTest(prompt=prompt), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                wrapper = root / 'usr/libexec/start-kubuntu-live-env'
                wrapper.parent.mkdir(parents=True)
                wrapper.write_text('#!/bin/sh\nset -e\n'
                    'kwin_wayland --xwayland --no-lockscreen ' + prompt + ';\n'
                    'startplasma-wayland\n')
                launcher = root / 'usr/share/applications/kubuntu-calamares.desktop'
                launcher.parent.mkdir(parents=True)
                launcher.write_text('[Desktop Entry]\nExec=sudo /usr/bin/calamares-launch-normal\n')
                install_live_branding(PROJECT, root)
                install_live_branding(PROJECT, root)
                binaries = root / 'bin'; binaries.mkdir()
                compositor = binaries / 'kwin_wayland'
                compositor.write_text('#!/bin/sh\nexit 99\n'); compositor.chmod(0o755)
                plasma = binaries / 'startplasma-wayland'
                plasma.write_text('#!/bin/sh\nprintf "PLASMA_STARTED"\n'); plasma.chmod(0o755)
                result = subprocess.run(['/bin/sh', str(wrapper)], capture_output=True, text=True,
                                        env={**os.environ, 'PATH': str(binaries)}, check=True)
                self.assertEqual(result.stdout, 'PLASMA_STARTED')


class BootBrandingTests(unittest.TestCase):
    def test_only_visible_titles_change(self):
        text = ('menuentry "Try or Install Kubuntu" --class kubuntu {\n'
                ' linux /casper/vmlinuz boot=casper maybe-ubiquity quiet splash ---\n}\n'
                "menuentry 'Kubuntu (safe graphics)' {\n linux /casper/vmlinuz nomodeset ---\n}\n"
                'search --label Kubuntu\nmenu label ^Try Kubuntu\n')
        expected = text.replace('Try or Install Kubuntu', 'Try or Install Sentinel Linux').replace(
            'Kubuntu (safe graphics)', 'Sentinel Linux (safe graphics)').replace('^Try Kubuntu', '^Try Sentinel Linux')
        self.assertEqual(branded_text(text), expected)
        self.assertEqual(branded_text(expected), expected)

    def test_boot_tree_and_wrong_directory_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'custom-disk'; (root / 'casper').mkdir(parents=True)
            config = root / 'boot/grub/grub.cfg'; config.parent.mkdir(parents=True)
            config.write_text('menuentry "Try Kubuntu" {\n linux /casper/vmlinuz ---\n}\n')
            brand_boot_tree(root)
            self.assertIn('Try Sentinel Linux', config.read_text())
            with self.assertRaises(ValueError):
                brand_boot_tree(root.parent)


if __name__ == '__main__':
    unittest.main()
