#!/usr/bin/python3
"""Root-owned logind guard for the one protected KDE session."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time


def sessions(uid):
    # A user not logged in yet is normal. Enumerate sessions rather than treating
    # show-user's "not logged in" error as a broken protection service.
    listing = subprocess.check_output(['loginctl', 'list-sessions', '--no-legend', '--no-pager'], text=True)
    ids = [line.split()[0] for line in listing.splitlines() if line.split()]
    result = []
    for sid in ids:
        lookup = subprocess.run(['loginctl', 'show-session', sid, '-p', 'User', '-p', 'Class',
            '-p', 'Type', '-p', 'Service', '-p', 'TimestampMonotonic'], text=True, capture_output=True, check=False)
        if lookup.returncode:
            continue  # A session can legitimately disappear between enumeration and lookup.
        data = dict(line.split('=',1) for line in lookup.stdout.splitlines() if '=' in line)
        if data.get('User') == str(uid) and data.get('Class') in {'user', 'user-early'} and data.get('Type') in {'wayland', 'x11', 'tty'}:
            result.append((sid, data))
    return result


def extra_sessions(current):
    valid = [(sid,d) for sid,d in current if d.get('Type') == 'wayland' and d.get('Service') in {'sddm', 'sddm-autologin'}]
    valid.sort(key=lambda item: (int(item[1].get('TimestampMonotonic', '0')), item[0]))
    keep = valid[0][0] if valid else None
    return [sid for sid,d in current if sid != keep]


def active(name):
    return subprocess.run(['systemctl', 'is-active', '--quiet', name], check=False).returncode == 0


def main():
    installed = json.loads(Path('/etc/sentinel/iso-installed.json').read_text())
    stopped = False
    while True:
        try:
            healthy = all(active(name) for name in ('sentinel-dns.service', 'sentinel-dns-firewall.service', 'sentineld.service'))
            if not healthy:
                if not stopped:
                    subprocess.run(['systemctl', 'stop', 'sddm.service'], check=True)
                    print('Required protection stopped; the desktop session was closed.', flush=True)
                    stopped = True
            elif stopped:
                # Re-run the gate rather than trusting an old ready marker after recovery.
                subprocess.run(['/usr/lib/sentinel-iso/first_boot', '--verify-only'], check=True, timeout=240)
                subprocess.run(['systemctl', 'start', '--no-block', 'sddm.service'], check=True)
                stopped = False
            for sid in extra_sessions(sessions(installed['uid'])):
                print('Closing additional or unsupported login session ' + sid, flush=True)
                subprocess.run(['loginctl', 'terminate-session', sid], check=True)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            # Failure to inspect sessions is not permission to operate unmonitored.
            subprocess.run(['systemctl', 'stop', 'sddm.service'], check=False)
            stopped = True
            print('Session verification failed: ' + str(exc), flush=True)
        time.sleep(2)


if __name__ == '__main__':
    main()
