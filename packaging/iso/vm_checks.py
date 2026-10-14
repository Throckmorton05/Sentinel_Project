#!/usr/bin/python3
"""Read-only installed-VM checks, run as its normal desktop user."""
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys


def main():
    if os.geteuid() == 0 or not Path('/etc/sentinel/iso-installed.json').is_file():
        raise SystemExit('Run as the normal user inside the installed Sentinel VM.')
    failures = []
    def check(label, good, detail):
        print(f'{"PASS" if good else "CHECK"}: {label}: {detail}')
        if not good:
            failures.append(label)
    marker = json.loads(Path('/etc/sentinel/iso-installed.json').read_text())
    user = pwd.getpwuid(os.getuid())
    check('protected account', user.pw_name == marker.get('protected_user') and os.getuid() == marker.get('uid'), user.pw_name)
    check('Wayland session', os.environ.get('XDG_SESSION_TYPE') == 'wayland', os.environ.get('XDG_SESSION_TYPE', 'unset'))
    groups = subprocess.check_output(['id', '-nG'], text=True).strip()
    forbidden = {'sudo', 'wheel', 'adm', 'disk', 'docker', 'lxd', 'libvirt', 'kvm', 'vboxusers'}
    check('desktop privilege groups', not (set(groups.split()) & forbidden), groups)
    sessions = subprocess.run(['loginctl', 'list-sessions', '--no-pager'], text=True, capture_output=True)
    print('\nLogin sessions (confirm only one normal desktop):\n' + sessions.stdout)
    for service in ('sentinel-first-boot', 'sentinel-dns', 'sentinel-dns-firewall', 'sentineld', 'sentinel-session-guard'):
        result = subprocess.run(['systemctl', 'is-active', service + '.service'], text=True, capture_output=True)
        check(service, result.returncode == 0, result.stdout.strip())
    config = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    data = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    appearance = {
        'Rofi editable config': config / 'rofi/config.rasi',
        'Starship editable config': Path(os.environ.get('STARSHIP_CONFIG') or config / 'starship.toml'),
        'Konsole editable profile': data / 'konsole/Sentinel.profile',
    }
    for label, path in appearance.items():
        check(label, path.is_file() and os.access(path, os.W_OK), str(path))
    for path in (Path('/etc/sentinel/adguard.toml'), Path('/etc/sentinel/adguard-home')):
        check('AdGuard private ' + path.name, path.exists() and not os.access(path, os.R_OK), str(path))
    result = subprocess.run(['curl', '--noproxy', '*', '--silent', '--max-time', '3',
                             'http://127.0.0.1:3000/control/status'], text=True, capture_output=True)
    check('desktop denied AdGuard control connection', result.returncode != 0, f'curl exit {result.returncode}')
    # A 401 response is not equivalent to the intended firewall-denied connection.
    for args in (['sen', 'state'], ['sen', 'dns', 'status'], ['sen', 'app-block', 'status'],
                 ['sen', 'ls-services'], ['sen', 'autostart-ls']):
        try:
            result = subprocess.run(args, text=True, capture_output=True, timeout=20)
            print('\n$ ' + ' '.join(args) + '\n' + result.stdout + result.stderr)
            check('command ' + ' '.join(args), result.returncode == 0, f'exit {result.returncode}')
        except (OSError, subprocess.TimeoutExpired) as exc:
            check('command ' + ' '.join(args), False, str(exc))
    print('\nBehavioral app blocking, autostart after reboot, alternative logins, splash and DNS queries still need the acceptance steps in docs/sentinel-updates.md.')
    raise SystemExit(bool(failures))


if __name__ == '__main__':
    main()
