#!/usr/bin/env bash
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then
    echo 'Run this installer with sudo.' >&2
    exit 1
fi
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
[[ -x /usr/lib/sentinel/venv/bin/python ]] || { echo 'Install the Sentinel runtime first.' >&2; exit 1; }
getent group sentinel >/dev/null || { echo 'The Sentinel group must already exist.' >&2; exit 1; }
if ! getent passwd sentinel >/dev/null; then
    useradd --system --gid sentinel --home-dir /var/lib/sentinel/image-filter --shell /usr/sbin/nologin sentinel
fi
install -d -o sentinel -g sentinel -m 0700 /var/lib/sentinel/image-filter
install -d -m 0755 /etc/systemd/system
for name in sentinel-opennsfw sentinel-drawn sentinel-hentai sentinel-nudenet sentineld; do
    install -o root -g root -m 0644 "$project_root/packaging/systemd/$name.service" /etc/systemd/system/
done
systemctl daemon-reload
# sentineld owns startup from the persisted setting; APIs must not start
# independently at boot. Disabling units does not stop currently running APIs.
systemctl disable sentinel-opennsfw.service sentinel-drawn.service sentinel-hentai.service sentinel-nudenet.service
echo 'Image API services installed. Restart sentineld to apply the saved Image Filtering setting.'
