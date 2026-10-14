"""Deploy Safe Steam's protected runtime bundle; never install from a user repo."""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import struct
import subprocess
from functools import lru_cache
import tempfile
import threading
import zipfile
from pathlib import Path
from xml.etree import ElementTree
from sentinel.dns.storage import protect_directory
from sentinel.settings.paths import SAFE_STEAM_EXTENSION_DIR, SAFE_STEAM_PROVISIONING_DIR, SAFE_STEAM_DISTRIBUTION_DIR
from .provision import SAFE_STEAM_KEY

BUNDLE = Path(__file__).resolve().parent
CHROME_ID = 'jdfobdpedcmlbmolbdfaoaidldkjjfnh'
NATIVE_FILES = {'manifest.json', 'content.js', 'background.js', 'policy.js'}
_lock = threading.RLock()


def read_bytes(path):
    protect_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('Safe Steam assets must be protected root-owned regular files.')
        data = stream.read(2*1024*1024+1)
        if len(data) > 2*1024*1024:
            raise ValueError('Safe Steam asset exceeds its size limit.')
        return data


def write_bytes(path, data):
    protect_directory(path.parent)
    if path.is_symlink():
        raise ValueError('Safe Steam asset destination must not be a symlink.')
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'.', dir=path.parent)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def native_bundle():
    source = BUNDLE/'steam_extension_tests'
    hashes_bytes = read_bytes(BUNDLE/'provisioning'/'safesteam-hashes.json')
    hashes = json.loads(hashes_bytes)
    if not isinstance(hashes, dict) or set(hashes) != NATIVE_FILES:
        raise ValueError('Incomplete Safe Steam runtime bundle.')
    files = {name: read_bytes(source/name) for name in sorted(NATIVE_FILES)}
    if any(hashlib.sha256(data).hexdigest() != hashes[name] for name, data in files.items()):
        raise ValueError('Safe Steam runtime bundle does not match its integrity manifest.')
    manifest = json.loads(files['manifest.json'])
    if not isinstance(manifest, dict) or manifest.get('key') != SAFE_STEAM_KEY or manifest.get('manifest_version') != 3:
        raise ValueError('Safe Steam runtime bundle has an unexpected extension identity.')
    template = read_bytes(BUNDLE/'provisioning'/'safesteam-pref-entry.json')
    entry = json.loads(template)
    if not isinstance(entry, dict) or entry.get('path') != '@SAFE_STEAM_PATH@' or entry.get('state') != 1 or entry.get('location') != 4:
        raise ValueError('Invalid Safe Steam provisioning template.')
    return files, {'safesteam-hashes.json': hashes_bytes, 'safesteam-pref-entry.json': template}


def same(path, data):
    try:
        return read_bytes(path) == data
    except (FileNotFoundError, ValueError):
        return False


def native_update_needed():
    with _lock:
        files, provisioning = native_bundle()
        protect_directory(SAFE_STEAM_EXTENSION_DIR)
        extra = set(path.name for path in SAFE_STEAM_EXTENSION_DIR.iterdir()) - set(files)
        return bool(extra) or any(not same(SAFE_STEAM_EXTENSION_DIR/name, data) for name, data in files.items()) or any(
            not same(SAFE_STEAM_PROVISIONING_DIR/name, data) for name, data in provisioning.items())


def install_native_assets():
    """Caller must first stop Steam as its desktop user when updating assets."""
    with _lock:
        files, provisioning = native_bundle()
        protect_directory(SAFE_STEAM_EXTENSION_DIR)
        for path in SAFE_STEAM_EXTENSION_DIR.iterdir():
            if path.name not in files:
                # Only obsolete regular files in our protected managed directory.
                read_bytes(path)
                path.unlink()
        for directory, values in ((SAFE_STEAM_EXTENSION_DIR, files), (SAFE_STEAM_PROVISIONING_DIR, provisioning)):
            for name, data in values.items():
                if not same(directory/name, data):
                    write_bytes(directory/name, data)


def protobuf_fields(data):
    result = {}
    offset = 0
    def varint():
        nonlocal offset
        value = 0
        for shift in range(0, 70, 7):
            if offset >= len(data):
                raise ValueError('Invalid Safe Steam CRX header.')
            byte = data[offset]
            offset += 1
            value |= (byte & 127) << shift
            if not byte & 128:
                return value
        raise ValueError('Invalid Safe Steam CRX header.')
    while offset < len(data):
        tag = varint()
        if tag & 7 != 2:
            raise ValueError('Invalid Safe Steam CRX header.')
        size = varint()
        if size > len(data)-offset:
            raise ValueError('Invalid Safe Steam CRX header.')
        result.setdefault(tag >> 3, []).append(data[offset:offset+size])
        offset += size
    return result


@lru_cache(maxsize=2)
def verify_chrome_package(crx):
    if len(crx) < 12 or crx[:4] != b'Cr24' or struct.unpack('<I', crx[4:8])[0] != 3:
        raise ValueError('Invalid Safe Steam Chrome package.')
    header_size = struct.unpack('<I', crx[8:12])[0]
    try:
        header = protobuf_fields(crx[12:12+header_size])
        signed_header = header[10000][0]
        crx_id = protobuf_fields(signed_header)[1][0]
        public = signature = None
        for data in header[2]:
            proof = protobuf_fields(data)
            candidate = proof[1][0]
            digest = hashlib.sha256(candidate).digest()[:16]
            identity = ''.join(chr(ord('a')+int(n,16)) for n in digest.hex())
            if identity == CHROME_ID and digest == crx_id:
                public, signature = candidate, proof[2][0]
                break
        if public is None:
            raise ValueError('Safe Steam Chrome package has the wrong extension identity.')
        archive = crx[12+header_size:]
        payload = b'CRX3 SignedData\0'+struct.pack('<I',len(signed_header))+signed_header+archive
        with tempfile.TemporaryDirectory(prefix='sentinel-crx-') as directory:
            public_path = Path(directory)/'public.der'
            signature_path = Path(directory)/'signature.bin'
            public_path.write_bytes(public)
            signature_path.write_bytes(signature)
            result = subprocess.run(['/usr/bin/openssl','dgst','-sha256','-verify',str(public_path),
                '-keyform','DER','-signature',str(signature_path)],input=payload,capture_output=True,timeout=5,check=False)
            if result.returncode:
                raise ValueError('Safe Steam Chrome package signature verification failed.')
        with zipfile.ZipFile(io.BytesIO(archive)) as packed:
            if packed.getinfo('manifest.json').file_size > 64*1024:
                raise ValueError('Invalid Chrome extension manifest.')
            manifest = json.loads(packed.read('manifest.json'))
        if not isinstance(manifest, dict) or manifest.get('manifest_version') != 3:
            raise ValueError('Invalid Chrome extension manifest.')
        return manifest
    except (KeyError, IndexError, zipfile.BadZipFile, subprocess.TimeoutExpired) as exc:
        raise ValueError('Invalid Safe Steam Chrome package.') from exc


def chrome_bundle():
    crx = read_bytes(BUNDLE/'browser_distribution'/'browser_extension.crx')
    xml = read_bytes(BUNDLE/'browser_distribution'/'updates.xml')
    manifest = verify_chrome_package(crx)
    try:
        tree = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ValueError('Invalid Safe Steam Chrome update manifest.') from exc
    app = tree.find('{http://www.google.com/update2/response}app')
    update = app.find('{http://www.google.com/update2/response}updatecheck') if app is not None else None
    if (app is None or app.get('appid') != CHROME_ID or update is None
            or update.get('version') != manifest.get('version')
            or update.get('codebase') != 'http://127.0.0.1:8765/extensions/safe-steam/browser_extension.crx'):
        raise ValueError('Safe Steam Chrome update metadata does not match its package.')
    return {'browser_extension.crx': crx, 'updates.xml': xml}


def install_chrome_assets():
    with _lock:
        files = chrome_bundle()
        for name, data in files.items():
            if not same(SAFE_STEAM_DISTRIBUTION_DIR/name, data):
                write_bytes(SAFE_STEAM_DISTRIBUTION_DIR/name, data)
