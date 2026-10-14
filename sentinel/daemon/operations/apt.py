"""Run unattended APT updates with a bounded package-process lifetime."""
from __future__ import annotations

import os
import signal
import subprocess

from sentinel.daemon.core.constants import APT_GET

INDEX_TIMEOUT = 600
UPGRADE_TIMEOUT = 3600
TERMINATE_GRACE = 30


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def run_apt(args: list[str], *, timeout: float = UPGRADE_TIMEOUT) -> None:
    # Keep existing local configuration, including Sentinel's policy files.
    # -y alone does not suppress debconf, dpkg, or needrestart prompts.
    command = [
        APT_GET,
        "-o", "DPkg::Lock::Timeout=120",
        "-o", "Acquire::Retries=2",
        "-o", "APT::Update::Error-Mode=any",
        "-o", "Acquire::http::Timeout=30",
        "-o", "Acquire::https::Timeout=30",
        "-o", "Dpkg::Options::=--force-confdef",
        "-o", "Dpkg::Options::=--force-confold",
        "--no-remove",
        *args,
    ]
    env = {
        **os.environ,
        "DEBIAN_FRONTEND": "noninteractive",
        "DEBIAN_PRIORITY": "critical",
        # Sentinel's package lifecycle queues restarts after the APT lock is
        # released. needrestart must not restart the broker mid-transaction.
        "NEEDRESTART_MODE": "l",
        "UCF_FORCE_CONFFOLD": "1",
    }
    print(f"[Sentinel updates] Starting apt-get {args[0]}.", flush=True)
    # Output stays in the backend log. A separate session prevents prompts
    # from opening the backend's controlling terminal via /dev/tty.
    with subprocess.Popen(command, stdin=subprocess.DEVNULL,
                          env=env, start_new_session=True) as process:
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # Stop the package process group, not just apt-get, before the
            # RPC releases its mutation lock. Never retry a partial upgrade.
            _signal_group(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=TERMINATE_GRACE)
            except subprocess.TimeoutExpired:
                pass
            finally:
                _signal_group(process.pid, signal.SIGKILL)
                process.wait()
            message = (
                f"System update timed out during apt-get {args[0]} after "
                f"{timeout:g} seconds. Some updates may already be installed. "
                "Check the Sentinel backend log and package configuration "
                "before retrying."
            )
            print(f"[Sentinel updates] {message}", flush=True)
            raise TimeoutError(message) from exc
    if returncode:
        print(f"[Sentinel updates] apt-get {args[0]} failed ({returncode}).", flush=True)
        raise subprocess.CalledProcessError(returncode, command)
    print(f"[Sentinel updates] Finished apt-get {args[0]}.", flush=True)
