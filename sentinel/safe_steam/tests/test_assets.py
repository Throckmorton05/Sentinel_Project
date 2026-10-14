"""Protected deployment, update integrity, idempotence and wheel completeness."""
import json
import os
import stat
from pathlib import Path
from unittest.mock import patch
import pytest
from sentinel.safe_steam import assets
from sentinel.dns.storage import protect_directory as real_protect

ROOT = Path(__file__).resolve().parents[3]

@pytest.fixture
def destinations(tmp_path):
    # Validate content separately from host UID in unprivileged test runners.
    def protect(path):
        if path.is_symlink():
            raise ValueError('Symlink destination')
        path.mkdir(parents=True, exist_ok=True)
    original = os.fstat
    with (patch.object(assets, 'SAFE_STEAM_EXTENSION_DIR', tmp_path/'native'),
          patch.object(assets, 'SAFE_STEAM_PROVISIONING_DIR', tmp_path/'provisioning'),
          patch.object(assets, 'SAFE_STEAM_DISTRIBUTION_DIR', tmp_path/'chrome'),
          patch.object(assets, 'protect_directory', side_effect=protect),
          patch.object(assets.os, 'fstat', wraps=os.fstat) as fstat):
        # Keep regular-file/mode checks but simulate the installed runtime owner.
        def root_owned(fd):
            value=list(original(fd)); value[4]=0
            return os.stat_result(value)
        fstat.side_effect=root_owned
        yield tmp_path


def test_installs_complete_native_assets_and_repairs_stale_files(destinations):
    assert assets.native_update_needed()
    assets.install_native_assets()
    assert not assets.native_update_needed()
    native=destinations/'native'
    assert set(p.name for p in native.iterdir())==assets.NATIVE_FILES
    assert (destinations/'provisioning'/'safesteam-pref-entry.json').is_file()
    for path in native.iterdir():
        assert stat.S_IMODE(path.stat().st_mode)==0o644
    inode=(native/'content.js').stat().st_ino
    assets.install_native_assets()
    assert (native/'content.js').stat().st_ino==inode
    (native/'content.js').chmod(0o666)
    assert assets.native_update_needed()
    assets.install_native_assets()
    assert stat.S_IMODE((native/'content.js').stat().st_mode)==0o644
    (native/'content.js').write_text('outdated extension')
    (native/'obsolete.js').write_text('old code')
    assert assets.native_update_needed()
    assets.install_native_assets()
    assert not assets.native_update_needed()
    assert not (native/'obsolete.js').exists()


def test_installs_signed_chrome_package_and_matching_update_manifest(destinations):
    assets.install_chrome_assets()
    assert (destinations/'chrome'/'browser_extension.crx').read_bytes().startswith(b'Cr24')
    inode=(destinations/'chrome'/'browser_extension.crx').stat().st_ino
    assets.install_chrome_assets()
    assert (destinations/'chrome'/'browser_extension.crx').stat().st_ino==inode
    (destinations/'chrome'/'browser_extension.crx').unlink()
    assets.install_chrome_assets()
    assert (destinations/'chrome'/'browser_extension.crx').is_file()


def test_malformed_bundle_does_not_overwrite_installed_files(destinations):
    assets.install_native_assets()
    before=(destinations/'native'/'content.js').read_bytes()
    with patch.object(assets,'native_bundle',side_effect=ValueError('bundle mismatch')):
        with pytest.raises(ValueError):
            assets.install_native_assets()
    assert (destinations/'native'/'content.js').read_bytes()==before


def test_symlink_destination_is_rejected_without_modifying_target(destinations):
    target=destinations/'private';target.write_text('keep')
    native=destinations/'native';native.mkdir()
    (native/'content.js').symlink_to(target)
    with pytest.raises(OSError):
        assets.install_native_assets()
    assert target.read_text()=='keep'


def test_root_reader_rejects_user_owned_or_writable_bundle(tmp_path):
    path=tmp_path/'bundle';path.write_bytes(b'not trusted')
    path.chmod(0o666)
    with patch.object(assets,'protect_directory'):
        with pytest.raises(ValueError):
            assets.read_bytes(path)
    path.chmod(0o644)
    value=list(path.stat());value[4]=1000
    with patch.object(assets,'protect_directory'), patch.object(assets.os,'fstat',return_value=os.stat_result(value)):
        with pytest.raises(ValueError):
            assets.read_bytes(path)


def test_corrupted_chrome_signature_is_rejected_before_install(destinations):
    data=bytearray((assets.BUNDLE/'browser_distribution'/'browser_extension.crx').read_bytes())
    data[-1] ^= 1
    with pytest.raises(ValueError, match='signature'):
        assets.verify_chrome_package(bytes(data))
    assert not (destinations/'chrome'/'browser_extension.crx').exists()


def test_invalid_chrome_update_manifest_is_rejected_before_install(destinations):
    original=assets.read_bytes
    def wrong_metadata(path):
        if path.name=='updates.xml':
            return b'<broken'
        return original(path)
    with patch.object(assets,'read_bytes',side_effect=wrong_metadata):
        with pytest.raises(ValueError, match='update manifest'):
            assets.install_chrome_assets()
    assert not (destinations/'chrome'/'browser_extension.crx').exists()
