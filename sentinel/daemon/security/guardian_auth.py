"""
Guardian authentication for Sentinel.

Guardian credentials are owned and verified exclusively by sentineld.

Clients may submit a guardian PIN, but they must never be able to
declare themselves authenticated. Only this module determines whether
guardian authentication succeeded.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any

from sentinel.settings.paths import GUARDIAN_AUTH_FILE


AUTH_VERSION = 2
LEGACY_AUTH_VERSION = 1

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


_auth_lock = threading.RLock()

_current_path: Path | None = None


def _validate_pin(
    pin: str,
) -> None:
    """
    Validate the format of a guardian PIN.
    """

    if not isinstance(
        pin,
        str,
    ):
        raise ValueError("Guardian PIN must be a string.")

    if not pin.isascii() or not pin.isdigit():
        raise ValueError("Guardian PIN must contain only digits.")

    if len(pin) < 4:
        raise ValueError("Guardian PIN must contain at least 4 digits.")

    if len(pin) > 32:
        raise ValueError("Guardian PIN is too long.")


def _hash_pin(
    pin: str,
    salt: bytes,
) -> bytes:
    """
    Derive a password hash from a guardian PIN.
    """

    return hashlib.scrypt(
        pin.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )


def _validate_parent(
    path: Path,
) -> None:
    """
    Ensure the guardian credential directory is protected.
    """

    path.parent.mkdir(
        mode=0o755,
        parents=True,
        exist_ok=True,
    )

    info = path.parent.stat()

    if info.st_uid != 0:
        raise ValueError(f"{path.parent} must be owned by root.")

    if info.st_mode & 0o022:
        raise ValueError(f"{path.parent} must not be writable by group or others.")


def _build_auth_data(
    pin: str,
) -> dict[str, Any]:
    """
    Generate a new salted guardian PIN hash.
    """

    _validate_pin(
        pin,
    )

    salt = secrets.token_bytes(
        32,
    )

    pin_hash = _hash_pin(
        pin,
        salt,
    )

    return {
        "version": AUTH_VERSION,
        "configured": True,
        "algorithm": "scrypt",
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "dklen": SCRYPT_DKLEN,
        "salt": salt.hex(),
        "hash": pin_hash.hex(),
    }


def _validate_auth_data(
    data: object,
) -> dict[str, Any]:
    """
    Validate guardian authentication data loaded from disk.
    """

    if not isinstance(
        data,
        dict,
    ):
        raise ValueError("Sentinel guardian authentication data must be a JSON object.")

    version = data.get("version")
    if type(version) is not int or version not in {LEGACY_AUTH_VERSION, AUTH_VERSION}:
        raise ValueError("Unsupported Sentinel guardian authentication version.")
    if version == LEGACY_AUTH_VERSION and "configured" in data:
        raise ValueError("Legacy Guardian credentials cannot declare a setup state.")

    if version == AUTH_VERSION:
        if data.get("configured") is False:
            if set(data) != {"version", "configured"}:
                raise ValueError("Unconfigured Guardian credentials cannot contain a PIN hash.")
            return data
        if data.get("configured") is not True:
            raise ValueError("Invalid Guardian PIN configuration state.")

    if data.get("algorithm") != "scrypt":
        raise ValueError("Unsupported Sentinel guardian authentication algorithm.")

    expected_values = {
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "dklen": SCRYPT_DKLEN,
    }

    for key, expected in expected_values.items():
        if data.get(key) != expected:
            raise ValueError(f"Invalid guardian authentication parameter: {key}")

    salt_hex = data.get(
        "salt",
    )

    hash_hex = data.get(
        "hash",
    )

    if not isinstance(
        salt_hex,
        str,
    ):
        raise ValueError("Invalid guardian authentication salt.")

    if not isinstance(
        hash_hex,
        str,
    ):
        raise ValueError("Invalid guardian authentication hash.")

    try:
        salt = bytes.fromhex(
            salt_hex,
        )

        pin_hash = bytes.fromhex(
            hash_hex,
        )

    except ValueError as exc:
        raise ValueError("Invalid guardian authentication encoding.") from exc

    if len(salt) != 32:
        raise ValueError("Invalid guardian authentication salt length.")

    if len(pin_hash) != SCRYPT_DKLEN:
        raise ValueError("Invalid guardian authentication hash length.")

    return data


def _read_auth(
    path: Path,
) -> dict[str, Any]:
    """
    Safely read the root-owned guardian credential file.
    """

    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

    try:
        fd = os.open(
            path,
            flags,
        )

    except OSError as exc:
        raise ValueError(
            f"Cannot load Sentinel guardian authentication data from {path}: {exc}"
        ) from exc

    with os.fdopen(
        fd,
        "r",
        encoding="utf-8",
    ) as stream:
        info = os.fstat(
            stream.fileno(),
        )

        if not stat.S_ISREG(
            info.st_mode,
        ):
            raise ValueError(f"{path} must be a regular file.")

        if info.st_uid != 0:
            raise ValueError(f"{path} must be owned by root.")

        if info.st_mode & 0o077:
            raise ValueError(f"{path} must only be accessible by root.")

        try:
            data = json.load(
                stream,
            )

        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {path}: {exc}") from exc

    return _validate_auth_data(
        data,
    )


def _write_auth(
    data: dict[str, Any],
    path: Path,
) -> None:
    """
    Atomically persist guardian authentication data.
    """

    _validate_parent(
        path,
    )

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )

    temporary_path = Path(
        temporary_name,
    )

    try:
        os.fchmod(
            fd,
            0o600,
        )

        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
        ) as stream:
            json.dump(
                data,
                stream,
                indent=2,
                sort_keys=True,
            )

            stream.write(
                "\n",
            )

            stream.flush()

            os.fsync(
                stream.fileno(),
            )

        os.replace(
            temporary_path,
            path,
        )

        directory_fd = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY,
        )

        try:
            os.fsync(
                directory_fd,
            )

        finally:
            os.close(
                directory_fd,
            )

    except Exception:
        try:
            os.close(
                fd,
            )

        except OSError:
            pass

        temporary_path.unlink(
            missing_ok=True,
        )

        raise


def initialize_guardian_auth(
    path: Path = GUARDIAN_AUTH_FILE,
    *,
    require_configured: bool = False,
) -> None:
    """Validate existing credentials or create a protected, PIN-free store.

    Legacy salted hashes remain valid. Missing or unset credentials never
    recreate a default PIN, and cannot start an already Guardian-locked system.
    """
    global _current_path

    with _auth_lock:
        _validate_parent(path)
        if path.exists() or path.is_symlink():
            data = _read_auth(path)
        else:
            if require_configured:
                raise ValueError("Guardian Lock requires an existing configured PIN.")
            data = {"version": AUTH_VERSION, "configured": False}
            _write_auth(data, path)
        if require_configured and not data.get("configured", True):
            raise ValueError("Guardian Lock requires an existing configured PIN.")
        _current_path = path


def guardian_pin_configured() -> bool:
    """Expose only whether a validated credential has been configured."""
    with _auth_lock:
        data = _read_auth(_require_initialized())
        return data.get("configured", True)


def setup_guardian_pin(new_pin: str, pin_confirmation: str, *,
                       replace_existing: bool = False) -> None:
    """Persist a confirmed PIN for entry from Unlocked.

    Only trusted dispatch may request replacement after checking the mode.
    A client JSON boolean never controls this flag.
    """
    _validate_pin(new_pin)
    _validate_pin(pin_confirmation)
    if new_pin != pin_confirmation:
        raise ValueError("The Guardian PINs do not match.")
    with _auth_lock:
        path = _require_initialized()
        if _read_auth(path).get("configured", True) and not replace_existing:
            raise ValueError("A Guardian PIN is already configured. Use Change guardian PIN.")
        _write_auth(_build_auth_data(new_pin), path)


def _require_initialized() -> Path:
    if _current_path is None:
        raise RuntimeError("Guardian authentication has not been initialized.")

    return _current_path


def verify_guardian_pin(
    pin: str,
) -> bool:
    """
    Verify a guardian PIN.

    Returns True only when the supplied PIN matches the stored hash.
    """

    _validate_pin(
        pin,
    )

    with _auth_lock:
        path = _require_initialized()

        data = _read_auth(
            path,
        )

        if not data.get("configured", True):
            return False

        salt = bytes.fromhex(
            data["salt"],
        )

        expected_hash = bytes.fromhex(
            data["hash"],
        )

        supplied_hash = _hash_pin(
            pin,
            salt,
        )

        return hmac.compare_digest(
            supplied_hash,
            expected_hash,
        )


def change_guardian_pin(
    current_pin: str,
    new_pin: str,
) -> None:
    """
    Replace the guardian PIN after authenticating the current PIN.
    """

    _validate_pin(
        current_pin,
    )

    _validate_pin(
        new_pin,
    )

    with _auth_lock:
        path = _require_initialized()

        if not verify_guardian_pin(
            current_pin,
        ):
            raise ValueError("Invalid guardian PIN.")

        data = _build_auth_data(
            new_pin,
        )

        _write_auth(
            data,
            path,
        )
