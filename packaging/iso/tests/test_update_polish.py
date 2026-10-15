"""Cleanup never removes the desktop; live branding preserves installer elevation."""
import configparser
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ISO = Path(__file__).resolve().parents[1]
PROJECT = ISO.parents[1]
sys.path.insert(0, str(ISO))
import desktop_polish
import image_cleanup
import live_branding
import platform_postinst


class ImageCleanupTests(unittest.TestCase):
    def test_cleanup_covers_games_discover_variants_and_wallpapers(self):
        names = ['kmahjongg', 'kmines', 'kpat', 'ksudoku', 'plasma-discover-common',
                 'plasma-discover-backend-fwupd', 'kubuntu-wallpapers', 'breeze-wallpaper',
                 'plasma-workspace-wallpapers', 'plasma-workspace', 'sddm', 'kate']
        inventory = ''.join(f'{name}\tinstalled\n' for name in names)
        selected = image_cleanup.removal_candidates(inventory)
        self.assertEqual(set(selected), set(names[:9]))

    def test_unexpected_desktop_removal_aborts_before_real_purge(self):
        for simulation in ('Remv plasma-workspace [1.0]\n', 'Purg sddm [1.0]\n'):
            with self.subTest(simulation=simulation), patch.object(image_cleanup.subprocess,
                    'check_output', return_value=simulation), patch.object(image_cleanup, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'cleanup would also remove'):
                    image_cleanup.purge_stock_packages('kmines\tinstalled\n')
                run.assert_not_called()

    def test_meta_package_removal_is_permitted_without_autoremove(self):
        with patch.object(image_cleanup.subprocess, 'check_output', return_value='Remv kubuntu-desktop [1]\nPurg kmines [1]\n'), \
             patch.object(image_cleanup, 'run') as run:
            image_cleanup.purge_stock_packages('kmines\tinstalled\n')
        run.assert_called_once_with('apt-get', 'purge', '-y', 'kmines')

    def test_only_known_duplicate_system_profile_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / 'usr/share/sentinel/desktop/Terminal/Konsole/Sentinel.profile'
            reference.parent.mkdir(parents=True)
            reference.write_bytes((PROJECT / 'assets/Terminal/Konsole/Sentinel.profile').read_bytes())
            system = root / 'usr/share/konsole/Sentinel.profile'; system.parent.mkdir(parents=True)
            system.write_bytes(reference.read_bytes())
            self.assertTrue(image_cleanup.remove_duplicate_profile(root))
            system.write_text('[General]\nName=Custom\n')
            self.assertFalse(image_cleanup.remove_duplicate_profile(root))
            self.assertTrue(system.exists())


class LiveBrandingTests(unittest.TestCase):
    def test_live_wrapper_and_installer_command_are_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / 'usr/libexec/start-kubuntu-live-env'; session.parent.mkdir(parents=True)
            original = ('#!/bin/bash\nkwin_wayland --xwayland --no-lockscreen kubuntu-installer-prompt;\n'
                        '# Portal fixes remain here\nstartplasma-wayland\n')
            session.write_text(original)
            launcher = root / 'usr/share/applications/kubuntu-calamares.desktop'
            launcher.parent.mkdir(parents=True)
            command = 'sudo /usr/bin/calamares-launch-normal'
            launcher.write_text('[Desktop Entry]\nExec=' + command + '\n')
            live_branding.install_live_branding(PROJECT, root)
            self.assertNotIn('kwin_wayland --xwayland', session.read_text())
            self.assertIn('# Portal fixes remain here\nstartplasma-wayland', session.read_text())
            self.assertIn('Exec=/usr/bin/sentinel-live-welcome',
                (root / 'etc/xdg/autostart/sentinel-live-welcome.desktop').read_text())
            import json
            config = json.loads((root / 'usr/lib/sentinel-iso/live-welcome.json').read_text())
            self.assertEqual(config['installer_exec'], command)
            live_branding.install_live_branding(PROJECT, root)  # Rebuild is idempotent.
            self.assertEqual((root / 'usr/bin/sentinel-live-welcome').stat().st_mode & 0o777, 0o755)

    def test_changed_live_wrapper_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / 'usr/libexec/start-kubuntu-live-env'; session.parent.mkdir(parents=True)
            session.write_text('custom startup\n')
            with self.assertRaisesRegex(ValueError, 'wrapper changed'):
                live_branding.install_live_branding(PROJECT, root)
            self.assertEqual(session.read_text(), 'custom startup\n')


class DesktopPolishTests(unittest.TestCase):
    def test_default_profile_is_kept_and_custom_profile_settings_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config = root / 'config'; data = root / 'data'
            (config / 'fastfetch').mkdir(parents=True); (data / 'konsole').mkdir(parents=True)
            fetch = config / 'fastfetch/config.jsonc'
            fetch.write_text('{\n  "modules": [\n    "os",\n    "theme",\n    "icons"\n  ]\n}\n')
            (config / 'konsolerc').write_text('[Desktop Entry]\nDefaultProfile=Second.profile\n')
            profile = '[General]\nName=Sentinel\n[Appearance]\nFont=Example,11\n'
            for name in ('Sentinel.profile', 'Second.profile'):
                (data / 'konsole' / name).write_text(profile)
            custom = data / 'konsole/Custom.profile'; custom.write_text(profile.replace('Example,11', 'Example,14'))
            edits, deletions = desktop_polish.changes(config, data)
            self.assertEqual(deletions, [data / 'konsole/Sentinel.profile'])
            self.assertNotIn('"theme"', edits[fetch])
            self.assertIn('"icons"', edits[fetch])
            self.assertTrue(custom.exists())

    def test_splash_layout_has_gap_at_common_logical_resolutions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stock = root / 'usr/share/plymouth/themes/spinner'; stock.mkdir(parents=True)
            (stock / 'spinner.plymouth').write_text('[Plymouth Theme]\nName=Spinner\nModuleName=two-step\n'
                '[two-step]\nImageDir=/stock\nDialogVerticalAlignment=.382\n'
                '[boot-up]\nUseEndAnimation=false\n')
            (stock / 'lock.png').write_bytes(b'preserved disk password asset')
            with patch.object(platform_postinst, 'protected', side_effect=lambda p: Path(p)), \
                 patch.object(platform_postinst.subprocess, 'run'):
                platform_postinst.configure_splash(root)
            theme = root / 'usr/share/plymouth/themes/sentinel'
            self.assertEqual((theme / 'lock.png').read_bytes(), b'preserved disk password asset')
            document = configparser.ConfigParser(); document.read(theme / 'sentinel.plymouth')
            self.assertEqual(document['Plymouth Theme']['ModuleName'], 'two-step')
            import struct
            def png_height(path):
                return struct.unpack('>I', path.read_bytes()[20:24])[0]
            mark_height = png_height(PROJECT / 'assets/Boot/watermark.png')
            spinner_height = png_height(PROJECT / 'assets/Boot/throbber-0001.png')
            for height in (480, 600, 768, 1080, 1600):
                mark_bottom = (height - mark_height) * document.getfloat('two-step', 'WatermarkVerticalAlignment') + mark_height
                spinner_top = height * document.getfloat('two-step', 'VerticalAlignment') - spinner_height / 2
                self.assertGreater(spinner_top - mark_bottom, 0)
