"""Start Plasma normally, then show the live-only welcome as an autostart app."""
from __future__ import annotations

import configparser
import json
from pathlib import Path

from common import plain_file, write
from installer_branding import installer_kind


def install_live_branding(kit, root=Path('/')):
    root = Path(root)
    session = root / 'usr/libexec/start-kubuntu-live-env'
    original = plain_file(session).read_text()
    command = 'kwin_wayland --xwayland --no-lockscreen kubuntu-installer-prompt;'
    legacy = 'kwin_wayland --xwayland --no-lockscreen /usr/bin/sentinel-live-welcome;'
    replacement = ':; # Sentinel welcome runs after Plasma starts.\n'
    if sum(original.count(item) for item in (command, legacy, replacement)) != 1:
        raise ValueError('The Kubuntu live-session wrapper changed; review welcome integration.')
    if 'startplasma-wayland' not in original:
        raise ValueError('The live-session wrapper no longer starts Plasma; review it before building.')
    launcher = None
    for name in ('kubuntu-calamares.desktop', 'calamares.desktop'):
        path = root / 'usr/share/applications' / name
        if not path.is_file():
            continue
        config = configparser.ConfigParser(interpolation=None, strict=False)
        config.read(path)
        candidate = config['Desktop Entry'].get('Exec', '')
        if installer_kind(candidate) == 'normal':
            launcher = candidate
            break
    if not launcher:
        raise ValueError('No checked normal installer launcher is available for the live welcome.')
    destination = root / 'usr/lib/sentinel-iso/live_welcome.py'
    write(destination, plain_file(Path(kit) / 'packaging/iso/live_welcome.py').read_text())
    write(root / 'usr/lib/sentinel-iso/live-welcome.json', json.dumps({'installer_exec': launcher}) + '\n')
    write(root / 'usr/bin/sentinel-live-welcome', '#!/bin/sh\n'
          'exec /usr/lib/sentinel/venv/bin/python -I -B /usr/lib/sentinel-iso/live_welcome.py "$@"\n', 0o755)
    # Do not create a temporary compositor whose lifetime depends on the prompt.
    # Retain the wrapper's OEM handling, portal repair and Plasma invocation.
    write(session, original.replace(command, replacement).replace(legacy, replacement), 0o755)
    write(root / 'etc/xdg/autostart/sentinel-live-welcome.desktop',
          '[Desktop Entry]\nType=Application\nName=Sentinel Live Welcome\n'
          'Exec=/usr/bin/sentinel-live-welcome\nOnlyShowIn=KDE;\n'
          'X-KDE-autostart-phase=2\n')
    entry = root / 'usr/share/wayland-sessions/kubuntu-live-environment.desktop'
    if entry.is_file():
        config = configparser.ConfigParser(interpolation=None, strict=False)
        config.optionxform = str
        config.read(entry)
        for key in list(config['Desktop Entry']):
            if key.startswith(('Name[', 'Comment[')):
                del config['Desktop Entry'][key]
        config['Desktop Entry'].update(Name='Sentinel Live Environment',
                                      Comment='Try or install Sentinel Linux')
        from io import StringIO
        content = StringIO(); config.write(content, space_around_delimiters=False)
        write(entry, content.getvalue())
