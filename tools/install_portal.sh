#!/usr/bin/env bash
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then
    echo 'Run this installer with sudo.' >&2
    exit 1
fi
[[ -x /usr/lib/sentinel/venv/bin/python ]] || { echo 'Install the Sentinel runtime first.' >&2; exit 1; }
/usr/lib/sentinel/venv/bin/python -I -c 'import sentinel.portal.main'
install -d -m 0755 /usr/share/applications /usr/share/sentinel/assets
portal_icon=$(/usr/lib/sentinel/venv/bin/python -I -c 'from sentinel.portal.theme import LOGO; print(LOGO)')
install -m 0644 "$portal_icon" /usr/share/sentinel/assets/sentinel-portal.svg
cat > /usr/share/applications/sentinel-portal.desktop <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Sentinel Portal
Comment=Manage Sentinel protections and focused time
Exec=/usr/lib/sentinel/venv/bin/python -I -m sentinel.portal.main
Icon=/usr/share/sentinel/assets/sentinel-portal.svg
Terminal=false
Categories=Settings;Security;
StartupWMClass=sentinel-portal
DESKTOP
chmod 0644 /usr/share/applications/sentinel-portal.desktop
if command -v update-desktop-database >/dev/null; then
    update-desktop-database /usr/share/applications
fi
echo 'Sentinel Portal installed. Restart sentineld before opening it.'
