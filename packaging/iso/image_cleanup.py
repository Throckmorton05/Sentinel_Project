"""Remove selected stock software and redundant Sentinel profile copies."""
from __future__ import annotations

from pathlib import Path
import os
import subprocess

from common import run

STOCK_PACKAGES = frozenset({
    'kmahjongg', 'kmines', 'kpat', 'ksudoku',
    'kubuntu-wallpapers', 'plasma-workspace-wallpapers', 'breeze-wallpaper',
})
DISCOVER_PACKAGES = frozenset({
    'plasma-discover', 'plasma-discover-common', 'plasma-discover-notifier',
    'plasma-discover-backend-flatpak', 'plasma-discover-backend-snap',
    'plasma-discover-backend-fwupd',
})


def removal_candidates(inventory):
    result = []
    for line in inventory.splitlines():
        name, separator, status = line.partition('\t')
        base = name.split(':')[0]
        if separator and status == 'installed' and (
                base in STOCK_PACKAGES | {'firefox', 'thunderbird'}
                or base.startswith('plasma-discover')):
            result.append(name)
    return sorted(set(result))


def purge_stock_packages(inventory):
    packages = removal_candidates(inventory)
    if not packages:
        return
    # Purging a recommended game can remove the Kubuntu meta-package, but
    # must never cascade into the actual desktop, installer or protections.
    simulation = subprocess.check_output(
        ['apt-get', '-s', 'purge', *packages], text=True, timeout=120,
        env={**os.environ, 'LC_ALL': 'C'},
    )
    removed = {line.split()[1].split(':')[0] for line in simulation.splitlines()
               if line.startswith(('Remv ', 'Purg '))}
    permitted = {name.split(':')[0] for name in packages} | {'kubuntu-desktop', 'kubuntu-full'}
    unexpected = removed - permitted
    if unexpected:
        raise ValueError('Stock cleanup would also remove required/unreviewed packages: '
                         + ', '.join(sorted(unexpected)))
    run('apt-get', 'purge', '-y', *packages)  # Never autoremove desktop dependencies.


def remove_duplicate_profile(root=Path('/')):
    root = Path(root)
    system = root / 'usr/share/konsole/Sentinel.profile'
    reference = root / 'usr/share/sentinel/desktop/Terminal/Konsole/Sentinel.profile'
    if not system.is_file() or system.is_symlink() or not reference.is_file():
        return False
    expected = reference.read_bytes()
    # These are the shipped 11/12 point defaults. Preserve customized system
    # profiles; the normal editable account profile is the retained default.
    known = {expected, expected.replace(b'Font=JetBrainsMono Nerd Font,11,',
                                       b'Font=JetBrainsMono Nerd Font,12,')}
    if system.read_bytes() not in known:
        return False
    system.unlink()
    return True
