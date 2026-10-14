"""Versioned discovery for local clients and a future paired-device adapter."""
from sentinel.daemon.rpc.protocol import PROTOCOL_VERSION, MAX_MESSAGE_BYTES
from sentinel.daemon.security.caller_context import (
    DEVICE_ACTION_PERMISSIONS, DEVICE_PERMISSIONS, current_caller,
)
from sentinel.activity.constants import RETENTION_DAYS


def get_capabilities() -> dict:
    caller = current_caller.get()
    return {
        "api_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "transport": {"unix_socket": True, "lan_listener": False,
                      "device_pairing": False},
        "features": ["settings_schema", "settings_snapshot", "settings_revision",
                     "activity_sync", "screen_time", "screen_time_blocks", "recurring_block_windows",
                     "daily_screen_time_limit", "activity_search", "device_permission_dispatch",
                     "sentinel_package_updates"],
        "limits": {"message_bytes": MAX_MESSAGE_BYTES,
                   "activity_page_events": 20, "activity_retention_days": RETENTION_DAYS},
        "companion_actions": dict(sorted(DEVICE_ACTION_PERMISSIONS.items())),
        "device_permissions": sorted(DEVICE_PERMISSIONS),
        "caller": {"kind": caller.kind, "device_id": caller.device_id,
                   "permissions": sorted(caller.permissions)},
        "available_companion_actions": sorted(
            action for action, permission in DEVICE_ACTION_PERMISSIONS.items()
            if caller.kind == "local" or permission in caller.permissions
        ),
        "authorization": {"lock_policy_always_applies": True,
                          "self_lock_is_permanent": True},
    }
