"""Apply installer-only fixes to an already configured Cubic image."""
from pathlib import Path
import json
import subprocess

from apt_sources import stage_installer_media_source, media_cleanup_config
from common import plain_file, write
from installer_branding import install_branding
from live_branding import install_live_branding


def apply_live_fixes(kit):
    import yaml
    # These dependencies must exist in the image, not merely on the build host.
    required = ('network-manager', 'plasma-nm', 'plasma-workspace')
    for name in required:
        state = subprocess.check_output(
            ['dpkg-query', '-W', '-f', '${db:Status-Status}', name], text=True).strip()
        if state != 'installed':
            raise ValueError('Inside Cubic install network-manager plasma-nm plasma-workspace, then retry.')
    for name in ('/usr/bin/nmcli', '/usr/bin/plasmawindowed', '/usr/lib/sentinel/venv/bin/python'):
        if not Path(name).is_file():
            raise ValueError('Required live-session executable missing: ' + name)
    subprocess.run(['/usr/lib/sentinel/venv/bin/python', '-I', '-c',
                    'from PyQt6.QtWidgets import QApplication'], check=True, timeout=30)
    # Repository filenames vary between customized images. This installer-only
    # repair does not use APT or alter online repository configuration.
    plain_file('/usr/share/keyrings/ubuntu-archive-keyring.gpg')
    # Validate wrapper and installer discovery before changing the APT source.
    install_live_branding(kit)
    install_branding(kit)
    stage_installer_media_source()
    write('/etc/calamares/modules/shellprocess_rmcdrom.conf',
          yaml.safe_dump(media_cleanup_config(), sort_keys=False))
    subprocess.run(['systemctl', 'enable', 'NetworkManager.service'], check=True)
    write('/usr/lib/sentinel-iso/live-repair.json', json.dumps({
        'revision': '20261015-a-fullscreen', 'welcome_after_plasma': True,
        'welcome_fullscreen': True,
        'media_source_disabled': True}) + '\n')
    print('Live repair applied. Full-screen welcome runs over Plasma; Wi-Fi is available; media copy input is disabled and present.')
    print('Boot-menu branding is separate: run build.sh boot-branding against Cubic custom-disk on the HOST.')
