#!/usr/bin/env bash
# Install prebuilt signed assets; rebuilding is optional and requires signing keys.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
    echo 'Run with sudo: sudo bash tools/install_activity_assets.sh' >&2
    exit 1
fi
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
for extension in activity safe_steam safe_crunchyroll; do
    destination=${extension//_/-}
    source_dir="$project_root/sentinel/$extension/browser_distribution"
    case "$extension" in
        activity) crx=sentinel_activity.crx ;;
        safe_steam) crx=browser_extension.crx ;;
        safe_crunchyroll) crx=safe_crunchyroll.crx ;;
    esac
    install -d -o root -g root -m 755 "/usr/share/sentinel/extensions/$destination"
    install -o root -g root -m 644 "$source_dir/$crx" "$source_dir/updates.xml" "/usr/share/sentinel/extensions/$destination/"
done
echo 'Sentinel extension assets installed. Restart sentineld and update extensions in Chrome.'
