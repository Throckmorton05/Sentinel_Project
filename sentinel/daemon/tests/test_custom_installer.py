"""Regression tests for approved, daemon-side custom package installation."""
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

import pytest

from sentinel.daemon.operations import custom_installer, package_ops


@pytest.mark.parametrize("uid", [1000, 1001])
def test_reaper_install_dispatch_uses_authenticated_peer(tmp_path, monkeypatch, uid):
    home = tmp_path / str(uid)
    (home / "Downloads").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated"))
    with patch.object(package_ops, "_peer_user", return_value=SimpleNamespace(pw_dir=str(home))) as peer, \
         patch.object(package_ops, "custom_install") as install:
        package_ops._install_custom("reaper", uid)
    peer.assert_called_once_with(uid)
    install.assert_called_once_with("reaper", downloads_dir=home / "Downloads")


def test_neovim_never_uses_user_supplied_archive(tmp_path, monkeypatch):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "nvim-linux-x86_64.tar.gz").write_bytes(b"malicious archive")
    (downloads / "nvim-linux-x86_64.deb").write_bytes(b"malicious package")
    with patch.object(custom_installer, "install_latest_neovim", return_value="0.12.5") as install, \
         patch.object(custom_installer, "_find_file") as find:
        custom_installer.custom_install("neovim", downloads_dir=downloads)
    install.assert_called_once_with()
    find.assert_not_called()


def test_unity_dispatch_does_not_require_local_archive(tmp_path):
    with patch.object(custom_installer, "_install_unity") as install:
        custom_installer.custom_install("unity", downloads_dir=tmp_path / "absent")
    install.assert_called_once_with()


def test_unknown_alias_never_executes_command(tmp_path):
    with patch.object(custom_installer.subprocess, "run") as run:
        with pytest.raises(ValueError, match="No custom installer"):
            custom_installer.custom_install("unknown", downloads_dir=tmp_path)
    run.assert_not_called()


def fake_release(*, digest="a" * 64, size=9000, tag="v0.12.5", arch="x86_64"):
    name = f"nvim-linux-{arch}.deb"
    return {
        "tag_name": tag, "draft": False, "prerelease": False,
        "assets": [{"name": name, "size": size, "digest": f"sha256:{digest}",
                    "browser_download_url": f"https://github.com/neovim/neovim-releases/releases/download/{tag}/{name}"}],
    }


def test_release_asset_must_match_fixed_upstream_and_checksum():
    info = fake_release()
    url, size, digest = custom_installer._neovim_asset(info, "x86_64")
    assert url.endswith("/v0.12.5/nvim-linux-x86_64.deb")
    assert size == 9000 and digest == "a" * 64
    for mutation in (
        lambda asset: asset.pop("digest"),
        lambda asset: asset.update(digest="sha256:NOT_A_HASH"),
        lambda asset: asset.update(browser_download_url="https://attacker.invalid/file.deb"),
        lambda asset: asset.update(size=999999999),
    ):
        changed = fake_release()
        mutation(changed["assets"][0])
        with pytest.raises(ValueError):
            custom_installer._neovim_asset(changed, "x86_64")


def test_verified_deb_rejects_unexpected_package_fields(tmp_path):
    package = tmp_path / "fake.deb"
    package.touch()
    for version in ("0.12.5", "0.12.5-1"):
        fields = {"Package": "neovim", "Architecture": "amd64", "Version": version}
        with patch.object(custom_installer.subprocess, "check_output", side_effect=lambda argv, text: fields[argv[-1]]):
            custom_installer._verified_deb_fields(package, "x86_64", "0.12.5")
    for wrong in ("Package", "Architecture", "Version"):
        fields = {"Package": "neovim", "Architecture": "amd64", "Version": "0.12.5"}
        fields[wrong] = "evil"
        with patch.object(custom_installer.subprocess, "check_output", side_effect=lambda argv, text: fields[argv[-1]]):
            with pytest.raises(ValueError, match="unexpected"):
                custom_installer._verified_deb_fields(package, "x86_64", "0.12.5")


def test_neovim_refuses_insecure_redirects():
    from urllib.request import Request
    with pytest.raises(ValueError, match="insecure"):
        custom_installer._HttpsRedirectsOnly().redirect_request(
            Request("https://github.com/source"), None, 302, "Moved", {}, "http://example.org/evil.deb")


def test_neovim_refuses_bad_checksum_or_size(tmp_path):
    data = b"some bytes of a fake deb"
    file = tmp_path / "fake.deb"
    class FakeHTTP:
        def open(self, req, timeout):
            return io.BytesIO(data)
    with patch.object(custom_installer, "_HTTP", FakeHTTP()):
        custom_installer._download_verified("https://github.com/asset", len(data), hashlib.sha256(data).hexdigest(), file)
        assert file.read_bytes() == data
        file.unlink()
        with pytest.raises(ValueError, match="SHA-256"):
            custom_installer._download_verified("https://github.com/asset", len(data), "0" * 64, file)
        file.unlink()
        with pytest.raises(ValueError, match="size"):
            custom_installer._download_verified("https://github.com/asset", len(data) - 1, hashlib.sha256(data).hexdigest(), file)


def test_install_uses_verified_deb_and_no_remove_in_self_lock(tmp_path):
    with patch.object(custom_installer.platform, "machine", return_value="x86_64"), \
         patch.object(custom_installer, "_installed_neovim_version", return_value="0.11.6"), \
         patch.object(custom_installer, "_version_at_least", return_value=False), \
         patch.object(custom_installer, "_read_release", return_value=fake_release()), \
         patch.object(custom_installer, "_download_verified") as download, \
         patch.object(custom_installer, "_verified_deb_fields") as verify, \
         patch.object(custom_installer, "_remove_legacy_neovim_symlink") as cleanup, \
         patch.object(custom_installer, "_retire_local_standalone_neovim") as retire, \
         patch.object(custom_installer, "_LEGACY_NVIM", tmp_path / "not-installed"), \
         patch.object(custom_installer, "get_lock_state", return_value=SimpleNamespace(mode=custom_installer.LockMode.SELF_LOCK)), \
         patch.object(custom_installer.subprocess, "run") as run:
        download.side_effect = lambda url, size, digest, package: package.touch()
        assert custom_installer.install_latest_neovim(update=True) == "0.12.5"
    download.assert_called_once()
    verify.assert_called_once()
    args = run.call_args.args[0]
    assert args[:4] == [custom_installer.APT_GET, "install", "-y", "--no-remove"]
    assert args[-2] == "--" and args[-1].endswith("/nvim-linux-x86_64.deb")
    cleanup.assert_called_once()
    retire.assert_called_once()


def test_standalone_neovim_migrates_after_successful_apt_install(tmp_path):
    local_bin = tmp_path / "bin"
    local_bin.mkdir()
    standalone = local_bin / "nvim"
    standalone.write_bytes(b"standalone Neovim from older manual tarball")
    with patch.object(custom_installer.platform, "machine", return_value="x86_64"), \
         patch.object(custom_installer, "_installed_neovim_version", return_value=None), \
         patch.object(custom_installer, "_LEGACY_NVIM", tmp_path / "no-sentinel-install"), \
         patch.object(custom_installer, "_LEGACY_SYMLINK", standalone), \
         patch.object(custom_installer, "_read_release", return_value=fake_release()), \
         patch.object(custom_installer, "_download_verified", side_effect=lambda url, size, digest, pkg: pkg.touch()), \
         patch.object(custom_installer, "_verified_deb_fields"), \
         patch.object(custom_installer, "get_lock_state", return_value=SimpleNamespace(mode=custom_installer.LockMode.UNLOCKED)), \
         patch.object(custom_installer.subprocess, "run") as apt:
        assert custom_installer.install_latest_neovim(update=True) == "0.12.5"
    apt.assert_called_once()
    assert not standalone.exists()
    backups = list(local_bin.glob("nvim.sentinel-pre-deb-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"standalone Neovim from older manual tarball"


def test_failed_download_preserves_standalone_neovim(tmp_path):
    standalone = tmp_path / "nvim"
    standalone.write_bytes(b"existing binary")
    with patch.object(custom_installer, "_installed_neovim_version", return_value=None), \
         patch.object(custom_installer, "_LEGACY_NVIM", tmp_path / "no-legacy"), \
         patch.object(custom_installer, "_LEGACY_SYMLINK", standalone), \
         patch.object(custom_installer, "_read_release", return_value=fake_release()), \
         patch.object(custom_installer, "_download_verified", side_effect=ValueError("checksum mismatch")), \
         patch.object(custom_installer.subprocess, "run") as apt:
        with pytest.raises(ValueError, match="checksum"):
            custom_installer.install_latest_neovim(update=True)
    assert standalone.read_bytes() == b"existing binary"
    assert not list(tmp_path.glob("nvim.sentinel-pre-deb-*"))
    apt.assert_not_called()


def test_standalone_symlink_is_not_moved(tmp_path):
    target = tmp_path / "manual-neovim"
    target.write_bytes(b"some neovim")
    link = tmp_path / "nvim"
    link.symlink_to(target)
    with patch.object(custom_installer, "_LEGACY_SYMLINK", link):
        assert not custom_installer._local_standalone_neovim_present()
        custom_installer._retire_local_standalone_neovim()
    assert link.is_symlink()
    assert target.read_bytes() == b"some neovim"


def test_up_to_date_skips_download_and_apt(tmp_path):
    with patch.object(custom_installer, "_installed_neovim_version", return_value="0.12.5"), \
         patch.object(custom_installer, "_version_at_least", return_value=True), \
         patch.object(custom_installer, "_read_release", return_value=fake_release()), \
         patch.object(custom_installer, "_LEGACY_NVIM", tmp_path / "no-legacy"), \
         patch.object(custom_installer, "_download_verified") as download, \
         patch.object(custom_installer.subprocess, "run") as run:
        assert custom_installer.install_latest_neovim(update=True) == "0.12.5"
    download.assert_not_called()
    run.assert_not_called()


def test_update_missing_neovim_does_not_access_network(tmp_path):
    with patch.object(custom_installer, "_installed_neovim_version", return_value=None), \
         patch.object(custom_installer, "_LEGACY_NVIM", tmp_path / "none"), \
         patch.object(custom_installer, "_read_release") as release:
        with pytest.raises(ValueError, match="sen install neovim"):
            custom_installer.install_latest_neovim(update=True)
    release.assert_not_called()


def test_failed_verify_does_not_invoke_apt(tmp_path):
    with patch.object(custom_installer, "_installed_neovim_version", return_value=None), \
         patch.object(custom_installer, "_LEGACY_NVIM", tmp_path / "none"), \
         patch.object(custom_installer, "_read_release", return_value=fake_release()), \
         patch.object(custom_installer, "_download_verified", side_effect=ValueError("bad checksum")), \
         patch.object(custom_installer.subprocess, "run") as run:
        with pytest.raises(ValueError, match="checksum"):
            custom_installer.install_latest_neovim()
    run.assert_not_called()


def test_reaper_no_longer_uses_downloads_archives(tmp_path):
    from sentinel.daemon.operations import reaper_installer
    evil = tmp_path / "reaper782_linux_x86_64.tar.xz"
    evil.write_bytes(b"arbitrary root script")
    with patch.object(reaper_installer, "install_latest_reaper", return_value="7.82") as run, \
         patch.object(custom_installer, "_find_file") as find:
        custom_installer.custom_install("reaper", downloads_dir=tmp_path)
    run.assert_called_once_with()
    find.assert_not_called()


def test_reaper_download_page_rejects_unofficial_archive():
    from sentinel.daemon.operations import reaper_installer as r
    page = b'<p>REAPER v7.82</p><a href="/files/7.x/reaper782_linux_x86_64.tar.xz">Linux</a>'
    assert r._release_from_page(page, "x86_64") == (
        "7.82", "https://www.reaper.fm/files/7.x/reaper782_linux_x86_64.tar.xz")
    for bad_page in (
        b'<p>REAPER v7.82</p><a href="https://evil.test/reaper782_linux_x86_64.tar.xz">Linux</a>',
        b'<p>REAPER v7.82</p><a href="/files/7.x/reaper781_linux_x86_64.tar.xz">Linux</a>',
    ):
        with pytest.raises(ValueError, match="do not list"):
            r._release_from_page(bad_page, "x86_64")


def _make_reaper_archive(path: Path, members: dict[str, bytes], *, symlink=None):
    import tarfile
    with tarfile.open(path, "w:xz") as tar:
        for name, data in members.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o755 if name.endswith("/reaper") else 0o644
            tar.addfile(member, io.BytesIO(data))
        if symlink:
            name, link = symlink
            member = tarfile.TarInfo(name)
            member.type = tarfile.SYMTYPE
            member.linkname = link
            tar.addfile(member)


def test_reaper_extracts_only_payload_not_shell_script(tmp_path):
    from sentinel.daemon.operations import reaper_installer as r
    tarball = tmp_path / "reaper.tar.xz"
    _make_reaper_archive(tarball, {
        "reaper_linux_x86_64/REAPER/reaper": b"#! /bin/sh\n",
        "reaper_linux_x86_64/REAPER/whatsnew.txt": b"v7.82 changes",
        "reaper_linux_x86_64/install-reaper.sh": b"rm -rf /",
    })
    folder = tmp_path / "stage"
    folder.mkdir()
    r._extract_application(tarball, folder, "x86_64")
    assert (folder / "reaper").is_file()
    assert (folder / "whatsnew.txt").is_file()
    assert not (folder / "install-reaper.sh").exists()


def test_reaper_rejects_traversal_and_escaping_symlink(tmp_path):
    from sentinel.daemon.operations import reaper_installer as r
    archive = tmp_path / "bad.tar.xz"
    root = "reaper_linux_x86_64/REAPER/"
    for members, link in (
        ({root + "reaper": b"hello", root + "../escape": b"bad"}, None),
        ({root + "reaper": b"hello"}, (root + "leak", "../../etc/passwd")),
    ):
        _make_reaper_archive(archive, members, symlink=link)
        with pytest.raises(ValueError, match="Unsafe|escapes"):
            r._extract_application(archive, tmp_path / f"target-{len(members)}-{bool(link)}", "x86_64")
        archive.unlink()


def test_reaper_refuses_insecure_or_nonpublisher_redirects():
    from sentinel.daemon.operations import reaper_installer as r
    from urllib.request import Request
    for url in ("http://www.reaper.fm/files/7.x/f.tar.xz",
                "https://example.com/f.tar.xz"):
        with pytest.raises(ValueError, match="Cockos HTTPS"):
            r._CockosRedirectsOnly().redirect_request(
                Request("https://www.reaper.fm/download.php"), None, 302,
                "moved", {}, url)


def test_reaper_install_update_and_preserve_old_install(tmp_path):
    from sentinel.daemon.operations import reaper_installer as r
    old = tmp_path / "REAPER"
    old.mkdir()
    (old / "reaper").write_text("previous binary")
    (old / "whatsnew.txt").write_text("v7.81 prior")
    archive = tmp_path / "mock.tar.xz"
    _make_reaper_archive(archive, {
        "reaper_linux_x86_64/REAPER/reaper": b"#!/bin/sh\nnew binary",
        "reaper_linux_x86_64/REAPER/whatsnew.txt": b"v7.82 next",
    })
    dest = tmp_path / "usr-local-bin-reaper"
    launcher = tmp_path / "cockos-reaper.desktop"
    backups = []
    # The staging directory is normally /opt. For this test mock the temp dir
    # creation while keeping its cleanup behavior.
    import tempfile
    original_tempdir = tempfile.TemporaryDirectory
    def tempdir(*args, **kwargs):
        kwargs["dir"] = tmp_path
        return original_tempdir(*args, **kwargs)
    with patch.object(r, "_REAPER_DIR", old), \
         patch.object(r, "_REAPER_COMMAND", dest), \
         patch.object(r, "_REAPER_DESKTOP", launcher), \
         patch.object(r.platform, "machine", return_value="x86_64"), \
         patch.object(r, "_latest_reaper", return_value=("7.82", "https://www.reaper.fm/files/7.x/reaper782_linux_x86_64.tar.xz")), \
         patch.object(r, "_download_reaper", side_effect=lambda url, target: (target.write_bytes(archive.read_bytes()), "fakehash")[1]), \
         patch.object(r.tempfile, "TemporaryDirectory", side_effect=tempdir), \
         patch.object(r.uuid, "uuid4", return_value=SimpleNamespace(hex="abcdef000000")):
        assert r.install_latest_reaper(update=True) == "7.82"
        backups = list(tmp_path.glob("REAPER.sentinel-backup*"))
        assert old.joinpath("reaper").read_text().endswith("new binary")
        assert dest.is_symlink()
        assert launcher.exists()
        assert len(backups) == 1
    # No attempt to run the vendor shell installer with elevated privileges.
    assert backups[0].joinpath("reaper").read_text() == "previous binary"


def test_reaper_update_requires_existing_install_without_network(tmp_path):
    from sentinel.daemon.operations import reaper_installer as r
    with patch.object(r, "_REAPER_DIR", tmp_path / "missing"), \
         patch.object(r.platform, "machine", return_value="x86_64"), \
         patch.object(r, "_latest_reaper") as latest:
        with pytest.raises(ValueError, match="sen install reaper"):
            r.install_latest_reaper(update=True)
    latest.assert_not_called()


def test_reaper_skip_current_version_without_download(tmp_path):
    from sentinel.daemon.operations import reaper_installer as r
    installed = tmp_path / "REAPER"
    installed.mkdir()
    executable = installed / "reaper"
    executable.write_text("existing")
    (installed / ".sentinel-reaper-version").write_text("7.82\n")
    with patch.object(r, "_REAPER_DIR", installed), \
         patch.object(r.platform, "machine", return_value="x86_64"), \
         patch.object(r, "_latest_reaper", return_value=("7.82", "https://www.reaper.fm/files/7.x/reaper782_linux_x86_64.tar.xz")), \
         patch.object(r, "_download_reaper") as download:
        assert r.install_latest_reaper(update=True) == "7.82"
    download.assert_not_called()


def test_reaper_uninstall_requires_sentinel_marker(tmp_path):
    from sentinel.daemon.operations import reaper_installer as r
    installed = tmp_path / "REAPER"
    installed.mkdir()
    (installed / "reaper").write_text("existing")
    with patch.object(r, "_REAPER_DIR", installed), \
         patch.object(r, "_REAPER_COMMAND", tmp_path / "cmd"), \
         patch.object(r, "_REAPER_DESKTOP", tmp_path / "desktop"):
        with pytest.raises(ValueError, match="not installed by Sentinel"):
            r.uninstall_reaper()
        assert installed.exists()
        (installed / ".sentinel-reaper-version").write_text("7.82")
        r.uninstall_reaper()
        assert not installed.exists()


def test_existing_neovim_install_is_successful_without_network():
    from sentinel.daemon.operations import package_ops
    with patch.object(package_ops, 'require_policy'), \
         patch.object(custom_installer, '_installed_neovim_version', return_value='0.12.1'), \
         patch.object(package_ops, '_install_custom') as install:
        result = package_ops._install_package('neovim', 1000)
    assert result == {'already_installed': True, 'message': 'neovim already installed'}
    install.assert_not_called()


def test_standalone_neovim_install_is_successful_without_network():
    from sentinel.daemon.operations import package_ops
    with patch.object(package_ops, 'require_policy'), \
         patch.object(custom_installer, '_installed_neovim_version', return_value=None), \
         patch.object(custom_installer, '_LEGACY_NVIM') as legacy, \
         patch.object(custom_installer, '_local_standalone_neovim_present', return_value=True), \
         patch.object(package_ops, '_install_custom') as install:
        legacy.is_file.return_value = False
        assert package_ops._install_package('neovim', 1000)['already_installed'] is True
    install.assert_not_called()


def test_sen_install_neovim_prints_existing_install_as_success():
    from typer.testing import CliRunner
    from sen.cli import sen
    with patch.object(sen, '_invoke', return_value={
        'already_installed': True, 'message': 'neovim already installed'
    }):
        result = CliRunner().invoke(sen.app, ['install', 'neovim'])
    assert result.exit_code == 0
    assert 'neovim already installed' in result.output
    assert 'installed successfully' not in result.output
