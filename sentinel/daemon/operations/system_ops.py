"""Explicit privileged update, restart and shutdown operations."""
from __future__ import annotations

import subprocess
from pathlib import Path

from sentinel.daemon.core.constants import SYSTEMCTL
from sentinel.daemon.operations.apt import INDEX_TIMEOUT, run_apt

SENTINEL_PACKAGES = (
    "sentinel", "sentinel-core", "sentinel-assets", "sentinel-desktop",
    "sentinel-models", "sentinel-platform",
)


def _sentinel_update() -> dict:
    """Upgrade the installed Sentinel release through authenticated APT."""
    inventory = subprocess.check_output(
        ["/usr/bin/dpkg-query", "-W", "-f=${binary:Package}\t${db:Status-Status}\n"],
        text=True, timeout=30,
    )
    installed = {line.split("\t")[0].split(":")[0]
                 for line in inventory.splitlines() if line.endswith("\tinstalled")}
    packages = [name for name in SENTINEL_PACKAGES if name in installed]
    if "sentinel" not in packages:
        raise ValueError("The Sentinel Debian release is not installed on this computer.")
    run_apt(["update"], timeout=INDEX_TIMEOUT)
    # No caller-selected package names, URLs, local DEBs, or downgrade flags.
    # Exact release dependencies bring the installed components forward together.
    run_apt(["install", "--only-upgrade", "-y", "--", *packages])
    return {"status": "complete", "packages": packages,
            "reboot_required": Path("/run/reboot-required").exists()}


def _system_update() -> dict:
    """
    Update package indexes and upgrade installed packages.
    """

    run_apt(["update"], timeout=INDEX_TIMEOUT)
    # The scheduled worker is a separate process without initialized in-memory
    # lock state. Updates maintain protection in every mode and never remove.
    run_apt(["upgrade", "-y", "--no-remove"])
    return {"status": "complete", "reboot_required": Path("/run/reboot-required").exists()}


def _system_restart() -> None:
    """
    Request a system reboot.

    Popen is intentional because the machine may begin shutting down
    before sentineld has time to finish a synchronous subprocess call.
    """

    subprocess.Popen(
        [
            SYSTEMCTL,
            "reboot",
        ],
        start_new_session=True,
    )


def _system_shutdown() -> None:
    """
    Request a system poweroff.
    """

    subprocess.Popen(
        [
            SYSTEMCTL,
            "poweroff",
        ],
        start_new_session=True,
    )
