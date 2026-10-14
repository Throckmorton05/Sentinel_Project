"""Shared build validation; importing this module never changes the host."""
from __future__ import annotations

import hashlib
import configparser
from io import StringIO
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

PACKAGES = {'sentinel', 'sentinel-core', 'sentinel-assets', 'sentinel-models', 'sentinel-desktop'}
REQUIRED_ASSETS = (
    'Rofi/sentinel-rofi.rasi', 'Terminal/Starship/starship.toml',
    'Terminal/Fastfetch/config.jsonc', 'Terminal/Fastfetch/Sentinel_Shield.txt',
    'Terminal/Konsole/Sentinel.profile', 'Terminal/Konsole/Sentinel.colorscheme',
    'Sentinel_Shield_Minimal_Solid-Visor.svg',
    'Installer/branding.desc', 'Installer/show.qml', 'Installer/stylesheet.qss',
    'Installer/logo.svg', 'Installer/welcome.svg',
)
WALLPAPERS = {'SentinelGradient': ('Sentinel_SS_Gradients', 'sentinel-dark-gradient'),
              'SentinelWaves': ('Sentinel_SS_Wave', 'sentinel-navy-waves')}
RESOLUTIONS = ('3840x2160', '3840x2400', '5120x2160', '5120x1440')


def kwin_defaults(source, lock_bridge=False):
    config = configparser.ConfigParser(interpolation=None, strict=False)
    config.optionxform = str
    config.read_string(source)
    if not config.has_section('Plugins'):
        config.add_section('Plugins')
    for key, value, immutable in (('wobblywindowsEnabled', 'false', True),
                                  ('blurEnabled', 'true', False),
                                  ('sentinel-window-bridgeEnabled', 'true', lock_bridge)):
        config['Plugins'].pop(key, None)
        config['Plugins'].pop(key + '[$i]', None)
        config['Plugins'][key + ('[$i]' if immutable else '')] = value
    result = StringIO()
    config.write(result, space_around_delimiters=False)
    return result.getvalue()


def run(*args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def plain_file(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not path.stat().st_size:
        raise ValueError(f'Missing, empty or symlinked input: {path}')
    return path


def write(path, text, mode=0o644):
    """Atomic root-owned configuration writes; reject symlinked ancestors."""
    path = Path(path)
    for parent in [path, *path.parents]:
        if parent.is_symlink():
            raise ValueError(f'Refusing symlinked output: {parent}')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.sentinel-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def validate_user(name):
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', name or ''):
        raise ValueError('Expected the normal account name supplied by Calamares.')
    return name


def cubic_namespace_root(mountinfo, pid1, hostname, systemd_running):
    """Recognize Cubic's bind-mounted image with a shell as namespace PID 1.

    The mount-root field comes from the kernel, unlike a marker or environment
    variable. An ordinary host root is '/', even if its hostname is 'cubic'.
    """
    roots = []
    for line in mountinfo.splitlines():
        fields = line.split(' - ', 1)[0].split()
        if len(fields) >= 6 and fields[4] == '/':
            root = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[3])
            roots.append(root)
    if len(roots) != 1:
        return False
    root = Path(roots[0])
    return (root.is_absolute() and root != Path('/') and root.name == 'custom-root'
            and pid1.strip() == 'bash' and hostname == 'cubic' and not systemd_running)


def require_image_environment():
    if os.geteuid() != 0:
        raise ValueError('image must run as root INSIDE Cubic, not with sudo on the host.')
    # Traditional chroots retain the host's PID 1. Cubic can instead create
    # a PID namespace where PID 1 is the image's shell, so that comparison
    # alone cannot distinguish its image from the host.
    try:
        root, init = os.stat('/'), os.stat('/proc/1/root/.')
    except OSError as exc:
        raise ValueError('Cannot establish the chroot boundary; refusing image changes.') from exc
    if (root.st_dev, root.st_ino) == (init.st_dev, init.st_ino):
        try:
            isolated_image = cubic_namespace_root(
                Path('/proc/self/mountinfo').read_text(), Path('/proc/1/comm').read_text(),
                os.uname().nodename, Path('/run/systemd/system').exists())
        except OSError as exc:
            raise ValueError('Cannot establish the Cubic image boundary; refusing image changes.') from exc
        if not isolated_image:
            raise ValueError('Refusing to configure the host root filesystem. Enter Cubic first.')
    if not Path('/etc/calamares/settings.conf').is_file():
        raise ValueError('Expected the extracted Kubuntu Calamares image.')
    values = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    if values.get('ID', '').strip('"') != 'ubuntu' or values.get('VERSION_ID', '').strip('"') != '26.04':
        raise ValueError('This build targets Kubuntu 26.04 amd64 only.')
    arch = subprocess.check_output(['dpkg', '--print-architecture'], text=True).strip()
    if arch != 'amd64':
        raise ValueError('Expected amd64 image.')
    if Path('/etc/sentinel/iso-installed.json').exists():
        raise ValueError('Refusing to rebuild an already provisioned installation.')


def verify_kit(kit):
    kit = Path(kit)
    manifest = json.loads(plain_file(kit / 'kit-manifest.json').read_text())
    if manifest.get('format_version') != 1 or manifest.get('target') != 'kubuntu-26.04-amd64':
        raise ValueError('Unsupported ISO input manifest.')
    entries = manifest.get('sha256')
    if not isinstance(entries, dict) or not entries:
        raise ValueError('Empty ISO input manifest.')
    for name, expected in entries.items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or not re.fullmatch('[0-9a-f]{64}', expected):
            raise ValueError('Invalid ISO checksum entry.')
        path = kit / relative
        if digest(plain_file(path)) != expected:
            raise ValueError(f'ISO input checksum mismatch: {name}')
    actual = {p.relative_to(kit).as_posix() for p in kit.rglob('*') if p.is_file() and p != kit / 'kit-manifest.json'}
    if any(p.is_symlink() for p in kit.rglob('*')) or actual != set(entries):
        raise ValueError('ISO kit contains unlisted files or symlinks.')
    return manifest
