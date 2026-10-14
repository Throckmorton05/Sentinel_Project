#!/usr/bin/python3
"""Gate the installed display manager on DNS and configured model health."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tomllib
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import run, write

APIS = ('sentinel-opennsfw.service', 'sentinel-drawn.service', 'sentinel-hentai.service', 'sentinel-nudenet.service')


def model_health():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for port in range(8000, 8004):
        with opener.open(f'http://127.0.0.1:{port}/health', timeout=3) as response:
            data = json.loads(response.read(65536))
        if data.get('status') != 'ok' or data.get('model_loaded') is not True:
            raise ValueError(f'Model service {port} is not ready.')


def main(verify_only=False):
    if os.geteuid() != 0:
        raise ValueError('Protected startup requires root.')
    marker = Path('/etc/sentinel/iso-installed.json')
    installed = json.loads(marker.read_text())
    deadline = time.monotonic() + 110
    while True:
        try:
            run('/usr/bin/sentinel-setup', '--check', stdout=subprocess.DEVNULL)
            # A separate, bounded process completes initial DNS policy before sentineld starts.
            code = ('from sentinel.daemon.client import get_dns_status; '
                    'result=get_dns_status()["application"]; assert result["status"] == "applied", result.get("error")') if verify_only else (
                    'from sentinel.daemon.operations.dns_ops import initialize_dns,apply_dns; '
                    'initialize_dns(); result=apply_dns(); assert result["status"] == "applied", result.get("error")')
            remaining = max(1, min(90, deadline - time.monotonic()))
            run('/usr/lib/sentinel/venv/bin/python', '-I', '-B', '-c', code, timeout=remaining)
            break
        except (OSError, ValueError, subprocess.SubprocessError):
            if time.monotonic() >= deadline:
                raise ValueError('Local DNS protection failed to initialize; desktop login remains disabled.')
            time.sleep(2)
    settings = tomllib.loads(Path('/etc/sentinel/settings.toml').read_text())
    if settings.get('image_filtering') is True:
        deadline = time.monotonic() + 110
        run('systemctl', 'start', *APIS)
        while True:
            try:
                model_health()
                break
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    raise ValueError('Image models are not healthy; desktop login remains disabled.')
                time.sleep(2)
    installed['ready'] = True
    write(marker, json.dumps(installed, indent=2) + '\n')
    print('Sentinel installed-system startup checks passed.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    try:
        main(args.verify_only)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise SystemExit(f'Sentinel protected startup failed: {exc}') from exc
