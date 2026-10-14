from __future__ import annotations

import re


SENTINEL_PROFILE_MARKER = "# Sentinel managed application block profile"

_LEGACY_PROFILE_RE = re.compile(
    r"""\A\s*abi\s+<abi/4\.0>,\s*\n\s*
    (?P<executable>/[^\s{]+)
    (?:\s+flags=\(attach_disconnected\))?\s*\{\s*\n\s*
    deny\s+/\*\*\s+r,\s*\n\s*
    deny\s+/\*\*\s+w,\s*\n\s*
    deny\s+/\*\*\s+x,\s*\n\s*
    \}\s*\Z""",
    re.VERBOSE,
)


def generate_block_profile(executable: str) -> str:
    return f"""{SENTINEL_PROFILE_MARKER}
abi <abi/4.0>,

{executable} {{
    deny /** r,
    deny /** w,
    deny /** x,
}}
"""


def legacy_blocked_executable(profile_text: str) -> str | None:
    """Return the executable when *profile_text* is an old Sentinel block.

    Sentinel's original app blocker wrote bare deny-all profiles directly into
    /etc/apparmor.d without a marker. Migration therefore recognizes only the
    tiny historic profile shape, with or without the attach_disconnected flag
    used by an earlier revision. Anything with includes, capabilities, extra
    rules, a named profile, or different flags is left untouched.
    """

    match = _LEGACY_PROFILE_RE.fullmatch(profile_text)
    if match is None:
        return None
    return match.group("executable")
