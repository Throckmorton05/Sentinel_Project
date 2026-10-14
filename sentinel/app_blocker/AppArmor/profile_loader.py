from __future__ import annotations

import subprocess
from pathlib import Path


APPARMOR_PARSER = "/usr/sbin/apparmor_parser"
APPARMOR_SECURITYFS = Path("/sys/kernel/security/apparmor")
APPARMOR_PROFILES = APPARMOR_SECURITYFS / "profiles"
APPARMOR_REMOVE = APPARMOR_SECURITYFS / ".remove"


def _profile_names(profile_path: Path) -> tuple[str, ...]:
    """Ask apparmor_parser for the kernel profile names in *profile_path*."""
    result = subprocess.run(
        [APPARMOR_PARSER, "-N", str(profile_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    names = tuple(line.strip() for line in result.stdout.splitlines() if line.strip())
    if not names:
        raise RuntimeError(f"AppArmor profile contains no policy names: {profile_path}")
    return names


def _loaded_profile_names() -> set[str] | None:
    """Return loaded root-namespace profile names when securityfs is available."""
    try:
        raw = APPARMOR_PROFILES.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None

    names: set[str] = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # securityfs renders entries as "profile-name (enforce)" or
        # "profile-name (complain)". Profile names themselves may contain
        # spaces, so strip only the final mode suffix.
        if line.endswith(" (enforce)"):
            line = line[: -len(" (enforce)")]
        elif line.endswith(" (complain)"):
            line = line[: -len(" (complain)")]
        elif line.endswith(" (kill)"):
            line = line[: -len(" (kill)")]
        names.add(line)
    return names


def _remove_profile_from_kernel(profile_name: str) -> None:
    """Remove a loaded profile directly through AppArmor securityfs."""
    with APPARMOR_REMOVE.open("w", encoding="utf-8") as stream:
        stream.write(profile_name)


def load_profile(profile_path: Path) -> None:
    """Load or reload an AppArmor profile as the root sentineld process."""
    subprocess.run(
        [APPARMOR_PARSER, "-r", str(profile_path)],
        check=True,
        capture_output=True,
        text=True,
    )


def unload_profile(profile_path: Path) -> None:
    """Unload a profile and verify that it actually left the kernel.

    apparmor_parser -R is the normal removal path, but Sentinel must not delete
    its on-disk retry marker merely because the parser returned success.  Some
    stale/upgrade states can leave the profile visible in AppArmor securityfs.
    In that case remove the exact policy name through securityfs' .remove
    interface, then verify again.
    """
    profile_names = _profile_names(profile_path)

    subprocess.run(
        [APPARMOR_PARSER, "-R", str(profile_path)],
        check=True,
        capture_output=True,
        text=True,
    )

    loaded = _loaded_profile_names()
    if loaded is None:
        # If AppArmor securityfs is unavailable there is no reliable kernel
        # state to verify here; preserve the parser's result.
        return

    remaining = [name for name in profile_names if name in loaded]
    for profile_name in remaining:
        _remove_profile_from_kernel(profile_name)

    if remaining:
        loaded = _loaded_profile_names()
        if loaded is None:
            return
        still_loaded = [name for name in remaining if name in loaded]
        if still_loaded:
            raise RuntimeError(
                "AppArmor profile remained loaded after removal: "
                + ", ".join(still_loaded)
            )
