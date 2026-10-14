"""Internal caller identity and a closed list of companion permissions.

Only trusted server code may construct a device context, AFTER verifying a
paired device's credentials and loading its grants from daemon-owned state.
This module does not authenticate devices or expose a network listener.
Never deserialize a CallerContext from request JSON.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import re

from sentinel.daemon.security.policy import PolicyDeniedError


DEVICE_ACTION_PERMISSIONS = {
    "api.capabilities": "api.read",
    "state.get": "settings.read",
    "settings.schema": "settings.read",
    "settings.snapshot": "settings.read",
    "settings.get": "settings.read",
    "settings.set": "settings.write",
    **{f"activity.{action}": "activity.read"
       for action in ("status", "events", "report", "sync", "usage")},
    **{action: "settings.read" for action in (
        "dns.status", "dns.blocklist.list", "dns.allowlist.list",
        "browser.status", "appblock.list", "appblock.status",
        "guardian.apps.list", "guardian.apps.policy", "guardian.websites.policy",
    )},
    **{action: "settings.write" for action in (
        "dns.blocklist.add", "dns.blocklist.remove",
        "dns.allowlist.add", "dns.allowlist.remove",
        "appblock.add", "appblock.remove",
        "guardian.apps.enable", "guardian.apps.approve", "guardian.apps.revoke",
        "guardian.apps.replace", "guardian.websites.enable",
        "guardian.websites.approve", "guardian.websites.revoke",
        "guardian.websites.replace",
    )},
    **{f"blockwindow.{action}": "block_windows.read"
       for action in ("policy", "list", "get", "effective")},
    **{f"blockwindow.{action}": "block_windows.write"
       for action in ("create", "update", "delete")},
    'screenblock.status': 'block_windows.read',
    'screenblock.bypass': 'block_windows.write',
    'screenlimit.status': 'settings.read',
    'screenlimit.set': 'settings.write',
}
DEVICE_PERMISSIONS = frozenset(DEVICE_ACTION_PERMISSIONS.values())


@dataclass(frozen=True)
class CallerContext:
    kind: str
    device_id: str | None = None
    permissions: frozenset[str] = frozenset()

    def __post_init__(self):
        if self.kind not in {"local", "device"}:
            raise ValueError("Invalid caller kind.")
        if not isinstance(self.permissions, frozenset):
            raise ValueError("Caller permissions must be immutable.")
        if self.kind == "local":
            if self.device_id is not None or self.permissions:
                raise ValueError("Local callers cannot claim device grants.")
        elif (not isinstance(self.device_id, str)
              or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.device_id)
              or not self.permissions <= DEVICE_PERMISSIONS):
            raise ValueError("Invalid device identity or permissions.")

    def require_action(self, action: str) -> None:
        if self.kind == "device":
            required = DEVICE_ACTION_PERMISSIONS.get(action)
            if required is None or required not in self.permissions:
                raise PolicyDeniedError("This device is not permitted to perform this action.")


LOCAL_CALLER = CallerContext("local")
current_caller: ContextVar[CallerContext] = ContextVar("sentineld_caller", default=LOCAL_CALLER)
