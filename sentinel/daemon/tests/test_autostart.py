"""Launcher selection and the unprivileged autostart file operation."""
from types import SimpleNamespace

import pytest
from sen import autostart
from sentinel.guardian_apps import desktop


@pytest.fixture
def launchers(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    native = tmp_path / 'applications'
    snap = tmp_path / 'snap-applications'
    native.mkdir()
    snap.mkdir()
    monkeypatch.setattr(autostart.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(autostart.pwd, 'getpwuid', lambda uid: SimpleNamespace(pw_dir=str(home)))
    monkeypatch.setattr(autostart, 'launcher_directories', lambda h: [native, snap])
    monkeypatch.setattr(desktop, 'canonical', lambda name: None)
    return home, native, snap


@pytest.mark.parametrize('name', ['org.kde.kate.desktop', 'firefox_firefox.desktop', 'nested-editor.desktop'])
def test_exact_picker_launcher_add_and_remove(launchers, name):
    home, native, snap = launchers
    source = (snap / name if name.startswith('firefox') else
              native / 'nested/editor.desktop' if name.startswith('nested') else native / name)
    source.parent.mkdir(parents=True, exist_ok=True)
    data = '[Desktop Entry]\nType=Application\nName=App\nExec=/usr/bin/app\n'
    source.write_text(data)
    autostart.manage_autostart('add', name)
    target = home / '.config/autostart' / (autostart.PREFIX + name)
    assert target.read_text() == data
    autostart.manage_autostart('remove', name)
    assert not target.exists()


def test_alias_still_works_and_generated_guardian_overrides_are_skipped(launchers):
    home, native, snap = launchers
    (native / 'org.kde.kate.desktop').write_text(desktop.MARKER + '[Desktop Entry]\nExec=guardian-launch\n')
    source = snap / 'org.kde.kate.desktop'
    source.write_text('[Desktop Entry]\nType=Application\nName=Kate\nExec=/usr/bin/kate\n')
    autostart.manage_autostart('add', 'kate')
    assert (home / '.config/autostart/sen-autostart-org.kde.kate.desktop').read_text() == source.read_text()


def test_autostart_refuses_root_and_existing_symlink(launchers, monkeypatch):
    home, native, snap = launchers
    monkeypatch.setattr(autostart.os, 'geteuid', lambda: 0)
    with pytest.raises(ValueError, match='desktop user'):
        autostart.manage_autostart('add', 'app.desktop')
    monkeypatch.setattr(autostart.os, 'geteuid', lambda: 1000)
    (native / 'app.desktop').write_text('[Desktop Entry]\nType=Application\nName=App\nExec=/usr/bin/app\n')
    destination = home / '.config/autostart'
    destination.mkdir(parents=True)
    victim = home / 'keep.txt'
    victim.write_text('keep')
    (destination / 'sen-autostart-app.desktop').symlink_to(victim)
    with pytest.raises(FileExistsError):
        autostart.manage_autostart('add', 'app.desktop')
    assert victim.read_text() == 'keep'


def test_steam_fallback_is_launchable_and_removable(launchers, monkeypatch):
    home, native, snap = launchers
    monkeypatch.setattr(desktop, 'canonical', lambda name: '/usr/games/steam')
    monkeypatch.setattr(desktop, 'is_system_steam_launcher', lambda path: path == '/usr/games/steam')
    apps = autostart.list_autostart_apps()
    assert apps[0]['desktop_id'] == autostart.STEAM_ID
    autostart.manage_autostart('add', apps[0]['desktop_id'])
    target = home / '.config/autostart' / (autostart.PREFIX + autostart.STEAM_ID)
    assert 'Exec="/usr/games/steam"' in target.read_text()
    autostart.manage_autostart('remove', apps[0]['desktop_id'])
    assert not target.exists()


def test_catalog_uses_launcher_id_icons_and_hides_tombstones(launchers, monkeypatch):
    home, native, snap = launchers
    monkeypatch.setattr(desktop, 'canonical', lambda name: '/usr/bin/editor' if name == '/usr/bin/editor' else None)
    (snap / 'editor_editor.desktop').write_text('[Desktop Entry]\nType=Application\nName=Editor\nIcon=editor\nExec=/usr/bin/editor\n')
    (native / 'hidden.desktop').write_text('[Desktop Entry]\nType=Application\nName=Hidden\nHidden=true\nExec=/usr/bin/editor\n')
    apps = autostart.list_autostart_apps()
    assert [app['desktop_id'] for app in apps] == ['editor_editor.desktop']
    assert apps[0]['icon'] == 'editor' and apps[0]['selectable']


def test_tombstone_does_not_hide_original_autostart_launcher(launchers):
    home, native, snap = launchers
    (native / 'editor.desktop').write_text('[Desktop Entry]\nType=Application\nHidden=true\n')
    source = snap / 'editor.desktop'
    source.write_text('[Desktop Entry]\nType=Application\nName=Editor\nExec=/usr/bin/editor\n')
    autostart.manage_autostart('add', 'editor.desktop')
    assert (home / '.config/autostart/sen-autostart-editor.desktop').read_text() == source.read_text()


def test_saved_entries_list_survives_missing_installed_apps(launchers):
    home, native, snap = launchers
    source = snap / 'firefox_firefox.desktop'
    source.write_text('[Desktop Entry]\nType=Application\nName=Firefox\nIcon=firefox\nExec=/usr/bin/firefox\n')
    autostart.manage_autostart('add', source.name)
    source.unlink()
    directory = home / '.config/autostart'
    (directory / 'unmanaged.desktop').write_text('[Desktop Entry]\nName=Other\n')
    assert autostart.list_managed_autostart_apps() == [
        {'desktop_id': 'firefox_firefox.desktop', 'name': 'Firefox', 'icon': 'firefox'}]
    autostart.manage_autostart('remove', 'firefox_firefox.desktop')
    assert autostart.list_managed_autostart_apps() == []
    assert (directory / 'unmanaged.desktop').is_file()


def test_damaged_saved_entry_can_still_be_listed_and_removed(launchers):
    home, native, snap = launchers
    directory = home / '.config/autostart'
    directory.mkdir(parents=True)
    (directory / 'sen-autostart-missing.desktop').symlink_to(home / 'not-installed.desktop')
    assert autostart.list_managed_autostart_apps() == [
        {'desktop_id': 'missing.desktop', 'name': 'missing', 'icon': ''}]
    autostart.manage_autostart('remove', 'missing.desktop')
    assert autostart.list_managed_autostart_apps() == []
