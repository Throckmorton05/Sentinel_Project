"""User-level autostart helper. Invoked by sentineld after authorization."""

from __future__ import annotations

import os
import configparser
import pwd
import shutil
import sys
from pathlib import Path

PREFIX = "sen-autostart-"
STEAM_ID = "sentinel-detected-steam.desktop"


def launcher_directories(home: Path) -> list[Path]:
    return [home / '.local/share/applications',
            home / '.local/share/flatpak/exports/share/applications',
            Path('/usr/share/applications'), Path('/usr/local/share/applications'),
            Path('/var/lib/flatpak/exports/share/applications'),
            Path('/var/lib/snapd/desktop/applications')]


def steam_launcher() -> str:
    from sentinel.guardian_apps import desktop
    executable = desktop.canonical('steam')
    if not desktop.is_system_steam_launcher(executable):
        raise ValueError('Could not find an installed Steam launcher.')
    command = executable.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
    return '[Desktop Entry]\nType=Application\nName=Steam\nIcon=steam\nExec="' + command + '"\n'


def list_autostart_apps() -> list[dict]:
    """Read actual desktop launchers as the current user, without executing them."""
    from sentinel.guardian_apps import desktop
    uid = os.getuid()
    home = Path(pwd.getpwuid(uid).pw_dir)
    inventory = desktop.discover(launcher_directories(home), uid=uid, include_hidden=True)
    entries = []
    for entry in inventory.values():
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str
        parser.read_string(entry['source'])
        if parser['Desktop Entry'].get('Hidden', '').lower() == 'true':
            continue
        entries.append({key: entry.get(key) for key in
            ('desktop_id', 'name', 'type', 'executable', 'flatpak_id', 'snap_instance')} |
            {'key': entry['desktop_id'], 'icon': parser['Desktop Entry'].get('Icon', ''),
             'selectable': True, 'reason': None})
    try:
        steam_launcher()
    except ValueError:
        pass
    else:
        # A real Steam executable can exist without a visible menu launcher.
        if not any(entry['name'].casefold() == 'steam' for entry in entries):
            entries.append({'key': STEAM_ID, 'desktop_id': STEAM_ID, 'name': 'Steam',
                'type': 'native', 'executable': desktop.canonical('steam'),
                'icon': 'steam', 'selectable': True, 'reason': None})
    return sorted(entries, key=lambda entry: (entry['name'].casefold(), entry['desktop_id']))


def list_managed_autostart_apps() -> list[dict]:
    """Read saved Sentinel entries as the desktop user, even if an app is gone."""
    from sentinel.guardian_apps.desktop import read_launcher
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    apps = []
    for path in (home / '.config/autostart').glob(PREFIX + '*.desktop'):
        desktop_id = path.name.removeprefix(PREFIX)
        name, icon = desktop_id.removesuffix('.desktop'), ''
        try:
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            parser.optionxform = str
            parser.read_string(read_launcher(path))
            entry = parser['Desktop Entry']
            name = entry.get('Name') or name
            icon = entry.get('Icon', '')
        except (OSError, UnicodeError, ValueError, configparser.Error, KeyError):
            # Keep damaged or dangling entries visible so they can be deleted.
            pass
        apps.append({'desktop_id': desktop_id, 'name': name[:255], 'icon': icon[:4096]})
    return sorted(apps, key=lambda app: (app['name'].casefold(), app['desktop_id']))


def manage_autostart(action: str, name: str) -> None:
    if os.geteuid() == 0:
        raise ValueError("Autostart helper must run as the desktop user.")
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("Invalid application name.")
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    requested = name.lower().removesuffix(".desktop")
    destination_dir = home / ".config/autostart"
    if action == "add":
        directories = launcher_directories(home)
        candidates = [
            (str(file.relative_to(directory)).replace('/', '-'), file)
            for directory in directories for file in directory.rglob("*.desktop")
        ]
        # Guardian's generated menu overrides are not original app launchers.
        from sentinel.guardian_apps.desktop import MARKER, read_launcher
        readable = []
        for key, file in candidates:
            try:
                source = read_launcher(file)
                parser = configparser.ConfigParser(interpolation=None, strict=False)
                parser.optionxform = str
                parser.read_string(source)
                entry = parser['Desktop Entry']
                if (not source.startswith(MARKER) and entry.get('Type') == 'Application'
                        and entry.get('Exec') and entry.get('Hidden', '').lower() != 'true'):
                    readable.append((key, file))
            except (OSError, UnicodeError, ValueError, configparser.Error, KeyError):
                continue
        candidates = readable
        if requested == STEAM_ID.removesuffix('.desktop'):
            source = steam_launcher()
            destination_dir.mkdir(parents=True, exist_ok=True)
            with (destination_dir / (PREFIX + STEAM_ID)).open('x', encoding='utf-8') as dst:
                dst.write(source)
            return
        exact = [(key, file) for key, file in candidates if key.removesuffix('.desktop').lower() == requested]
        matches = exact[:1] or [
            (key, file) for key, file in candidates if key.removesuffix('.desktop').lower().endswith(f".{requested}")
        ]
        if len(matches) != 1:
            raise ValueError("Could not find a unique installed application launcher.")
        desktop_id, source = matches[0]
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"{PREFIX}{desktop_id}"
        # Exclusive creation also refuses an existing symlink.
        with source.open("rb") as src, destination.open("xb") as dst:
            shutil.copyfileobj(src, dst)
    elif action == "remove":
        matches = [
            file
            for file in destination_dir.glob(f"{PREFIX}*.desktop")
            if (stem := file.name.removeprefix(PREFIX).removesuffix(".desktop").lower())
            == requested
            or stem.endswith(f".{requested}")
        ]
        if len(matches) != 1:
            raise ValueError("Could not find a unique Sentinel autostart entry.")
        matches[0].unlink()
    else:
        raise ValueError("Unknown autostart operation.")


if __name__ == "__main__":
    try:
        manage_autostart(*sys.argv[1:])
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from exc
