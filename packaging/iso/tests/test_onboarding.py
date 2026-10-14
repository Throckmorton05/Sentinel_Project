"""Fresh image defaults, boot preservation, branding, and localized desktop seeding."""
import configparser
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

ISO = Path(__file__).resolve().parents[1]
PROJECT = ISO.parents[1]
sys.path.insert(0, str(ISO))
import desktop_defaults
import first_boot
import installer_branding


class FreshInstallTests(unittest.TestCase):
    def test_every_registered_protection_is_explicitly_off(self):
        from sentinel.daemon.operations.settings_ops import _registered_values
        settings = tomllib.loads((ISO / 'fresh-install-settings.toml').read_text())
        values = _registered_values(settings)
        for key, value in values.items():
            with self.subTest(setting=key):
                self.assertIs(value, False)
                current = settings
                for name in key.split('.'):
                    current = current[name]  # No fallback default can re-enable it.
                self.assertIs(current, False)

    def test_boot_checks_respect_saved_image_choice_and_do_not_reset_settings(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                def read(path):
                    if str(path).endswith('iso-installed.json'):
                        return json.dumps({'protected_user': 'fixture', 'ready': False})
                    self.assertEqual(str(path), '/etc/sentinel/settings.toml')
                    return 'image_filtering = ' + str(enabled).lower()
                with patch.object(first_boot.os, 'geteuid', return_value=0), \
                     patch.object(first_boot.Path, 'read_text', read), \
                     patch.object(first_boot, 'run') as run, \
                     patch.object(first_boot, 'model_health') as health, \
                     patch.object(first_boot, 'write') as write:
                    first_boot.main()
                writes = [str(call.args[0]) for call in write.call_args_list]
                self.assertEqual(writes, ['/etc/sentinel/iso-installed.json'])
                starts = [call for call in run.call_args_list if call.args[0] == 'systemctl']
                self.assertEqual(len(starts), int(enabled))
                self.assertEqual(health.call_count, int(enabled))


class BrandingTests(unittest.TestCase):
    def test_brand_assets_are_complete_and_safe_yaml(self):
        import yaml
        root = PROJECT / 'assets/Installer'
        branding = yaml.safe_load((root / 'branding.desc').read_text())
        self.assertEqual(branding['componentName'], 'sentinel')
        self.assertEqual(branding['strings']['bootloaderEntryName'], 'Sentinel Linux')
        self.assertEqual(branding['slideshowAPI'], 2)
        for name in [*branding['images'].values(), branding['slideshow'], 'stylesheet.qss']:
            self.assertTrue((root / name).is_file())

    def test_launcher_preserves_elevation_and_arguments(self):
        source = ('[Desktop Entry]\nType=Application\nName=Install Kubuntu\n'
                  'Name[es]=Instalar Kubuntu\nExec=pkexec /usr/bin/calamares -D 6\n'
                  'Icon=kubuntu\nTerminal=false\n')
        text = installer_branding.branded_launcher(source)
        config = configparser.ConfigParser(interpolation=None)
        config.read_string(text)
        self.assertEqual(config['Desktop Entry']['Exec'], 'pkexec /usr/bin/calamares -D 6')
        self.assertEqual(config['Desktop Entry']['Name'], 'Install Sentinel Linux')
        self.assertNotIn('Name[es]', text)
        self.assertIsNone(installer_branding.branded_launcher(source.replace('calamares', 'kate')))

    def test_reported_kubuntu_wrapper_and_shell_commands_are_preserved(self):
        for command in ('sh -c "sudo -E calamares -D6"',
                        'sudo /usr/bin/calamares-launch-normal'):
            with self.subTest(command=command):
                source = ('[Desktop Entry]\nType=Application\nName=Install Kubuntu 26.04\n'
                          'Exec=' + command + '\nTerminal=false\n')
                config = configparser.ConfigParser(interpolation=None)
                config.optionxform = str
                config.read_string(installer_branding.branded_launcher(source))
                self.assertEqual(config['Desktop Entry']['Exec'], command)
                self.assertEqual(config['Desktop Entry']['Name'], 'Install Sentinel Linux')

    def test_oem_entry_is_hidden_without_changing_command(self):
        source = ('[Desktop Entry]\nType=Application\nName=Install Kubuntu 26.04 (OEM mode)\n'
                  'Exec=/usr/bin/calamares-launch-oem\n')
        config = configparser.ConfigParser(interpolation=None)
        config.read_string(installer_branding.branded_launcher(source))
        self.assertEqual(config['Desktop Entry']['Exec'], '/usr/bin/calamares-launch-oem')
        self.assertEqual(config['Desktop Entry']['Hidden'], 'true')
        self.assertEqual(config['Desktop Entry']['NoDisplay'], 'true')

    def test_actual_launcher_files_and_artwork_are_installed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            applications = root / 'usr/share/applications'
            applications.mkdir(parents=True)
            commands = {'calamares.desktop': 'sh -c "sudo -E calamares -D6"',
                        'kubuntu-calamares.desktop': 'sudo /usr/bin/calamares-launch-normal',
                        'calamares-launch-oem.desktop': '/usr/bin/calamares-launch-oem'}
            for name, command in commands.items():
                (applications / name).write_text('[Desktop Entry]\nType=Application\n'
                    'Name=Install Kubuntu\nExec=' + command + '\n')
            installer_branding.install_branding(PROJECT, root)
            for name, command in commands.items():
                config = configparser.ConfigParser(interpolation=None)
                config.read(applications / name)
                self.assertEqual(config['Desktop Entry']['Exec'], command)
                if 'oem' in name:
                    self.assertEqual(config['Desktop Entry']['Hidden'], 'true')
                else:
                    self.assertEqual(config['Desktop Entry']['Name'], 'Install Sentinel Linux')
            self.assertTrue((root / 'usr/share/calamares/branding/sentinel/show.qml').is_file())


class GuideShortcutTests(unittest.TestCase):
    def test_localized_desktop_shortcut_is_executable_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            folder = home / 'Escritorio'
            def copy(source, target, mode):
                folder.mkdir()
                target.write_text('fixed packaged guide launcher')
                target.chmod(mode)
            with patch.object(desktop_defaults.Path, 'home', return_value=home), \
                 patch.object(desktop_defaults, 'run'), \
                 patch.object(desktop_defaults.subprocess, 'check_output', return_value=str(folder) + '\n'), \
                 patch.object(desktop_defaults, 'atomic_copy', side_effect=copy) as copier:
                desktop_defaults.seed_guide()
                target = folder / 'sentinel-getting-started.desktop'
                self.assertEqual(target.stat().st_mode & 0o777, 0o755)
                target.write_text('user custom shortcut')
                desktop_defaults.seed_guide()
                self.assertEqual(target.read_text(), 'user custom shortcut')
                copier.assert_called_once()

    def test_desktop_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            folder = home / 'Desktop'
            folder.mkdir()
            (folder / 'sentinel-getting-started.desktop').symlink_to(home / 'missing')
            with patch.object(desktop_defaults.Path, 'home', return_value=home), \
                 patch.object(desktop_defaults, 'run'), \
                 patch.object(desktop_defaults.subprocess, 'check_output', return_value=str(folder)), \
                 patch.object(desktop_defaults, 'atomic_copy') as copier:
                desktop_defaults.seed_guide()
            copier.assert_not_called()


if __name__ == '__main__':
    unittest.main()
