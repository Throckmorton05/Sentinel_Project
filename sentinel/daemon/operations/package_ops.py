"""Validate package requests and execute Sentinel-approved installers."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from sen.install.packages import APPROVED_PACKAGES
from sentinel.browser_policies.restrictions import require_supported_browser_package
from sentinel.vm_protection import require_allowed_package
from sentinel.daemon.core.constants import APT_GET, ENV, PIPX, RUNUSER
from sentinel.daemon.core.lock_state import LockMode, get_lock_state
from sentinel.daemon.core.session import _peer_user
from sentinel.daemon.operations.custom_installer import custom_install, install_latest_neovim, _remove_legacy_neovim_symlink
from sentinel.daemon.operations import custom_installer
from sentinel.daemon.operations.reaper_installer import install_latest_reaper, uninstall_reaper
from sentinel.daemon.security.policy import ChangeKind, require_policy
from sentinel.safe_steam.installation import NATIVE_PACKAGE_NAMES


def _steam_package_changed(name: str) -> None:
    if name in NATIVE_PACKAGE_NAMES:
        from sentinel.safe_steam.status import request_reconcile
        request_reconcile()

_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._:-]{0,127}$")


def _require_package_name(
    params: dict[str, Any],
) -> str:
    """
    Extract and validate a package/alias name from a request.

    This prevents option injection and prevents paths or arbitrary
    command strings from being passed into package-management tools.
    """

    name = params.get("name")

    if not isinstance(name, str):
        raise ValueError("Missing package name.")

    name = name.strip()

    if not _PACKAGE_NAME_RE.fullmatch(name):
        raise ValueError("Invalid package name.")

    return name


def _run_pipx_as_user(
    uid: int,
    *args: str,
) -> None:
    """
    Run pipx as the requesting desktop user instead of root.

    pipx applications belong in the user's environment, not /root.
    """

    user = _peer_user(uid)

    subprocess.run(
        [
            RUNUSER,
            "-u",
            user.pw_name,
            "--",
            ENV,
            f"HOME={user.pw_dir}",
            f"USER={user.pw_name}",
            f"LOGNAME={user.pw_name}",
            PIPX,
            *args,
        ],
        check=True,
    )


def _install_apt(packages: list[str]) -> None:
    for package in packages:
        require_supported_browser_package(package)
        require_allowed_package(package)
    subprocess.run(
        [
            APT_GET,
            "install",
            "-y",
            *(["--no-remove"] if get_lock_state().mode is LockMode.SELF_LOCK else []),
            "--",
            *packages,
        ],
        check=True,
    )


def _install_custom(
    package_name: str,
    uid: int,
) -> None:
    """
    Run one of Sentinel's explicitly approved custom installers.

    Resolve the Downloads directory from the authenticated peer UID.
    The backend installer receives that context explicitly for each request.
    """

    user = _peer_user(uid)

    custom_install(package_name, downloads_dir=Path(user.pw_dir) / "Downloads")


def _install_package(
    package_name: str,
    uid: int,
    *,
    guardian_authenticated: bool = False,
) -> dict[str, Any] | None:
    """
    Install software according to Sentinel's package policy.

    Unlocked permits any APT package. Self Lock permits approved aliases
    and their exact APT package names. Guardian Lock requires a verified PIN.
    """

    package_name = _require_package_name({"name": package_name})
    require_supported_browser_package(package_name)
    require_allowed_package(package_name)
    approved_apt = {
        package
        for definition in APPROVED_PACKAGES.values()
        if definition["type"] == "apt"
        for package in definition["packages"]
    }
    approved = package_name in APPROVED_PACKAGES or package_name in approved_apt
    require_policy(
        "package.install",
        ChangeKind.MAINTAIN if approved else ChangeKind.WEAKEN,
        guardian_authenticated=guardian_authenticated,
    )

    if package_name == "neovim" and (
        custom_installer._installed_neovim_version() is not None
        or custom_installer._LEGACY_NVIM.is_file()
        or custom_installer._local_standalone_neovim_present()
    ):
        return {"already_installed": True, "message": "neovim already installed"}

    if package_name in APPROVED_PACKAGES:
        package_data = APPROVED_PACKAGES[package_name]

        install_type = package_data["type"]
        packages = package_data["packages"]

        for package in packages:
            require_allowed_package(package)

        if install_type == "apt":
            _install_apt(packages)
            _steam_package_changed(package_name)
            return

        if install_type == "pipx":
            for package in packages:
                _run_pipx_as_user(
                    uid,
                    "install",
                    package,
                )

            return

        if install_type == "custom":
            _install_custom(
                package_name,
                uid,
            )
            return

        if install_type == "flatpak":
            raise ValueError(
                f"{package_name} is managed through the Sentinel App Store."
            )

        raise ValueError(f"Unknown install type: {install_type}")

    _install_apt([package_name])
    _steam_package_changed(package_name)


def _update_package(
    package_name: str,
    uid: int,
    *,
    guardian_authenticated: bool = False,
) -> str:
    """Update a fixed-source approved application through the daemon."""
    package_name = _require_package_name({"name": package_name})
    if package_name not in ("neovim", "reaper"):
        raise ValueError("Only 'sen update neovim' and 'sen update reaper' are supported.")
    require_allowed_package(package_name)
    require_policy(
        "package.update", ChangeKind.MAINTAIN,
        guardian_authenticated=guardian_authenticated,
    )
    if package_name == "neovim":
        return install_latest_neovim(update=True)
    return install_latest_reaper(update=True)


def _uninstall_apt(
    packages: list[str],
) -> None:

    subprocess.run(
        [
            APT_GET,
            "remove",
            "-y",
            "--",
            *packages,
        ],
        check=True,
    )


def _uninstall_custom(
    package_name: str,
) -> None:
    """
    Remove applications installed by Sentinel custom installers.
    """

    if package_name == "neovim":
        # New installations are tracked in dpkg; old tarball installations
        # left /opt and /usr/local/bin behind. Clean those up too.
        _uninstall_apt(["neovim"])
        _remove_legacy_neovim_symlink()
        install_dir = Path("/opt/nvim-linux-x86_64")
        if install_dir.is_dir() and not install_dir.is_symlink():
            shutil.rmtree(install_dir)
        return

    if package_name == "unity":
        _uninstall_apt(["unityhub"])
        return

    if package_name == "reaper":
        uninstall_reaper()
        return

    raise ValueError(f"No custom uninstaller exists for {package_name}.")


def _uninstall_package(
    package_name: str,
    uid: int,
    *,
    guardian_authenticated: bool = False,
) -> None:
    """
    Uninstall software according to Sentinel's package policy.
    """

    package_name = _require_package_name({"name": package_name})
    # Removal can also remove dependencies or Sentinel components. Until
    # there is a dependency-aware removal policy, treat every removal as weakening.
    require_policy(
        "package.uninstall",
        ChangeKind.WEAKEN,
        guardian_authenticated=guardian_authenticated,
    )

    if package_name in APPROVED_PACKAGES:
        package_data = APPROVED_PACKAGES[package_name]

        install_type = package_data["type"]
        packages = package_data["packages"]

        if install_type == "apt":
            _uninstall_apt(packages)
            _steam_package_changed(package_name)
            return

        if install_type == "pipx":
            for package in packages:
                _run_pipx_as_user(
                    uid,
                    "uninstall",
                    package,
                )

            return

        if install_type == "custom":
            _uninstall_custom(package_name)
            return

        if install_type == "flatpak":
            raise ValueError(
                f"{package_name} is managed through the Sentinel App Store."
            )

        raise ValueError(f"Unknown install type: {install_type}")

    _uninstall_apt([package_name])
    _steam_package_changed(package_name)
