#!/usr/bin/python3
"""Protect GRUB editing while keeping ordinary Linux boot password-free."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import run, write

MARK = 'CLASS="$CLASS --unrestricted" # Sentinel: normal boot remains password-free'


def normal_boot_script(source):
    if MARK in source:
        return source
    matches = list(re.finditer(r'^CLASS=[^\n]+$', source, re.MULTILINE))
    if len(matches) != 1:
        raise ValueError('Unrecognized GRUB Linux generator; refusing to require an unknown password for normal boot.')
    match = matches[0]
    return source[:match.end()] + '\n' + MARK + source[match.end():]


def refresh(initialize=False):
    if not initialize and not Path('/etc/sentinel/iso-installed.json').exists():
        return
    if os.geteuid() != 0 or not Path('/usr/lib/sentinel-iso/image-built.json').is_file():
        raise ValueError('GRUB security belongs only to the installed Sentinel target.')
    generator = Path('/etc/grub.d/10_linux')
    source = generator.read_text()
    updated = normal_boot_script(source)
    if updated != source:
        write(generator, updated, 0o755)
    run('/bin/sh', '-n', generator)
    auth = Path('/etc/grub.d/01_sentinel_auth')
    if not auth.exists():
        if not initialize:
            raise ValueError('GRUB authentication configuration is missing.')
        password = secrets.token_urlsafe(64)
        result = subprocess.run(['/usr/bin/grub-mkpasswd-pbkdf2'],
            input=password + '\n' + password + '\n', text=True, capture_output=True, check=True)
        match = re.search(r'grub\.pbkdf2\.sha512\.\d+\.[A-Fa-f0-9]+\.[A-Fa-f0-9]+', result.stdout)
        if not match:
            raise ValueError('GRUB password hashing failed.')
        # The random boot-edit password is deliberately not saved or displayed.
        write(auth, '#!/bin/sh\ncat <<\'GRUB\'\nset superusers="sentinel-maintenance"\n'
              'export superusers\npassword_pbkdf2 sentinel-maintenance ' + match.group() + '\nGRUB\n', 0o700)
    if auth.is_symlink() or auth.stat().st_uid != 0 or auth.stat().st_mode & 0o022:
        raise ValueError('Untrusted GRUB authentication configuration.')
    write('/etc/default/grub.d/99-sentinel-security.cfg', 'GRUB_DISABLE_RECOVERY=true\nGRUB_DISABLE_OS_PROBER=true\n')
    run('/usr/sbin/update-grub', stdout=subprocess.DEVNULL)
    config = Path('/boot/grub/grub.cfg')
    text = config.read_text()
    if 'password_pbkdf2 sentinel-maintenance ' not in text or not re.search(r'^menuentry .*--unrestricted', text, re.MULTILINE):
        raise ValueError('Generated GRUB config lacks authentication or a password-free normal boot entry.')
    if re.search(r'^\s*menuentry .*recovery mode', text, re.MULTILINE):
        raise ValueError('GRUB still offers a recovery entry.')
    config.chmod(0o600)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--initialize', action='store_true')
    choice.add_argument('--refresh-if-installed', action='store_true')
    args = parser.parse_args()
    try:
        refresh(args.initialize)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise SystemExit(f'Sentinel boot protection failed: {exc}') from exc
