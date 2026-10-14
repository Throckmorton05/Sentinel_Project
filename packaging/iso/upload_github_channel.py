#!/usr/bin/python3
"""Explicit developer-side publication of a signed snapshot to a mutable release."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import digest, plain_file
from github_repository import authenticated_index, METADATA
from repository_config import github_url, verify_key


def gh(*args, capture=False):
    if capture:
        return subprocess.check_output(['gh', *args], text=True, stderr=subprocess.PIPE)
    subprocess.run(['gh', *args], check=True)


def release(repo, tag):
    github_url(repo, tag)  # Validate before constructing authenticated API paths.
    try:
        value = json.loads(gh('api', f'repos/{repo}/releases/tags/{tag}', capture=True))
    except subprocess.CalledProcessError as exc:
        if '(HTTP 404)' not in (exc.stderr or ''):
            raise
        # The tag endpoint is documented for published releases. An authorized
        # author can also enumerate drafts before the first publication.
        # Older gh versions support --paginate but not --slurp. Their output
        # contains one JSON array per page, rather than a single nested array.
        output = gh('api', '--paginate',
                    f'repos/{repo}/releases?per_page=100', capture=True)
        decoder, offset, matches = json.JSONDecoder(), 0, []
        while offset < len(output):
            if output[offset].isspace():
                offset += 1
                continue
            page, offset = decoder.raw_decode(output, offset)
            if not isinstance(page, list):
                raise ValueError('Unexpected GitHub releases response.')
            matches.extend(item for item in page if item.get('tag_name') == tag)
        if len(matches) != 1:
            raise ValueError('Create exactly one channel release/draft before uploading.')
        value = matches[0]
    if value.get('tag_name') != tag or value.get('immutable') is not False:
        raise ValueError('Expected a mutable GitHub channel release; immutable releases cannot be updated.')
    return value


def matching_asset(repo, tag, path, asset):
    """Existing package versions are never overwritten, even when digest is absent."""
    if asset.get('state') != 'uploaded' or asset.get('size') != path.stat().st_size:
        return False
    expected = digest(path)
    if asset.get('digest'):
        return asset['digest'] == 'sha256:' + expected
    # Older responses may omit the digest. gh handles authenticated draft
    # downloads without exposing the token to this script or its command line.
    with tempfile.TemporaryDirectory(prefix='sentinel-asset-verify-') as folder:
        gh('release', 'download', tag, '--repo', repo, '--pattern', path.name, '--dir', folder)
        downloaded = plain_file(Path(folder) / path.name)
        return downloaded.stat().st_size == path.stat().st_size and digest(downloaded) == expected


def upload(directory, repo, tag, fingerprint):
    directory = Path(directory).resolve()
    data = json.loads(plain_file(directory / 'channel.json').read_text())
    expected_url = github_url(repo, tag)
    if (data.get('github_repo') != repo or data.get('github_tag') != tag
            or data.get('url') != expected_url or data.get('fingerprint') != fingerprint.upper()):
        raise ValueError('Channel snapshot does not match the explicit repository/tag/fingerprint.')
    key = plain_file(directory / 'sentinel-archive-keyring.gpg').read_bytes()
    verify_key(key, fingerprint.upper())
    packages = authenticated_index(directory, key)
    files = [*packages.values(), *(directory / name for name in METADATA),
             directory / 'sentinel-archive-keyring.gpg']
    if set(data.get('sha256', {})) != {p.name for p in files}:
        raise ValueError('Unexpected publication file list.')
    for path in files:
        if digest(plain_file(path)) != data['sha256'][path.name]:
            raise ValueError('Channel snapshot changed after signing: ' + path.name)
    state = release(repo, tag)
    assets = {item['name']: item for item in state['assets']}
    if len(set(assets) | {p.name for p in files}) > 1000:
        raise ValueError('Release would exceed GitHub’s asset limit.')
    # Verify all old package-name collisions before publishing any new bytes.
    for path in packages.values():
        if path.name in assets and not matching_asset(repo, tag, path, assets[path.name]):
            raise ValueError('Existing package has different bytes; bump the version: ' + path.name)
    for path in packages.values():
        if path.name not in assets:
            gh('release', 'upload', tag, str(path), '--repo', repo)
            current = {item['name']: item for item in release(repo, tag)['assets']}
            if path.name not in current or not matching_asset(repo, tag, path, current[path.name]):
                raise ValueError('Uploaded package checksum verification failed: ' + path.name)
    # Publish packages first, then indexes/auxiliary signatures, InRelease last.
    # GitHub replacement is delete+upload, not an atomic swap. An interrupted
    # publication may temporarily fail APT refresh; signatures stay mandatory.
    for name in ('sentinel-archive-keyring.gpg', 'Packages', 'Packages.gz', 'Release', 'Release.gpg', 'InRelease'):
        path = directory / name
        gh('release', 'upload', tag, str(path), '--repo', repo, '--clobber')
        current = {item['name']: item for item in release(repo, tag)['assets']}
        if name not in current or not matching_asset(repo, tag, path, current[name]):
            raise ValueError('Uploaded metadata verification failed: ' + name)
    if state.get('draft'):
        print('Verified assets uploaded to draft. Publish the draft to make the APT URL public.')
    else:
        print('Signed APT channel published at ' + expected_url + '/')
    print('Keep this complete local snapshot as --previous for the next publication.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--tag', default='apt-beta')
    parser.add_argument('--fingerprint', required=True)
    args = parser.parse_args()
    try:
        upload(args.directory, args.repo, args.tag, args.fingerprint)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, 'stderr', None) or str(exc)
        raise SystemExit('GitHub publication stopped: ' + detail.strip()) from exc
