"""Supported Steam installation identities, shared by daemon and store."""
from pathlib import Path

from sentinel.guardian_apps.desktop import is_system_steam_launcher

UNSUPPORTED_FLATPAK_IDS = frozenset({'com.valvesoftware.Steam'})
NATIVE_APP_ID = 'native:steam'
NATIVE_LAUNCHERS = (Path('/usr/games/steam'), Path('/usr/bin/steam'),
                    Path('/usr/local/bin/steam'))
NATIVE_PACKAGE_NAMES = frozenset({'steam', 'steam-installer', 'steam-launcher'})


def native_steam_launcher() -> Path | None:
    # Fixed system paths only. A stale home profile, a Flatpak launcher, or a
    # desktop-user-owned script does not mean supported native Steam is installed.
    return next((path for path in NATIVE_LAUNCHERS if is_system_steam_launcher(path)), None)


def native_steam_installed() -> bool:
    return native_steam_launcher() is not None
