#!/usr/bin/env python3
"""Preserve legacy extension keys outside a public checkout; never rewrite history."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import subprocess

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ('activity', 'safe_steam', 'safe_crunchyroll', 'extension_catalog', 'newtab')


def relocate(repo: Path, key_dir: Path, *, untrack: bool = False) -> int:
    repo, key_dir = repo.resolve(), key_dir.expanduser()
    if not key_dir.is_absolute() or key_dir.is_symlink():
        raise ValueError('Use an absolute, non-symlink private key directory')
    key_dir = key_dir.resolve()
    if key_dir.is_relative_to(repo):
        raise ValueError('Private key directory must be outside the project')
    moves = []
    # Check every collision before relocating any source key.
    for name in COMPONENTS:
        source = repo / 'sentinel' / name / 'browser_distribution/browser_extension.pem'
        target = key_dir / f'{name}.pem'
        if source.is_symlink() or target.is_symlink():
            raise ValueError('Refusing symlinked signing-key inputs')
        if not source.exists():
            continue
        if not stat.S_ISREG(source.stat().st_mode):
            raise ValueError('Signing key must be a regular file')
        if target.exists() and (not target.is_file() or target.read_bytes() != source.read_bytes()):
            raise ValueError('Different private key already exists; retain both and resolve manually')
        moves.append((source, target))
    if moves:
        key_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        key_dir.chmod(0o700)
    for source, target in moves:
        if not target.exists():
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as output:
                output.write(source.read_bytes())
                output.flush()
                os.fsync(output.fileno())
        target.chmod(0o600)
        if target.read_bytes() != source.read_bytes():
            raise ValueError('Private backup verification failed; original key retained')
        source.unlink()
    if untrack:
        # Remove only these legacy paths from the current index, not past commits.
        for name in COMPONENTS:
            relative = f'sentinel/{name}/browser_distribution/browser_extension.pem'
            tracked = subprocess.check_output(['git', '-C', str(repo), 'ls-files', '--', relative])
            if tracked:
                subprocess.run(['git', '-C', str(repo), 'rm', '--cached', '--quiet', '--', relative], check=True)
    return len(moves)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=ROOT)
    parser.add_argument('--key-dir', type=Path, default=Path(os.environ.get(
        'SENTINEL_EXTENSION_KEY_DIR', str(Path.home() / '.local/share/sentinel-extension-keys'))))
    parser.add_argument('--untrack', action='store_true', help='also remove legacy keys from the current Git index')
    args = parser.parse_args()
    count = relocate(args.repo, args.key_dir, untrack=args.untrack)
    print(f'Preserved {count} private keys outside the checkout. Git history remains unchanged.')


if __name__ == '__main__':
    main()
