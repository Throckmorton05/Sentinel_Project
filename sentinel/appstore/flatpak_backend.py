"""Daemon-backed management; application launch stays in the desktop session."""
import subprocess

from sentinel.daemon import client


def install_app(flatpak_id: str, *, guardian_pin: str | None = None):
    return client.install_flatpak(flatpak_id, guardian_pin=guardian_pin)


def uninstall_app(flatpak_id: str, *, guardian_pin: str | None = None):
    return client.uninstall_flatpak(flatpak_id, guardian_pin=guardian_pin)


def launch_app(flatpak_id: str):
    # Do not block the Qt event loop until the launched application exits.
    return subprocess.Popen(["flatpak", "run", "--system", flatpak_id], start_new_session=True)


def get_installed_entries() -> list[dict]:
    return client.get_appstore_installed()["apps"]


def get_installed_apps() -> set[str]:
    return {entry["id"] for entry in get_installed_entries()}


def update_all_apps(*, guardian_pin: str | None = None):
    return client.update_flatpak(guardian_pin=guardian_pin)
