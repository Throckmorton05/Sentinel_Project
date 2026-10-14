#!/usr/bin/env python3
"""Build Sentinel Extensions using a private, locally retained signing key.

Run as the desktop developer, then install the public assets with sudo.
The key is ignored by git, excluded from wheels and never installed or served.
Rebuild with the same key and a higher manifest version to update Chrome.
"""
import hashlib
import json
import os
import subprocess
from pathlib import Path

from build_safe_extensions import build, signing_key_path, validate_signing_key

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "sentinel" / "extension_catalog"


def signing_identity(distribution):
    distribution.mkdir(parents=True, exist_ok=True)
    key = signing_key_path(distribution.parent.name)
    # Do not replace an existing key: changing it changes the extension's ID.
    if not key.exists():
        if (distribution / "metadata.json").exists() or any(distribution.glob("*.crx")):
            raise ValueError("Restore the original private signing key; existing extension identity must not change")
        key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            result = subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"],
                                    capture_output=True, check=True)
            with os.fdopen(fd, "wb") as stream:
                fd = None
                stream.write(result.stdout)
        except BaseException:
            key.unlink(missing_ok=True)
            raise
        finally:
            if fd is not None:
                os.close(fd)
    validate_signing_key(key)
    public = subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-outform", "DER"],
                            capture_output=True, check=True).stdout
    extension_id = "".join(chr(ord("a") + int(n, 16)) for n in hashlib.sha256(public).digest()[:16].hex())
    metadata = distribution / "metadata.json"
    if metadata.exists():
        if json.loads(metadata.read_text())["id"] != extension_id:
            raise ValueError("Private signing key does not match the existing extension identity")
    return extension_id


def main():
    distribution = BASE / "browser_distribution"
    extension_id = signing_identity(distribution)
    newtab_distribution = ROOT / "sentinel/newtab/browser_distribution"
    newtab_id = signing_identity(newtab_distribution)
    if newtab_id == extension_id:
        raise ValueError("Catalog and New Tab must use different private signing keys")
    # No duplicate UI or preference store: the optional companion opens the
    # existing start page, preserving catalog-origin shortcuts and theme.
    target = f"chrome-extension://{extension_id}/newtab.html"
    generated_assets = {"catalog-target.js": ("const CATALOG_NEW_TAB_URL = " + json.dumps(target) + ";\n").encode()}
    for size in (16, 48, 128):
        generated_assets[f"icons/{size}.png"] = (BASE / f"browser_extension/icons/{size}.png").read_bytes()
    build("newtab", "sentinel_newtab.crx", newtab_id,
          extra_files=generated_assets)
    (newtab_distribution / "metadata.json").write_text(json.dumps({"id": newtab_id}, indent=2) + "\n")
    # The signed CRX defines the identity; metadata lets policy follow this key.
    build("extension_catalog", "sentinel_extensions.crx", extension_id,
          manifest_overrides={"web_accessible_resources": [
              {"resources": ["newtab.html"], "extension_ids": [newtab_id]},
          ]})
    # The general builder maps underscores to hyphens; this extension's public
    # route is intentionally the shorter /extensions/catalog/ endpoint.
    updates = distribution / "updates.xml"
    updates.write_text(updates.read_text().replace("/extensions/extension-catalog/", "/extensions/catalog/"))
    (distribution / "metadata.json").write_text(json.dumps({"id": extension_id, "newtab_id": newtab_id}, indent=2) + "\n")
    print("Built Sentinel Extensions and optional Sentinel New Tab. Retain both private signing keys for future updates.")


if __name__ == "__main__":
    main()
