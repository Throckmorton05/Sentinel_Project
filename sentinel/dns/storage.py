"""Root-owned DNS data and atomic, durable file replacement."""

import os
import stat
import tempfile
from pathlib import Path


def protect_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"{path} must not be a symlink.")
    if not path.exists():
        protect_directory(path.parent)
        path.mkdir(mode=0o755)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError(f"{path} must be a root-owned directory without group/other write access.")
    if path != path.parent:
        # Validate ancestors too: a writable parent could replace this directory.
        protect_directory(path.parent)


def read_protected(path: Path, *, private: bool = False) -> str:
    protect_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError(f"{path} must be a root-owned regular file without group/other write access.")
        if private and info.st_mode & 0o077:
            raise ValueError(f"{path} contains credentials and must be readable only by root (0600).")
        return stream.read()


def atomic_write(path: Path, text: str, *, mode: int = 0o644) -> None:
    protect_directory(path.parent)
    if path.is_symlink():
        raise ValueError(f"{path} must not be a symlink.")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
