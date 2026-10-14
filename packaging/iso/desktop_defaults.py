#!/usr/bin/python3
"""Seed editable desktop defaults during installation, or preview them as a user."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import tempfile

APPLICATIONS = (
    'org.kde.konsole.desktop', 'sentinel-appstore.desktop', 'org.kde.dolphin.desktop',
    'systemsettings.desktop', 'sentinel-portal.desktop', 'google-chrome.desktop',
)


def launcher_entry(icon='/usr/share/sentinel/assets/Sentinel_Shield_Minimal_Solid-Visor.svg'):
    return ('[Desktop Entry]\nType=Application\nName=Sentinel Launcher\n'
            'Exec=sentinel-launcher\nIcon=' + icon + '\nCategories=Utility;\n'
            'Terminal=false\nStartupNotify=false\nX-KDE-Shortcuts=Ctrl+Space\n'
            'X-KDE-GlobalAccel-CommandShortcut=true\n')


def run(*args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def atomic_copy(source, destination, mode=0o644):
    """Write a normal, editable user file without following a destination symlink."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix='.sentinel-')
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(source.read_bytes())
            os.fchmod(output.fileno(), mode)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def apply(assets, initial_install=False):
    if os.geteuid() == 0:
        raise ValueError('Run as the desktop user, without sudo.')
    if not initial_install and not Path('/etc/sentinel/iso-installed.json').is_file():
        raise ValueError('Preview these settings inside the installed Sentinel VM, not on the development host.')
    config = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    data = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    state = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state')
    copies = {
        config / 'rofi/config.rasi': assets / 'Rofi/sentinel-rofi.rasi',
        data / 'konsole/Sentinel.profile': assets / 'Terminal/Konsole/Sentinel.profile',
        data / 'konsole/Sentinel.colorscheme': assets / 'Terminal/Konsole/Sentinel.colorscheme',
    }
    for source in copies.values():
        if not source.is_file():
            raise ValueError(f'Missing desktop asset: {source}')
    if not shutil.which('kwriteconfig6'):
        raise ValueError('KDE kwriteconfig6 is required.')
    modified = [*copies, config / 'konsolerc', config / 'kwinrc',
                config / 'kglobalshortcutsrc', config / 'kicker-extra-favoritesrc',
                data / 'applications/sentinel-launcher.desktop']
    if not initial_install:
        backup = state / 'sentinel/desktop-backups' / datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
        backup.mkdir(parents=True, mode=0o700)
        mapping = {}
        for number, path in enumerate(modified):
            if path.is_file():
                saved = backup / str(number)
                shutil.copyfile(path, saved)
                saved.chmod(0o600)
                mapping[str(path)] = str(saved)
        (backup / 'files.json').write_text(json.dumps(mapping, indent=2) + '\n')
        print(f'Previous user configuration saved in {backup}', flush=True)

    config.mkdir(parents=True, exist_ok=True)
    # Stop only the user's shortcut service so its cached Meta+Space setting
    # cannot overwrite the new config. No system services or monitors are stopped.
    shortcut_service = 'plasma-kglobalaccel.service'
    restart_shortcuts = False
    if not initial_install:
        status = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', shortcut_service],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if status.returncode == 0:
            run('systemctl', '--user', 'stop', shortcut_service)
            restart_shortcuts = True

    def entry(filename, groups, key, value=None):
        command = ['kwriteconfig6', '--file', config / filename]
        for group in groups:
            command.extend(['--group', group])
        command.extend(['--key', key])
        command.extend(['--delete'] if value is None else [value])
        run(*command)

    try:
        for destination, source in copies.items():
            atomic_copy(source, destination)
        launcher = data / 'applications/sentinel-launcher.desktop'
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text(launcher_entry())
        launcher.chmod(0o644)
        entry('konsolerc', ['Desktop Entry'], 'DefaultProfile', 'Sentinel.profile')
        entry('kwinrc', ['Plugins'], 'blurEnabled', 'true')
        entry('kglobalshortcutsrc', ['sentinel-launcher.desktop'], '_launch')
        entry('kglobalshortcutsrc', ['sentinel-launcher.desktop'], '_k_friendly_name')
        entry('kglobalshortcutsrc', ['services', 'sentinel-launcher.desktop'], '_launch', 'Ctrl+Space')
        entry('kicker-extra-favoritesrc', ['General'], 'Prepend', ';'.join(APPLICATIONS))
        entry('kicker-extra-favoritesrc', ['General'], 'IgnoreDefaults', 'true')
        if initial_install:
            seed_guide()
        if not initial_install:
            run('kbuildsycoca6', '--noincremental')
    finally:
        if restart_shortcuts:
            run('systemctl', '--user', 'start', shortcut_service)

    if not initial_install:
        # Use the running shell's API; editing appletsrc directly can be overwritten.
        script = '''var count = 0;
var allPanels = panels();
for (var p = 0; p < allPanels.length; ++p) {
    var widgets = allPanels[p].widgets();
    for (var w = 0; w < widgets.length; ++w) {
        if (widgets[w].type === "org.kde.plasma.icontasks" || widgets[w].type === "org.kde.plasma.taskmanager") {
            widgets[w].currentConfigGroup = ["General"];
            widgets[w].writeConfig("launchers", LAUNCHERS);
            ++count;
        }
    }
}
if (!count) throw new Error("No task manager found; pin the applications manually.");
'''.replace('LAUNCHERS', json.dumps(['applications:' + app for app in APPLICATIONS]))
        qdbus = shutil.which('qdbus6') or '/usr/lib/qt6/bin/qdbus'
        failures = []
        for service, path, method, argument in (
            ('org.kde.KWin', '/KWin', 'org.kde.KWin.reconfigure', None),
            ('org.kde.plasmashell', '/PlasmaShell', 'org.kde.PlasmaShell.evaluateScript', script),
        ):
            try:
                args = [qdbus, service, path, method]
                if argument is not None:
                    args.append(argument)
                run(*args)
            except (OSError, subprocess.CalledProcessError) as exc:
                failures.append(f'{service}: {exc}')
        print('Editable Rofi and Konsole defaults, Ctrl+Space, and blur configured.')
        print('Close all Konsole windows, then reopen Konsole to load the new profile.')
        print('Kickoff favorites are seeded for first use; existing menu favorites are preserved.')
        if failures:
            raise ValueError('Some live changes failed; user config was saved. ' + '; '.join(failures))
        print('The six requested applications are pinned to the taskbar.')


def seed_guide():
    """Seed one removable shortcut in the account's localized desktop folder."""
    # The installer may still be running with its live-session locale. Use the
    # target's selected locale before XDG initializes its first desktop path.
    environment = dict(os.environ)
    locale_file = Path('/etc/default/locale')
    if locale_file.is_file():
        for line in locale_file.read_text().splitlines():
            name, separator, value = line.partition('=')
            if separator and name in {'LANG', 'LANGUAGE', 'LC_ALL', 'LC_MESSAGES'}:
                tokens = shlex.split(value, comments=True)
                if len(tokens) == 1:
                    environment[name] = tokens[0]
    run('xdg-user-dirs-update', env=environment)
    folder = Path(subprocess.check_output(['xdg-user-dir', 'DESKTOP'], text=True,
                                         env=environment).strip())
    home = Path.home().resolve()
    if not folder.is_absolute() or not folder.resolve().is_relative_to(home) or folder.resolve() == home:
        raise ValueError('Expected a desktop folder inside the new user home.')
    target = folder / 'sentinel-getting-started.desktop'
    if target.exists() or target.is_symlink():
        return  # Never overwrite an existing shortcut or follow its symlink.
    atomic_copy(Path('/usr/share/applications/sentinel-getting-started.desktop'), target, 0o755)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets', type=Path, default=Path('/usr/share/sentinel/desktop'))
    parser.add_argument('--initial-install', action='store_true', help='Seed an offline new account; do not contact its session.')
    args = parser.parse_args()
    try:
        apply(args.assets, args.initial_install)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f'Sentinel desktop defaults stopped: {exc}') from exc
