#!/usr/bin/env bash
set -euo pipefail
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
exec /usr/bin/python3 "$project_root/packaging/debian/build.py" "$@"
