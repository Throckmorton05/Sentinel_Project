"""Catalog views agree with sentineld's fixed browser baseline."""
from types import SimpleNamespace
from unittest.mock import patch

from sentinel.appstore import app_loader
from sentinel.appstore.models import AppEntry
from sentinel.safe_steam.installation import NATIVE_APP_ID


def test_full_catalog_hides_flatpak_browsers():
    result = SimpleNamespace(stdout=(
        'Firefox\torg.mozilla.firefox\tBrowser\n'
        'Chrome\tcom.google.Chrome\tBrowser\n'
        'Steam\tcom.valvesoftware.Steam\tGames\n'
        'Krita\torg.kde.krita\tPainting\n'))
    with patch.object(app_loader.subprocess, 'run', return_value=result):
        apps = app_loader._load_flatpak_repo({})
    assert [app.flatpak_id for app in apps] == ['org.kde.krita']


def test_approved_catalog_hides_browsers_even_if_old_catalog_enables_them(tmp_path):
    catalog = tmp_path / 'approved.toml'
    catalog.write_text('''[apps.chrome]
flatpak_id = "com.google.Chrome"
name = "Chrome"
summary = "Browser"
categories = ["browsers"]
enabled = true
[apps.steam]
flatpak_id = "com.valvesoftware.Steam"
name = "Steam"
summary = "Games"
categories = ["games"]
enabled = true
[apps.krita]
flatpak_id = "org.kde.krita"
name = "Krita"
summary = "Painting"
categories = ["art"]
enabled = true
''')
    blocked = tmp_path / 'blocked.toml'
    blocked.write_text('[apps]\n')
    with patch.object(app_loader, 'CONFIG_PATH', catalog), patch.object(app_loader, 'BLOCKED_APPS_FILE', blocked):
        apps = app_loader._load_approved_apps({})
    assert [app.flatpak_id for app in apps] == ['org.kde.krita']


def test_native_steam_is_available_in_both_catalog_modes():
    other = AppEntry('krita', 'Krita', 'Painting', [], 'org.kde.krita')
    metadata = [{'flatpak_id': 'com.valvesoftware.Steam', 'icon_path': '/icons/steam.png'}]
    with patch.object(app_loader, 'parse_appstream', return_value=metadata), \
         patch.object(app_loader, '_load_approved_apps', return_value=[other]) as approved, \
         patch.object(app_loader, '_load_flatpak_repo', return_value=[other]) as full:
        for safe_installs in (True, False):
            apps = app_loader.load_apps(safe_installs)
            steam = apps[0]
            assert steam.name == 'Steam'
            assert steam.installation_id == NATIVE_APP_ID
            assert steam.installer == 'native_steam'
            assert steam.flatpak_id == ''
            assert steam.icon_path == '/icons/steam.png'
            assert apps[1:] == [other]
        approved.assert_called_once()
        full.assert_called_once()


def test_native_steam_catalog_does_not_require_appstream_metadata():
    with patch.object(app_loader, 'parse_appstream', side_effect=FileNotFoundError), \
         patch.object(app_loader, '_load_approved_apps', return_value=[]):
        apps = app_loader.load_apps(True)
    assert [app.installation_id for app in apps] == [NATIVE_APP_ID]
