"""Loopback bridge, Steam policy consumers and signed distribution tests."""
import contextlib
import hashlib
import http.client
import io
import json
import socket
import struct
import subprocess
import threading
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from sentinel.safe_steam import server, watchdog, provision

ROOT = Path(__file__).resolve().parents[3]


@contextlib.contextmanager
def endpoint():
    httpd = server.ExtensionHTTPServer(("127.0.0.1", 0))
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        yield httpd.server_port
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=2)


def get(port, path, method="GET", headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_http_endpoint_reads_live_daemon_snapshot_without_exposing_secrets():
    with endpoint() as port, patch.object(server.browser_ops, "get_settings") as settings:
        for enabled in (True, False, True):
            settings.return_value = {"safe_browsing": True, "safe_steam": enabled, "safe_crunchyroll": enabled}
            for slug, key in (("safe-steam", "safe_steam"), ("safe-crunchyroll", "safe_crunchyroll")):
                status, headers, body = get(port, f"/api/{slug}")
                assert status == 200
                assert json.loads(body) == {key: enabled}
                assert headers["Cache-Control"] == "no-store"
        assert set(json.loads(get(port, "/api/protections")[2])) == {"safe_steam", "safe_crunchyroll", "safe_browsing"}
        assert get(port, "/api/safe-steam", method="POST")[0] == 403
        assert get(port, "/api/safe-steam", headers={"Host": "attacker.example"})[0] == 403
        assert get(port, "/extensions/safe-crunchyroll/browser_extension.pem")[0] == 404
        assert get(port, "/extensions/../../etc/sentinel/settings.toml")[0] == 404
        settings.side_effect = ValueError("bad policy")
        assert get(port, "/api/safe-steam")[0] == 503


def test_fixed_assets_only_and_chrome_update_query():
    with endpoint() as port, patch.object(server, "read_asset", return_value=b"signed-asset") as read:
        status, headers, body = get(port, "/extensions/safe-steam/updates.xml?x=id%3Dtest")
    assert status == 200 and body == b"signed-asset"
    assert headers["Content-Type"] == "application/xml"
    assert read.call_args.args[0].name == "updates.xml"


def test_missing_or_unprotected_asset_is_never_served():
    with endpoint() as port, patch.object(server, "read_asset") as read:
        read.side_effect = FileNotFoundError()
        assert get(port, "/extensions/safe-steam/browser_extension.crx")[0] == 404
        read.side_effect = ValueError("unprotected")
        assert get(port, "/extensions/safe-steam/browser_extension.crx")[0] == 503


def test_daemon_cannot_share_listener_with_standalone_server():
    with endpoint() as port, patch.object(server, "PORT", port):
        with pytest.raises(OSError):
            server.start_server()


def test_absent_steam_never_reads_or_edits_stale_user_profile():
    with patch.object(watchdog, "native_steam_installed", return_value=False), patch.object(watchdog, "preferences_path") as profile:
        assert watchdog.check_once()
        assert watchdog._last_absent
    profile.assert_not_called()


def test_enabled_watchdog_repairs_unchanged_but_invalid_registration(tmp_path):
    prefs = tmp_path / "Preferences"
    prefs.write_text("{}")
    with (patch.object(watchdog, "native_steam_installed", return_value=True),
          patch.object(watchdog, "preferences_path", return_value=prefs),
          patch.object(watchdog, "verify_source"), patch.object(watchdog, "verify_integrity"),
          patch.object(watchdog, "repair_safe_steam") as repair):
        assert not watchdog.check_once()
        assert not watchdog.check_once()
    assert repair.call_count == 2  # Re-enabling needs no Preferences edit to trigger repair.


def test_bad_integrity_stops_running_steam(tmp_path):
    prefs = tmp_path / "Preferences"
    prefs.write_text("{}")
    with (patch.object(watchdog, "native_steam_installed", return_value=True),
          patch.object(watchdog, "preferences_path", return_value=prefs),
          patch.object(watchdog, "verify_source", side_effect=SystemExit(1)),
          patch.object(watchdog, "steam_is_running", return_value=True),
          patch.object(watchdog, "shutdown_steam") as shutdown,
          patch.object(watchdog, "run_provisioner") as repair):
        assert not watchdog.check_once()
    shutdown.assert_called_once()
    repair.assert_not_called()


def test_steam_source_matches_complete_integrity_manifest():
    folder = ROOT / "sentinel/safe_steam/steam_extension_tests"
    hashes = ROOT / "sentinel/safe_steam/provisioning/safesteam-hashes.json"
    with patch.object(provision, "SAFE_STEAM_PATH", folder), patch.object(provision, "HASH_MANIFEST", hashes):
        provision.verify_source()
        provision.verify_integrity()


def test_extra_or_missing_steam_asset_rejected(tmp_path):
    for name in ("manifest.json", "content.js", "policy.js", "background.js", "untracked.js"):
        (tmp_path / name).write_text("test")
    hashes = tmp_path / "hashes.json"
    hashes.write_text(json.dumps({name: hashlib.sha256(b"test").hexdigest()
                                 for name in ("manifest.json", "content.js", "policy.js", "background.js")}))
    with patch.object(provision, "SAFE_STEAM_PATH", tmp_path), patch.object(provision, "HASH_MANIFEST", hashes):
        with pytest.raises(SystemExit):
            provision.verify_integrity()


def fields(data):
    position = 0
    def varint():
        nonlocal position
        result = 0
        shift = 0
        while True:
            byte = data[position]
            position += 1
            result |= (byte & 127) << shift
            if not byte & 128:
                return result
            shift += 7
    result = {}
    while position < len(data):
        tag = varint()
        assert tag & 7 == 2
        length = varint()
        result[tag >> 3] = data[position:position+length]
        position += length
    return result


@pytest.mark.parametrize("name,filename,extension_id,expected_version", [
    ("safe_steam", "browser_extension.crx", "jdfobdpedcmlbmolbdfaoaidldkjjfnh", "0.2.0"),
    ("safe_crunchyroll", "safe_crunchyroll.crx", "njcmpekfomicmkpnaihnkohgpoeooeoa", "0.2.2"),
])
def test_signed_crx_identity_signature_and_exact_sources(tmp_path, name, filename, extension_id, expected_version):
    distribution = ROOT / "sentinel" / name / "browser_distribution"
    source = ROOT / "sentinel" / name / "browser_extension"
    expected_version = json.loads((source / "manifest.json").read_text())["version"]
    data = (distribution / filename).read_bytes()
    assert data[:4] == b"Cr24"
    version, header_size = struct.unpack("<II", data[4:12])
    assert version == 3
    header = fields(data[12:12+header_size])
    proof = fields(header[2])
    crx_id = hashlib.sha256(proof[1]).digest()[:16]
    assert fields(header[10000])[1] == crx_id
    assert "".join(chr(ord("a") + int(n, 16)) for n in crx_id.hex()) == extension_id
    public_file = tmp_path / "public.der"
    signature_file = tmp_path / "signature.bin"
    public_file.write_bytes(proof[1])
    signature_file.write_bytes(proof[2])
    archive = data[12+header_size:]
    payload = b"CRX3 SignedData\0" + struct.pack("<I", len(header[10000])) + header[10000] + archive
    result = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(public_file), "-keyform", "DER",
                             "-signature", str(signature_file)], input=payload, capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
    with zipfile.ZipFile(io.BytesIO(archive)) as packed:
        assert set(packed.namelist()) == {p.name for p in source.iterdir() if p.is_file()}
        assert not any("pem" in name for name in packed.namelist())
        for path in source.iterdir():
            assert packed.read(path.name) == path.read_bytes()
        manifest = json.loads(packed.read("manifest.json"))
        assert manifest["version"] == expected_version
        if name == "safe_crunchyroll":
            assert manifest["update_url"] == "http://127.0.0.1:8765/extensions/safe-crunchyroll/updates.xml"
    assert f'version="{expected_version}"' in (distribution / "updates.xml").read_text()


def test_extension_runtime_behavior():
    import shutil
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for the extension JavaScript behavior tests")
    result = subprocess.run([node, str(Path(__file__).with_name("extension_runtime.test.cjs"))],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "119 extension behavior assertions passed" in result.stdout
