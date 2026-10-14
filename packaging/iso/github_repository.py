#!/usr/bin/python3
"""Build signed flat APT assets for a fixed GitHub Release channel (no uploads)."""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import PACKAGES, digest, plain_file
from prepare import validate_debs, deb_fields
from repository_config import github_url, public_key, verify_configuration

MAX_ASSET = 2 * 1024**3  # GitHub requires every asset to be UNDER 2 GiB.
METADATA = ('Packages', 'Packages.gz', 'Release', 'Release.gpg', 'InRelease')


def authenticated_index(directory, key):
    """Verify the prior signed index and every referenced local package."""
    directory = Path(directory)
    if any(p.is_symlink() for p in directory.rglob('*')):
        raise ValueError('Refusing symlinks in a GitHub channel snapshot.')
    with tempfile.TemporaryDirectory(prefix='sentinel-channel-verify-') as folder:
        keyring = Path(folder) / 'archive.gpg'
        keyring.write_bytes(key)
        release = subprocess.check_output(['gpgv', '--keyring', str(keyring), '--output', '-',
                                          str(plain_file(directory / 'InRelease'))]).decode()
    hashes, in_hashes = {}, False
    for line in release.splitlines():
        if not line.startswith(' '):
            in_hashes = line == 'SHA256:'
        elif in_hashes:
            fields = line.split()
            if len(fields) == 3:
                hashes[fields[2].removeprefix('./')] = fields[:2]
    index = plain_file(directory / 'Packages')
    expected = hashes.get('Packages')
    if not expected or digest(index) != expected[0] or index.stat().st_size != int(expected[1]):
        raise ValueError('Package index does not match the signed channel metadata.')
    packages = {}
    for stanza in index.read_text().strip().split('\n\n'):
        fields = dict(line.split(': ', 1) for line in stanza.splitlines() if ': ' in line and not line.startswith(' '))
        name = fields.get('Filename', '')
        if (fields.get('Package') not in PACKAGES | {'sentinel-platform'}
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+~:-]*\.deb', name)
                or name in packages):
            raise ValueError('Unexpected package path in the flat channel index.')
        path = plain_file(directory / name)
        if path.stat().st_size >= MAX_ASSET:
            raise ValueError('GitHub release asset must be under 2 GiB: ' + name)
        if digest(path) != fields.get('SHA256') or path.stat().st_size != int(fields['Size']):
            raise ValueError('Channel package checksum mismatch: ' + name)
        packages[name] = path
    if set(packages) != {p.name for p in directory.glob('*.deb')}:
        raise ValueError('Channel snapshot contains unindexed DEBs.')
    # Confirm compressed index too; clients may acquire it instead of Packages.
    gz = plain_file(directory / 'Packages.gz')
    expected = hashes.get('Packages.gz')
    if not expected or digest(gz) != expected[0] or gz.stat().st_size != int(expected[1]):
        raise ValueError('Compressed index does not match signed channel metadata.')
    if gzip.decompress(gz.read_bytes()) != index.read_bytes():
        raise ValueError('Compressed and uncompressed indexes disagree.')
    return packages


def build_channel(debs, platform, config, output, previous=None, valid_days=30):
    metadata = verify_configuration(config)
    if metadata.get('layout') != 'github-flat':
        raise ValueError('Create repository configuration with --github-repo first.')
    packages, version = validate_debs(debs)
    if deb_fields(plain_file(platform)) != {'Package': 'sentinel-platform', 'Version': version, 'Architecture': 'amd64'}:
        raise ValueError('Platform package must match the five application packages.')
    if not 1 <= valid_days <= 90:
        raise ValueError('Channel expiry must be 1–90 days.')
    key = public_key(metadata['fingerprint'])
    if key != (Path(config) / 'sentinel-archive-keyring.gpg').read_bytes():
        raise ValueError('Exported signing key differs from the reviewed platform trust; regenerate public config.')
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Choose a new channel output directory.')
    retained = {}
    if previous:
        previous = Path(previous).resolve()
        if previous in output.parents:
            raise ValueError('Output must be outside the previous channel directory.')
        retained = authenticated_index(previous, key)
    for path in [*packages.values(), Path(platform)]:
        if path.stat().st_size >= MAX_ASSET:
            raise ValueError('GitHub asset exceeds its per-file limit: ' + str(path))
    output.mkdir(parents=True)
    for name, path in retained.items():
        shutil.copyfile(path, output / name)
    for package, path in [*packages.items(), ('sentinel-platform', Path(platform))]:
        architecture = deb_fields(path)['Architecture']
        target = output / f'{package}_{version}_{architecture}.deb'
        if target.exists() and digest(target) != digest(path):
            raise ValueError('Never replace a different package under the same version: ' + target.name)
        shutil.copyfile(path, target)
    if len(list(output.glob('*.deb'))) + len(METADATA) + 1 > 1000:
        raise ValueError('Channel would exceed GitHub’s 1000-asset release limit; review retention.')
    index = subprocess.check_output(['apt-ftparchive', 'packages', '.'], cwd=output)
    index = index.replace(b'Filename: ./', b'Filename: ')
    (output / 'Packages').write_bytes(index)
    (output / 'Packages.gz').write_bytes(gzip.compress(index, mtime=0))
    options = {'Origin': 'Sentinel', 'Label': 'Sentinel Beta', 'Suite': './', 'Codename': './',
               'Architectures': 'amd64', 'Acquire-By-Hash': 'no',
               'Description': 'Sentinel Linux beta updates for Kubuntu 26.04 amd64'}
    command = ['apt-ftparchive']
    for field, value in options.items():
        command.extend(['-o', f'APT::FTPArchive::Release::{field}={value}'])
    release = subprocess.check_output([*command, 'release', '.'], cwd=output)
    expires = format_datetime(datetime.now(timezone.utc) + timedelta(days=valid_days), usegmt=True)
    (output / 'Release').write_bytes(f'Valid-Until: {expires}\n'.encode() + release)
    for mode, name in (('--detach-sign', 'Release.gpg'), ('--clearsign', 'InRelease')):
        subprocess.run(['gpg', '--local-user', metadata['fingerprint'], '--digest-algo', 'SHA256',
                        '--output', str(output / name), mode, str(output / 'Release')], check=True)
    (output / 'sentinel-archive-keyring.gpg').write_bytes(key)
    authenticated_index(output, key)
    metadata.update(package_version=version, valid_until=expires,
                    sha256={p.name: digest(p) for p in sorted(output.iterdir()) if p.is_file()})
    (output / 'channel.json').write_text(json.dumps(metadata, indent=2) + '\n')
    print('Signed GitHub channel ready: ' + str(output))
    print('Fixed APT URL: ' + github_url(metadata['github_repo'], metadata['github_tag']) + '/')
    print('Metadata expires ' + expires + '; refresh signatures before expiry.')
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--debs', type=Path, required=True)
    parser.add_argument('--platform', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().parents[2] / 'configs/updates')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--previous', type=Path)
    parser.add_argument('--valid-days', type=int, default=30)
    args = parser.parse_args()
    try:
        build_channel(args.debs, args.platform, args.config, args.output, args.previous, args.valid_days)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        raise SystemExit('GitHub channel build stopped: ' + str(exc)) from exc
