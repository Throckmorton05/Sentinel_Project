#!/usr/bin/python3
"""Export public APT trust/configuration on the developer machine (never a secret key)."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

PACKAGES = 'sentinel sentinel-core sentinel-assets sentinel-models sentinel-desktop sentinel-platform'


def validate_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in url)):
        raise ValueError('Use an HTTPS repository URL without credentials, query, fragment or whitespace.')
    return url.rstrip('/')


def github_url(repo, tag):
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo or '')
            or any(part in {'.', '..'} for part in (repo or '').split('/'))):
        raise ValueError('Expected GitHub OWNER/REPO.')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', tag or ''):
        raise ValueError('Use a simple fixed channel tag, such as apt-beta.')
    return f'https://github.com/{repo}/releases/download/{tag}'


def public_key(fingerprint):
    fingerprint = fingerprint.upper()
    if not re.fullmatch(r'[0-9A-F]{40}|[0-9A-F]{64}', fingerprint):
        raise ValueError('Supply the complete primary signing-key fingerprint, not a name or short ID.')
    key = subprocess.check_output(['gpg', '--batch', '--export', fingerprint])
    if not key:
        raise ValueError('Signing public key not found in your GPG keyring.')
    verify_key(key, fingerprint)
    return key


def verify_key(key, fingerprint):
    listing = subprocess.check_output(['gpg', '--batch', '--with-colons', '--show-keys'], input=key).decode()
    primary = []
    want = False
    for line in listing.splitlines():
        fields = line.split(':')
        if fields[0] == 'pub':
            want = True
        elif fields[0] == 'fpr' and want:
            primary.append(fields[9]); want = False
    if primary != [fingerprint]:
        raise ValueError('Export did not resolve exactly the requested primary key.')


def source_text(url, layout='standard'):
    if layout == 'github-flat':
        return ('Types: deb\nURIs: ' + validate_url(url) + '/\nSuites: ./\n'
                'Architectures: amd64\nSigned-By: /usr/share/keyrings/sentinel-archive-keyring.gpg\n'
                'Check-Valid-Until: yes\nBy-Hash: no\n')
    if layout != 'standard':
        raise ValueError('Unsupported repository layout.')
    return ('Types: deb\nURIs: ' + validate_url(url) + '\nSuites: sentinel-beta\nComponents: main\n'
            'Architectures: amd64\nSigned-By: /usr/share/keyrings/sentinel-archive-keyring.gpg\n'
            'Check-Valid-Until: yes\n')


def preference_text():
    return (f'Package: {PACKAGES}\nPin: release o=Sentinel\nPin-Priority: 700\n\n'
            f'Package: {PACKAGES}\nPin: origin "*"\nPin-Priority: -1\n')


def verify_configuration(directory, expected_fingerprint=None):
    directory = Path(directory)
    for name in ('repository.json', 'sentinel.sources', 'sentinel.pref', 'sentinel-archive-keyring.gpg'):
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('Missing or symlinked repository input: ' + name)
    metadata = json.loads((directory / 'repository.json').read_text())
    fingerprint = metadata['fingerprint']
    if not re.fullmatch(r'[0-9A-F]{40}|[0-9A-F]{64}', fingerprint):
        raise ValueError('Invalid repository primary-key fingerprint.')
    if expected_fingerprint and fingerprint != expected_fingerprint.upper():
        raise ValueError('Repository key does not match the independently supplied fingerprint.')
    verify_key((directory / 'sentinel-archive-keyring.gpg').read_bytes(), fingerprint)
    layout = metadata.get('layout', 'standard')
    if layout == 'github-flat' and metadata['url'] != github_url(metadata['github_repo'], metadata['github_tag']):
        raise ValueError('GitHub repository URL does not match the configured repository/channel.')
    if (metadata['suite'] != ('./' if layout == 'github-flat' else 'sentinel-beta')
            or (directory / 'sentinel.sources').read_text() != source_text(metadata['url'], layout)
            or (directory / 'sentinel.pref').read_text() != preference_text()):
        raise ValueError('Unexpected repository URL, suite, trust path or pinning configuration.')
    return metadata


def configure(url, fingerprint, output, github_repo=None, github_tag='apt-beta'):
    layout = 'github-flat' if github_repo else 'standard'
    if github_repo:
        url = github_url(github_repo, github_tag)
    url = validate_url(url)
    key = public_key(fingerprint)
    output = Path(output)
    if output.exists():
        raise ValueError('Choose a new output directory; existing repository trust is never overwritten.')
    output.mkdir(parents=True)
    (output / 'sentinel-archive-keyring.gpg').write_bytes(key)
    (output / 'sentinel.sources').write_text(source_text(url, layout))
    (output / 'sentinel.pref').write_text(preference_text())
    metadata = {'url': url, 'fingerprint': fingerprint.upper(), 'layout': layout,
                'suite': './' if github_repo else 'sentinel-beta'}
    if github_repo:
        metadata.update(github_repo=github_repo, github_tag=github_tag)
    (output / 'repository.json').write_text(json.dumps(metadata, indent=2) + '\n')
    print(f'Public repository configuration created: {output}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    location = parser.add_mutually_exclusive_group(required=True)
    location.add_argument('--url')
    location.add_argument('--github-repo', help='OWNER/REPO for a GitHub Releases flat APT channel')
    parser.add_argument('--github-tag', default='apt-beta')
    parser.add_argument('--signing-key', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        configure(args.url, args.signing_key, args.output, args.github_repo, args.github_tag)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise SystemExit(str(exc)) from exc
