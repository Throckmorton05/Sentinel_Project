#!/usr/bin/python3
"""Build a signed, static HTTPS APT repository; publishing bytes is a separate step."""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import gzip
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import PACKAGES, digest, plain_file
from prepare import deb_fields, validate_debs
from repository_config import public_key


def validate_previous(previous, key):
    """Do not re-sign tampered pools or unindexed DEBs from an old directory."""
    with tempfile.TemporaryDirectory(prefix='sentinel-repo-verify-') as folder:
        keyring = Path(folder) / 'archive.gpg'
        keyring.write_bytes(key)
        release = subprocess.check_output(['gpgv', '--keyring', str(keyring), '--output', '-',
                                          str(previous / 'dists/sentinel-beta/InRelease')]).decode()
    checksum = None
    in_hashes = False
    for line in release.splitlines():
        if not line.startswith(' '):
            in_hashes = line == 'SHA256:'
        elif in_hashes:
            fields = line.split()
            if len(fields) == 3 and fields[2] == 'main/binary-amd64/Packages':
                checksum = fields
    index = plain_file(previous / 'dists/sentinel-beta/main/binary-amd64/Packages')
    if not checksum or digest(index) != checksum[0] or index.stat().st_size != int(checksum[1]):
        raise ValueError('Previous repository index does not match signed metadata.')
    referenced = set()
    for stanza in index.read_text().strip().split('\n\n'):
        fields = dict(line.split(': ', 1) for line in stanza.splitlines() if ': ' in line and not line.startswith(' '))
        relative = Path(fields.get('Filename', ''))
        if (fields.get('Package') not in PACKAGES | {'sentinel-platform'}
                or relative.is_absolute() or '..' in relative.parts
                or relative.parts[:1] != ('pool',) or relative.suffix != '.deb'):
            raise ValueError('Unexpected package in previous signed repository.')
        package = plain_file(previous / relative)
        if digest(package) != fields.get('SHA256') or package.stat().st_size != int(fields['Size']):
            raise ValueError('Previous repository package checksum mismatch: ' + str(relative))
        referenced.add(relative.as_posix())
    if referenced != {p.relative_to(previous).as_posix() for p in (previous / 'pool').rglob('*.deb')}:
        raise ValueError('Previous repository contains unindexed package files.')
    for path in (previous / 'dists/sentinel-beta/main/binary-amd64/by-hash/SHA256').glob('*'):
        if digest(plain_file(path)) != path.name:
            raise ValueError('Previous immutable index checksum mismatch.')


def build_repository(debs, platform, fingerprint, output, previous=None, valid_days=30):
    packages, version = validate_debs(debs)
    fields = deb_fields(plain_file(platform))
    if fields != {'Package': 'sentinel-platform', 'Version': version, 'Architecture': 'amd64'}:
        raise ValueError('Platform package must match the five core packages exactly.')
    if not 1 <= valid_days <= 90:
        raise ValueError('Repository expiry must be between 1 and 90 days.')
    key = public_key(fingerprint)
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Choose a new repository output directory for each publication.')
    if previous:
        previous = Path(previous).resolve()
        if previous == output or previous in output.parents:
            raise ValueError('Repository output must be outside the previous repository.')
        if any(p.is_symlink() for p in previous.rglob('*')):
            raise ValueError('Previous repository must not contain symlinks.')
        validate_previous(previous, key)
    output.mkdir(parents=True)
    # Keep old immutable pools and by-hash indexes for clients using an earlier InRelease.
    if previous:
        shutil.copytree(previous / 'pool', output / 'pool')
        relative = 'dists/sentinel-beta/main/binary-amd64/by-hash/SHA256'
        if (previous / relative).is_dir():
            shutil.copytree(previous / relative, output / relative)
    pool = output / 'pool/main/s/sentinel'
    pool.mkdir(parents=True, exist_ok=True)
    for name, path in [*packages.items(), ('sentinel-platform', Path(platform))]:
        arch = deb_fields(path)['Architecture']
        destination = pool / f'{name}_{version}_{arch}.deb'
        if destination.exists() and digest(destination) != digest(path):
            raise ValueError('Never replace different bytes under an existing package version: ' + destination.name)
        shutil.copyfile(path, destination)
    suite = output / 'dists/sentinel-beta'
    index_dir = suite / 'main/binary-amd64'
    index_dir.mkdir(parents=True, exist_ok=True)
    index = subprocess.check_output(['apt-ftparchive', 'packages', 'pool'], cwd=output)
    (index_dir / 'Packages').write_bytes(index)
    (index_dir / 'Packages.gz').write_bytes(gzip.compress(index, mtime=0))
    by_hash = index_dir / 'by-hash/SHA256'
    by_hash.mkdir(parents=True, exist_ok=True)
    for name in ('Packages', 'Packages.gz'):
        path = index_dir / name
        immutable = by_hash / digest(path)
        if not immutable.exists():
            shutil.copyfile(path, immutable)
    for name in ('Release', 'Release.gpg', 'InRelease'):
        (suite / name).unlink(missing_ok=True)
    expires = format_datetime(datetime.now(timezone.utc) + timedelta(days=valid_days), usegmt=True)
    options = {'Origin': 'Sentinel', 'Label': 'Sentinel', 'Suite': 'sentinel-beta',
               'Codename': 'sentinel-beta', 'Architectures': 'amd64', 'Components': 'main',
               'Acquire-By-Hash': 'yes', 'Valid-Until': expires,
               'Description': 'Sentinel Linux beta updates for Kubuntu 26.04 amd64'}
    command = ['apt-ftparchive']
    for field, value in options.items():
        command.extend(['-o', f'APT::FTPArchive::Release::{field}={value}'])
    command.extend(['release', 'dists/sentinel-beta'])
    release = subprocess.check_output(command, cwd=output)
    # Older apt-ftparchive versions silently ignore the Valid-Until option.
    # Always include the expiry explicitly before signing the metadata.
    release = b'\n'.join(line for line in release.split(b'\n') if not line.startswith(b'Valid-Until:'))
    (suite / 'Release').write_bytes(f'Valid-Until: {expires}\n'.encode() + release)
    for mode, filename in (('--clearsign', 'InRelease'), ('--detach-sign', 'Release.gpg')):
        subprocess.run(['gpg', '--local-user', fingerprint, '--digest-algo', 'SHA256',
                        '--output', str(suite / filename), mode, str(suite / 'Release')], check=True)
    (output / 'sentinel-archive-keyring.gpg').write_bytes(key)
    subprocess.run(['gpgv', '--keyring', str(output / 'sentinel-archive-keyring.gpg'),
                    str(suite / 'InRelease')], check=True, stdout=subprocess.DEVNULL)
    print(f'Signed repository ready: {output}')
    print(f'Metadata expires {expires}. Re-sign/publish before expiry even when packages are unchanged.')
    print('Upload pool/ and by-hash files first, other indexes next, InRelease last. Keep previous files.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--debs', type=Path, required=True)
    parser.add_argument('--platform', type=Path, required=True)
    parser.add_argument('--signing-key', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--previous', type=Path)
    parser.add_argument('--valid-days', type=int, default=30)
    args = parser.parse_args()
    try:
        build_repository(args.debs, args.platform, args.signing_key, args.output, args.previous, args.valid_days)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise SystemExit(f'Repository build stopped: {exc}') from exc
