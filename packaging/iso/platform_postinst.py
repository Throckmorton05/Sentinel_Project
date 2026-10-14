#!/usr/bin/python3
"""Refresh package-owned platform files without rerunning account provisioning."""
from __future__ import annotations
import json
import hashlib
import configparser
from io import StringIO
import os
from pathlib import Path
import stat
import subprocess
import sys
import shutil

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import write
from repository_config import verify_configuration
from image_cleanup import DISCOVER_PACKAGES, remove_duplicate_profile

UNITS = ('sentinel-first-boot.service', 'sentinel-dns.service',
         'sentinel-dns-firewall.service', 'sentinel-session-guard.service')


def protected(path):
    path = Path(path)
    for parent in (path, *path.parents):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('Refusing unprotected platform path: ' + str(parent))
    if not path.is_file():
        raise ValueError('Expected a regular file: ' + str(path))
    return path


def repository(directory):
    directory = Path(directory)
    for name in ('repository.json', 'sentinel.sources', 'sentinel.pref', 'sentinel-archive-keyring.gpg'):
        protected(directory / name)
    metadata = verify_configuration(directory)
    for source, target in (
        ('sentinel.sources', '/etc/apt/sources.list.d/sentinel.sources'),
        ('sentinel.pref', '/etc/apt/preferences.d/sentinel'),
    ):
        write(target, (directory / source).read_text())
    key = directory / 'sentinel-archive-keyring.gpg'
    destination = Path('/usr/share/keyrings/sentinel-archive-keyring.gpg')
    if destination.is_symlink():
        raise ValueError('Refusing a symlinked repository key.')
    if destination.exists():
        protected(destination)
    # On normal upgrades dpkg already replaced this public key file.
    if not destination.exists() or destination.read_bytes() != key.read_bytes():
        destination.parent.mkdir(parents=True, exist_ok=True)
        import tempfile
        fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix='.sentinel-key-')
        try:
            with os.fdopen(fd, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o644)
                stream.write(key.read_bytes())
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return metadata


def update_policy(path=Path('/etc/sentinel/iso-package-policy.json'),
                  inputs=Path('/usr/lib/sentinel-iso/package-policy-input.json')):
    policy = json.loads(protected(path).read_text())
    additions = json.loads(protected(inputs).read_text())
    for field, values in (('allowed_install', additions['approved_apt']),
                          ('blocked_install', [*additions['blocked_browsers'], *DISCOVER_PACKAGES]),
                          ('protected', ['sentinel-platform'])):
        policy[field] = sorted(set(policy[field]) | set(values))
    write(path, json.dumps(policy, indent=2) + '\n')


def protected_legacy_parent(path):
    # A legacy file's metadata may be wrong, but its directory boundary must
    # still be root-owned, non-writable to others, and free of symlinks.
    for parent in Path(path).parents:
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022):
            raise ValueError('Refusing unprotected platform path: ' + str(parent))


def remove_known_legacy(path, expected):
    """Remove exact old package bytes, including its root:root 0664 copies."""
    path = Path(path)
    if path.is_symlink():
        return False  # Preserve custom overrides; never follow their targets.
    protected_legacy_parent(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o002
                or (info.st_mode & 0o020 and info.st_gid != 0)):
            raise ValueError('Refusing unprotected platform path: ' + str(path))
        if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
            protected(path)  # Custom files still need the ordinary strict guard.
            return False
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError('Legacy file changed during migration: ' + str(path))
        path.unlink()
        return True


def migrate_units():
    legacy = json.loads(protected('/usr/lib/sentinel-iso/legacy-units.json').read_text())
    provenance = Path('/usr/lib/sentinel-iso/input-manifest.json')
    if provenance.is_file():
        manifest = json.loads(protected(provenance).read_text())['sha256']
        for unit in UNITS:
            key = 'packaging/iso/systemd/' + unit
            if key in manifest:
                legacy['/etc/systemd/system/' + unit] = manifest[key]
    retained, removed_units = [], False
    for name, expected in legacy.items():
        path = Path(name)
        if not path.exists() and not path.is_symlink():
            continue
        if remove_known_legacy(path, expected):
            removed_units |= path.name in UNITS
        else:
            retained.append(name)
    if retained:
        print('Custom service overrides retained; review before reboot: ' + ', '.join(retained))
    vendors = json.loads(protected('/usr/lib/sentinel-iso/legacy-vendor.json').read_text())
    for name, expected in vendors.items():
        path = Path(name)
        if path.is_file() and not path.is_symlink():
            remove_known_legacy(path, expected)  # Packaged /usr/bin/starship takes precedence.
    return removed_units


def main():
    if os.geteuid() != 0:
        raise ValueError('Platform configuration requires root maintainer context.')
    repository('/usr/share/sentinel/updates')
    removed_units = migrate_units()
    configure_splash()
    remove_duplicate_profile()
    installed = Path('/etc/sentinel/iso-installed.json').exists()
    if installed:
        update_policy()
        # Never call provision(), create_credentials(), or desktop_defaults.apply().
        # Changed binaries/unit restrictions take effect at reboot; no DNS gap in postinst.
        write('/run/reboot-required', 'Sentinel platform update requires a reboot.\n')
        reboot_packages = Path('/run/reboot-required.pkgs')
        existing = reboot_packages.read_text().splitlines() if reboot_packages.exists() else []
        write(reboot_packages, '\n'.join(sorted(set(existing) | {'sentinel-platform'})) + '\n')
    if installed or removed_units:
        # Earlier images enabled units using links to unmanaged /etc copies.
        # Repair those links in installed systems AND partially customized
        # Cubic images. reenable does not start/stop running services.
        subprocess.run(['systemctl', 'reenable', *UNITS], check=True)
    if Path('/run/systemd/system').is_dir():
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['fc-cache', '-f'], check=True)
    print('Sentinel platform configured. Saved policy, credentials and user appearance files retained.')


def configure_splash(root=Path('/')):
    """Use the distro's two-step plugin, including its encrypted-disk prompts."""
    stock = Path(root) / 'usr/share/plymouth/themes/spinner'
    theme = Path(root) / 'usr/share/plymouth/themes/sentinel'
    document = configparser.ConfigParser(interpolation=None)
    document.optionxform = str
    document.read(protected(stock / 'spinner.plymouth'))
    if document.get('Plymouth Theme', 'ModuleName') != 'two-step':
        raise ValueError('Expected the packaged Plymouth two-step spinner theme.')
    document['Plymouth Theme']['Name'] = 'Sentinel'
    document['Plymouth Theme']['Description'] = 'Sentinel startup splash'
    document['two-step']['ImageDir'] = str(theme)
    document['two-step']['BackgroundStartColor'] = '0x0b0b0b'
    document['two-step']['BackgroundEndColor'] = '0x0b0b0b'
    document['two-step']['WatermarkHorizontalAlignment'] = '.5'
    document['two-step']['WatermarkVerticalAlignment'] = '.40'
    # The watermark uses (screen_height - image_height) * alignment;
    # the spinner uses screen_height * alignment for its CENTER. Explicit
    # independent positions leave a gap even at 640x480 logical resolution.
    document['two-step']['HorizontalAlignment'] = '.5'
    document['two-step']['VerticalAlignment'] = '.70'
    document['two-step']['DialogVerticalAlignment'] = '.70'
    document['two-step']['MessageBelowAnimation'] = 'true'
    # Preserve distro assets for password, caps-lock and keyboard UI. Our
    # package supplies its own watermark and animation frames at this path.
    theme.mkdir(parents=True, exist_ok=True)
    for source in stock.glob('*.png'):
        destination = theme / source.name
        if not destination.exists() and not destination.is_symlink() and not source.is_symlink():
            shutil.copyfile(protected(source), destination)
    text = StringIO(); document.write(text, space_around_delimiters=False)
    selected = theme / 'sentinel.plymouth'
    write(selected, text.getvalue())
    subprocess.run(['update-alternatives', '--install', '/usr/share/plymouth/themes/default.plymouth',
                    'default.plymouth', str(selected), '200'], check=True)
    subprocess.run(['update-alternatives', '--set', 'default.plymouth', str(selected)], check=True)
    subprocess.run(['update-initramfs', '-u', '-k', 'all'], check=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        raise SystemExit(f'Sentinel platform configuration failed: {exc}') from exc
