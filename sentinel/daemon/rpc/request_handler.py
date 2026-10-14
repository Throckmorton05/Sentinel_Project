"""Route the daemon's explicit RPC actions to their owning modules."""

from __future__ import annotations

import subprocess
from sentinel.activity.store import get_store
from sentinel.activity.collectors import monitor_block
import threading
from typing import Any

from sentinel.daemon.core.lock_state import (
    LockMode,
    enter_guardian_lock,
    enter_self_lock,
    exit_guardian_lock,
    get_lock_state,
)
from sentinel.daemon.operations import (
    appstore_ops,
    app_blocker_ops,
    guardian_app_ops,
    guardian_website_ops,
    block_window_ops,
    dns_ops,
    browser_ops,
    extension_ops,
    image_filter_ops,
    daily_screen_time_ops,
    vm_ops,
)
from sentinel.daemon.operations.desktop_ops import configure_desktop
from sentinel.daemon.operations.portal_ops import desktop_status, extension_catalog, installed_apps
from sentinel.daemon.operations.managed_services import (
    _add_managed_service,
    _delete_managed_service,
    _disable_managed_service,
    _enable_managed_service,
    _require_service_name,
    _start_managed_service,
    _stop_managed_service,
)
from sentinel.daemon.operations.package_ops import (
    _install_package,
    _update_package,
    _require_package_name,
    _uninstall_package,
)
from sentinel.daemon.operations.settings_ops import (
    get_setting as _get_setting,
)
from sentinel.daemon.operations.settings_ops import (
    set_setting as _set_setting,
    get_schema as _get_settings_schema,
    get_snapshot as _get_settings_snapshot,
    SettingsRevisionConflict,
)
from sentinel.daemon.rpc.capabilities import get_capabilities
from sentinel.daemon.security.caller_context import CallerContext, LOCAL_CALLER, current_caller
from sentinel.daemon.operations.system_ops import (
    _system_restart,
    _system_shutdown,
    _system_update,
    _sentinel_update,
)
from sentinel.daemon.rpc.protocol import PROTOCOL_VERSION
from sentinel.daemon.rpc.responses import _error, _success
from sentinel.daemon.security.guardian_auth import (
    change_guardian_pin,
    guardian_pin_configured,
    setup_guardian_pin,
    verify_guardian_pin,
)
from sentinel.daemon.security.policy import (
    ChangeKind,
    PolicyDeniedError,
    require_policy,
)
from sentinel.daemon.security.tamper_protection import _register_tamper_blocker
from sentinel.daemon.supervision.screen_monitor import (
    _register_screen_monitor,
    _screen_monitor_heartbeat,
)


def _guardian_authenticated(
    params: dict[str, Any],
) -> bool:
    """
    Verify guardian credentials supplied with an RPC request.

    Clients supply only the PIN. The trusted authentication boolean
    is generated exclusively inside sentineld.
    """

    # This context comes only from trusted dispatch code, never request JSON.
    # Device action permissions have already been checked before routing.
    if current_caller.get().kind == "device":
        return True

    guardian_pin = params.get(
        "guardian_pin",
    )

    if not isinstance(
        guardian_pin,
        str,
    ):
        return False

    try:
        return verify_guardian_pin(
            guardian_pin,
        )

    except ValueError:
        return False


# User-managed services and desktop configuration have no proven protection
# ordering, so Self Lock denies their mutations conservatively. Reads and normal
# system update/restart/shutdown remain available; Guardian requires a PIN for
# every explicit administrative mutation.
_ADMIN_CHANGES = {
    "system.update": ChangeKind.MAINTAIN,
    "sentinel.update": ChangeKind.MAINTAIN,
    "system.restart": ChangeKind.MAINTAIN,
    "system.shutdown": ChangeKind.MAINTAIN,
    **{f"service.{name}": ChangeKind.WEAKEN
       for name in ("add", "start", "stop", "enable", "disable", "delete")},
    **{f"autostart.{name}": ChangeKind.WEAKEN for name in ("add", "remove")},
    **{f"autologin.{name}": ChangeKind.WEAKEN for name in ("enable", "disable")},
    "updates.enable": ChangeKind.WEAKEN,
    "updates.disable": ChangeKind.WEAKEN,
    "updates.restart_enable": ChangeKind.WEAKEN,
    "updates.restart_disable": ChangeKind.WEAKEN,
}
_DESKTOP_ACTIONS = {
    action for action in _ADMIN_CHANGES
    if action.startswith(("updates.", "autologin.", "autostart."))
}


def _authorize_admin(action: str, params: dict[str, Any]) -> None:
    authenticated = (
        _guardian_authenticated(params)
        if get_lock_state().mode is LockMode.GUARDIAN_LOCK else False
    )
    require_policy(action, _ADMIN_CHANGES[action], guardian_authenticated=authenticated)


# Keep policy evaluation, authentication and mutation in one serialized
# operation. Read/heartbeat requests remain available during package work.
_mutation_lock = threading.Lock()
_CONCURRENT_ACTIONS = {
    "api.capabilities", "settings.schema", "settings.snapshot", "activity.sync",
    "activity.usage", "activity.status", "activity.events", "activity.report", "activity.monitor_block",
    "ping", "state.get", "guardian.pin_status", "settings.get", "appstore.policy",
    "appstore.installed", "appstore.updates", "monitor.register",
    "monitor.heartbeat", "tamper.blocker.register",
    "dns.status", "dns.blocklist.list", "dns.allowlist.list", "browser.status",
    "appblock.list", "appblock.status", "appblock.catalog",
    "guardian.apps.list", "guardian.apps.policy", "guardian.apps.launch",
    "guardian.websites.policy",
    "blockwindow.policy", "blockwindow.list", "blockwindow.get",
    "blockwindow.effective", "screenblock.status", "screenblock.register",
    "screenlimit.status",
    "desktop.status", "browser.extensions.list", "imagefilter.status",
}


def _handle_request(request: dict[str, Any], *, uid: int, pid: int,
                    caller: CallerContext | None = None) -> dict[str, Any]:
    """Dispatch a verified local peer, or a future adapter's verified device.

    uid/pid identify the actual local peer/adapter. A future device adapter must
    independently authenticate the device and load grants from protected state.
    JSON fields must never be used to construct the caller argument.
    """
    token = current_caller.set(caller if caller is not None else LOCAL_CALLER)
    try:
        return _handle_scoped_request(request, uid=uid, pid=pid)
    finally:
        current_caller.reset(token)


def _handle_scoped_request(request: dict[str, Any], *, uid: int, pid: int) -> dict[str, Any]:
    action = request.get("action")
    if isinstance(action, str) and action in _CONCURRENT_ACTIONS:
        return _dispatch_request(request, uid=uid, pid=pid)
    if not _mutation_lock.acquire(blocking=False):
        request_id = request.get("request_id")
        return _error(request_id if isinstance(request_id, str) else None,
                      "daemon_busy", "Another administrative operation is running. Try again when it finishes.")
    try:
        return _dispatch_request(request, uid=uid, pid=pid)
    finally:
        _mutation_lock.release()


def _dispatch_request(
    request: dict[str, Any],
    *,
    uid: int,
    pid: int,
) -> dict[str, Any]:

    request_id = request.get("request_id")

    if not isinstance(
        request_id,
        str,
    ):
        return _error(
            None,
            "invalid_request",
            "Missing or invalid request_id.",
        )

    action = request.get("action")

    if not isinstance(
        action,
        str,
    ):
        return _error(
            request_id,
            "invalid_request",
            "Missing or invalid action.",
        )

    params = request.get(
        "params",
        {},
    )

    if not isinstance(
        params,
        dict,
    ):
        return _error(
            request_id,
            "invalid_request",
            "params must be an object.",
        )

    #
    # sentineld NEVER accepts arbitrary shell commands.
    #
    # Every privileged operation must have an explicit action below.
    #

    try:
        current_caller.get().require_action(action)
        if current_caller.get().kind == "device" and any(
            name in params for name in ("guardian_pin", "guardian_authenticated")
        ):
            raise PolicyDeniedError("Device requests use paired credentials, not PIN parameters.")
        if action == "api.capabilities":
            return _success(request_id, get_capabilities())
        if action == "settings.schema":
            return _success(request_id, _get_settings_schema())
        if action == "settings.snapshot":
            return _success(request_id, _get_settings_snapshot())
        if action in _ADMIN_CHANGES:
            _authorize_admin(action, params)
        if action in _DESKTOP_ACTIONS:
            return _success(request_id, configure_desktop(action, params, uid))

        if action == "guardian.websites.policy":
            return _success(request_id, guardian_website_ops.policy())
        if action.startswith("guardian.websites."):
            authenticated = _guardian_authenticated(params)
            guardian_website_ops._authorize(authenticated)
            if action == "guardian.websites.authenticate":
                result = {"authenticated": True}
            elif action == "guardian.websites.enable":
                result = guardian_website_ops.set_enabled(params.get("enabled"), guardian_authenticated=authenticated)
            elif action == "guardian.websites.approve":
                result = guardian_website_ops.approve(params.get("domain"), guardian_authenticated=authenticated)
            elif action == "guardian.websites.revoke":
                result = guardian_website_ops.revoke(params.get("domain"), guardian_authenticated=authenticated)
            elif action == "guardian.websites.replace":
                result = guardian_website_ops.replace_selection(params.get("domains"), params.get("enabled"),
                                                               guardian_authenticated=authenticated)
            else:
                raise ValueError("Unknown Guardian website operation.")
            return _success(request_id, result)

        if action == "guardian.apps.list":
            return _success(request_id, guardian_app_ops.list_apps())
        if action == "guardian.apps.policy":
            return _success(request_id, guardian_app_ops.policy())
        if action == "guardian.apps.launch":
            return _success(request_id, guardian_app_ops.launch_info(params.get("desktop_id")))
        if action.startswith("guardian.apps."):
            authenticated = _guardian_authenticated(params)
            guardian_app_ops._authorize(authenticated)
            if action == "guardian.apps.authenticate":
                result = {"authenticated": True}
            elif action == "guardian.apps.enable":
                result = guardian_app_ops.set_enabled(params.get("enabled"), guardian_authenticated=authenticated)
            elif action == "guardian.apps.approve":
                result = guardian_app_ops.approve(params.get("desktop_id"), guardian_authenticated=authenticated)
            elif action == "guardian.apps.revoke":
                result = guardian_app_ops.revoke(params.get("desktop_id"), guardian_authenticated=authenticated)
            elif action == "guardian.apps.replace":
                result = guardian_app_ops.replace_selection(params.get("desktop_ids"), params.get("enabled"),
                                                           guardian_authenticated=authenticated)
            else:
                raise ValueError("Unknown Guardian app operation.")
            return _success(request_id, result)

        # --------------------------------------------------------
        # Diagnostic
        # --------------------------------------------------------

        if action == "imagefilter.status":
            return _success(request_id, image_filter_ops.get_status())
        if action == "desktop.status":
            return _success(request_id, desktop_status())
        if action == "appblock.catalog":
            return _success(request_id, installed_apps(offset=params.get("offset", 0), limit=params.get("limit", 25)))
        if action == "browser.extensions.list":
            return _success(request_id, extension_catalog(offset=params.get("offset", 0), limit=params.get("limit", 20)))

        if action == "ping":
            return _success(
                request_id,
                {
                    "status": "ok",
                    "daemon": "sentineld",
                    "protocol_version": PROTOCOL_VERSION,
                    "peer_uid": uid,
                    "peer_pid": pid,
                },
            )

        if action == "activity.status":
            return _success(request_id, get_store().status())
        if action == "activity.sync":
            if set(params) - {"cursor", "limit"}:
                raise ValueError("activity.sync accepts only cursor and limit.")
            return _success(request_id, get_store().sync(
                cursor=params.get("cursor"), limit=params.get("limit", 20)))
        if action == "activity.events":
            return _success(request_id, get_store().events(
                start=params.get("start"), end=params.get("end"),
                kind=params.get("kind"), before_id=params.get("before_id"),
                limit=params.get("limit", 10), query=params.get("query")))
        if action == "activity.usage":
            return _success(request_id, get_store().usage_report(
                date=params.get("date"), timezone_name=params.get("timezone", "UTC")))
        if action == "activity.report":
            return _success(request_id, get_store().report(
                period=params.get("period", "week"), date=params.get("date"),
                timezone_name=params.get("timezone", "UTC")))
        if action == "activity.monitor_block":
            return _success(request_id, monitor_block(params, uid=uid, pid=pid))

        # --------------------------------------------------------
        # System
        # --------------------------------------------------------

        if action == "system.update":
            result = _system_update()

            return _success(
                request_id,
                {
                    **(result or {}),
                    "status": "complete",
                    "action": action,
                },
            )

        if action == "sentinel.update":
            if set(params) - {"guardian_pin"}:
                raise ValueError("Sentinel updates do not accept package names or sources.")
            result = _sentinel_update()
            return _success(request_id, {**result, "action": action})

        if action == "system.restart":
            _system_restart()

            return _success(
                request_id,
                {
                    "status": "accepted",
                    "action": action,
                },
            )

        if action == "system.shutdown":
            _system_shutdown()

            return _success(
                request_id,
                {
                    "status": "accepted",
                    "action": action,
                },
            )

        # App-store operations own their policy checks. Never trust client
        # claims about approval, lock mode or guardian authentication.
        if action == "appstore.policy":
            return _success(request_id, appstore_ops.get_appstore_policy())
        if action == "appstore.installed":
            return _success(request_id, {"apps": appstore_ops.get_installed_apps()})
        if action == "appstore.updates":
            return _success(request_id, {"updates": appstore_ops.get_updates()})
        if action in {
            "appstore.install", "appstore.uninstall", "appstore.update_flatpak",
            "appstore.update_system_package", "appstore.update_all",
        }:
            authenticated = (
                _guardian_authenticated(params)
                if get_lock_state().mode is LockMode.GUARDIAN_LOCK else False
            )
            kwargs = {"guardian_authenticated": authenticated}
            if action == "appstore.install":
                appstore_ops.install_app(params.get("app_id"), **kwargs)
            elif action == "appstore.uninstall":
                appstore_ops.uninstall_app(params.get("app_id"), **kwargs)
            elif action == "appstore.update_flatpak":
                appstore_ops.update_flatpak(params.get("ref"), **kwargs)
            elif action == "appstore.update_system_package":
                appstore_ops.update_system_package(params.get("name"), **kwargs)
            else:
                appstore_ops.update_all(**kwargs)
            return _success(request_id, {"status": "complete", "action": action})

        # --------------------------------------------------------
        # Packages
        # --------------------------------------------------------

        if action == "package.install":
            package_name = _require_package_name(params)

            installation = _install_package(
                package_name,
                uid,
                guardian_authenticated=(
                    _guardian_authenticated(params)
                    if get_lock_state().mode is LockMode.GUARDIAN_LOCK else False
                ),
            )

            return _success(
                request_id,
                {
                    "status": "complete",
                    "action": action,
                    "package": package_name,
                    **(installation if isinstance(installation, dict) else {}),
                },
            )

        if action == "package.update":
            package_name = _require_package_name(params)
            version = _update_package(
                package_name,
                uid,
                guardian_authenticated=(
                    _guardian_authenticated(params)
                    if get_lock_state().mode is LockMode.GUARDIAN_LOCK else False
                ),
            )
            return _success(request_id, {
                "status": "complete", "action": action,
                "package": package_name, "version": version,
            })

        if action == "package.uninstall":
            package_name = _require_package_name(params)

            _uninstall_package(
                package_name,
                uid,
                guardian_authenticated=(
                    _guardian_authenticated(params)
                    if get_lock_state().mode is LockMode.GUARDIAN_LOCK else False
                ),
            )

            return _success(
                request_id,
                {
                    "status": "complete",
                    "action": action,
                    "package": package_name,
                },
            )

        # --------------------------------------------------------
        # Sentinel lock state
        # --------------------------------------------------------

        if action == "guardian.authenticate":
            if get_lock_state().mode is not LockMode.GUARDIAN_LOCK:
                raise PolicyDeniedError("Guardian Lock is not active. Recheck the lock mode.")
            if not _guardian_authenticated(params):
                raise PolicyDeniedError("Incorrect Guardian PIN.")
            return _success(request_id, {"authenticated": True})

        if action == "guardian.pin_status":
            return _success(request_id, {
                "configured": guardian_pin_configured(),
                "mode": get_lock_state().mode.value,
            })

        if action == "state.get":
            state = get_lock_state()

            return _success(
                request_id,
                state.as_dict(),
            )

        if action == "state.enter_self_lock":
            current = get_lock_state()
            if current.mode is LockMode.SELF_LOCK:
                raise PolicyDeniedError("Self Lock is permanent; mode changes are denied.")
            if current.mode is LockMode.GUARDIAN_LOCK:
                raise PolicyDeniedError("Exit Guardian Lock before enabling Self Lock.")
            state = enter_self_lock()

            guardian_app_ops.reconcile()
            application = browser_ops.apply_browser_policy()
            if guardian_website_ops.configured_enabled():
                app_blocker_ops.reconcile_app_blocker()

            return _success(
                request_id,
                {**state.as_dict(), "browser_application": application},
            )

        if action == "state.enter_guardian_lock":
            current = get_lock_state()
            if current.mode is LockMode.SELF_LOCK:
                raise PolicyDeniedError("Self Lock is permanent; mode changes are denied.")
            configuring = "new_pin" in params or "pin_confirmation" in params
            if current.mode is LockMode.GUARDIAN_LOCK:
                if configuring:
                    raise PolicyDeniedError("A Guardian PIN is already configured. Use Change guardian PIN.")
                if not _guardian_authenticated(params):
                    raise PolicyDeniedError("Guardian authentication failed.")
                # An authenticated repeat request leaves Guardian Lock unchanged.
                state = current
            else:
                setup_guardian_pin(params.get("new_pin"), params.get("pin_confirmation"),
                                   replace_existing=True)
                # Credentials are durable before the lock state. If this write
                # fails, retain the new PIN; a retry asks for a confirmed PIN again.
                state = enter_guardian_lock()

            guardian_app_ops.reconcile()
            application = browser_ops.apply_browser_policy()
            if guardian_website_ops.configured_enabled():
                app_blocker_ops.reconcile_app_blocker()

            return _success(
                request_id,
                {**state.as_dict(), "browser_application": application},
            )

        if action == "state.exit_guardian_lock":
            if get_lock_state().mode is LockMode.SELF_LOCK:
                raise PolicyDeniedError("Self Lock is permanent; mode changes are denied.")
            if not _guardian_authenticated(
                params,
            ):
                raise PolicyDeniedError("Guardian authentication failed.")

            state = exit_guardian_lock()

            guardian_app_ops.reconcile()
            application = browser_ops.apply_browser_policy()
            if guardian_website_ops.configured_enabled():
                app_blocker_ops.reconcile_app_blocker()

            return _success(
                request_id,
                {**state.as_dict(), "browser_application": application},
            )

        if action == "guardian.change_pin":
            current_pin = params.get(
                "guardian_pin",
            )

            new_pin = params.get(
                "new_pin",
            )

            if not isinstance(
                current_pin,
                str,
            ):
                raise PolicyDeniedError("Guardian authentication failed.")

            if not _guardian_authenticated(
                params,
            ):
                raise PolicyDeniedError("Guardian authentication failed.")

            if not isinstance(
                new_pin,
                str,
            ):
                raise ValueError("Missing or invalid new guardian PIN.")

            change_guardian_pin(
                current_pin,
                new_pin,
            )

            return _success(
                request_id,
                {
                    "status": "complete",
                    "action": action,
                },
            )

        # --------------------------------------------------------
        # Sentinel settings
        # --------------------------------------------------------

        if action == "settings.get":
            name = params.get("name")

            if not isinstance(
                name,
                str,
            ):
                raise ValueError("Missing or invalid setting name.")

            value = _get_setting(
                name,
            )

            return _success(
                request_id,
                {
                    "name": name,
                    "value": value,
                },
            )

        if action == "settings.set":
            name = params.get("name")

            if not isinstance(
                name,
                str,
            ):
                raise ValueError("Missing or invalid setting name.")

            if "value" not in params:
                raise ValueError("Missing setting value.")

            value = params["value"]

            guardian_authenticated = False

            state = get_lock_state()

            if state.mode is LockMode.GUARDIAN_LOCK:
                guardian_authenticated = _guardian_authenticated(
                    params,
                )

            change = _set_setting(
                name,
                value,
                guardian_authenticated=guardian_authenticated,
                **({"expected_revision": params["expected_revision"]}
                   if "expected_revision" in params else {}),
            )

            return _success(
                request_id,
                {
                    "status": "complete",
                    "action": action,
                    "name": name,
                    "value": value,
                    "change": change.value,
                    **({"application": image_filter_ops.application_status()}
                       if name == "image_filtering" else {}),
                    **({"application": {"devices": vm_ops.application_status(),
                                         "tools": app_blocker_ops.application_status()}}
                       if name == "vm_protection" else {}),
                    **({"application": dns_ops.application_status()} if name.startswith("dns.") else {}),
                    **({"application": browser_ops.application_status()}
                       if name in ("safe_browsing", "safe_steam", "safe_crunchyroll") else {}),
                    **({"dns_application": dns_ops.application_status()}
                       if name == "safe_browsing" else {}),
                },
            )

        # --------------------------------------------------------
        # Scheduled block windows
        # --------------------------------------------------------

        if action == "blockwindow.policy":
            return _success(request_id, block_window_ops.get_policy_info())

        if action == "blockwindow.list":
            return _success(
                request_id,
                block_window_ops.list_block_windows(
                    offset=params.get("offset", 0),
                    limit=params.get("limit", 20),
                    include_ended=params.get("include_ended", True),
                    include_targets=params.get("include_targets", False),
                    range_start=params.get("range_start"),
                    range_end=params.get("range_end"),
                ),
            )

        if action == "blockwindow.get":
            return _success(
                request_id,
                block_window_ops.get_block_window(params.get("id")),
            )

        if action == "blockwindow.effective":
            return _success(request_id, block_window_ops.get_effective_policy())

        if action == 'screenblock.status':
            return _success(request_id, block_window_ops.get_screen_time_status())
        if action == 'screenlimit.status':
            return _success(request_id, daily_screen_time_ops.status())
        if action == 'screenlimit.set':
            return _success(request_id, daily_screen_time_ops.set_limit(
                params.get('enabled'), params.get('minutes'), params.get('timezone'),
                expected_revision=params.get('expected_revision'),
                guardian_authenticated=_guardian_authenticated(params)))
        if action == 'screenblock.bypass':
            if params.get('id') == daily_screen_time_ops.LIMIT_ID:
                current_caller.get().require_action('screenlimit.set')
            return _success(request_id, block_window_ops.bypass_screen_time(
                params.get('id'), guardian_authenticated=_guardian_authenticated(params)))
        if action == 'screenblock.register':
            from sentinel.daemon.supervision.screen_time import register_overlay
            register_overlay(uid, pid)
            return _success(request_id, {'registered': True})

        if action == "blockwindow.create":
            result = block_window_ops.create_block_window(
                name=params.get("name"),
                start_at=params.get("start_at"),
                end_at=params.get("end_at"),
                timezone_name=params.get("timezone", "UTC"),
                lock_type=params.get("lock_type"),
                apps=params.get("apps", []),
                domains=params.get("domains", []),
                enabled=params.get("enabled", True),
                notes=params.get("notes", ""),
                block_type=params.get('block_type', 'targets'),
                recurrence=params.get('recurrence', 'none'),
                guardian_authenticated=_guardian_authenticated(params),
                actor_uid=uid,
            )
            return _success(request_id, result)

        if action == "blockwindow.update":
            mutable_fields = {
                "name", "notes", "enabled", "lock_type", "start_at",
                "end_at", "timezone", "apps", "domains", "block_type", "recurrence",
            }
            changes = {key: params[key] for key in mutable_fields if key in params}
            result = block_window_ops.update_block_window(
                params.get("id"),
                changes,
                guardian_authenticated=_guardian_authenticated(params),
            )
            return _success(request_id, result)

        if action == "blockwindow.delete":
            result = block_window_ops.delete_block_window(
                params.get("id"),
                guardian_authenticated=_guardian_authenticated(params),
            )
            return _success(request_id, result)

        # --------------------------------------------------------
        # Application blocker
        # --------------------------------------------------------

        if action == "appblock.list":
            return _success(request_id, app_blocker_ops.list_apps())

        if action == "appblock.status":
            return _success(request_id, app_blocker_ops.get_status())

        if action in {"appblock.add", "appblock.remove", "appblock.sync"}:
            authenticated = (
                _guardian_authenticated(params)
                if get_lock_state().mode is LockMode.GUARDIAN_LOCK else False
            )

            if action == "appblock.add":
                result = app_blocker_ops.add_app(
                    params.get("key"),
                    app_type=params.get("type"),
                    name=params.get("name"),
                    executable=params.get("executable"),
                    flatpak_id=params.get("flatpak_id"),
                    snap_instance=params.get("snap_instance"),
                    guardian_authenticated=authenticated,
                )
            elif action == "appblock.remove":
                result = app_blocker_ops.remove_app(
                    params.get("key"),
                    guardian_authenticated=authenticated,
                )
            else:
                result = app_blocker_ops.sync(
                    guardian_authenticated=authenticated,
                )

            return _success(request_id, result)

        if action == "browser.status":
            return _success(request_id, browser_ops.get_status())

        if action in {"browser.extension.approve", "browser.extension.revoke"}:
            return _success(request_id, extension_ops.change_approval(
                params.get("extension_id"), action.rsplit(".", 1)[-1],
                guardian_pin=params.get("guardian_pin"),
            ))

        if action == "browser.sync":
            require_policy(action, ChangeKind.MAINTAIN,
                           guardian_authenticated=_guardian_authenticated(params))
            return _success(request_id, {"application": browser_ops.apply_browser_policy()})

        if action == "dns.status":
            return _success(request_id, dns_ops.get_status())

        if action in {"dns.blocklist.list", "dns.allowlist.list"}:
            list_name = action.split(".")[1]
            return _success(request_id, dns_ops.list_domains(
                list_name, offset=params.get("offset", 0), limit=params.get("limit", 50),
            ))

        if action in {"dns.blocklist.add", "dns.blocklist.remove",
                      "dns.allowlist.add", "dns.allowlist.remove", "dns.regenerate"}:
            authenticated = (
                _guardian_authenticated(params)
                if get_lock_state().mode is LockMode.GUARDIAN_LOCK else False
            )
            if action == "dns.regenerate":
                result = dns_ops.regenerate(
                    guardian_authenticated=authenticated,
                    update_sources=params.get("update_sources", False),
                )
            else:
                _, list_name, operation = action.split(".")
                result = dns_ops.change_domain(
                    list_name, operation, params.get("domain"),
                    guardian_authenticated=authenticated,
                )
            return _success(request_id, result)

        # --------------------------------------------------------
        # Sentinel-managed services
        # --------------------------------------------------------

        if action == "service.add":
            name = _require_service_name(params)

            path_value = params.get("path")

            if not isinstance(
                path_value,
                str,
            ):
                raise ValueError("Missing service path.")

            _add_managed_service(
                name,
                path_value,
                uid,
            )

            return _success(
                request_id,
                {
                    "status": "complete",
                    "action": action,
                    "service": name,
                },
            )

        if action == "service.start":
            name = _require_service_name(params)

            _start_managed_service(name)

            return _success(
                request_id,
                {
                    "status": "complete",
                    "service": name,
                },
            )

        if action == "service.stop":
            name = _require_service_name(params)

            _stop_managed_service(name)

            return _success(
                request_id,
                {
                    "status": "complete",
                    "service": name,
                },
            )

        if action == "service.enable":
            name = _require_service_name(params)

            _enable_managed_service(name)

            return _success(
                request_id,
                {
                    "status": "complete",
                    "service": name,
                },
            )

        if action == "service.disable":
            name = _require_service_name(params)

            _disable_managed_service(name)

            return _success(
                request_id,
                {
                    "status": "complete",
                    "service": name,
                },
            )

        if action == "service.delete":
            name = _require_service_name(params)

            _delete_managed_service(name)

            return _success(
                request_id,
                {
                    "status": "complete",
                    "service": name,
                },
            )

        # --------------------------------------------------------
        # Screen monitor watchdog
        # --------------------------------------------------------
        if action == "monitor.register":
            _register_screen_monitor(
                uid,
                pid,
            )

            return _success(
                request_id,
                {
                    "status": "registered",
                    "pid": pid,
                },
            )

        if action == "monitor.heartbeat":
            _screen_monitor_heartbeat(
                uid,
                pid,
            )

            return _success(
                request_id,
                {
                    "status": "healthy",
                },
            )

        #
        # Sentinel anti-tamper blocker registration.
        #
        if action == "tamper.blocker.register":
            _register_tamper_blocker(
                uid,
                pid,
            )

            return _success(
                request_id,
                {
                    "status": "registered",
                    "pid": pid,
                },
            )

        # Unknown action
        return _error(
            request_id,
            "unknown_action",
            f"Unknown action: {action}",
        )

    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip()[-1000:] if isinstance(exc.stderr, str) else ""
        return _error(
            request_id,
            "command_failed",
            (f"{action} failed with exit code {exc.returncode}."
             + (f" {detail}" if detail else "")),
        )

    except PolicyDeniedError as exc:
        return _error(
            request_id,
            "permission_denied",
            str(exc),
        )

    except SettingsRevisionConflict as exc:
        return _error(request_id, "revision_conflict", str(exc))

    except (
        OSError,
        subprocess.TimeoutExpired,
        ValueError,
        FileNotFoundError,
        NotImplementedError,
    ) as exc:
        return _error(
            request_id,
            "operation_failed",
            str(exc),
        )
