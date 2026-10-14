"""Route each card to its installer; native Steam uses the same RPC as sen."""
import subprocess

from sentinel.daemon import client
from sentinel.safe_steam.installation import NATIVE_APP_ID, native_steam_launcher
from . import flatpak_backend
from .models import AppEntry


def _native_steam(app: AppEntry) -> bool:
    if app.installer == "flatpak":
        return False
    if app.installer == "native_steam" and app.app_id == NATIVE_APP_ID and not app.flatpak_id:
        return True
    raise ValueError("Unsupported application installer.")


def install_app(app: AppEntry, *, guardian_pin: str | None = None):
    if _native_steam(app):
        return client.install_package("steam", guardian_pin=guardian_pin)
    return flatpak_backend.install_app(app.flatpak_id, guardian_pin=guardian_pin)


def uninstall_app(app: AppEntry, *, guardian_pin: str | None = None):
    if _native_steam(app):
        return client.uninstall_package("steam", guardian_pin=guardian_pin)
    return flatpak_backend.uninstall_app(app.flatpak_id, guardian_pin=guardian_pin)


def launch_app(app: AppEntry):
    if _native_steam(app):
        launcher = native_steam_launcher()
        if launcher is None:
            raise FileNotFoundError("Native Steam is no longer installed.")
        return subprocess.Popen([str(launcher)], start_new_session=True)
    return flatpak_backend.launch_app(app.flatpak_id)
