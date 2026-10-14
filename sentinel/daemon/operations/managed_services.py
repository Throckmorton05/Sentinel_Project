"""Create and manage only Sentinel-managed systemd units."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from sentinel.daemon.core.constants import RUNUSER, SYSTEMCTL
from sentinel.daemon.core.session import _peer_user
from sentinel.settings.paths import MANAGED_SERVICE_DIR, SYSTEMD_SYSTEM_DIR


MANAGED_SERVICE_PREFIX = "sen-managed-"


def _normalize_service_name(
    name: str,
) -> str:
    """
    Validate and normalize a Sentinel-managed service name.
    """

    if name.endswith(".service"):
        name = name[:-8]

    if name.startswith(MANAGED_SERVICE_PREFIX):
        name = name[len(MANAGED_SERVICE_PREFIX) :]

    if not re.fullmatch(
        r"[A-Za-z0-9_.-]+",
        name,
    ):
        raise ValueError(
            "Service names may only contain letters, numbers, '.', '_', and '-'."
        )

    if not name:
        raise ValueError("Service name cannot be empty.")

    return name


def _require_service_name(
    params: dict[str, Any],
) -> str:
    name = params.get("name")

    if not isinstance(name, str):
        raise ValueError("Missing service name.")

    return _normalize_service_name(name)


def _managed_service_info(
    name: str,
) -> tuple[str, Path]:

    unit_name = f"{MANAGED_SERVICE_PREFIX}{name}.service"

    service_path = MANAGED_SERVICE_DIR / unit_name

    return unit_name, service_path


def _systemd_quote(
    value: str,
) -> str:
    """
    Quote a value safely for systemd ExecStart.
    """

    value = value.replace(
        "\\",
        "\\\\",
    )

    value = value.replace(
        '"',
        '\\"',
    )

    value = value.replace(
        "%",
        "%%",
    )

    return f'"{value}"'


def _require_managed_service(
    name: str,
) -> tuple[str, Path]:

    unit_name, service_path = _managed_service_info(name)

    if not service_path.is_file():
        raise ValueError(f"'{name}' is not a Sentinel-managed service.")

    return unit_name, service_path


def _add_managed_service(
    name: str,
    path_value: str,
    uid: int,
) -> None:
    """
    Register a user-owned program as a systemd service.

    The unit itself is root-owned, but the program always runs as
    the authenticated desktop user.
    """

    user = _peer_user(uid)

    user_home = Path(user.pw_dir).resolve()

    try:
        program_path = Path(path_value).expanduser().resolve(strict=True)

    except FileNotFoundError as exc:
        raise ValueError(f"'{path_value}' does not exist.") from exc

    if not program_path.is_file():
        raise ValueError("Service path must point to a file.")

    #
    # For now, user-managed services must come from that user's
    # own home directory.
    #
    # They can still call system binaries from their script.
    #

    if not program_path.is_relative_to(user_home):
        raise ValueError(
            "Managed service programs must be inside the desktop user's home directory."
        )

    unit_name, service_path = _managed_service_info(name)

    if service_path.exists():
        raise ValueError(f"A Sentinel-managed service named '{name}' already exists.")

    if program_path.suffix.lower() == ".py":
        exec_start = f"/usr/bin/python3 {_systemd_quote(str(program_path))}"

    else:
        #
        # Check executable permission as the actual user,
        # not as root.
        #

        result = subprocess.run(
            [
                RUNUSER,
                "-u",
                user.pw_name,
                "--",
                "/usr/bin/test",
                "-x",
                str(program_path),
            ],
            check=False,
        )

        if result.returncode != 0:
            raise ValueError("Non-Python service files must be executable.")

        exec_start = _systemd_quote(str(program_path))

    unit_contents = f"""[Unit]
Description=Sentinel Managed Service: {name}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user.pw_name}
ExecStart={exec_start}
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""

    MANAGED_SERVICE_DIR.mkdir(
        parents=True,
        exist_ok=True,
        mode=0o755,
    )

    service_path.write_text(
        unit_contents,
        encoding="utf-8",
    )

    service_path.chmod(0o644)

    try:
        subprocess.run(
            [
                SYSTEMCTL,
                "link",
                str(service_path),
            ],
            check=True,
        )

        subprocess.run(
            [
                SYSTEMCTL,
                "daemon-reload",
            ],
            check=True,
        )

    except subprocess.CalledProcessError:
        service_path.unlink(missing_ok=True)

        raise


def _start_managed_service(
    name: str,
) -> None:

    unit_name, _ = _require_managed_service(name)

    subprocess.run(
        [
            SYSTEMCTL,
            "start",
            unit_name,
        ],
        check=True,
    )


def _stop_managed_service(
    name: str,
) -> None:

    unit_name, _ = _require_managed_service(name)

    subprocess.run(
        [
            SYSTEMCTL,
            "stop",
            unit_name,
        ],
        check=True,
    )


def _enable_managed_service(
    name: str,
) -> None:

    unit_name, _ = _require_managed_service(name)

    subprocess.run(
        [
            SYSTEMCTL,
            "enable",
            unit_name,
        ],
        check=True,
    )


def _disable_managed_service(
    name: str,
) -> None:

    unit_name, service_path = _require_managed_service(name)

    subprocess.run(
        [
            SYSTEMCTL,
            "disable",
            unit_name,
        ],
        check=True,
    )

    #
    # systemctl disable may remove the link created by
    # systemctl link. Re-link the unit so it remains available
    # for manual start/stop while disabled at boot.
    #

    subprocess.run(
        [
            SYSTEMCTL,
            "link",
            str(service_path),
        ],
        check=True,
    )

    subprocess.run(
        [
            SYSTEMCTL,
            "daemon-reload",
        ],
        check=True,
    )


def _delete_managed_service(
    name: str,
) -> None:

    unit_name, service_path = _require_managed_service(name)

    subprocess.run(
        [
            SYSTEMCTL,
            "stop",
            unit_name,
        ],
        check=False,
    )

    subprocess.run(
        [
            SYSTEMCTL,
            "disable",
            unit_name,
        ],
        check=False,
    )

    systemd_link = SYSTEMD_SYSTEM_DIR / unit_name

    if systemd_link.is_symlink():
        target = systemd_link.resolve(strict=False)

        if target != service_path.resolve():
            raise ValueError(
                "Systemd unit points somewhere unexpected. Refusing to delete it."
            )

        systemd_link.unlink()

    elif systemd_link.exists():
        raise ValueError(
            "A non-Sentinel systemd unit exists with this name. Refusing to delete it."
        )

    service_path.unlink()

    subprocess.run(
        [
            SYSTEMCTL,
            "daemon-reload",
        ],
        check=True,
    )
