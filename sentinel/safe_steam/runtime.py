"""Keep host asset paths separate from paths visible to Steam's runtime."""
from __future__ import annotations

import os
from pathlib import Path

from sentinel.settings.paths import SAFE_STEAM_EXTENSION_DIR


HOST_EXTENSION_PATH = SAFE_STEAM_EXTENSION_DIR
CONTAINER_EXTENSION_PATH = Path('/run/host') / HOST_EXTENSION_PATH.relative_to('/')
LOAD_PATHS = (CONTAINER_EXTENSION_PATH, HOST_EXTENSION_PATH)


def validate_load_path(path: Path | str) -> Path:
    path = Path(path)
    if path not in LOAD_PATHS:
        raise ValueError('Steam extension load path must refer to Sentinel\'s protected assets.')
    return path


def running_extension_path(uid: int, *, proc: Path = Path('/proc')) -> Path | None:
    """Inspect the main webhelper's filesystem as root; never run container code.

    Match directory identity, not just existence: an unrelated directory inside
    the runtime must not be mistaken for our protected host installation.
    No helper means Steam is stopped and its saved load path can be retained.
    """
    for process in sorted(proc.iterdir()):
        if not process.name.isdecimal():
            continue
        try:
            if (process / 'comm').read_text().strip() != 'steamwebhelper':
                continue
            executable = (process / 'exe').readlink().name.removesuffix(' (deleted)')
            if executable != 'steamwebhelper':
                continue
            properties = dict(line.split(':', 1) for line in
                              (process / 'status').read_text().splitlines() if ':' in line)
            owners = properties.get('Uid', '').split()
            if len(owners) < 2 or owners[:2] != [str(uid), str(uid)]:
                continue
            args = (process / 'cmdline').read_bytes().split(b'\0')
            if any(arg.startswith(b'--type=') for arg in args):
                continue
        except (FileNotFoundError, ProcessLookupError):
            continue
        # Root in sentineld can inspect this mount namespace even when its
        # /proc/pid/root is inaccessible to the ordinary desktop user.
        for candidate in LOAD_PATHS:
            visible = process / 'root' / candidate.relative_to('/')
            try:
                if os.path.samefile(visible, HOST_EXTENSION_PATH):
                    return candidate
            except (FileNotFoundError, ProcessLookupError):
                continue
        if not process.exists():
            continue
        raise RuntimeError('Steam cannot see Sentinel\'s protected extension directory in its runtime.')
    return None
