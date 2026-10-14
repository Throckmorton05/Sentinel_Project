#!/usr/bin/python3.14
"""Idempotent system setup, deferred upgrades, and bounded owned-file cleanup.

Only maintainer scripts/root setup run this helper. No state is reset on upgrade.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack
import fcntl
import grp
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
from urllib.parse import urlsplit

APIS = tuple(f'sentinel-{name}.service' for name in ('opennsfw', 'drawn', 'hentai', 'nudenet'))
UNITS = ('sentineld.service', 'sentinel-guardian-recovery.socket', *APIS)
PREFIX = Path('/usr/lib/sentinel')
DEFAULTS = Path('/usr/share/sentinel/defaults')


def run(*args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def protected_directory(path: Path):
    if path != path.parent:
        protected_directory(path.parent)
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o755)
    info = path.lstat()
    forbidden_write = 0o022
    # Ubuntu's logging service owns the syslog group and may write /var/log.
    # Trust only that exact system parent/group, without changing its modes.
    # Sentinel's own directories and every other ancestor remain strict.
    if path == Path('/var/log'):
        try:
            syslog_gid = grp.getgrnam('syslog').gr_gid
        except KeyError:
            pass
        else:
            if info.st_gid == syslog_gid:
                forbidden_write = 0o002
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & forbidden_write:
        raise ValueError(f'Refusing unprotected directory: {path}')


def protected_file(path: Path):
    protected_directory(path.parent)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError(f'Refusing unprotected file: {path}')


def create_once(source: Path, destination: Path, mode=0o644):
    protected_directory(destination.parent)
    if destination.exists() or destination.is_symlink():
        protected_file(destination)
        return False
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(source.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return True


def atomic_text(path: Path, text: str, mode=0o644):
    protected_directory(path.parent)
    if path.exists() or path.is_symlink():
        protected_file(path)
    fd, filename = tempfile.mkstemp(prefix='.sentinel-package-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(filename, path)
    finally:
        Path(filename).unlink(missing_ok=True)


def configure():
    try:
        group = grp.getgrnam('sentinel')
    except KeyError:
        run('addgroup', '--system', 'sentinel')
        group = grp.getgrnam('sentinel')
    if group.gr_gid == 0:
        raise ValueError('The Sentinel control group must not be the root group.')
    try:
        user = pwd.getpwnam('sentinel')
    except KeyError:
        run('adduser', '--system', '--ingroup', 'sentinel', '--home',
            '/var/lib/sentinel/image-filter', '--no-create-home',
            '--shell', '/usr/sbin/nologin', 'sentinel')
        user = pwd.getpwnam('sentinel')
    if user.pw_uid == 0 or user.pw_gid != group.gr_gid or user.pw_shell not in {'/usr/sbin/nologin', '/bin/false'}:
        raise ValueError('Existing sentinel account is not the expected non-login service account.')
    for name in ('/etc/sentinel', '/etc/sentinel/dns', '/etc/sentinel/services',
                 '/var/lib/sentinel', '/var/lib/sentinel/dns', '/var/lib/sentinel/guardian-desktop',
                 '/var/lib/sentinel/apparmor/profiles', '/var/cache/sentinel/dns', '/var/log/sentinel'):
        protected_directory(Path(name))
    home = Path('/var/lib/sentinel/image-filter')
    if home.is_symlink():
        raise ValueError(f'Refusing symlinked service home: {home}')
    home.mkdir(exist_ok=True, mode=0o700)
    info = home.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, user.pw_uid}:
        raise ValueError('Unexpected image-filter home ownership')
    os.chown(home, user.pw_uid, group.gr_gid)
    home.chmod(0o700)
    for filename in ('settings.toml', 'blocked_apps.toml'):
        create_once(DEFAULTS / filename, Path('/etc/sentinel') / filename)
    for filename in ('user_allowlist.txt', 'user_blocklist.txt'):
        create_once(DEFAULTS / 'dns' / filename, Path('/etc/sentinel/dns') / filename)
    for source in (DEFAULTS / 'dns-seed').glob('*.txt'):
        create_once(source, Path('/var/lib/sentinel/dns') / source.name)
    if not Path('/etc/sentinel/daemon.toml').exists():
        print('Sentinel installed. Select the protected account with: sudo sentinel-setup --user USERNAME')


def systemd_available():
    return Path('/run/systemd/system').is_dir()


def schedule_restart():
    if not systemd_available() or not Path('/etc/sentinel/daemon.toml').exists():
        return
    unit = 'sentinel-package-restart.service'
    active = subprocess.run(['systemctl', 'is-active', '--quiet', unit], check=False)
    if active.returncode == 0:
        return  # One worker handles the whole APT transaction and all triggers.
    run('systemd-run', '--quiet', '--collect', '--unit=sentinel-package-restart',
        '--property=Type=exec', '--property=RuntimeMaxSec=3700',
        '/usr/bin/python3.14', '-I', '-B', PREFIX / 'package_lifecycle.py', 'restart-when-idle')


def package_manager_idle(paths=(Path('/var/lib/dpkg/lock'), Path('/var/lib/dpkg/lock-frontend'))):
    """dpkg uses POSIX record locks; flock() would check the wrong lock type."""
    try:
        with ExitStack() as stack:
            for path in paths:
                stream = stack.enter_context(path.open('r+'))
                fcntl.lockf(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
    except (BlockingIOError, PermissionError):
        return False


def restart_when_idle():
    deadline = time.monotonic() + 3600
    while not package_manager_idle():
        if time.monotonic() >= deadline:
            raise ValueError('APT is still busy; restart Sentinel manually after package operations finish.')
        time.sleep(2)
    # Prevent a worker queued by an older postinst from resurrecting a removed
    # package or a daemon the administrator disabled/stopped deliberately.
    status = subprocess.check_output(['dpkg-query', '-W', '-f=${Status}', 'sentinel-core'], text=True).strip()
    if status != 'install ok installed' or not Path('/etc/sentinel/daemon.toml').exists():
        return
    sync_monitor_interpreter()
    run('deb-systemd-invoke', 'try-restart', *APIS, 'sentineld.service')
    # First installation/start is explicit through sentinel-setup or reboot.


def sync_monitor_interpreter():
    """Keep KWin's distinct interpreter current after distro Python updates."""
    source = Path('/usr/bin/python3.14').resolve()
    target = PREFIX / 'venv/bin/sentinel-screen-monitor-python'
    protected_file(source)
    protected_file(target)
    data = source.read_bytes()
    if target.read_bytes() == data:
        return
    fd, filename = tempfile.mkstemp(prefix='.monitor-python-', dir=target.parent)
    try:
        os.fchmod(fd, 0o755)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(filename, target)  # Running processes retain the old inode.
    finally:
        Path(filename).unlink(missing_ok=True)


def remove_owned(path: Path, expected_prefix: str | None = None):
    if not path.exists() and not path.is_symlink():
        return
    protected_file(path)
    if expected_prefix is not None and not path.read_text().startswith(expected_prefix):
        raise ValueError(f'Edited/non-Sentinel file retained: {path}')
    path.unlink()


def cleanup_guardian_launchers():
    marker_dir = Path('/var/lib/sentinel/guardian-desktop/launchers')
    if marker_dir.exists():
        protected_directory(marker_dir)
        for marker in marker_dir.glob('*.desktop'):
            protected_file(marker)
            remove_owned(Path('/usr/local/share/applications') / marker.name,
                         '# Generated by Sentinel Guardian App Access\n')
            marker.unlink()
    globals_file = Path('/etc/xdg/kdeglobals')
    if globals_file.exists():
        protected_file(globals_file)
        original = globals_file.read_text()
        begin, end = '# BEGIN Sentinel Guardian App Access\n', '# END Sentinel Guardian App Access\n'
        if begin in original:
            if original.count(begin) != 1 or original.count(end) != 1:
                raise ValueError('Edited/incomplete Sentinel Kiosk block retained in kdeglobals')
            before, rest = original.split(begin, 1)
            _, after = rest.split(end, 1)
            atomic_text(globals_file, before + after)


def cleanup_apparmor():
    directory = Path('/var/lib/sentinel/apparmor/profiles')
    if not directory.exists():
        return
    protected_directory(directory)
    loaded = Path('/sys/kernel/security/apparmor/profiles').exists()
    for profile in directory.iterdir():
        protected_file(profile)
        if not profile.read_text().startswith('# Sentinel managed application block profile'):
            raise ValueError(f'Non-Sentinel AppArmor profile retained: {profile}')
        if loaded:
            # Use Sentinel's unload-and-verify implementation while the runtime
            # is still present; stop removal on failure rather than leave blocks.
            run(PREFIX / 'venv/bin/python', '-I', '-B', '-c',
                'import sys; from pathlib import Path; '
                'from sentinel.app_blocker.AppArmor.profile_loader import unload_profile; '
                'unload_profile(Path(sys.argv[1]))', profile)
        profile.unlink()


def disable_adguard_filter():
    """Remove only Sentinel's subscription; retain unrelated AdGuard filters."""
    filter_path = Path('/opt/AdGuardHome/userfilters/sentinel.txt')
    credentials = Path('/etc/sentinel/adguard.toml')
    if not filter_path.exists():
        return
    protected_file(credentials)
    if credentials.stat().st_mode & 0o077:
        raise ValueError('AdGuard credentials must be private to root before removal.')
    info = tomllib.loads(credentials.read_text()).get('adguard', {})
    url = info.get('url', '').rstrip('/')
    parsed = urlsplit(url)
    if (parsed.scheme not in {'http', 'https'} or parsed.hostname not in {'127.0.0.1', '::1', 'localhost'}
            or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError('Refusing nonlocal AdGuard control URL during removal.')
    token = base64.b64encode(f'{info.get("username", "")}:{info.get("password", "")}'.encode()).decode()
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    def request(endpoint, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(url + '/control/' + endpoint, data=data,
                                     headers={'Authorization': 'Basic ' + token,
                                              'Content-Type': 'application/json'})
        with opener.open(req, timeout=10) as response:
            raw = response.read(65536)
            return json.loads(raw) if raw else None
    status = request('filtering/status')
    if not isinstance(status, dict) or not isinstance(status.get('filters') or [], list):
        raise ValueError('Invalid AdGuard filter status during removal.')
    filters = status.get('filters') or []
    for item in filters:
        if not isinstance(item, dict):
            raise ValueError('Invalid AdGuard subscription during removal.')
        if item.get('url') in {str(filter_path), 'file://' + str(filter_path)}:
            request('filtering/set_url', {'url': item['url'], 'whitelist': False,
                    'data': {'name': item.get('name', 'Sentinel'), 'url': item['url'], 'enabled': False}})
    # Keep bytes for reinstall diagnostics; the subscription is disabled.


def remove():
    if systemd_available():
        # Cancel queued upgrades before removing runtime or enforcement assets.
        subprocess.run(['systemctl', 'stop', 'sentinel-package-restart.service'], check=False)
        run('deb-systemd-invoke', 'stop', *UNITS)
        for unit in UNITS:
            if subprocess.run(['systemctl', 'is-active', '--quiet', unit], check=False).returncode == 0:
                raise ValueError(f'{unit} is still active; stop it before removal')
    disable_adguard_filter()
    cleanup_apparmor()
    cleanup_guardian_launchers()
    for name, marker in (
        ('/etc/udev/rules.d/71-sentinel-boot-media.rules', '# Managed by sentineld;'),
        ('/etc/udev/rules.d/71-sentinel-vm-protection.rules', '# Managed by Sentinel:'),
        ('/etc/polkit-1/rules.d/00-sentinel-boot-media.rules', '// Managed by sentineld:'),
        ('/etc/polkit-1/rules.d/00-sentinel-vm-protection.rules', '// Managed by Sentinel:'),
    ):
        remove_owned(Path(name), marker)
    if systemd_available():
        run('udevadm', 'control', '--reload-rules')
        run('udevadm', 'trigger', '--action=add', '--subsystem-match=block')
        for name in ('kvm', 'vboxdrv', 'vboxdrvu', 'vboxnetctl', 'vmmon'):
            run('udevadm', 'trigger', '--action=add', '--subsystem-match=misc', f'--sysname-match={name}')
        run('udevadm', 'settle', '--timeout=10')
    # This exact filename is the backend's owned Chrome policy. Other policies
    # are never removed. OS DNS routing and AdGuard Home are external setup.
    remove_owned(Path('/etc/opt/chrome/policies/managed/sentinel.json'))
    print('Sentinel stopped and owned enforcement removed. AdGuard Home and network DNS settings are retained.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('configure', 'schedule-restart', 'restart-when-idle', 'remove'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('Package lifecycle operations require root.')
    {'configure': configure, 'schedule-restart': schedule_restart,
     'restart-when-idle': restart_when_idle, 'remove': remove}[args.action]()


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f'Sentinel package lifecycle failed: {exc}') from exc
