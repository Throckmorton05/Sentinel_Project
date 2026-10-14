"""Install Sentinel's Calamares branding without changing installer commands."""
from __future__ import annotations

import configparser
from io import StringIO
from pathlib import Path
import shlex
import shutil

from common import plain_file, write

FILES = ('branding.desc', 'show.qml', 'stylesheet.qss', 'logo.svg', 'welcome.svg')


def installer_kind(command):
    """Recognize Kubuntu's direct, shell-wrapped, and normal/OEM launchers."""
    argv = shlex.split(command)
    if len(argv) >= 3 and Path(argv[0]).name in {'sh', 'bash'} and argv[1] == '-c':
        argv = shlex.split(argv[2])  # Parse for identification only; never execute.
    names = {Path(arg).name for arg in argv}
    if 'calamares-launch-oem' in names:
        return 'oem'
    if names & {'calamares', 'calamares-launch-normal'}:
        return 'normal'
    return None


def branded_launcher(source):
    config = configparser.ConfigParser(interpolation=None, strict=False)
    config.optionxform = str
    config.read_string(source)
    entry = config['Desktop Entry']
    # Keep Kubuntu's checked elevation wrapper and installer flags verbatim.
    kind = installer_kind(entry.get('Exec', ''))
    if kind is None:
        return None
    if kind == 'oem':
        # The guarded Sentinel image supports only the normal installer.
        entry.update(Hidden='true', NoDisplay='true')
    else:
        for key in list(entry):
            if key.startswith(('Name[', 'GenericName[', 'Comment[')):
                del entry[key]
        entry.update(Name='Install Sentinel Linux', GenericName='System Installer',
                     Comment='Install Sentinel Linux 0.1.0 Beta',
                     Icon='/usr/share/calamares/branding/sentinel/logo.svg')
    result = StringIO()
    config.write(result, space_around_delimiters=False)
    return result.getvalue()


def install_branding(kit, root=Path('/')):
    root = Path(root)
    target = root / 'usr/share/calamares/branding/sentinel'
    target.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        source = plain_file(Path(kit) / 'assets/Installer' / name)
        shutil.copyfile(source, target / name)
        (target / name).chmod(0o644)
    count = 0
    # The desktop file's existing Exec/permissions remain the live-image entry
    # point. Do not invent a new pkexec/sudo invocation for the installer.
    for source in (root / 'usr/share/applications').glob('*.desktop'):
        try:
            content = branded_launcher(source.read_text())
        except (configparser.Error, KeyError, ValueError, UnicodeError):
            continue
        if content is not None:
            write(source, content)
            count += 1
    if not count:
        print('Sentinel branding installed; review the live installer shortcut name in Cubic.')
    else:
        print(f'Sentinel installer entries configured: {count}. Unsupported OEM entries are hidden.')
