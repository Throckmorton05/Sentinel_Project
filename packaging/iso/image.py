"""Configure an extracted Kubuntu image; never start services in the chroot."""
from __future__ import annotations

import configparser
import json
import os
from pathlib import Path
import shutil
import subprocess

from common import WALLPAPERS, RESOLUTIONS, kwin_defaults, run, write
from apt_sources import disable_media_source, media_cleanup_config, stage_installer_media_source
from desktop_defaults import APPLICATIONS, launcher_entry
from installer_branding import install_branding
from image_cleanup import purge_stock_packages, remove_duplicate_profile
from live_branding import install_live_branding


def calamares_config(settings, users):
    """Keep the distro's install jobs; replace only app selection and add the finalizer."""
    import copy
    settings, users = copy.deepcopy(settings), copy.deepcopy(users)
    if settings.get('dont-chroot') is not False or settings.get('oem-setup') is not False:
        raise ValueError('Only the normal chrooted Kubuntu installer is supported, not OEM mode.')
    blocks = [b['exec'] for b in settings['sequence'] if 'exec' in b]
    if len(blocks) != 1:
        raise ValueError('Unexpected Calamares execution sequence.')
    jobs = blocks[0]
    if not all(name in jobs for name in ('users', 'displaymanager', 'networkcfg', 'packages', 'umount')):
        raise ValueError('Missing required installer jobs.')
    if not jobs.index('users') < jobs.index('packages') < jobs.index('umount'):
        raise ValueError('Unexpected installer account/package order.')
    hook = 'shellprocess@sentinel_provision'
    for block in settings['sequence']:
        if 'show' in block:
            block['show'] = [job for job in block['show'] if job != 'pkgselect']
    # These jobs can remove bundled apps or reintroduce Firefox after image setup.
    jobs[:] = [job for job in jobs if job not in {
        'contextualprocess@pkgselect_action', 'contextualprocess@pkgselect_snap_action', hook}]
    jobs.insert(jobs.index('umount'), hook)
    instances = [i for i in settings.get('instances', []) if i['id'] not in {
        'pkgselect_action', 'pkgselect_snap_action', 'sentinel_provision'}]
    instances.append({'id': 'sentinel_provision', 'module': 'shellprocess',
                      'config': 'shellprocess_sentinel_provision.conf'})
    settings['instances'] = instances
    settings['branding'] = 'sentinel'
    users['sudoersGroup'] = ''  # Calamares skips SetupSudoJob for an empty group.
    users['defaultGroups'] = [g for g in users['defaultGroups']
                              if (g if isinstance(g, str) else g['name']) not in {'sudo', 'wheel', 'adm', 'lpadmin', 'dip'}]
    users['setRootPassword'] = False
    users['doAutologin'] = False
    names = users.setdefault('user', {}).setdefault('forbidden_names', [])
    for name in ('sentinel', 'sentinel-dns'):
        if name not in names:
            names.append(name)
    return settings, users


def install_desktop(kit):
    assets = kit / 'assets'
    shared = Path('/usr/share/sentinel/desktop')
    shared.mkdir(parents=True, exist_ok=True)
    for folder in ('Rofi', 'Terminal'):
        shutil.copytree(assets / folder, shared / folder, dirs_exist_ok=True)
    logo = Path('/usr/share/sentinel/assets/Sentinel_Shield_Minimal_Solid-Visor.svg')
    logo.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(assets / logo.name, logo)
    # The source config already uses the packaged path, independent of account names.
    fetch = (shared / 'Terminal/Fastfetch/config.jsonc').read_text()
    expected = '/usr/share/sentinel/desktop/Terminal/Fastfetch/Sentinel_Shield.txt'
    if expected not in fetch:
        raise ValueError('Fastfetch logo path changed; review the source config before building.')
    write(shared / 'Terminal/Fastfetch/config.jsonc', fetch)
    skel = Path('/etc/skel/.config')
    for folder in ('rofi', 'fastfetch'):
        (skel / folder).mkdir(parents=True, exist_ok=True)
    shutil.copy2(shared / 'Rofi/sentinel-rofi.rasi', skel / 'rofi/config.rasi')
    shutil.copy2(shared / 'Terminal/Starship/starship.toml', skel / 'starship.toml')
    shutil.copy2(shared / 'Terminal/Fastfetch/config.jsonc', skel / 'fastfetch/config.jsonc')
    konsole = Path('/usr/share/konsole')
    shutil.copy2(shared / 'Terminal/Konsole/Sentinel.colorscheme', konsole / 'Sentinel.colorscheme')
    # One editable user profile; do not also register the same named system profile.
    local_konsole = Path('/etc/skel/.local/share/konsole')
    local_konsole.mkdir(parents=True, exist_ok=True)
    for name in ('Sentinel.profile', 'Sentinel.colorscheme'):
        shutil.copy2(shared / 'Terminal/Konsole' / name, local_konsole / name)
    remove_duplicate_profile()
    write(skel / 'konsolerc', '[Desktop Entry]\nDefaultProfile=Sentinel.profile\n\n[MainWindow]\nMenuBar=Disabled\nToolBarsMovable=Disabled\n')
    for package, (folder, prefix) in WALLPAPERS.items():
        dest = Path('/usr/share/wallpapers') / package
        (dest / 'contents/images').mkdir(parents=True, exist_ok=True)
        for size in RESOLUTIONS:
            shutil.copy2(assets / 'Screensavers' / folder / f'{prefix}-{size}.png', dest / 'contents/images' / f'{size}.png')
        write(dest / 'metadata.json', json.dumps({'KPlugin': {'Id': package,
              'Name': 'Sentinel Gradient' if package == 'SentinelGradient' else 'Sentinel Navy Waves',
              'Authors': [{'Name': 'Sentinel'}]}}, indent=2) + '\n')
    colors = Path('/usr/share/color-schemes/BreezeDark.colors')
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    cp.read(colors)
    if not cp.has_section('Colors:Selection'):
        raise ValueError('Breeze Dark color scheme is missing.')
    cp['General']['Name'] = 'Sentinel Dark'
    cp['Colors:Selection']['BackgroundNormal'] = '118,159,240'
    cp['Colors:Selection']['ForegroundNormal'] = '17,24,39'
    from io import StringIO
    text = StringIO()
    cp.write(text, space_around_delimiters=False)
    write('/usr/share/color-schemes/SentinelDark.colors', text.getvalue())
    globals_text = ('[General]\nColorScheme=SentinelDark\nAccentColor=118,159,240\n\n'
                    '[KDE]\nLookAndFeelPackage=org.sentinel.desktop\n\n[Icons]\nTheme=breeze-dark\n\n'
                    '[KDE Action Restrictions][$i]\naction/switch_user=false\n')
    write(skel / 'kdeglobals', globals_text)
    write(skel / 'plasmarc', '[Theme]\nname=breeze-dark\n')
    wallpaper = 'file:///usr/share/wallpapers/SentinelGradient/contents/images/3840x2160.png'
    write(skel / 'kscreenlockerrc', '[Greeter]\nTheme=org.kde.breeze.desktop\n\n'
          '[Greeter][Wallpaper][org.kde.image][General]\nImage=' + wallpaper + '\nFillMode=2\n')
    write('/usr/share/sddm/themes/breeze/theme.conf.user', '[General]\nbackground=' + wallpaper.removeprefix('file://') + '\ntype=image\n')
    kwin_path = Path('/etc/xdg/kwinrc')
    # sentinel-setup writes the user's initial setting during installation.
    # Freeze it only afterwards, or kwriteconfig6 can reject the setup write.
    write(kwin_path, kwin_defaults(kwin_path.read_text() if kwin_path.exists() else ''))
    write(skel / 'mimeapps.list', '[Default Applications]\n'
          'x-scheme-handler/http=google-chrome.desktop;\n'
          'x-scheme-handler/https=google-chrome.desktop;\n'
          'text/html=google-chrome.desktop;\ntext/plain=org.kde.kate.desktop;\n')
    write('/etc/profile.d/sentinel-editor.sh', 'export EDITOR=nvim\nexport VISUAL=nvim\n')
    bashrc = Path('/etc/skel/.bashrc')
    start, end = '# BEGIN SENTINEL TERMINAL', '# END SENTINEL TERMINAL'
    original = bashrc.read_text()
    if start in original:
        original = original[:original.index(start)] + original[original.index(end)+len(end):]
    write(bashrc, original.rstrip() + '\n\n' + start + '''
if [[ $- == *i* && -t 1 && ${SENTINEL_TERMINAL_BRANDING:-1} != 0 ]]; then
    command -v starship >/dev/null && eval "$(starship init bash)"
    command -v fastfetch >/dev/null && fastfetch --config "$HOME/.config/fastfetch/config.jsonc"
fi
''' + end + '\n')
    write('/usr/bin/sentinel-launcher', '#!/bin/sh\nexec /usr/bin/rofi -show drun -wayland-layer top "$@"\n', 0o755)
    write('/usr/share/applications/sentinel-launcher.desktop', launcher_entry(str(logo)))
    write(skel / 'kglobalshortcutsrc', '[services][sentinel-launcher.desktop]\n_launch=Ctrl+Space\n')
    write('/etc/xdg/kicker-extra-favoritesrc', '[General]\nPrepend=' + ';'.join(APPLICATIONS) + '\nIgnoreDefaults=true\n')
    theme = Path('/usr/share/plasma/look-and-feel/org.sentinel.desktop')
    write(theme / 'metadata.json', json.dumps({'KPlugin': {'Id': 'org.sentinel.desktop', 'Name': 'Sentinel Dark'},
          'KPackageStructure': 'Plasma/LookAndFeel', 'X-Plasma-API': '5.0'}, indent=2) + '\n')
    write(theme / 'contents/defaults', '[kdeglobals][General]\nColorScheme=SentinelDark\n\n'
          '[plasmarc][Theme]\nname=breeze-dark\n\n[konsolerc][Desktop Entry]\nDefaultProfile=Sentinel.profile\n\n'
          '[kwinrc][Plugins]\nblurEnabled=true\n\n[Wallpaper]\ndefaultWallpaperTheme=SentinelGradient\n')
    write(theme / 'contents/layouts/org.kde.plasma.desktop-layout.js', (kit / 'packaging/iso/panel.js').read_text())
    run('fc-cache', '-f')
    run('update-desktop-database', '/usr/share/applications')


def install_runtime(kit):
    target = Path('/usr/lib/sentinel-iso')
    if (kit / 'platform').is_dir():
        # Helpers and units belong to sentinel-platform, not unmanaged copies.
        shutil.copy2(kit / 'kit-manifest.json', target / 'input-manifest.json')
        for name in ('sentinel-first-boot', 'sentinel-dns', 'sentinel-dns-firewall', 'sentinel-session-guard'):
            run('systemctl', 'reenable', name + '.service')
        return
    shutil.copytree(kit / 'packaging/iso', target, dirs_exist_ok=True)
    shutil.copy2(kit / 'kit-manifest.json', target / 'input-manifest.json')
    for path in target.glob('*.py'):
        path.chmod(0o644)
    for name in ('provision_install', 'first_boot', 'session_guard', 'protected_apt', 'boot_security'):
        write(target / name, f'#!/bin/sh\nexec /usr/bin/python3 -I -B /usr/lib/sentinel-iso/{name}.py "$@"\n', 0o755)
    for source in (kit / 'packaging/iso/systemd').glob('*.service'):
        # Source checkouts can be 0664 under the developer's umask. Service
        # configuration must use explicit root-owned, read-only-to-others mode.
        write(Path('/etc/systemd/system') / source.name, source.read_text())
    write('/etc/systemd/system/sentineld.service.d/iso.conf', '[Unit]\nConditionPathExists=/etc/sentinel/iso-installed.json\nRequires=sentinel-first-boot.service\nAfter=sentinel-first-boot.service\n')
    write('/etc/systemd/system/sddm.service.d/sentinel.conf', '[Unit]\nRequires=sentinel-first-boot.service sentineld.service sentinel-session-guard.service\nAfter=sentinel-first-boot.service sentineld.service sentinel-session-guard.service\n')
    write('/etc/apt/apt.conf.d/99-sentinel-platform', '''DPkg::Pre-Install-Pkgs { "/usr/lib/sentinel-iso/protected_apt"; };
DPkg::Tools::Options::/usr/lib/sentinel-iso/protected_apt::Version "2";
DPkg::Tools::Options::/usr/lib/sentinel-iso/protected_apt::InfoFD "0";
DPkg::Post-Invoke { "/usr/lib/sentinel-iso/boot_security --refresh-if-installed"; };
''')
    for name in ('sentinel-first-boot', 'sentinel-dns', 'sentinel-dns-firewall', 'sentinel-session-guard'):
        run('systemctl', 'enable', name + '.service')  # Offline symlinks only; NEVER --now.


def configure_image(kit):
    from prepare import validate_debs
    validate_debs(kit / 'debs')
    # No shared credentials or account state may be copied into a distributable ISO.
    for name in ('adguard.toml', 'daemon.toml', 'iso-installed.json'):
        if (Path('/etc/sentinel') / name).exists():
            raise ValueError(f'Unexpected personal/provisioned state in image: {name}')
    os.environ['DEBIAN_FRONTEND'] = 'noninteractive'
    # Calamares compatibility is checked before APT makes any changes.
    try:
        import yaml
    except ImportError as exc:
        raise ValueError('Inside Cubic, install python3-yaml first, then rerun image setup.') from exc
    settings_path, users_path = Path('/etc/calamares/settings.conf'), Path('/etc/calamares/modules/users.conf')
    settings, users = calamares_config(yaml.safe_load(settings_path.read_text()), yaml.safe_load(users_path.read_text()))
    policy_path = Path('/usr/sbin/policy-rc.d')
    old_policy = policy_path.read_bytes() if policy_path.exists() else None
    old_mode = policy_path.stat().st_mode & 0o777 if policy_path.exists() else None
    write(policy_path, '#!/bin/sh\nexit 101\n', 0o755)
    try:
        disable_media_source()
        run('apt-get', 'update')
        run('apt-get', 'install', '-y', '--no-remove', 'python3-yaml', 'ca-certificates', 'gnupg', 'curl', 'software-properties-common')
        run('add-apt-repository', '-y', 'universe')
        run('add-apt-repository', '-y', 'multiverse')
        run('dpkg', '--add-architecture', 'i386')
        run('curl', '--fail', '--show-error', '--location', '--proto', '=https',
            '--output', '/tmp/sentinel-google-key.pub', 'https://dl.google.com/linux/linux_signing_key.pub')
        Path('/etc/apt/keyrings').mkdir(exist_ok=True)
        run('gpg', '--batch', '--yes', '--dearmor', '--output', '/etc/apt/keyrings/google-chrome.gpg', '/tmp/sentinel-google-key.pub')
        write('/etc/apt/sources.list.d/google-chrome.list', 'deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main\n')
        run('apt-get', 'update')
        # No autoremove: do not discard image dependencies while customizing.
        installed = subprocess.check_output(['dpkg-query', '-W', '-f', '${binary:Package}\t${db:Status-Status}\n'], text=True)
        purge_stock_packages(installed)
        run('apt-get', 'install', '-y', '--no-remove', 'google-chrome-stable', 'steam-installer', 'neovim', 'rofi',
            'kate', 'konsole', 'git', 'unzip', 'wl-clipboard', 'nftables', 'apache2-utils', 'python3-yaml',
            'libgl1-mesa-dri:i386', 'mesa-vulkan-drivers:i386', 'libgl1:i386', 'flatpak', 'breeze', 'breeze-icon-theme',
            'plasma-workspace', 'sddm', kit / 'vendor/fastfetch.deb', *sorted((kit / 'debs').glob('*.deb')),
            *sorted((kit / 'platform').glob('*.deb')))
        # Explicit Breeze/KDE installs may reintroduce recommended wallpapers.
        # Final cleanup runs after all package installation, before seeding ours.
        installed = subprocess.check_output(['dpkg-query', '-W', '-f', '${binary:Package}\t${db:Status-Status}\n'], text=True)
        purge_stock_packages(installed)
        audit = subprocess.check_output(['dpkg', '--audit'], text=True)
        if audit.strip():
            raise ValueError('Package configuration is incomplete: ' + audit)
        run('flatpak', 'remote-add', '--system', '--if-not-exists', 'flathub',
            'https://dl.flathub.org/repo/flathub.flatpakrepo')
        run('flatpak', 'update', '--system', '--appstream', '-y', 'flathub')
        # These are first-install defaults, not changes to a developer's machine or saved lock state.
        write('/etc/sentinel/settings.toml',
              (kit / 'packaging/iso/fresh-install-settings.toml').read_text())
        vendor = kit / 'vendor'
        for src, dest in [('AdGuardHome', '/opt/AdGuardHome/AdGuardHome'), ('starship', '/usr/local/bin/starship')]:
            if not (kit / 'platform').is_dir():
                run('install', '-D', '-o', 'root', '-g', 'root', '-m', '0755', vendor / src, dest)
        run('install', '-D', '-m', '0644', vendor / 'AdGuardHome-LICENSE.txt', '/usr/share/doc/sentinel-iso/AdGuardHome-LICENSE.txt')
        run('install', '-D', '-m', '0644', vendor / 'starship-LICENSE.txt', '/usr/share/doc/sentinel-iso/starship-LICENSE.txt')
        font_dir = Path('/usr/share/fonts/truetype/jetbrainsmono-nerd')
        font_dir.mkdir(parents=True, exist_ok=True)
        for font in (vendor / 'fonts').iterdir():
            shutil.copy2(font, font_dir / font.name)
        install_desktop(kit)
        install_branding(kit)
        install_runtime(kit)
        install_live_branding(kit)
        for directory in (Path('/usr/lib/sentinel-iso'), Path('/usr/share/sentinel/desktop')):
            for path in directory.rglob('*'):
                path.chmod(0o755 if path.is_dir() or (directory.name == 'sentinel-iso' and path.name in {
                    'provision_install', 'first_boot', 'session_guard', 'protected_apt', 'boot_security'}) else 0o644)
        write(settings_path, yaml.safe_dump(settings, sort_keys=False))
        write(users_path, yaml.safe_dump(users, sort_keys=False))
        # Kubuntu's original cleanup uses plain rm and fails after disabling the
        # media source in Cubic. Keep cleanup, but make missing files harmless.
        write('/etc/calamares/modules/shellprocess_rmcdrom.conf',
              yaml.safe_dump(media_cleanup_config(), sort_keys=False))
        write('/etc/calamares/modules/shellprocess_sentinel_provision.conf', yaml.safe_dump({
            'dontChroot': False, 'timeout': 600, 'verbose': False,
            'script': ['/usr/lib/sentinel-iso/provision_install --user "${USER}"'],
            'i18n': {'name': 'Set up Sentinel'}}, sort_keys=False))
        # No embedded account, AdGuard password or installed-system marker in the ISO.
        for name in ('adguard.toml', 'daemon.toml', 'iso-installed.json'):
            if (Path('/etc/sentinel') / name).exists():
                raise ValueError(f'Unexpected personal/provisioned state in image: {name}')
        write('/usr/lib/sentinel-iso/image-built.json', json.dumps({'version': 1, 'source': 'Cubic',
              'package_version': json.loads((kit / 'kit-manifest.json').read_text())['package_version']}) + '\n')
        # Keep networking additions separate from the customizable app list.
        run('apt-get', 'install', '-y', '--no-remove', 'network-manager', 'plasma-nm')
        run('apt-get', 'clean')
        # Calamares copies this path even when the installer runs offline.
        # Reintroduce it only after build-time APT work, with Enabled: no.
        stage_installer_media_source()
        run('systemctl', 'enable', 'NetworkManager.service')
    finally:
        if old_policy is None:
            policy_path.unlink(missing_ok=True)
        else:
            policy_path.write_bytes(old_policy)
            policy_path.chmod(old_mode)
    print('Image configured. Cubic can now generate the TEST ISO. Do not remove Sentinel packages on the Packages page.')
    print('Installed-system credentials and restrictions are provisioned by Calamares, not in the live session.')
