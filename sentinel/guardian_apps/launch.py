"""Unprivileged .desktop launch gate; argv comes from sentineld's inventory."""
from __future__ import annotations

import os
import subprocess
import sys
from urllib.parse import unquote, urlparse

from sentinel.daemon.client import request, SentineldError


def expand_argv(argv: list[str], arguments: list[str]) -> list[str]:
    result = []
    files = [unquote(urlparse(a).path) if a.startswith('file://') else a for a in arguments]
    for token in argv:
        if token in {'%U', '%F'}:
            result.extend(arguments if token == '%U' else files)
        elif token in {'%u', '%f'}:
            result.extend((arguments if token == '%u' else files)[:1])
        elif token in {'%i', '%c', '%k'}:
            continue
        elif '%' in token:
            if token.replace('%%', '').find('%') >= 0:
                raise ValueError('Unsupported desktop Exec field code.')
            result.append(token.replace('%%', '%'))
        else:
            result.append(token)
    if not result:
        raise ValueError('Application has no executable.')
    return result


def main() -> None:
    if os.geteuid() == 0:
        raise SystemExit('Desktop applications must run as the desktop user.')
    try:
        if len(sys.argv) < 2:
            raise ValueError('Missing desktop ID.')
        if sys.argv[1] in {'sentinel-guardian-access.desktop', 'sentinel-portal.desktop', 'sentinel-getting-started.desktop'}:
            # Old cached overrides may still invoke this gate after an update.
            # Only fixed Portal/recovery/read-only guide routes bypass the RPC.
            # They run unprivileged; recovery mutations still verify the PIN.
            module = ('sentinel.portal.main' if sys.argv[1] != 'sentinel-guardian-access.desktop'
                      else 'sentinel.guardian_apps.manager')
            flags = ['--guide'] if sys.argv[1] == 'sentinel-getting-started.desktop' else []
            subprocess.Popen(['/usr/lib/sentinel/venv/bin/python', '-I', '-m', module, *flags],
                             start_new_session=True)
            return
        result = request('guardian.apps.launch', {'desktop_id': sys.argv[1]})
        argv = expand_argv(result['argv'], sys.argv[2:])
        # No shell, no root launch, and no client-controlled executable.
        subprocess.Popen(argv, start_new_session=True)
    except (SentineldError, OSError, ValueError) as exc:
        from PyQt6.QtWidgets import QApplication, QMessageBox
        app = QApplication(sys.argv[:1])
        QMessageBox.information(None, 'Sentinel', str(exc))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
