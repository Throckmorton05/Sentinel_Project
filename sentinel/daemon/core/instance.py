"""Serialize daemon lifetime and offline recovery across root processes."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat

from sentinel.settings.paths import RUN_DIR

INSTANCE_LOCK = RUN_DIR / 'daemon.lock'


class InstanceBusyError(RuntimeError):
    pass


def protected_directory(path: Path) -> None:
    for part in (path, *path.parents):
        info = part.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError(f'{part} must be a protected root-owned directory.')


@contextmanager
def instance_lock(path: Path | None = None):
    """Never unlink this file: all contenders must lock the same inode."""
    path = path or INSTANCE_LOCK
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    protected_directory(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError('Sentinel instance lock must be a private root-owned regular file.')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstanceBusyError('sentineld or another recovery operation is running.') from exc
        yield
    finally:
        os.close(fd)
