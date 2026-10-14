#!/usr/bin/env bash
# Install only public assets; never copy the private signing key.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
    echo 'Run with sudo: sudo bash tools/install_catalog_assets.sh' >&2
    exit 1
fi
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source_dir="$project_root/sentinel/extension_catalog/browser_distribution"
newtab_dir="$project_root/sentinel/newtab/browser_distribution"
for asset in sentinel_extensions.crx updates.xml metadata.json; do
    if [[ ! -f "$source_dir/$asset" ]]; then
        echo 'First run: .venv/bin/python tools/build_catalog_extension.py' >&2
        exit 1
    fi
done
for asset in sentinel_newtab.crx updates.xml metadata.json; do
    if [[ ! -f "$newtab_dir/$asset" ]]; then
        echo 'First run: .venv/bin/python tools/build_catalog_extension.py' >&2
        exit 1
    fi
done
install -d -o root -g root -m 755 /usr/share/sentinel/browser \
    /usr/share/sentinel/extensions/catalog /usr/share/sentinel/extensions/newtab
install -o root -g root -m 644 "$project_root/configs/browser/approved_extensions.toml" /usr/share/sentinel/browser/approved_extensions.toml
install -o root -g root -m 644 "$source_dir/sentinel_extensions.crx" "$source_dir/updates.xml" "$source_dir/metadata.json" /usr/share/sentinel/extensions/catalog/
install -o root -g root -m 644 "$newtab_dir/sentinel_newtab.crx" "$newtab_dir/updates.xml" "$newtab_dir/metadata.json" /usr/share/sentinel/extensions/newtab/
echo 'Sentinel Extensions installed. Restart sentineld and native Chrome.'
