"""Daemon-side installers for approved custom package aliases.

The package RPC supplies the authenticated desktop user's Downloads directory.
Importing this module never infers a user from root's environment or changes
shared per-request state.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

from sentinel.daemon.core.constants import APT_GET
from sentinel.daemon.core.lock_state import LockMode, get_lock_state


def custom_install(package_name: str, *, downloads_dir: Path) -> None:

    if package_name == "neovim":
        # Never install a user-supplied archive as root. Fetch the upstream
        # stable .deb and verify the digest from its release metadata instead.
        install_latest_neovim()

    elif package_name == "reaper":
        from sentinel.daemon.operations.reaper_installer import install_latest_reaper
        install_latest_reaper()
    elif package_name == "unity":
        _install_unity()

    else:
        raise ValueError(f"No custom installer for {package_name}")


def _find_file(downloads_dir: Path, pattern: str) -> Path:

    matches = list(downloads_dir.glob(pattern))

    if not matches:
        raise FileNotFoundError(f"No file matching '{pattern}' found in {downloads_dir}")

    return matches[0]


# Fixed upstream origin; neither the RPC nor the local Downloads folder may
# override the repository, asset name, URL, or expected hash.
_NEOVIM_REPO = "neovim/neovim-releases"
_NEOVIM_API = f"https://api.github.com/repos/{_NEOVIM_REPO}/releases/latest"
_RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_SHA256 = re.compile(r"^sha256:([0-9a-f]{64})$")
_MAX_DEB_BYTES = 100 * 1024 * 1024
_LEGACY_NVIM = Path("/opt/nvim-linux-x86_64/bin/nvim")
_LEGACY_SYMLINK = Path("/usr/local/bin/nvim")


class _HttpsRedirectsOnly(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme != "https":
            raise ValueError("Neovim download redirected to an insecure URL.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_HTTP = build_opener(_HttpsRedirectsOnly)


def _read_release() -> dict:
    request = Request(_NEOVIM_API, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "Sentinel-Neovim-Updater",
    })
    with _HTTP.open(request, timeout=15) as response:
        # Release metadata is small; reject unexpectedly large responses.
        payload = response.read(1024 * 1024 + 1)
    if len(payload) > 1024 * 1024:
        raise ValueError("Neovim release metadata is too large.")
    release = json.loads(payload)
    if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
        raise ValueError("Upstream did not supply a stable Neovim release.")
    if not _RELEASE_TAG.fullmatch(str(release.get("tag_name", ""))):
        raise ValueError("Unexpected Neovim stable release version.")
    return release


def _neovim_asset(release: dict, arch: str) -> tuple[str, int, str]:
    tag = release["tag_name"]
    filename = f"nvim-linux-{arch}.deb"
    expected_url = f"https://github.com/{_NEOVIM_REPO}/releases/download/{tag}/{filename}"
    assets = release.get("assets", [])
    matches = [item for item in assets if isinstance(item, dict) and item.get("name") == filename]
    if len(matches) != 1:
        raise ValueError(f"Stable Neovim release does not provide {filename}.")
    item = matches[0]
    match = _SHA256.fullmatch(str(item.get("digest", "")))
    size = item.get("size")
    if (item.get("browser_download_url") != expected_url
            or not match or type(size) is not int or not 1024 < size <= _MAX_DEB_BYTES):
        raise ValueError("Neovim release asset has invalid URL, checksum, or size.")
    return expected_url, size, match.group(1)


def _installed_neovim_version() -> str | None:
    # Read dpkg's root-controlled package database, not an executable found in
    # user-controlled PATH. The legacy /opt archive is replaced by the .deb.
    result = subprocess.run(
        ["/usr/bin/dpkg-query", "-W", "-f=${Status}\t${Version}", "neovim"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0 or not result.stdout.startswith("install ok installed\t"):
        return None
    return result.stdout.split("\t", 1)[1].strip()


def _version_at_least(installed: str, upstream: str) -> bool:
    # Debian's comparison algorithm handles distro version suffixes correctly.
    return subprocess.run(
        ["/usr/bin/dpkg", "--compare-versions", installed, "ge", upstream],
        check=False,
    ).returncode == 0


def _download_verified(url: str, size: int, sha256: str, dest: Path) -> None:
    digest = hashlib.sha256()
    total = 0
    request = Request(url, headers={"User-Agent": "Sentinel-Neovim-Updater"})
    with _HTTP.open(request, timeout=45) as response, dest.open("xb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > size:
                raise ValueError("Neovim download exceeds the published size.")
            digest.update(chunk)
            output.write(chunk)
    if total != size or digest.hexdigest() != sha256:
        raise ValueError("Neovim package failed size or SHA-256 verification.")


def _verified_deb_fields(package: Path, arch: str, version: str) -> None:
    def field(name):
        return subprocess.check_output(
            ["/usr/bin/dpkg-deb", "-f", str(package), name],
            text=True,
        ).strip()

    expected_arch = {"x86_64": "amd64", "arm64": "arm64"}[arch]
    package_version = field("Version")
    # CPack generally produces the bare release version; also allow a
    # normal Debian revision (e.g. 0.12.5-1) of the exact release.
    if (field("Package") != "neovim"
            or field("Architecture") != expected_arch
            or not re.fullmatch(re.escape(version) + r"(?:[-+][A-Za-z0-9.+~]+)?", package_version)):
        raise ValueError("Downloaded .deb has unexpected package metadata.")


def _remove_legacy_neovim_symlink() -> None:
    # A previous Sentinel version created /usr/local/bin/nvim. Remove only
    # *that exact symlink*, never a user's independently installed Neovim.
    if _LEGACY_SYMLINK.is_symlink() and os.readlink(_LEGACY_SYMLINK) == str(_LEGACY_NVIM):
        _LEGACY_SYMLINK.unlink()


def _local_standalone_neovim_present() -> bool:
    """Recognize independently installed Neovim binaries without running them.

    In particular, historical manual tarball installs sometimes copied the
    binary itself to /usr/local/bin/nvim rather than using Sentinel's symlink.
    Executing that user-owned file in the privileged daemon is unsafe.
    """
    return _LEGACY_SYMLINK.is_file() and not _LEGACY_SYMLINK.is_symlink()


def _retire_local_standalone_neovim() -> None:
    """Preserve a manual /usr/local/bin/nvim after successful .deb install.

    Keep a unique backup instead of deleting the old binary. Do not follow
    symlinks or replace arbitrary custom links with other targets.
    """
    if not _local_standalone_neovim_present():
        return
    fd, backup = tempfile.mkstemp(prefix="nvim.sentinel-pre-deb-", dir=_LEGACY_SYMLINK.parent)
    os.close(fd)
    try:
        # Atomic rename within /usr/local/bin; never execute the original.
        _LEGACY_SYMLINK.rename(backup)
    except Exception:
        Path(backup).unlink(missing_ok=True)
        raise
    print(f"Previous standalone Neovim preserved at {backup}.")


def install_latest_neovim(*, update: bool = False) -> str:
    """Install or upgrade the latest stable Neovim .deb from fixed upstream.

    Requires privileged daemon context. No caller-selected .deb, URL, tag, or
    digest is accepted. Fails closed if GitHub omits checksum metadata.
    """
    arch = {"x86_64": "x86_64", "aarch64": "arm64"}.get(platform.machine())
    if arch is None:
        raise ValueError("Neovim updater supports only x86_64 and arm64.")
    installed = _installed_neovim_version()
    legacy = _LEGACY_NVIM.is_file()
    standalone = _local_standalone_neovim_present()
    if update and installed is None and not legacy and not standalone:
        raise ValueError("Neovim is not installed. Use: sen install neovim")
    release = _read_release()
    version = release["tag_name"][1:]
    if installed and _version_at_least(installed, version) and not legacy and not standalone:
        print(f"Neovim {installed} is already current (upstream stable {version}).")
        return installed
    url, size, digest = _neovim_asset(release, arch)
    with tempfile.TemporaryDirectory(prefix="sentinel-neovim-") as folder:
        package = Path(folder) / f"nvim-linux-{arch}.deb"
        _download_verified(url, size, digest, package)
        _verified_deb_fields(package, arch, version)
        # apt-get may use its unprivileged _apt helper to read local .debs.
        # The temp directory remains root-owned and never user-writable.
        os.chmod(folder, 0o755)
        package.chmod(0o644)
        subprocess.run(
            [APT_GET, "install", "-y",
             *(["--no-remove"] if get_lock_state().mode is LockMode.SELF_LOCK else []),
             "--", str(package)],
            check=True,
        )
    _remove_legacy_neovim_symlink()
    _retire_local_standalone_neovim()
    print(f"Neovim {version} installed successfully from the official stable release.")
    return version


def _install_unity():

    print("Installing Unity Hub repository key...")

    subprocess.run(
        ["install", "-d", "/etc/apt/keyrings"],
        check=True,
    )

    subprocess.run(
        [
            "bash",
            "-c",
            (
                "curl -fsSL https://hub.unity3d.com/linux/keys/public "
                "| gpg --dearmor -o /etc/apt/keyrings/unityhub.gpg"
            ),
        ],
        check=True,
    )

    print("Adding Unity repository...")

    repo_line = (
        "deb [arch=amd64 signed-by=/etc/apt/keyrings/unityhub.gpg] "
        "https://hub.unity3d.com/linux/repos/deb stable main"
    )

    subprocess.run(
        [
            "bash",
            "-c",
            f'echo "{repo_line}" > /etc/apt/sources.list.d/unityhub.list',
        ],
        check=True,
    )

    print("Updating package lists...")

    subprocess.run(
        ["apt", "update"],
        check=True,
    )

    print("Installing Unity Hub...")

    subprocess.run(
        ["apt", "install", "-y", "unityhub"],
        check=True,
    )

    print("Unity Hub installed successfully.")
