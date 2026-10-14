#!/usr/bin/env python3
"""Build signed CRX3 updates with the existing RSA keys (Python + OpenSSL).

Run from the project root. Private keys remain build inputs and are never
included in extension archives. Uses Chromium's developer CRX3 signature format.
"""

import hashlib
import io
import json
import os
import stat
import struct
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTENSIONS = (
    ("activity", "sentinel_activity.crx", "edjckliccdbcopglliobjnjdeilcnhpj"),
    ("safe_steam", "browser_extension.crx", "jdfobdpedcmlbmolbdfaoaidldkjjfnh"),
    ("safe_crunchyroll", "safe_crunchyroll.crx", "njcmpekfomicmkpnaihnkohgpoeooeoa"),
)


def signing_key_path(name):
    """Keep private release inputs outside the editable/public project."""
    if name not in {item[0] for item in EXTENSIONS} | {"extension_catalog", "newtab"}:
        raise ValueError("Unknown extension signing identity")
    directory = Path(os.environ.get(
        "SENTINEL_EXTENSION_KEY_DIR",
        str(Path.home() / ".local/share/sentinel-extension-keys"),
    )).expanduser()
    if not directory.is_absolute():
        raise ValueError("SENTINEL_EXTENSION_KEY_DIR must be an absolute directory")
    directory = directory.resolve()
    if directory.is_relative_to(ROOT.resolve()):
        raise ValueError("Private extension keys must be stored outside the project")
    return directory / f"{name}.pem"


def validate_signing_key(key):
    key = Path(key).expanduser()
    if key.is_symlink() or not key.is_file():
        raise ValueError("Restore the original private signing key outside the project before rebuilding")
    info = key.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("Private signing key must be a regular file with mode 0600")
    if key.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("Private extension keys must be stored outside the project")
    return key


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value):
    return varint((number << 3) | 2) + varint(len(value)) + value


def build(
    name,
    filename,
    expected_id,
    *,
    key_path=None,
    manifest_overrides=None,
    extra_files=None,
):
    base = ROOT / "sentinel" / name
    source = base / "browser_extension"
    distribution = base / "browser_distribution"
    key = (
        Path(key_path)
        if key_path is not None
        else signing_key_path(name)
    )
    key = validate_signing_key(key)
    distribution.mkdir(parents=True, exist_ok=True)
    public = subprocess.run(
        ["openssl", "pkey", "-in", str(key), "-pubout", "-outform", "DER"],
        capture_output=True,
        check=True,
    ).stdout
    crx_id = hashlib.sha256(public).digest()[:16]
    extension_id = "".join(chr(ord("a") + int(n, 16)) for n in crx_id.hex())
    if extension_id != expected_id:
        raise ValueError(f"Signing key does not match the existing {name} extension ID")
    files = {}
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        if (
            path.suffix not in {".js", ".json", ".html", ".css", ".png", ".svg"}
            or path.is_symlink()
        ):
            raise ValueError(f"Unexpected extension source file: {path}")
        files[path.relative_to(source).as_posix()] = path.read_bytes()
    if manifest_overrides:
        manifest = json.loads(files["manifest.json"])
        manifest.update(manifest_overrides)
        files["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    for filename_in_archive, content in (extra_files or {}).items():
        path = Path(filename_in_archive)
        if (
            path.is_absolute()
            or ".." in path.parts
            or path.as_posix() in files
            or path.suffix not in {".js", ".json", ".html", ".css", ".png"}
        ):
            raise ValueError("Invalid generated extension asset")
        files[path.as_posix()] = content
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative_path, content in sorted(files.items()):
            entry = zipfile.ZipInfo(relative_path, (1980, 1, 1, 0, 0, 0))
            entry.external_attr = 0o100644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, content)
    archive_bytes = buffer.getvalue()
    signed_header = field(1, crx_id)
    signed = (
        b"CRX3 SignedData\x00"
        + struct.pack("<I", len(signed_header))
        + signed_header
        + archive_bytes
    )
    signature = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(key)],
        input=signed,
        capture_output=True,
        check=True,
    ).stdout
    header = field(2, field(1, public) + field(2, signature)) + field(
        10000, signed_header
    )
    (distribution / filename).write_bytes(
        b"Cr24" + struct.pack("<II", 3, len(header)) + header + archive_bytes
    )
    version = json.loads((source / "manifest.json").read_text())["version"]
    endpoint = name.replace("_", "-")
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<gupdate xmlns="http://www.google.com/update2/response" protocol="2.0">
  <app appid="{expected_id}">
    <updatecheck codebase="http://127.0.0.1:8765/extensions/{endpoint}/{filename}" version="{version}" />
  </app>
</gupdate>
'''
    (distribution / "updates.xml").write_text(xml)
    print(f"Built {name} {version}: {extension_id}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only', choices=[item[0] for item in EXTENSIONS])
    args = parser.parse_args()
    for extension in EXTENSIONS:
        if args.only is None or extension[0] == args.only:
            build(*extension)
