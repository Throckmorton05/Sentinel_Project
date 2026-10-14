"""
Central authorization policy for sentineld.

The policy layer decides whether a requested operation is allowed
under Sentinel's current lock mode.

Lock modes:

    unlocked
        The user may freely strengthen or weaken Sentinel.

    self_lock
        The user may maintain or strengthen protection, but may
        never weaken protection.

    guardian_lock
        Read-only operations are allowed normally. Any change
        requires successful guardian authentication.
"""

from __future__ import annotations

from enum import Enum

from sentinel.daemon.core.lock_state import (
    LockMode,
    get_lock_state,
)


class ChangeKind(str, Enum):
    """
    Security effect of a requested operation.
    """

    READ = "read"

    MAINTAIN = "maintain"

    STRENGTHEN = "strengthen"

    WEAKEN = "weaken"


class PolicyDeniedError(ValueError):
    """
    Raised when Sentinel policy rejects an operation.
    """


def require_policy(
    action: str,
    change: ChangeKind,
    *,
    guardian_authenticated: bool = False,
) -> None:
    """
    Require permission for an operation.

    Parameters
    ----------
    action:
        Human-readable/internal name of the operation.

    change:
        Whether the operation reads, maintains, strengthens,
        or weakens Sentinel protection.

    guardian_authenticated:
        True only when sentineld itself has successfully verified
        guardian credentials.

        Clients must NEVER be allowed to supply this value directly.
    """

    state = get_lock_state()

    #
    # UNLOCKED
    #
    # The protected user controls Sentinel normally.
    #

    if state.mode is LockMode.UNLOCKED:
        return

    #
    # SELF LOCK
    #
    # Protection may stay the same or become stronger.
    #
    # It may never become weaker.
    #

    if state.mode is LockMode.SELF_LOCK:
        if change is ChangeKind.WEAKEN:
            raise PolicyDeniedError(
                f"{action} would weaken Sentinel protection "
                "and is not allowed while Self Lock is enabled."
            )

        return

    #
    # GUARDIAN LOCK
    #
    # Reading state is harmless.
    #
    # Every mutation requires guardian authentication,
    # regardless of whether it strengthens or weakens protection.
    #

    if state.mode is LockMode.GUARDIAN_LOCK:
        if change is ChangeKind.READ:
            return

        if not guardian_authenticated:
            raise PolicyDeniedError(f"{action} requires guardian authentication.")

        return

    #
    # Defensive fail-closed fallback.
    #

    raise PolicyDeniedError("Sentinel is in an unknown lock state.")


def classify_boolean_change(
    *,
    old: bool,
    new: bool,
) -> ChangeKind:
    """
    Classify an on/off protection setting.

    False -> True   strengthens protection
    True  -> False  weakens protection
    same  -> same   maintains protection
    """

    if old == new:
        return ChangeKind.MAINTAIN

    if new:
        return ChangeKind.STRENGTHEN

    return ChangeKind.WEAKEN


def classify_ranked_change(
    *,
    old: int,
    new: int,
) -> ChangeKind:
    """
    Classify settings where larger numbers mean stronger protection.

    Example:

        0 = disabled
        1 = basic explicit-site blocking
        2 = adult-heavy-site blocking
        3 = strict filtering
    """

    if new == old:
        return ChangeKind.MAINTAIN

    if new > old:
        return ChangeKind.STRENGTHEN

    return ChangeKind.WEAKEN
