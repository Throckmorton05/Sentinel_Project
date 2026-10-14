from __future__ import annotations

import shutil
import subprocess


def resolve_executable(app_name: str) -> str:
    executable = shutil.which(app_name)

    if executable is None:
        raise ValueError(f"Could not resolve executable for '{app_name}'")

    return executable


def _flatpak_command(flatpak_id: str) -> str:
    """Read the configured Flatpak command without launching the sandbox."""
    metadata = subprocess.run(
        ["flatpak", "info", "--show-metadata", flatpak_id],
        capture_output=True,
        text=True,
        check=True,
    )

    for line in metadata.stdout.splitlines():
        if line.startswith("command="):
            command = line.removeprefix("command=").strip()
            if command:
                return command

    raise ValueError(f"Could not determine command for Flatpak '{flatpak_id}'")


def resolve_flatpak_executable(flatpak_id: str) -> str:
    """Return the executable path as seen inside the Flatpak mount namespace.

    The previous implementation launched `flatpak run --command=which` merely
    to discover this path. That conflicts with runtime blocking: sentineld would
    correctly identify the helper sandbox as belonging to a blocked app and
    terminate it. Flatpak metadata already supplies the application command, so
    no blocked sandbox needs to be started during policy reconciliation.
    """
    command = _flatpak_command(flatpak_id)
    if command.startswith("/"):
        return command
    if "/" in command:
        return f"/app/{command.lstrip('/')}"
    return f"/app/bin/{command}"
