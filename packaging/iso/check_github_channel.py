#!/usr/bin/python3
"""Verify public GitHub APT indexes and download the tiny meta-DEB without installation."""
import argparse
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from repository_config import verify_configuration


def check(config):
    config = Path(config).resolve()
    metadata = verify_configuration(config)
    if metadata.get('layout') != 'github-flat':
        raise ValueError('Expected GitHub flat repository configuration.')
    with tempfile.TemporaryDirectory(prefix='sentinel-public-apt-check-') as folder:
        root = Path(folder)
        for name in ('lists/partial', 'archives/partial', 'empty'):
            (root / name).mkdir(parents=True)
        (root / 'status').touch()
        source = (config / 'sentinel.sources').read_text().replace(
            '/usr/share/keyrings/sentinel-archive-keyring.gpg', str(config / 'sentinel-archive-keyring.gpg'))
        (root / 'sentinel.sources').write_text(source)
        (root / 'apt.conf').write_text(
            f'Dir::Etc::parts "{root / "empty"}";\nDir::Etc::main "{root / "no-main"}";\n')
        env = {**os.environ, 'APT_CONFIG': str(root / 'apt.conf'), 'DEBIAN_FRONTEND': 'noninteractive'}
        options = ['-o', 'APT::Update::Error-Mode=any', '-o', 'Acquire::Retries=2',
                   '-o', 'Acquire::https::Timeout=30',
                   '-o', 'APT::Sandbox::User=' + pwd.getpwuid(os.getuid()).pw_name,
                   '-o', 'Dir::Etc::sourcelist=' + str(root / 'sentinel.sources'),
                   '-o', 'Dir::Etc::sourceparts=' + str(root / 'empty'),
                   '-o', 'Dir::Etc::preferences=' + str(config / 'sentinel.pref'),
                   '-o', 'Dir::Etc::preferencesparts=' + str(root / 'empty'),
                   '-o', 'Dir::State::status=' + str(root / 'status'),
                   '-o', 'Dir::State::lists=' + str(root / 'lists'),
                   '-o', 'Dir::Cache::archives=' + str(root / 'archives'),
                   '-o', 'Dir::Cache::pkgcache=', '-o', 'Dir::Cache::srcpkgcache=']
        subprocess.run(['apt-get', *options, 'update'], cwd=root, env=env, check=True, timeout=180)
        subprocess.run(['apt-cache', *options, 'policy', 'sentinel', 'sentinel-platform'],
                       cwd=root, env=env, check=True, timeout=30)
        subprocess.run(['apt-get', *options, 'download', 'sentinel'], cwd=root, env=env, check=True, timeout=180)
        packages = list(root.glob('sentinel_*.deb'))
        if len(packages) != 1:
            raise ValueError('Expected one verified Sentinel meta-package download.')
        version = subprocess.check_output(['dpkg-deb', '-f', str(packages[0]), 'Version'], text=True).strip()
        print(f'Public channel signature/index/download checks passed for {version}. No packages installed.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().parents[2] / 'configs/updates')
    args = parser.parse_args()
    try:
        check(args.config)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise SystemExit('Public channel check failed: ' + str(exc)) from exc
