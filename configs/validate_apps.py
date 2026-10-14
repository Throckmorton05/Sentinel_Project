#!/usr/bin/env python3

from typing import Any
import subprocess
import sys
import tomllib
from pathlib import Path


CONFIG_PATH = Path(__file__).with_name("approved_apps.toml")
FLATPAK_REMOTE = "flathub"
TIMEOUT_SECONDS = 15


AppConfig = dict[str, dict[str, Any]]


def load_approved_apps() -> AppConfig:
    """Load approved_apps.toml."""

    try:
        with CONFIG_PATH.open("rb") as file:
            config = tomllib.load(file)
    except FileNotFoundError:
        print(f"ERROR: Config file not found: {CONFIG_PATH}")
        sys.exit(1)
    except tomllib.TOMLDecodeError as exc:
        print(f"ERROR: Invalid TOML in {CONFIG_PATH}")
        print(exc)
        sys.exit(1)

    apps = config.get("apps")

    if not isinstance(apps, dict):
        print("ERROR: approved_apps.toml does not contain an [apps] table.")
        sys.exit(1)

    return apps


def validate_flatpak_id(flatpak_id: str) -> tuple[bool, str | None]:
    """
    Check whether a Flatpak application exists on Flathub.

    Returns:
        (True, None) if the application exists.
        (False, reason) otherwise.
    """

    try:
        result = subprocess.run(
            [
                "flatpak",
                "remote-info",
                FLATPAK_REMOTE,
                flatpak_id,
            ],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )

    except FileNotFoundError:
        print("ERROR: Flatpak is not installed or is not in PATH.")
        sys.exit(1)

    except subprocess.TimeoutExpired:
        return False, "request timed out"

    if result.returncode == 0:
        return True, None

    error = result.stderr.strip()

    if error:
        return False, error

    return False, "application was not found on Flathub"


def main() -> int:
    apps = load_approved_apps()

    valid = 0
    invalid = 0
    skipped = 0

    print(f"Validating {len(apps)} Sentinel AppStore entries...")
    print(f"Remote: {FLATPAK_REMOTE}")
    print()

    for key, app in apps.items():
        if not isinstance(app, dict):
            print(f"⚠ {key}: invalid application entry")
            invalid += 1
            continue

        if not app.get("enabled", True):
            print(f"– {key}: disabled, skipping")
            skipped += 1
            continue

        name = app.get("name", key)
        flatpak_id = app.get("flatpak_id")

        if not flatpak_id:
            print(f"✗ {name}: missing flatpak_id")
            invalid += 1
            continue

        is_valid, error = validate_flatpak_id(flatpak_id)

        if is_valid:
            print(f"✓ {name:<30} {flatpak_id}")
            valid += 1
        else:
            print(f"✗ {name:<30} {flatpak_id}")
            print(f"  └─ {error}")
            invalid += 1

    print()
    print("=" * 65)
    print("Validation complete")
    print(f"  Valid:   {valid}")
    print(f"  Invalid: {invalid}")
    print(f"  Skipped: {skipped}")
    print(f"  Total:   {len(apps)}")
    print("=" * 65)

    if invalid:
        print()
        print("One or more AppStore entries need attention.")
        return 1

    print()
    print("All enabled Flatpak IDs are valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
