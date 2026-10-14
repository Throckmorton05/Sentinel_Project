#!/usr/bin/python3
"""One-time maintainer bootstrap for an already installed pre-repository Sentinel VM."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import stat
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import write
from platform_postinst import protected, repository
from repository_config import verify_configuration


def bootstrap(config, fingerprint):
    if os.geteuid() != 0:
        raise ValueError('Run from an authorized maintenance root shell or a live-ISO target chroot; never through sen.')
    marker = json.loads(protected('/etc/sentinel/iso-installed.json').read_text())
    release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    if (not marker.get('protected_user') or release.get('ID', '').strip('"') != 'ubuntu'
            or release.get('VERSION_ID', '').strip('"') != '26.04'
            or subprocess.check_output(['dpkg', '--print-architecture'], text=True).strip() != 'amd64'):
        raise ValueError('Expected an installed Sentinel Kubuntu 26.04 amd64 target.')
    policy_path = protected('/etc/sentinel/iso-package-policy.json')
    policy = json.loads(policy_path.read_text())
    # Snapshot user-supplied public inputs into a root-private directory before
    # verifying them. A writable source cannot swap the key after validation.
    with tempfile.TemporaryDirectory(prefix='sentinel-update-trust-', dir='/run') as temporary:
        snapshot = Path(temporary)
        for name in ('repository.json', 'sentinel.sources', 'sentinel.pref', 'sentinel-archive-keyring.gpg'):
            descriptor = os.open(Path(config) / name, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError('Expected regular public repository input: ' + name)
                data = stream.read(1024 * 1024 + 1)
            if not data or len(data) > 1024 * 1024:
                raise ValueError('Missing or oversized public repository input: ' + name)
            (snapshot / name).write_bytes(data)
        verify_configuration(snapshot, fingerprint)
        repository(snapshot)
    # Only this named distribution package is added, not arbitrary local archives.
    policy['allowed_install'] = sorted(set(policy['allowed_install']) | {'sentinel-platform'})
    write(policy_path, json.dumps(policy, indent=2) + '\n')
    env = {**os.environ, 'DEBIAN_FRONTEND': 'noninteractive', 'NEEDRESTART_MODE': 'l'}
    options = ['-o', 'APT::Update::Error-Mode=any', '-o', 'DPkg::Lock::Timeout=120',
               '-o', 'Dpkg::Options::=--force-confdef', '-o', 'Dpkg::Options::=--force-confold']
    subprocess.run(['apt-get', *options, 'update'], env=env, check=True)
    subprocess.run(['apt-get', *options, 'install', '-y', '--no-remove', 'sentinel-platform'], env=env, check=True)
    print('Signed updates enabled. Reboot, then use sen update-sys for subsequent releases.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--fingerprint', required=True, help='Fingerprint obtained independently from the developer keyring')
    args = parser.parse_args()
    try:
        bootstrap(args.config, args.fingerprint)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        raise SystemExit(f'Update bootstrap stopped: {exc}') from exc
