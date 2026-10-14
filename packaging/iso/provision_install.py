#!/usr/bin/python3
"""Calamares finalizer: explicit account, unique secrets, offline target setup."""
from __future__ import annotations

import argparse
import configparser
import grp
import json
import os
from pathlib import Path
import pwd
import secrets
import subprocess
import sys
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import PACKAGES, kwin_defaults, run, validate_user, write
from image_cleanup import DISCOVER_PACKAGES

PROTECTED = sorted(PACKAGES | {'sentinel-platform', 'google-chrome-stable', 'python3', 'python3.14', 'python3.14-venv',
    'systemd', 'systemd-sysv', 'libpam-modules', 'polkitd', 'nftables', 'sddm', 'kwin-wayland',
    'plasma-workspace', 'grub-common', 'grub2-common', 'python3-yaml'})
PRIVILEGED_GROUPS = {'sudo', 'wheel', 'adm', 'lpadmin', 'dip', 'disk', 'shadow', 'docker', 'lxd',
    'incus', 'libvirt', 'libvirt-qemu', 'kvm', 'vboxusers', 'systemd-journal'}


def private_file(path, expected_uid=0):
    path = Path(path)
    stat = path.lstat()
    if path.is_symlink() or stat.st_uid != expected_uid or stat.st_mode & 0o077:
        raise ValueError(f'Expected private, correctly owned configuration: {path}')
    return path


def adguard_document(username, password_hash):
    # This schema is pinned to the AdGuard release in vendor-lock.json.
    return {'schema_version': 34, 'http': {'address': '127.0.0.1:3000'},
        'users': [{'name': username, 'password': password_hash}], 'auth_attempts': 5,
        'block_auth_min': 15, 'theme': 'dark', 'dns': {
            'bind_hosts': ['127.0.0.1', '::1'], 'port': 53,
            'upstream_dns': ['https://dns.cloudflare.com/dns-query'],
            'bootstrap_dns': ['1.1.1.1', '1.0.0.1'], 'fallback_dns': [],
            'use_private_ptr_resolvers': False, 'upstream_mode': 'load_balance'},
        'filtering': {'protection_enabled': True, 'filtering_enabled': True,
                      'blocking_mode': 'nxdomain',
                      'safe_fs_patterns': ['/opt/AdGuardHome/userfilters/sentinel.txt']},
        'filters': [], 'whitelist_filters': [], 'user_rules': [],
        'dhcp': {'enabled': False}, 'tls': {'enabled': False},
        'querylog': {'enabled': False}, 'statistics': {'enabled': False}}


def create_credentials():
    import yaml
    credentials = Path('/etc/sentinel/adguard.toml')
    config = Path('/etc/sentinel/adguard-home/AdGuardHome.yaml')
    if credentials.exists() or config.exists():
        if not credentials.exists() or not config.exists():
            raise ValueError('Partial AdGuard provisioning exists; refusing to replace any saved credentials.')
        private_file(credentials)
        # YAML belongs to the non-login DNS service, which must update its own settings.
        private_file(config, pwd.getpwnam('sentinel-dns').pw_uid)
        return
    username = 'sentinel-' + secrets.token_hex(12)
    password = secrets.token_urlsafe(48)
    result = subprocess.run(['htpasswd', '-n', '-B', '-C', '12', '-i', username],
        input=password + '\n', text=True, capture_output=True, check=True)
    password_hash = result.stdout.strip().split(':', 1)[1]
    dns = pwd.getpwnam('sentinel-dns')
    config.parent.mkdir(parents=True, exist_ok=True)
    os.chown(config.parent, dns.pw_uid, dns.pw_gid)
    config.parent.chmod(0o700)
    write(config, yaml.safe_dump(adguard_document(username, password_hash), sort_keys=False), 0o600)
    os.chown(config, dns.pw_uid, dns.pw_gid)
    write(credentials, '[adguard]\nurl = "http://127.0.0.1:3000"\nusername = ' + json.dumps(username) +
          '\npassword = ' + json.dumps(password) + '\n', 0o600)
    # Credentials never appear in argv, terminal output, Calamares logs or the ISO.
    run('/opt/AdGuardHome/AdGuardHome', '--check-config', '-c', config, '-w', '/var/lib/sentinel/adguard')


def firewall_rules(dns_uid):
    return f'''table inet sentinel_dns {{
    chain route_dns {{
        type nat hook output priority dstnat; policy accept;
        meta skuid {dns_uid} return
        udp dport 53 redirect to :53
        tcp dport 53 redirect to :53
    }}
    chain enforce_dns {{
        type filter hook output priority filter; policy accept;
        meta skuid {dns_uid} return
        meta skuid 0 tcp dport 3000 ip daddr 127.0.0.1 accept
        tcp dport 3000 ip daddr 127.0.0.1 reject
        tcp dport 853 reject
        udp dport {{ 853, 784, 8853 }} reject
        ip daddr != 127.0.0.1 udp dport 53 reject
        ip daddr != 127.0.0.1 tcp dport 53 reject
        ip6 daddr != ::1 udp dport 53 reject
        ip6 daddr != ::1 tcp dport 53 reject
    }}
}}
'''


def polkit_rules(user):
    return '''// Sentinel distribution baseline; no general administrative authorization.
polkit.addRule(function(action, subject) {
    if (subject.user !== USERNAME) return;
    var ordinary = [
        "org.freedesktop.NetworkManager.network-control",
        "org.freedesktop.NetworkManager.enable-disable-network",
        "org.freedesktop.NetworkManager.enable-disable-wifi",
        "org.freedesktop.NetworkManager.enable-disable-wwan",
        "org.freedesktop.NetworkManager.settings.modify.own",
        "org.freedesktop.login1.power-off", "org.freedesktop.login1.reboot",
        "org.freedesktop.login1.suspend", "org.freedesktop.login1.hibernate"
    ];
    if (ordinary.indexOf(action.id) >= 0 && subject.local && subject.active)
        return polkit.Result.YES;
    // System-device and other-seat mounts use different action IDs and stay denied.
    if (action.id === "org.freedesktop.udisks2.filesystem-mount" &&
        subject.local && subject.active)
        return polkit.Result.YES;
    return polkit.Result.NO;
});
'''.replace('USERNAME', json.dumps(user))


def pam_config(user):
    write('/etc/security/sentinel-graphical-access.conf', f'+ : {user} : ALL\n- : ALL : ALL\n')
    write('/etc/security/sentinel-console-access.conf', '- : ALL : ALL\n')
    write('/etc/security/limits.d/90-sentinel-session.conf', f'{user} hard maxlogins 1\n')
    for name in ('sddm', 'sddm-autologin', 'login', 'sshd'):
        path = Path('/etc/pam.d') / name
        if not path.exists():
            continue
        content = path.read_text()
        access = 'graphical' if name.startswith('sddm') else 'console'
        line = f'account required pam_access.so accessfile=/etc/security/sentinel-{access}-access.conf'
        if line not in content:
            content += '\n# Sentinel login boundary\n' + line + '\n'
        if name.startswith('sddm') and 'session required pam_limits.so' not in content:
            content += 'session required pam_limits.so\n'
        write(path, content)


def configure_session():
    choices = sorted(Path('/usr/share/wayland-sessions').glob('*plasma*.desktop'))
    if len(choices) != 1:
        raise ValueError('Expected one packaged KDE Plasma Wayland session.')
    directory = Path('/usr/share/sentinel/wayland-sessions')
    directory.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy2(choices[0], directory / choices[0].name)
    Path('/usr/share/sentinel/no-x11-sessions').mkdir(parents=True, exist_ok=True)
    write('/etc/sddm.conf.d/99-sentinel-platform.conf', '[Users]\nHideUsers=root,sentinel,sentinel-dns\n\n'
          '[Theme]\nCurrent=breeze\n\n'
          '[Wayland]\nSessionDir=/usr/share/sentinel/wayland-sessions\n\n'
          '[X11]\nSessionDir=/usr/share/sentinel/no-x11-sessions\n')
    write('/etc/ssh/sshd_config.d/00-sentinel.conf', 'PermitRootLogin no\nPasswordAuthentication no\nAllowUsers __sentinel_remote_login_disabled__\n')
    run('systemctl', 'mask', 'ssh.service', 'ssh.socket', 'xrdp.service', 'xrdp-sesman.service')


def package_policy():
    declared = json.loads(Path('/usr/lib/sentinel-iso/package-policy-input.json').read_text())
    installed = set(subprocess.check_output(['dpkg-query', '-W', '-f', '${binary:Package}\t${db:Status-Status}\n'], text=True).splitlines())
    installed = {line.split('\t')[0].split(':')[0] for line in installed if line.endswith('\tinstalled')}
    if not (PACKAGES | {'google-chrome-stable', 'steam-installer', 'neovim', 'rofi', 'fastfetch'}) <= installed:
        raise ValueError('Required bundled software was removed during installation.')
    protected = sorted(set(PROTECTED) & installed)
    policy = {'protected': protected, 'allowed_install': sorted(installed | set(declared['approved_apt'])),
              'blocked_install': declared['blocked_browsers'] + ['gdm3', 'lightdm', 'xrdp', 'tightvncserver', 'tigervnc-standalone-server',
                  *sorted(DISCOVER_PACKAGES)]}
    write('/etc/sentinel/iso-package-policy.json', json.dumps(policy, indent=2) + '\n')


def provision(user):
    if os.geteuid() != 0 or not Path('/usr/lib/sentinel-iso/image-built.json').is_file():
        raise ValueError('Run only as the installed target finalizer, not on the development host.')
    validate_user(user)
    account = pwd.getpwnam(user)
    if not 1000 <= account.pw_uid < 60000 or account.pw_shell != '/bin/bash':
        raise ValueError('Calamares must supply the one normal Bash desktop account.')
    others = [p.pw_name for p in pwd.getpwall() if 1000 <= p.pw_uid < 60000 and p.pw_name != user]
    if others:
        raise ValueError('Unexpected additional human accounts: ' + ', '.join(others))
    marker = Path('/etc/sentinel/iso-installed.json')
    if marker.exists() and json.loads(marker.read_text())['protected_user'] != user:
        raise ValueError('Refusing to replace the protected account on an existing installation.')
    # Remove Calamares-generated per-user sudo grants as well as privileged groups.
    for group in PRIVILEGED_GROUPS:
        try:
            g = grp.getgrnam(group)
        except KeyError:
            continue
        if account.pw_gid == g.gr_gid:
            raise ValueError('The desktop account has an administrative primary group.')
        if user in g.gr_mem:
            run('gpasswd', '-d', user, group)
    write('/etc/sudoers.d/99-sentinel-desktop', f'{user} ALL=(ALL:ALL) !ALL\n', 0o440)
    run('visudo', '-c')
    run('passwd', '--lock', 'root')
    try:
        pwd.getpwnam('sentinel-dns')
    except KeyError:
        run('useradd', '--system', '--user-group', '--home-dir', '/var/lib/sentinel/adguard',
            '--no-create-home', '--shell', '/usr/sbin/nologin', 'sentinel-dns')
    dns = pwd.getpwnam('sentinel-dns')
    if dns.pw_uid == 0 or dns.pw_shell not in {'/usr/sbin/nologin', '/bin/false'}:
        raise ValueError('Invalid DNS service account.')
    state = Path('/var/lib/sentinel/adguard')
    state.mkdir(parents=True, exist_ok=True)
    os.chown(state, dns.pw_uid, dns.pw_gid)
    state.chmod(0o700)
    Path('/opt/AdGuardHome/userfilters').mkdir(parents=True, exist_ok=True)
    create_credentials()
    write('/etc/sentinel/dns-transport.nft', firewall_rules(dns.pw_uid))
    # Removing the resolver symlink is deliberate: NetworkManager must not supply a bypass resolver.
    resolver = Path('/etc/resolv.conf')
    if resolver.is_symlink():
        resolver.unlink()
    write(resolver, 'nameserver 127.0.0.1\nnameserver ::1\noptions timeout:2 attempts:2\n')
    write('/etc/NetworkManager/conf.d/90-sentinel-dns.conf', '[main]\ndns=none\nrc-manager=unmanaged\n')
    run('systemctl', 'mask', 'systemd-resolved.service')
    write('/etc/polkit-1/rules.d/00-sentinel-distro.rules', polkit_rules(user))
    pam_config(user)
    configure_session()
    # Explicitly select the account; no UID guessing and no services started in the installer chroot.
    run('/usr/bin/sentinel-setup', '--user', user, '--no-start')
    # Apply appearance as the account itself after installer-created config files.
    # This runs only during provisioning, never on subsequent logins.
    run('runuser', '-u', user, '--', '/usr/bin/env',
        f'HOME={account.pw_dir}', f'XDG_CONFIG_HOME={account.pw_dir}/.config',
        f'XDG_DATA_HOME={account.pw_dir}/.local/share', f'XDG_STATE_HOME={account.pw_dir}/.local/state',
        '/usr/bin/python3', '-I', '-B',
        '/usr/lib/sentinel-iso/desktop_defaults.py', '--initial-install')
    kwin = Path('/etc/xdg/kwinrc')
    write(kwin, kwin_defaults(kwin.read_text(), lock_bridge=True))
    run('/usr/lib/sentinel-iso/boot_security', '--initialize')
    package_policy()
    write(marker, json.dumps({'format_version': 1, 'protected_user': user, 'uid': account.pw_uid,
          'ready': False}, indent=2) + '\n')
    print('Sentinel account and protected startup configured. First boot verifies DNS and local model services.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', required=True)
    args = parser.parse_args()
    try:
        provision(args.user)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f'Sentinel installation provisioning failed: {exc}') from exc
