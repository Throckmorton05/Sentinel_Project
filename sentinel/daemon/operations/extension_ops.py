"""Guardian-authenticated per-extension installation, under the RPC mutation lock."""
import time

from sentinel.daemon.core.lock_state import LockMode, get_lock_state
from sentinel.daemon.security.guardian_auth import verify_guardian_pin
from sentinel.daemon.security.policy import PolicyDeniedError
from sentinel.extension_catalog.catalog import load_inventory
from sentinel.extension_catalog.approvals import load_approvals, save_approvals

_failed_attempts = []
_blocked_until = 0.0


def change_approval(extension_id, operation, *, guardian_pin=None):
    """No caller-supplied authentication flag is accepted or trusted."""
    global _blocked_until
    if operation not in {"approve", "revoke"}:
        raise ValueError("Unknown extension approval operation")
    if not isinstance(extension_id, str) or extension_id not in {entry["id"] for entry in load_inventory()}:
        raise ValueError("This extension is not in the approved catalog")
    if get_lock_state().mode is not LockMode.GUARDIAN_LOCK:
        raise PolicyDeniedError("Guardian approval is only used in Guardian mode. Refresh the catalog.")
    now = time.monotonic()
    if now < _blocked_until:
        raise PolicyDeniedError("Too many incorrect PIN attempts. Try again in 30 seconds.")
    try:
        authenticated = isinstance(guardian_pin, str) and verify_guardian_pin(guardian_pin)
    except ValueError:
        authenticated = False
    if not authenticated:
        _failed_attempts[:] = [value for value in _failed_attempts if now - value < 60]
        _failed_attempts.append(now)
        if len(_failed_attempts) >= 5:
            _blocked_until = now + 30
            _failed_attempts.clear()
        raise PolicyDeniedError("Incorrect Guardian PIN.")
    _failed_attempts.clear()
    approvals = load_approvals()
    if operation == "approve":
        approvals.add(extension_id)
    else:
        approvals.discard(extension_id)
    save_approvals(approvals)
    from sentinel.daemon.operations import browser_ops
    application = browser_ops.apply_browser_policy()
    return {"extension_id": extension_id, "operation": operation, "application": application}
