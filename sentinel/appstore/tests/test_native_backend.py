"""Native launch remains in the desktop session; installer identities are fixed."""
from pathlib import Path
from unittest.mock import patch

import pytest

from sentinel.appstore import backend
from sentinel.appstore.models import AppEntry, native_steam_entry
from sentinel.daemon import client


def test_native_launch_uses_only_validated_system_launcher():
    with patch.object(backend, 'native_steam_launcher', return_value=Path('/usr/games/steam')), \
         patch.object(backend.subprocess, 'Popen') as launch:
        backend.launch_app(native_steam_entry())
    launch.assert_called_once_with(['/usr/games/steam'], start_new_session=True)


def test_removed_native_steam_does_not_fall_back_to_flatpak_or_path_lookup():
    with patch.object(backend, 'native_steam_launcher', return_value=None), \
         patch.object(backend.subprocess, 'Popen') as launch:
        with pytest.raises(FileNotFoundError, match='no longer installed'):
            backend.launch_app(native_steam_entry())
    launch.assert_not_called()


@pytest.mark.parametrize('action', [backend.install_app, backend.uninstall_app, backend.launch_app])
@pytest.mark.parametrize('app', [
    AppEntry('native:other', 'Other', '', [], '', installer='native_steam'),
    AppEntry('native:steam', 'Steam', '', [], 'com.valvesoftware.Steam', installer='native_steam'),
    AppEntry('native:steam', 'Steam', '', [], '', installer='unknown'),
])
def test_malformed_native_cards_cannot_choose_an_arbitrary_installer(action, app):
    with patch.object(client, 'install_package') as install, \
         patch.object(client, 'uninstall_package') as remove, \
         patch.object(backend.subprocess, 'Popen') as launch:
        with pytest.raises(ValueError, match='Unsupported'):
            action(app)
    install.assert_not_called()
    remove.assert_not_called()
    launch.assert_not_called()
