#!/usr/bin/python3
"""Build a sixth DEB for ISO integration using verified kit vendors and current source."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import REQUIRED_ASSETS, WALLPAPERS, RESOLUTIONS, digest, plain_file, verify_kit
from prepare import validate_debs, source_constant
from repository_config import verify_configuration
from desktop_defaults import launcher_entry

RUNTIME = ('common.py', 'provision_install.py', 'first_boot.py', 'session_guard.py',
           'protected_apt.py', 'boot_security.py', 'desktop_defaults.py', 'load_firewall.sh',
           'platform_postinst.py', 'repository_config.py', 'image_cleanup.py',
           'live_welcome.py', 'desktop_polish.py')
WRAPPERS = ('provision_install', 'first_boot', 'session_guard', 'protected_apt', 'boot_security')
APT_HOOK = '''DPkg::Pre-Install-Pkgs { "/usr/lib/sentinel-iso/protected_apt"; };
DPkg::Tools::Options::/usr/lib/sentinel-iso/protected_apt::Version "2";
DPkg::Tools::Options::/usr/lib/sentinel-iso/protected_apt::InfoFD "0";
DPkg::Post-Invoke { "/usr/lib/sentinel-iso/boot_security --refresh-if-installed"; };
'''
DROPINS = {
    'sentineld.service.d/iso.conf': '[Unit]\nConditionPathExists=/etc/sentinel/iso-installed.json\nRequires=sentinel-first-boot.service\nAfter=sentinel-first-boot.service\n',
    'sddm.service.d/sentinel.conf': '[Unit]\nRequires=sentinel-first-boot.service sentineld.service sentinel-session-guard.service\nAfter=sentinel-first-boot.service sentineld.service sentinel-session-guard.service\n',
}


def build_platform(project, kit, debs, output):
    project, kit, output = Path(project), Path(kit), Path(output)
    verify_kit(kit)
    _, version = validate_debs(debs)
    config = project / 'configs/updates'
    verify_configuration(config)
    if output.exists():
        raise ValueError('Choose a new platform output directory.')
    # Complete source and config validation precedes creating the output.
    for name in RUNTIME:
        plain_file(project / 'packaging/iso' / name)
    for name in REQUIRED_ASSETS:
        plain_file(project / 'assets' / name)
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix='sentinel-platform-') as folder:
        root = Path(folder)
        def copy(source, relative, mode=0o644):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(plain_file(source), target)
            target.chmod(mode)
        def text(relative, data, mode=0o644):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(data); target.chmod(mode)
        for name in RUNTIME:
            copy(project / 'packaging/iso' / name, 'usr/lib/sentinel-iso/' + name)
        for name in WRAPPERS:
            text('usr/lib/sentinel-iso/' + name,
                 f'#!/bin/sh\nexec /usr/bin/python3 -I -B /usr/lib/sentinel-iso/{name}.py "$@"\n', 0o755)
        for source in (project / 'packaging/iso/systemd').glob('*.service'):
            copy(source, 'usr/lib/systemd/system/' + source.name)
        for name, content in DROPINS.items():
            text('usr/lib/systemd/system/' + name, content)
        text('etc/apt/apt.conf.d/99-sentinel-platform', APT_HOOK)
        # Legacy /etc units shadow packaged /usr/lib units. Remove only copies
        # whose bytes match recorded image provenance, never custom overrides.
        legacy = {}
        for source in (kit / 'packaging/iso/systemd').glob('*.service'):
            legacy['/etc/systemd/system/' + source.name] = digest(source)
        import hashlib
        for name, content in DROPINS.items():
            legacy['/etc/systemd/system/' + name] = hashlib.sha256(content.encode()).hexdigest()
        text('usr/lib/sentinel-iso/legacy-units.json', json.dumps(legacy, indent=2) + '\n')
        text('usr/lib/sentinel-iso/legacy-vendor.json', json.dumps(
            {'/usr/local/bin/starship': digest(kit / 'vendor/starship')}) + '\n')
        approved = source_constant(project / 'sen/install/packages.py', 'APPROVED_PACKAGES')
        allowed = sorted({p for d in approved.values() if d['type'] == 'apt' for p in d['packages']} | {'neovim', 'unityhub', 'reaper', 'sentinel-platform'})
        blocked = sorted(source_constant(project / 'sentinel/browser_policies/restrictions.py', 'BROWSER_PACKAGES'))
        text('usr/lib/sentinel-iso/package-policy-input.json', json.dumps(
            {'approved_apt': allowed, 'blocked_browsers': blocked}, indent=2) + '\n')
        for name in ('repository.json', 'sentinel.sources', 'sentinel.pref', 'sentinel-archive-keyring.gpg'):
            copy(config / name, 'usr/share/sentinel/updates/' + name)
        copy(config / 'sentinel-archive-keyring.gpg', 'usr/share/keyrings/sentinel-archive-keyring.gpg')
        for name in REQUIRED_ASSETS:
            if name.startswith(('Rofi/', 'Terminal/')):
                copy(project / 'assets' / name, 'usr/share/sentinel/desktop/' + name)
            elif name.startswith('Installer/'):
                copy(project / 'assets' / name, 'usr/share/calamares/branding/sentinel/' + name.removeprefix('Installer/'))
        copy(project / 'assets/Sentinel_Shield_Minimal_Solid-Visor.svg',
             'usr/share/sentinel/assets/Sentinel_Shield_Minimal_Solid-Visor.svg')
        for name in ('Sentinel.colorscheme',):
            copy(project / 'assets/Terminal/Konsole' / name, 'usr/share/konsole/' + name)
        text('usr/bin/sentinel-launcher',
             '#!/bin/sh\nexec /usr/bin/rofi -show drun -wayland-layer top "$@"\n', 0o755)
        text('usr/share/applications/sentinel-launcher.desktop', launcher_entry())
        boot = project / 'assets/Boot'
        for source in sorted(boot.glob('*.png')):
            copy(source, 'usr/share/plymouth/themes/sentinel/' + source.name)
        if not (root / 'usr/share/plymouth/themes/sentinel/watermark.png').is_file():
            raise ValueError('Missing Sentinel Plymouth artwork.')
        fetch = root / 'usr/share/sentinel/desktop/Terminal/Fastfetch/config.jsonc'
        if '/usr/share/sentinel/desktop/Terminal/Fastfetch/Sentinel_Shield.txt' not in fetch.read_text():
            raise ValueError('Fastfetch logo path changed; review the source config before building.')
        for package, (folder_name, prefix) in WALLPAPERS.items():
            for size in RESOLUTIONS:
                copy(kit / 'assets/Screensavers' / folder_name / f'{prefix}-{size}.png',
                     f'usr/share/wallpapers/{package}/contents/images/{size}.png')
            text(f'usr/share/wallpapers/{package}/metadata.json', json.dumps({'KPlugin': {
                'Id': package, 'Name': 'Sentinel Gradient' if package == 'SentinelGradient' else 'Sentinel Navy Waves',
                'Authors': [{'Name': 'Sentinel'}]}}, indent=2) + '\n')
        copy(kit / 'vendor/AdGuardHome', 'opt/AdGuardHome/AdGuardHome', 0o755)
        copy(kit / 'vendor/starship', 'usr/bin/starship', 0o755)
        for source in (kit / 'vendor/fonts').iterdir():
            copy(source, 'usr/share/fonts/truetype/jetbrainsmono-nerd/' + source.name)
        for name in ('AdGuardHome-LICENSE.txt', 'starship-LICENSE.txt'):
            copy(kit / 'vendor' / name, 'usr/share/doc/sentinel-platform/' + name)
        for name in ('LICENSE', 'NOTICE.md', 'THIRD_PARTY.md'):
            copy(project / name, 'usr/share/doc/sentinel-platform/' + name)
        text('usr/share/doc/sentinel-platform/copyright',
             'Sentinel integration: Copyright (C) 2026 Sentinel contributors.\n'
             'License: GPL-3.0-only; see LICENSE and NOTICE.md in this directory.\n'
             'Source: https://github.com/Throckmorton05/Sentinel_Project\n'
             'AdGuard Home and Starship retain the bundled upstream licenses.\n')
        text('DEBIAN/control', f'Package: sentinel-platform\nVersion: {version}\nArchitecture: amd64\n'
             'Maintainer: Sentinel Project <noreply@sentinel.invalid>\nSection: admin\nPriority: optional\n'
             f'Depends: sentinel (= {version}), python3 (>= 3.11), python3-yaml, gnupg, nftables, apache2-utils, sddm, fontconfig, plymouth-theme-spinner, initramfs-tools, xdg-user-dirs\n'
             'Description: Sentinel installed distribution integration and update trust\n'
             ' Versioned ISO helpers, protected startup, DNS service, desktop assets\n'
             ' and public APT trust. Existing account state and credentials persist.\n')
        text('DEBIAN/conffiles', '/etc/apt/apt.conf.d/99-sentinel-platform\n')
        text('DEBIAN/postinst', '#!/bin/sh\nset -e\nif [ "$1" = configure ]; then\n'
             ' /usr/bin/python3 -I -B /usr/lib/sentinel-iso/platform_postinst.py\nfi\n', 0o755)
        # mkdir inherits the developer's umask (often 0002). Package-owned
        # directories must not be group-writable: postinst's trust checks
        # deliberately reject those paths. Normalize the archive root too;
        # TemporaryDirectory starts with mode 0700.
        for directory in (root, *(p for p in root.rglob('*') if p.is_dir())):
            directory.chmod(0o755)
        filename = output / f'sentinel-platform_{version}_amd64.deb'
        subprocess.run(['dpkg-deb', '--root-owner-group', '--build', str(root), str(filename)], check=True)
        (output / 'SHA256SUMS').write_text(digest(filename) + '  ' + filename.name + '\n')
    print(f'Platform package ready: {filename}')
    return filename


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kit', type=Path, required=True)
    parser.add_argument('--debs', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        build_platform(Path(__file__).resolve().parents[2], args.kit, args.debs, args.output)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise SystemExit(f'Platform build stopped: {exc}') from exc
