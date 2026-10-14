from __future__ import annotations

from typing import Any, Never

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


SAFE_STEAM_ID = "fbgjjhaoclncakjkdebkllglkgpffkfb"

SAFE_STEAM_KEY = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA6/5yl0R2szSsdes5nVNlR3l"
    "afn89Kr79cEVjjR1gdddH0VFEtb+UCArDCNZGwcb7G/jBE0L7EW6i6+iO+0axuUHoo"
    "t+rWjQEb32jnONeM2ZbrTq/w+1oQN0yINogS4zYQc1OW89w09vH1DNeG2/BCcQHv7"
    "V0NFOReVrnCkCNUcWW1RPv7onKta80p2uTC7Q8B1XPtJGBx+slquU8Mn4AP8ItKGsc"
    "bFsUAyC/6zH8M0zr+g9r636xZ7vAJREqML+guAQ+aT9i+C21BufMtmYaW+9936l+lm"
    "jCMZQYZ7bwNB5QWiMi61da0RXlY5PwaTgTZoa16kqDQ4W1fa6OGsaqVwIDAQAB"
)


from sentinel.settings.paths import (
    SAFE_STEAM_EXTENSION_DIR,
    SAFE_STEAM_PROVISIONING_DIR,
)
from sentinel.safe_steam.runtime import CONTAINER_EXTENSION_PATH, LOAD_PATHS, validate_load_path


SAFE_STEAM_PATH = SAFE_STEAM_EXTENSION_DIR

PREF_TEMPLATE = SAFE_STEAM_PROVISIONING_DIR / "safesteam-pref-entry.json"

HASH_MANIFEST = SAFE_STEAM_PROVISIONING_DIR / "safesteam-hashes.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    try:
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
    except FileNotFoundError:
        fail(f"Safe Steam file missing: {path}")

    return digest.hexdigest()


def verify_integrity() -> None:
    expected_hashes = load_json(HASH_MANIFEST)

    if not expected_hashes:
        fail("Safe Steam hash manifest is empty.")

    if not isinstance(expected_hashes, dict):
        fail("Invalid Safe Steam hash manifest.")
    required = {"manifest.json", "content.js", "policy.js", "background.js"}
    actual_files = {p.relative_to(SAFE_STEAM_PATH).as_posix()
                    for p in SAFE_STEAM_PATH.rglob("*") if p.is_file()}
    if not required <= expected_hashes.keys() or actual_files != expected_hashes.keys():
        fail("Safe Steam extension tree does not match the complete hash manifest.")

    for relative_path, expected_hash in expected_hashes.items():
        if not isinstance(relative_path, str):
            fail("Invalid file path in Safe Steam hash manifest.")

        if not isinstance(expected_hash, str):
            fail(f"Invalid SHA-256 value for Safe Steam file: {relative_path}")

        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            fail("Unsafe file path in Safe Steam hash manifest.")
        source_file = SAFE_STEAM_PATH / relative
        if source_file.is_symlink():
            fail("Safe Steam assets must not be symlinks.")

        if not source_file.is_file():
            fail(f"Safe Steam file missing: {source_file}")

        actual_hash = sha256_file(source_file)

        if actual_hash.lower() != expected_hash.lower():
            fail(
                "Safe Steam integrity check failed for "
                f"{relative_path}.\n"
                f"Expected: {expected_hash}\n"
                f"Actual:   {actual_hash}"
            )

        print(f"[Safe Steam Provisioner] Integrity OK: {relative_path}")


def fail(message: str) -> Never:
    print(f"[Safe Steam Provisioner] ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def steam_root() -> Path:
    home = Path.home()

    debian_path = home / ".steam" / "debian-installation"

    if debian_path.exists():
        return debian_path.resolve()

    return home / ".local" / "share" / "Steam"


def steam_is_running() -> bool:
    result = subprocess.run(
        [
            "pgrep",
            "-u",
            str(os.getuid()),
            "-f",
            "steamwebhelper|ubuntu12_32/steam",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )

    return result.returncode == 0


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        fail(f"Missing file: {path}")
    except json.JSONDecodeError as exc:
        fail(f"Invalid JSON in {path}: {exc}")


def build_safe_steam_entry(extension_path: Path = CONTAINER_EXTENSION_PATH) -> dict[str, Any]:
    entry = load_json(PREF_TEMPLATE)

    if entry.get("path") != "@SAFE_STEAM_PATH@":
        fail('Preference template does not contain "@SAFE_STEAM_PATH@" as its path.')

    entry["path"] = str(validate_load_path(extension_path))

    return entry


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=".Preferences.",
    )

    temp_path = Path(temp_name)

    try:
        with os.fdopen(fd, "w") as file:
            json.dump(
                data,
                file,
                separators=(",", ":"),
                sort_keys=True,
            )

            file.write("\n")
            file.flush()
            os.fsync(file.fileno())

        if path.exists():
            shutil.copymode(path, temp_path)

        os.replace(temp_path, path)

    finally:
        if temp_path.exists():
            temp_path.unlink()


def verify_source() -> None:
    manifest = SAFE_STEAM_PATH / "manifest.json"
    content_script = SAFE_STEAM_PATH / "content.js"

    if not manifest.is_file():
        fail(f"Safe Steam manifest missing: {manifest}")

    if not content_script.is_file():
        fail(f"Safe Steam content script missing: {content_script}")

    manifest_data = load_json(manifest)

    if manifest_data.get("key") != SAFE_STEAM_KEY:
        fail("Safe Steam manifest key does not match the expected permanent key.")

    if manifest_data.get("manifest_version") != 3:
        fail("Safe Steam manifest_version is not 3.")


def provision(extension_path: Path = CONTAINER_EXTENSION_PATH) -> None:
    print("[Safe Steam Provisioner] Starting.")

    if os.geteuid() == 0:
        fail("Run the provisioner as the protected desktop user, not root.")
    extension_path = validate_load_path(extension_path)

    if steam_is_running():
        fail("Steam is currently running. Shut Steam down before provisioning.")

    verify_source()
    verify_integrity()

    root = steam_root()

    prefs = root / "config" / "htmlcache" / "Default" / "Preferences"

    if not prefs.is_file():
        fail(
            "Steam CEF Preferences do not exist yet. "
            "Steam must be initialized at least once."
        )

    preferences = load_json(prefs)

    extensions = preferences.setdefault("extensions", {})
    settings = extensions.setdefault("settings", {})

    old_entry = settings.get(SAFE_STEAM_ID)

    if old_entry is None:
        print("[Safe Steam Provisioner] Safe Steam registration is missing.")
    else:
        print("[Safe Steam Provisioner] Existing Safe Steam registration found.")

    new_entry = build_safe_steam_entry(extension_path)

    settings[SAFE_STEAM_ID] = new_entry

    backup = prefs.with_name("Preferences.safesteam-backup")

    shutil.copy2(prefs, backup)

    print(f"[Safe Steam Provisioner] Backup created: {backup}")

    write_json_atomic(prefs, preferences)

    verify_preferences = load_json(prefs)

    installed_entry = (
        verify_preferences.get("extensions", {}).get("settings", {}).get(SAFE_STEAM_ID)
    )

    if installed_entry is None:
        fail("Safe Steam registration was not written successfully.")

    if installed_entry.get("state") != 1:
        fail("Safe Steam registration state is not enabled.")

    if installed_entry.get("location") != 4:
        fail("Safe Steam registration location is not 4.")

    if installed_entry.get("path") != str(extension_path):
        fail("Safe Steam registration path is incorrect.")

    print("[Safe Steam Provisioner] Provisioning successful.")
    print(f"[Safe Steam Provisioner] Extension ID: {SAFE_STEAM_ID}")
    print(f"[Safe Steam Provisioner] Host assets: {SAFE_STEAM_PATH}")
    print(f"[Safe Steam Provisioner] Steam load path: {extension_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--extension-path', choices=[str(path) for path in LOAD_PATHS],
                        default=str(CONTAINER_EXTENSION_PATH),
                        help='Path visible inside Steam; defaults to its container host mount')
    provision(Path(parser.parse_args().extension_path))
