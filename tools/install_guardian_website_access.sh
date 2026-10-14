#!/usr/bin/env bash
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then
    echo 'Run this installer with sudo.' >&2
    exit 1
fi
[[ -x /usr/lib/sentinel/venv/bin/python ]] || { echo 'Install the Sentinel runtime first.' >&2; exit 1; }
/usr/lib/sentinel/venv/bin/python -I -c 'import sentinel.guardian_websites.manager'
install -d -m 0755 /usr/share/applications
cat > /usr/share/applications/sentinel-guardian-websites.desktop <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Sentinel Guardian Website Access
Comment=Guardian PIN required to manage website access
Exec=/usr/lib/sentinel/venv/bin/python -I -m sentinel.guardian_websites.manager
Icon=/usr/share/sentinel/assets/Sentinel_Shield.svg
Terminal=false
Categories=Settings;Security;
DESKTOP
chmod 0644 /usr/share/applications/sentinel-guardian-websites.desktop
echo 'Guardian Website Access installed. Restart sentineld before testing.'
