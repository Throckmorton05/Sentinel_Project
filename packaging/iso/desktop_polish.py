#!/usr/bin/python3
"""Apply only theme-row/profile cleanup to an existing user's Sentinel desktop."""
from __future__ import annotations

import configparser
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil

from desktop_defaults import atomic_copy


def profile_settings(path):
    if path.is_symlink() or not path.is_file():
        return None
    document = configparser.ConfigParser(interpolation=None, strict=False)
    document.optionxform = str
    try:
        document.read(path)
    except (configparser.Error, UnicodeError):
        return None
    if document.get('General', 'Name', fallback='').lower() != 'sentinel':
        return None
    return tuple((section, tuple(sorted(document[section].items())))
                 for section in sorted(document.sections()))


def changes(config, data):
    """Plan changes without reseeding wallpapers, palettes, shortcuts or profiles."""
    edits, deletions = {}, []
    fetch = config / 'fastfetch/config.jsonc'
    if fetch.is_file() and not fetch.is_symlink():
        original = fetch.read_text()
        updated = re.sub(r'^\s*"theme"\s*,?\s*(?://[^\n]*)?\n', '', original, flags=re.MULTILINE)
        if updated != original:
            edits[fetch] = updated
    konsolerc = configparser.ConfigParser(interpolation=None, strict=False)
    konsolerc.read(config / 'konsolerc')
    preferred = Path(konsolerc.get('Desktop Entry', 'DefaultProfile', fallback='Sentinel.profile')).name
    profiles = sorted((data / 'konsole').glob('*.profile'), key=lambda p: (p.name != preferred, p.name != 'Sentinel.profile', p.name))
    seen = set()
    for path in profiles:
        settings = profile_settings(path)
        if settings is None:
            continue
        if settings in seen:
            deletions.append(path)
        else:
            seen.add(settings)
    return edits, deletions


def main():
    if os.geteuid() == 0 or not Path('/etc/sentinel/iso-installed.json').is_file():
        raise SystemExit('Run as the normal desktop user inside the installed Sentinel VM, without sudo.')
    home = Path.home()
    config = Path(os.environ.get('XDG_CONFIG_HOME') or home / '.config')
    data = Path(os.environ.get('XDG_DATA_HOME') or home / '.local/share')
    state = Path(os.environ.get('XDG_STATE_HOME') or home / '.local/state')
    edits, deletions = changes(config, data)
    if not edits and not deletions:
        print('No duplicate user profiles or Fastfetch theme row found.')
        return
    backup = state / 'sentinel/appearance-backups' / datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    backup.mkdir(parents=True, mode=0o700)
    mapping = {}
    for number, path in enumerate([*edits, *deletions]):
        target = backup / str(number)
        shutil.copyfile(path, target); target.chmod(0o600)
        mapping[str(path)] = str(target)
    (backup / 'files.json').write_text(json.dumps(mapping, indent=2) + '\n')
    for path, content in edits.items():
        temporary = backup / 'edited-config'
        temporary.write_text(content)
        atomic_copy(temporary, path)
        temporary.unlink()
    for path in deletions:
        path.unlink()
    print('Terminal appearance cleaned up. Close and reopen Konsole.')
    print('Previous files saved in ' + str(backup))


if __name__ == '__main__':
    main()
