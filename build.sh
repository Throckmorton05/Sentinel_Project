#!/bin/bash
# Host: bash build.sh prepare --debs DIR
# Cubic: bash /root/sentinel-iso-kit/build.sh image
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/python3 -I -B "$SCRIPT_DIR/packaging/iso/build.py" "$@"
