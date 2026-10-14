"""Verify a signed release can be rebuilt without changing its identity."""
import hashlib
import io
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import zipfile
from pathlib import Path



ROOT = Path(__file__).resolve().parents[3]


def protobuf_fields(data):
    def integer(offset):
        value = shift = 0
        while True:
            byte = data[offset]
            offset += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value, offset
            shift += 7
    offset = 0
    result = {}
    while offset < len(data):
        tag, offset = integer(offset)
        assert tag & 7 == 2
        length, offset = integer(offset)
        result[tag >> 3] = data[offset:offset + length]
        offset += length
    return result


def test_build_id_signature_archive_and_private_key(tmp_path):
    project = tmp_path / "project"
    tools = project / "tools"
    tools.mkdir(parents=True)
    for name in ("build_catalog_extension.py", "build_safe_extensions.py"):
        shutil.copyfile(ROOT / "tools" / name, tools / name)
    base = project / "sentinel" / "extension_catalog"
    shutil.copytree(ROOT / "sentinel/extension_catalog/browser_extension", base / "browser_extension")
    shutil.copytree(ROOT / "sentinel/newtab/browser_extension", project / "sentinel/newtab/browser_extension")
    command = [sys.executable, str(tools / "build_catalog_extension.py")]
    keys = tmp_path / "private-keys"
    environment = {**os.environ, "SENTINEL_EXTENSION_KEY_DIR": str(keys)}
    subprocess.run(command, check=True, capture_output=True, env=environment)
    distribution = base / "browser_distribution"
    crx = (distribution / "sentinel_extensions.crx").read_bytes()
    metadata = json.loads((distribution / "metadata.json").read_text())
    assert stat.S_IMODE((keys / "extension_catalog.pem").stat().st_mode) == 0o600
    assert not list(project.rglob('*.pem'))
    subprocess.run(command, check=True, capture_output=True, env=environment)
    assert (distribution / "sentinel_extensions.crx").read_bytes() == crx
    assert json.loads((distribution / "metadata.json").read_text()) == metadata
    magic, version, header_length = struct.unpack("<4sII", crx[:12])
    assert (magic, version) == (b"Cr24", 3)
    header = protobuf_fields(crx[12:12 + header_length])
    proof = protobuf_fields(header[2])
    public, signature = proof[1], proof[2]
    signed_header = header[10000]
    crx_id = protobuf_fields(signed_header)[1]
    assert crx_id == hashlib.sha256(public).digest()[:16]
    expected_id = "".join(chr(ord("a") + int(n, 16)) for n in crx_id.hex())
    assert metadata["id"] == expected_id
    assert expected_id in (distribution / "updates.xml").read_text()
    assert "/extensions/catalog/sentinel_extensions.crx" in (distribution / "updates.xml").read_text()
    archive_bytes = crx[12 + header_length:]
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        names = set(archive.namelist())
        assert {"manifest.json", "newtab.html", "catalog.html", "catalog.js", "styles.css", "icons/16.png", "icons/48.png", "icons/128.png"} <= names
        assert not any(name.endswith(".pem") for name in names)
        manifest = json.loads(archive.read("manifest.json"))
        assert "chrome_url_overrides" not in manifest
        assert manifest["web_accessible_resources"] == [
            {"resources": ["newtab.html"], "extension_ids": [metadata["newtab_id"]]},
        ]
        assert manifest["host_permissions"] == ["http://127.0.0.1/*"]
        assert manifest["permissions"] == ["favicon"]
    newtab_distribution = project / "sentinel/newtab/browser_distribution"
    newtab_crx = (newtab_distribution / "sentinel_newtab.crx").read_bytes()
    assert stat.S_IMODE((keys / "newtab.pem").stat().st_mode) == 0o600
    assert metadata['newtab_id'] != metadata['id']
    assert metadata['newtab_id'] in (newtab_distribution / 'updates.xml').read_text()
    magic2, version2, length2 = struct.unpack('<4sII', newtab_crx[:12])
    assert (magic2, version2) == (b'Cr24', 3)
    newtab_header = protobuf_fields(newtab_crx[12:12 + length2])
    newtab_public = protobuf_fields(newtab_header[2])[1]
    assert newtab_public != public
    with zipfile.ZipFile(io.BytesIO(newtab_crx[12 + length2:])) as archive:
        assert not any(name.endswith('.pem') for name in archive.namelist())
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['chrome_url_overrides'] == {'newtab': 'newtab.html'}
        assert 'permissions' not in manifest and 'host_permissions' not in manifest
        assert f'chrome-extension://{metadata["id"]}/newtab.html' in archive.read('catalog-target.js').decode()
    # OpenSSL independently verifies the exact CRX3 signed-data bytes.
    public_der = tmp_path / "public.der"
    public_der.write_bytes(public)
    public_pem = tmp_path / "public.pem"
    subprocess.run(["openssl", "pkey", "-pubin", "-inform", "DER", "-in", str(public_der), "-out", str(public_pem)], check=True, capture_output=True)
    signature_file = tmp_path / "signature"
    signature_file.write_bytes(signature)
    signed_data = b"CRX3 SignedData\x00" + struct.pack("<I", len(signed_header)) + signed_header + archive_bytes
    verified = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(public_pem), "-signature", str(signature_file)], input=signed_data, capture_output=True, check=True)
    assert b"Verified OK" in verified.stdout
    # Lost original keys must not cause a released extension to change identity.
    (keys / 'extension_catalog.pem').unlink()
    failed = subprocess.run(command, capture_output=True, env=environment)
    assert failed.returncode != 0
    assert b'Restore the original' in failed.stderr
    assert not (keys / 'extension_catalog.pem').exists()
    assert (distribution / 'sentinel_extensions.crx').read_bytes() == crx


def test_signing_directory_cannot_be_inside_project(tmp_path):
    tools = tmp_path / 'tools'
    tools.mkdir()
    for name in ('build_catalog_extension.py', 'build_safe_extensions.py'):
        shutil.copyfile(ROOT / 'tools' / name, tools / name)
    environment = {**os.environ, 'SENTINEL_EXTENSION_KEY_DIR': str(tmp_path / 'keys')}
    failed = subprocess.run([sys.executable, str(tools / 'build_catalog_extension.py')],
                            capture_output=True, env=environment)
    assert failed.returncode != 0
    assert b'outside the project' in failed.stderr
    assert not (tmp_path / 'keys').exists()
