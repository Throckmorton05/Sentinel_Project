#!/usr/bin/python3.14
"""Select the protected account explicitly; configure KWin as that account."""
from __future__ import annotations

import argparse
import base64
import grp
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tomllib
import urllib.request
from urllib.parse import urlsplit

# Also importable by stdlib-only unit tests, without importing the application.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from package_lifecycle import atomic_text, configure, protected_file, run, sync_monitor_interpreter

CONFIG = Path('/etc/sentinel/daemon.toml')
PYTHON = Path('/usr/lib/sentinel/venv/bin/python')


def account_for_setup(name: str, current: Path = CONFIG):
    user = pwd.getpwnam(name)
    if user.pw_uid == 0 or user.pw_name == 'sentinel' or user.pw_shell in {'/usr/sbin/nologin', '/bin/false'}:
        raise ValueError('Select a normal, non-root desktop account.')
    if current.exists() or current.is_symlink():
        protected_file(current)
        existing = tomllib.loads(current.read_text()).get('protected_user')
        if existing != user.pw_name:
            raise ValueError(f'{current} already protects {existing!r}; setup will not replace that account.')
    return user


def check_adguard(path=Path('/etc/sentinel/adguard.toml')):
    if not path.exists():
        raise ValueError('Configure AdGuard Home and /etc/sentinel/adguard.toml first; see docs/debian-packaging.md.')
    protected_file(path)
    if path.stat().st_mode & 0o077:
        raise ValueError(f'{path} must be private to root (chmod 0600).')
    info = tomllib.loads(path.read_text()).get('adguard', {})
    url = info.get('url', '').rstrip('/')
    username, password = info.get('username'), info.get('password')
    parsed = urlsplit(url)
    if (parsed.scheme not in {'http', 'https'} or parsed.hostname not in {'127.0.0.1', '::1', 'localhost'}
            or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError('AdGuard control URL must point to a local HTTP(S) endpoint.')
    if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
        raise ValueError('Enter your real AdGuard username and password; the packages contain no default credentials.')
    token = base64.b64encode(f'{username}:{password}'.encode()).decode()
    request = urllib.request.Request(url + '/control/status', headers={'Authorization': 'Basic ' + token})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=5) as response:
        data = json.loads(response.read(65536))
    if not isinstance(data, dict) or data.get('running') is not True or data.get('protection_enabled') is not True:
        raise ValueError('AdGuard DNS protection is not running.')


def check_runtime():
    if not PYTHON.is_file():
        raise ValueError('Install sentinel-core first.')
    for path in (
        '/usr/lib/sentinel/venv/bin/sentinel-screen-monitor-python',
        '/usr/share/applications/sentinel-screen-monitor.desktop',
        '/usr/share/kwin/scripts/sentinel-window-bridge/metadata.json',
        '/usr/share/sentinel/models/openNSFW2_ONNX_Model/open_nsfw.onnx',
        '/usr/share/sentinel/models/anime_real_cls/mobilenetv3_v1.4_dist/model.onnx',
        '/usr/share/sentinel/models/hentai-vit/model.onnx',
    ):
        if not Path(path).is_file():
            raise ValueError(f'Install the complete package set; missing {path}')
    run(PYTHON, '-I', '-B', '-c', 'import fastapi, uvicorn, dbus_next, onnxruntime, nudenet; '
        'from PyQt6 import QtCore, QtGui, QtWidgets, QtDBus')
    run(PYTHON, '-I', '-B', '-c', 'from sentinel.image_blocking.screen_blocker.system_qt import use_system_pyqt; '
        'use_system_pyqt()')
    qml = Path('/usr/lib/x86_64-linux-gnu/qt6/qml/org/kde/layershell/qmldir')
    if not qml.is_file():
        raise ValueError('KDE layer-shell QML plugin is missing; install sentinel-desktop dependencies.')


def configure_kwin():
    """Executed as the protected user, never write inside their home as root."""
    if os.geteuid() == 0:
        raise ValueError('KWin preference setup must run as the desktop user.')
    # A development symlink/user-installed copy shadows the protected system
    # package. Report it rather than silently trusting mutable script code.
    override = Path.home() / '.local/share/kwin/scripts/sentinel-window-bridge'
    if override.exists() or override.is_symlink():
        raise ValueError(f'Move the existing user KWin script aside first: {override}')
    run('/usr/bin/kwriteconfig6', '--file', 'kwinrc', '--group', 'Plugins',
        '--key', 'sentinel-window-bridgeEnabled', 'true')


def set_account(user):
    group = grp.getgrnam('sentinel')
    if group.gr_gid not in os.getgrouplist(user.pw_name, user.pw_gid):
        run('usermod', '-a', '-G', 'sentinel', user.pw_name)
    env = {'PATH': '/usr/bin:/bin', 'HOME': user.pw_dir, 'USER': user.pw_name,
           'LOGNAME': user.pw_name, 'PYTHONDONTWRITEBYTECODE': '1'}
    run('runuser', '-u', user.pw_name, '--', '/usr/bin/env', '-i',
        *(f'{key}={value}' for key, value in env.items()), '/usr/bin/python3.14', '-I', '-B',
        Path(__file__).resolve(), '--configure-kwin')
    # Validation above runs even on repeated setup; existing config bytes and
    # comments remain untouched, and no lock/settings/state file is rewritten.
    if not CONFIG.exists():
        atomic_text(CONFIG, f'protected_user = {json.dumps(user.pw_name)}\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', help='the existing non-root desktop account to protect')
    parser.add_argument('--check', action='store_true', help='verify runtime and AdGuard prerequisites without changes')
    parser.add_argument('--no-start', action='store_true', help='configure account/KWin without starting services or requiring AdGuard')
    parser.add_argument('--configure-kwin', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.configure_kwin:
        configure_kwin()
        return
    if os.geteuid() != 0:
        raise ValueError('Run: sudo sentinel-setup --user YOUR_DESKTOP_USER')
    if args.check:
        check_runtime()
        check_adguard()
        print('Runtime and local AdGuard control API are ready. Verify network DNS routing separately.')
        return
    if not args.user:
        parser.error('--user is required; the account is never guessed')
    user = account_for_setup(args.user)
    check_runtime()
    if not args.no_start:
        check_adguard()
    configure()
    sync_monitor_interpreter()
    set_account(user)
    if not args.no_start and Path('/run/systemd/system').is_dir():
        run('systemctl', 'daemon-reload')
        run('deb-systemd-invoke', 'start', 'sentinel-guardian-recovery.socket', 'sentineld.service')
    print(f'Sentinel configured for {user.pw_name}. Log out and back into KDE Wayland to activate KWin and group membership.')
    print('No lock mode was changed. Verify DNS routing and all model health endpoints before relying on protection.')


if __name__ == '__main__':
    try:
        main()
    except (KeyError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f'Sentinel setup stopped: {exc}') from exc
