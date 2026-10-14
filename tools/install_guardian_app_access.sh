#!/usr/bin/env bash
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then
    echo 'Run this installer with sudo.' >&2
    exit 1
fi
[[ -x /usr/lib/sentinel/venv/bin/python ]] || { echo 'Install the Sentinel runtime first.' >&2; exit 1; }
# Caller must install the updated wheel into the runtime first.
/usr/lib/sentinel/venv/bin/python -I -c 'import sentinel.guardian_apps.manager, sentinel.guardian_apps.launch, sentinel.guardian_apps.recovery'
installer_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
install -d -m 0755 /etc/systemd/system /var/lib/sentinel/guardian-desktop \
    /usr/local/share/applications /etc/xdg /etc/sentinel
install -m 0644 "${installer_dir}/../packaging/systemd/sentinel-guardian-recovery.socket" \
    "${installer_dir}/../packaging/systemd/sentinel-guardian-recovery@.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sentinel-guardian-recovery.socket
install -d -m 0755 /usr/share/applications
cat > /usr/share/applications/sentinel-guardian-access.desktop <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Sentinel Guardian App Access
Comment=Guardian PIN required to manage application access
Exec=/usr/lib/sentinel/venv/bin/python -I -m sentinel.guardian_apps.manager
Icon=/usr/share/sentinel/assets/Sentinel_Shield.svg
Terminal=false
Categories=Settings;Security;
DESKTOP
chmod 0644 /usr/share/applications/sentinel-guardian-access.desktop
echo 'Guardian App Access and independent recovery installed. Restart sentineld before testing.'
