"""Root-owned daemon configuration; registration never chooses the user."""
from __future__ import annotations

import os
import pwd
import stat
import tomllib
from pathlib import Path

from sentinel.daemon.security.peer_auth import _uid_is_authorized
from sentinel.settings.paths import CONFIG_DIR


DAEMON_CONFIG_FILE = CONFIG_DIR / "daemon.toml"


def load_protected_uid(path: Path = DAEMON_CONFIG_FILE) -> int:
    """Resolve the explicitly configured desktop account before serving clients."""
    try:
        parent = path.parent.stat()
        if parent.st_uid != 0 or parent.st_mode & 0o022:
            raise ValueError(f"{path.parent} must be root-owned and not writable by users.")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError(f"{path} must be a root-owned, protected regular file.")
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Cannot load {path}: {exc}. Configure protected_user first.") from exc

    name = data.get("protected_user")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f'{path} must contain protected_user = "your-desktop-user".')
    try:
        user = pwd.getpwnam(name.strip())
    except KeyError as exc:
        raise ValueError(f"Protected user {name!r} does not exist.") from exc
    if user.pw_uid == 0:
        raise ValueError("The protected desktop user must not be root.")
    if not _uid_is_authorized(user.pw_uid):
        raise ValueError(f"Protected user {user.pw_name!r} must belong to the sentinel group.")
    return user.pw_uid
