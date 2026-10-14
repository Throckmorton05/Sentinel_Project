"""Privileged, fixed-origin REAPER stable installer for Sentinel.

REAPER publishes Linux .tar.xz archives, not official .deb packages.  Never
execute a user-supplied archive or its shell installer with root privileges.
Only the REAPER application payload is installed from Cockos' HTTPS release.
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import stat
import tarfile
import tempfile
import uuid
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

_DOWNLOAD_PAGE = "https://www.reaper.fm/download.php"
_ARCHES = {"x86_64": "x86_64", "aarch64": "aarch64"}
_REAPER_DIR = Path("/opt/REAPER")
_REAPER_COMMAND = Path("/usr/local/bin/reaper")
_REAPER_DESKTOP = Path("/usr/share/applications/cockos-reaper.desktop")
_VERSION_MARKER = ".sentinel-reaper-version"
_MAX_PAGE_BYTES = 2 * 1024 * 1024
_MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024
_MAX_EXTRACT_BYTES = 1024 * 1024 * 1024
_MAX_FILES = 20000
_VERSION_RE = re.compile(r"\bREAPER\s+v(\d+)\.(\d{1,3})(?!\d)", re.I)


class _CockosRedirectsOnly(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if target.scheme != "https" or target.hostname not in {
            "www.reaper.fm", "reaper.fm", "dlz.reaper.fm"
        } or target.username or target.password:
            raise ValueError("REAPER download redirected away from Cockos HTTPS.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_REAPER_HTTP = build_opener(_CockosRedirectsOnly)


class _DownloadLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.extend(v for k, v in attrs if k == "href" and v)


def _release_from_page(page: bytes, arch: str) -> tuple[str, str]:
    """Return (display version, validated canonical Cockos archive URL)."""
    html = page.decode("utf-8", errors="replace")
    version_match = _VERSION_RE.search(html)
    if not version_match:
        raise ValueError("Cannot determine stable REAPER version from official downloads.")
    major, minor = version_match.groups()
    version = f"{int(major)}.{minor}"
    filename = f"reaper{major}{minor}_linux_{arch}.tar.xz"
    canonical = f"https://www.reaper.fm/files/{major}.x/{filename}"
    parser = _DownloadLinks()
    parser.feed(html)
    urls = [urljoin(_DOWNLOAD_PAGE, item) for item in parser.links]
    # The release page must explicitly publish the expected stable archive.
    if canonical not in urls:
        raise ValueError(f"Official REAPER downloads do not list {filename}.")
    return version, canonical


def _latest_reaper(arch: str) -> tuple[str, str]:
    request = Request(_DOWNLOAD_PAGE, headers={"User-Agent": "Sentinel-REAPER-Updater"})
    with _REAPER_HTTP.open(request, timeout=20) as response:
        page = response.read(_MAX_PAGE_BYTES + 1)
    if len(page) > _MAX_PAGE_BYTES:
        raise ValueError("REAPER downloads page exceeds limit.")
    return _release_from_page(page, arch)


def _download_reaper(url: str, destination: Path) -> str:
    """Limit archive size; SHA-256 is recorded for auditing, NOT a signature."""
    digest = hashlib.sha256()
    total = 0
    request = Request(url, headers={"User-Agent": "Sentinel-REAPER-Updater"})
    with _REAPER_HTTP.open(request, timeout=45) as response, destination.open("xb") as out:
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > _MAX_DOWNLOAD_BYTES:
                raise ValueError("REAPER archive exceeds download size limit.")
            digest.update(chunk)
            out.write(chunk)
    if total < 1024:
        raise ValueError("REAPER download appears incomplete.")
    return digest.hexdigest()


def _valid_relative_path(raw: str) -> PurePosixPath:
    path = PurePosixPath(raw)
    if (not raw or raw.startswith("/") or "\\" in raw or "\x00" in raw
            or ".." in path.parts or "." in path.parts):
        raise ValueError("Unsafe REAPER archive member path.")
    return path


def _extract_application(archive: Path, target: Path, arch: str) -> None:
    """Extract only the program tree; never extract/run install-reaper.sh.

    Defer internal symlinks until after regular files so symlinks cannot alter
    the destination of file writes. Reject special files and hard links.
    """
    prefix = PurePosixPath(f"reaper_linux_{arch}/REAPER")
    root = target
    count = total = 0
    symlinks: list[tuple[Path, str]] = []
    seen: set[PurePosixPath] = set()
    with tarfile.open(archive, mode="r:xz") as tf:
        for member in tf:
            path = _valid_relative_path(member.name.rstrip("/"))
            if path == prefix or prefix not in path.parents:
                # Exclude the wrapper's install script and README; these are
                # outside the application tree and should never run as root.
                continue
            if path == prefix:
                continue
            rel = PurePosixPath(*path.parts[len(prefix.parts):])
            if not rel.parts or rel in seen:
                raise ValueError("Duplicate or empty REAPER archive path.")
            seen.add(rel)
            count += 1
            if count > _MAX_FILES:
                raise ValueError("REAPER archive exceeds file count limit.")
            dest = root.joinpath(*rel.parts)
            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            if member.issym():
                # Validate link text while keeping it relative to the payload.
                link = PurePosixPath(member.linkname)
                if link.is_absolute() or "\\" in member.linkname or "\x00" in member.linkname:
                    raise ValueError("REAPER archive contains an unsafe symlink.")
                # PurePosixPath doesn't normalize '..'; normalize component-wise.
                stack = list(rel.parts[:-1])
                for part in link.parts:
                    if part == "..":
                        if not stack:
                            raise ValueError("REAPER archive symlink escapes program tree.")
                        stack.pop()
                    elif part not in ("", "."):
                        stack.append(part)
                symlinks.append((dest, member.linkname))
                continue
            if not member.isfile() or member.size < 0:
                raise ValueError("REAPER archive contains an unsupported file type.")
            total += member.size
            if total > _MAX_EXTRACT_BYTES:
                raise ValueError("REAPER extracted data exceeds size limit.")
            dest.parent.mkdir(parents=True, exist_ok=True)
            content = tf.extractfile(member)
            if content is None:
                raise ValueError("REAPER archive member could not be opened.")
            with content, dest.open("xb") as output:
                shutil.copyfileobj(content, output, length=1024 * 1024)
            dest.chmod(0o755 if member.mode & 0o111 else 0o644)
    for dest, text in symlinks:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Never create a symlink over a previously extracted directory or file.
        if dest.exists() or dest.is_symlink():
            raise ValueError("REAPER archive contains overlapping symlinks.")
        dest.symlink_to(text)
    if not (root / "reaper").is_file() or not (root / "reaper").stat().st_mode & stat.S_IXUSR:
        raise ValueError("Official REAPER executable missing from archive.")


def _installed_reaper_version() -> str | None:
    # Never execute the user-supplied or installed binary as root.
    if _REAPER_DIR.is_symlink() or not (_REAPER_DIR / "reaper").is_file():
        return None
    marker = _REAPER_DIR / _VERSION_MARKER
    if marker.is_file() and not marker.is_symlink():
        value = marker.read_text(encoding="utf-8")[:50].strip()
        if re.fullmatch(r"\d+\.\d{1,3}", value):
            return value
    whatsnew = _REAPER_DIR / "whatsnew.txt"
    if whatsnew.is_file() and not whatsnew.is_symlink():
        # Older REAPER installs include release info in the changelog.
        top = whatsnew.open(encoding="utf-8", errors="replace").read(2048)
        match = re.search(r"(?im)^\s*(?:REAPER\s*)?v(\d+\.\d{1,3})\b", top)
        if match:
            return match.group(1)
    return "unknown"


def _version_tuple(version: str) -> tuple[int, int]:
    major, minor = version.split(".")
    return int(major), int(minor)


def _ensure_desktop_integration() -> None:
    """Leave manually managed launchers in place; don't overwrite them."""
    if not (_REAPER_COMMAND.exists() or _REAPER_COMMAND.is_symlink()):
        _REAPER_COMMAND.symlink_to(_REAPER_DIR / "reaper")
    elif (_REAPER_COMMAND.is_symlink()
          and os.readlink(_REAPER_COMMAND) == str(_REAPER_DIR / "reaper")):
        pass
    else:
        print(f"Existing REAPER command left unchanged: {_REAPER_COMMAND}")
    if not (_REAPER_DESKTOP.exists() or _REAPER_DESKTOP.is_symlink()):
        icon_file = _REAPER_DIR / "Resources" / "main.png"
        icon = str(icon_file) if icon_file.is_file() else "audio-x-generic"
        _REAPER_DESKTOP.parent.mkdir(parents=True, exist_ok=True)
        _REAPER_DESKTOP.write_text(
            "[Desktop Entry]\nType=Application\nName=REAPER\n"
            "Comment=Digital audio workstation\nExec=/opt/REAPER/reaper %F\n"
            f"Icon={icon}\nTerminal=false\nCategories=AudioVideo;Audio;\n"
            "X-Sentinel-Managed=true\n", encoding="utf-8",
        )


def install_latest_reaper(*, update: bool = False) -> str:
    arch = _ARCHES.get(platform.machine())
    if arch is None:
        raise ValueError("REAPER installer supports only x86_64 and aarch64.")
    if _REAPER_DIR.is_symlink():
        raise ValueError("Refusing REAPER install: /opt/REAPER is a symlink.")
    installed = _installed_reaper_version()
    if update and installed is None:
        raise ValueError("REAPER is not installed in /opt/REAPER. Use: sen install reaper")
    version, url = _latest_reaper(arch)
    if installed and installed != "unknown" and _version_tuple(installed) >= _version_tuple(version):
        print(f"REAPER {installed} is already current (upstream stable {version}).")
        return installed
    print(f"Installing official REAPER {version} for {arch}...")
    # Must stage on the same filesystem as /opt/REAPER for atomic rename.
    with tempfile.TemporaryDirectory(prefix=".sentinel-reaper-", dir=_REAPER_DIR.parent) as tmp:
        stage = Path(tmp)
        archive = stage / "reaper.tar.xz"
        download_hash = _download_reaper(url, archive)
        payload = stage / "REAPER"
        payload.mkdir(mode=0o755)
        _extract_application(archive, payload, arch)
        (payload / _VERSION_MARKER).write_text(version + "\n", encoding="utf-8")
        backup: Path | None = None
        if _REAPER_DIR.exists():
            if not _REAPER_DIR.is_dir():
                raise ValueError("Existing /opt/REAPER is not a normal directory.")
            backup = _REAPER_DIR.parent / f"REAPER.sentinel-backup-{uuid.uuid4().hex[:12]}"
            _REAPER_DIR.rename(backup)
        try:
            payload.rename(_REAPER_DIR)
        except Exception:
            if backup is not None:
                backup.rename(_REAPER_DIR)
            raise
        _ensure_desktop_integration()
    print(f"REAPER {version} installed successfully (download SHA-256: {download_hash}).")
    if backup is not None:
        print(f"Previous REAPER files retained at {backup} (includes any local extensions).")
    return version


def uninstall_reaper() -> None:
    """Remove only Sentinel-managed REAPER; never delete third-party installs."""
    marker = _REAPER_DIR / _VERSION_MARKER
    if _REAPER_DIR.is_symlink() or not marker.is_file() or marker.is_symlink():
        raise ValueError("REAPER was not installed by Sentinel; refusing automatic removal.")
    if _REAPER_COMMAND.is_symlink() and os.readlink(_REAPER_COMMAND) == str(_REAPER_DIR / "reaper"):
        _REAPER_COMMAND.unlink()
    if _REAPER_DESKTOP.is_file() and not _REAPER_DESKTOP.is_symlink():
        if "X-Sentinel-Managed=true" in _REAPER_DESKTOP.read_text(encoding="utf-8"):
            _REAPER_DESKTOP.unlink()
    shutil.rmtree(_REAPER_DIR)
    print("Sentinel-managed REAPER removed. User projects and preferences preserved.")
