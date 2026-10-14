#!/usr/bin/python3
"""APT protocol-v2 transaction guard installed only in the Sentinel ISO."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from urllib.parse import unquote


def approved_request(text, allowed, blocked):
    """Permit dependency installs only for explicitly approved install targets."""
    directives = text.split('\n\n', 1)[0].splitlines()
    values = [unquote(line.split('=', 1)[1]) for line in directives
              if line.startswith('CommandLine::AsString=')]
    if len(values) != 1:
        return False
    words = shlex.split(values[0])
    if 'install' not in words:
        return False
    words = words[words.index('install') + 1:]
    targets = []
    skip = False
    for word in words:
        if skip:
            skip = False
            continue
        if word in {'-o', '--option', '-c', '--config-file', '-t', '--target-release'}:
            skip = True
            continue
        if word.startswith('-'):
            continue
        if word.endswith('.deb') and (word.startswith('/') or word.startswith('./')):
            # Only root can supply local .debs; the Sentinel RPC does not accept paths.
            name = subprocess.check_output(['/usr/bin/dpkg-deb', '-f', word, 'Package'], text=True).strip()
        else:
            name = word.split('=', 1)[0].split(':', 1)[0]
        targets.append(name)
    return bool(targets) and all(name in allowed and name not in blocked for name in targets)


def actions(text):
    lines = text.splitlines()
    if not lines or lines.pop(0) != 'VERSION 2':
        raise ValueError('Unsupported APT hook protocol; refusing an unchecked transaction.')
    try:
        start = lines.index('') + 1
    except ValueError as exc:
        raise ValueError('Truncated APT transaction header.') from exc
    result = []
    for line in lines[start:]:
        if not line:
            continue
        fields = line.split()
        if len(fields) != 5 or not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*(?::[a-z0-9-]+)?', fields[0]):
            raise ValueError('Malformed APT transaction action.')
        result.append((fields[0].split(':')[0], fields[1], fields[3], fields[4]))
    return result


def check_plan(text, policy, compare=None):
    protected = set(policy['protected'])
    allowed = set(policy['allowed_install'])
    blocked = set(policy['blocked_install'])
    dependencies_allowed = approved_request(text, allowed, blocked)
    for name, old, new, action in actions(text):
        if action == '**REMOVE**':
            if name in protected:
                raise ValueError(f'{name} is required by Sentinel and cannot be removed, including through dependency changes.')
        elif action != '**CONFIGURE**':
            if name in blocked:
                raise ValueError(f'{name} is outside the supported Sentinel platform.')
            # Existing signed-distribution packages may be updated. New kernel
            # ABI packages are normal update dependencies; the repositories stay root-owned.
            if old == '-' and name not in allowed and not dependencies_allowed and not name.startswith(('linux-image-', 'linux-modules-', 'linux-headers-', 'linux-tools-', 'linux-objects-', 'linux-signatures-')):
                raise ValueError(f'{name} is not an approved system package. Install applications through Sentinel.')
            if name in protected and old != '-' and new != '-':
                downgrade = compare(new, old) if compare else subprocess.run(
                    ['/usr/bin/dpkg', '--compare-versions', new, 'lt', old], check=False).returncode == 0
                if downgrade:
                    raise ValueError(f'Downgrading required package {name} is not allowed.')


def main():
    # Calamares legitimately removes installer packages before final provisioning.
    if not Path('/etc/sentinel/iso-installed.json').is_file():
        return
    path = Path('/etc/sentinel/iso-package-policy.json')
    info = path.lstat()
    if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('The protected package policy is not root-owned and read-only.')
    policy = json.loads(path.read_text())
    check_plan(sys.stdin.read(), policy)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f'Sentinel package policy: {exc}', file=sys.stderr)
        raise SystemExit(1)
