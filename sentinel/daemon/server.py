"""Root daemon entry point: configuration, socket handling and lifecycle."""

from __future__ import annotations

import grp
from sentinel.activity.store import initialize_activity
from sentinel.activity.collectors import activity_loop
from sentinel.activity.screen_time import screen_time_loop
import os
import signal
import socket
import stat
import threading

import requests

from sentinel.daemon.security.peer_auth import _get_peer_credentials, _uid_is_authorized
from sentinel.daemon.core.config import load_protected_uid
from sentinel.daemon.core.instance import instance_lock, InstanceBusyError
from sentinel.daemon.core.constants import CONTROL_GROUP
from sentinel.daemon.rpc.protocol import (
    MAX_MESSAGE_BYTES,
    ProtocolError,
    decode_message,
    encode_message,
)

from sentinel.daemon.core.lock_state import LockMode, initialize_lock_state
from sentinel.daemon.security.guardian_auth import initialize_guardian_auth


from sentinel.daemon.rpc.request_handler import _handle_request, _mutation_lock
from sentinel.daemon.operations import (
    app_blocker_ops,
    guardian_app_ops,
    block_window_ops,
    dns_ops,
    dns_maintenance_ops,
    browser_ops,
    image_filter_ops,
    vm_ops,
)
from sentinel.safe_steam.server import start_server as start_extension_server
from sentinel.daemon.supervision.steam import steam_watchdog_loop
from sentinel.daemon.supervision.app_blocker import app_blocker_watchdog_loop
from sentinel.daemon.rpc.responses import _error
from sentinel.daemon.supervision.screen_monitor import (
    _screen_monitor_watchdog_loop,
    initialize_screen_monitor,
)
from sentinel.daemon.supervision.state import _stop_event
from sentinel.settings.paths import RUN_DIR, SENTINELD_SOCKET


def _read_message(conn: socket.socket) -> bytes:
    """
    Read one newline-terminated request.
    """

    data = bytearray()

    while True:
        chunk = conn.recv(4096)

        if not chunk:
            break

        data.extend(chunk)

        if len(data) > MAX_MESSAGE_BYTES + 1:
            raise ProtocolError("Request is too large.")

        if b"\n" in chunk:
            break

    if not data:
        raise ProtocolError("Empty request.")

    line, _, _ = bytes(data).partition(b"\n")

    return line


def _handle_connection(
    conn: socket.socket,
) -> None:
    """
    Handle one client connection.
    """

    request_id: str | None = None

    try:
        pid, uid, _gid = _get_peer_credentials(conn)

        if not _uid_is_authorized(uid):
            response = _error(
                None,
                "permission_denied",
                ("This user is not authorized to use sentineld."),
            )

            conn.sendall(encode_message(response))

            return

        raw_request = _read_message(conn)

        request = decode_message(raw_request)

        candidate_id = request.get("request_id")

        if isinstance(
            candidate_id,
            str,
        ):
            request_id = candidate_id

        response = _handle_request(
            request,
            uid=uid,
            pid=pid,
        )

    except ProtocolError as exc:
        response = _error(
            request_id,
            "protocol_error",
            str(exc),
        )

    except Exception as exc:
        response = _error(
            request_id,
            "internal_error",
            ("sentineld encountered an internal error."),
        )

        print(
            f"sentineld internal error: {exc}",
            flush=True,
        )

    try:
        conn.sendall(encode_message(response))

    except OSError:
        pass


# Bound accepted connections without allowing an install to block monitor
# heartbeats. Mutation serialization lives in the RPC handler.
_connection_slots = threading.BoundedSemaphore(32)


def _start_connection_worker(conn: socket.socket) -> threading.Thread | None:
    if not _connection_slots.acquire(blocking=False):
        with conn:
            conn.settimeout(1.0)
            try:
                conn.sendall(encode_message(_error(None, "daemon_busy", "sentineld is busy. Try again shortly.")))
            except OSError:
                pass
        return

    def serve():
        try:
            with conn:
                # Incomplete client messages cannot occupy a slot indefinitely.
                conn.settimeout(10.0)
                _handle_connection(conn)
        finally:
            _connection_slots.release()

    thread = threading.Thread(target=serve, name="sentineld-client", daemon=True)
    try:
        thread.start()
        return thread
    except Exception:
        conn.close()
        _connection_slots.release()
        raise


def _prepare_socket() -> socket.socket:
    """
    Create /run/sentinel/sentineld.sock safely.
    """

    RUN_DIR.mkdir(
        mode=0o755,
        parents=True,
        exist_ok=True,
    )

    if SENTINELD_SOCKET.exists() or SENTINELD_SOCKET.is_symlink():
        info = SENTINELD_SOCKET.lstat()

        if not stat.S_ISSOCK(info.st_mode):
            raise RuntimeError(f"{SENTINELD_SOCKET} exists but is not a socket.")

        SENTINELD_SOCKET.unlink()

    server = socket.socket(
        socket.AF_UNIX,
        socket.SOCK_STREAM,
    )

    server.bind(str(SENTINELD_SOCKET))

    try:
        group = grp.getgrnam(CONTROL_GROUP)

    except KeyError as exc:
        server.close()

        SENTINELD_SOCKET.unlink(missing_ok=True)

        raise RuntimeError(f"Required group '{CONTROL_GROUP}' does not exist.") from exc

    os.chown(
        SENTINELD_SOCKET,
        0,
        group.gr_gid,
    )

    os.chmod(
        SENTINELD_SOCKET,
        0o660,
    )

    server.listen(16)

    server.settimeout(1.0)

    return server


def _signal_handler(
    signum: int,
    frame: object,
) -> None:

    _stop_event.set()


def _block_window_reconcile_loop() -> None:
    """Apply scheduled policy exactly when block-window activity changes."""

    from sentinel.daemon.supervision import screen_time
    last_signature = block_window_ops.effective_signature()
    previous_apps = block_window_ops.get_active_apps()

    while not _stop_event.is_set():
        try:
            screen_time.tick()
        except (OSError, ValueError, RuntimeError) as exc:
            print(f'sentineld screen time supervision failed: {exc}', flush=True)
        try:
            signature = block_window_ops.effective_signature()
            if signature != last_signature and _mutation_lock.acquire(blocking=False):
                try:
                    block_window_ops.reconcile_transition(previous_apps)
                    previous_apps = block_window_ops.get_active_apps()
                    last_signature = block_window_ops.effective_signature()
                finally:
                    _mutation_lock.release()
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"sentineld block-window reconciliation failed: {exc}", flush=True)

        _stop_event.wait(0.5)
    screen_time.stop()


def _dns_reconcile_loop() -> None:
    """Retry persisted browser and DNS policy after backend outages."""
    while not _stop_event.is_set():
        if _mutation_lock.acquire(blocking=False):
            try:
                from sentinel.daemon.operations.boot_media_ops import apply_boot_media_policy
                apply_boot_media_policy()
                vm_ops.apply_vm_policy()
                guardian_app_ops.reconcile()
                browser_ops.apply_browser_policy()
                dns_ops.reconcile_dns()
                app_blocker_ops.reconcile_app_blocker()
            except (OSError, ValueError) as exc:
                print(f"sentineld policy reconciliation failed: {exc}", flush=True)
            finally:
                _mutation_lock.release()
        _stop_event.wait(30.0)


def _dns_sources_loop() -> None:
    """Download outside the mutation lock, then apply current durable policy."""
    while not _stop_event.is_set():
        try:
            dns_maintenance_ops.refresh_if_due()
            if _stop_event.is_set():
                break
            if dns_maintenance_ops.needs_apply() and _mutation_lock.acquire(blocking=False):
                try:
                    dns_ops.apply_dns()
                    dns_maintenance_ops.mark_applied()
                finally:
                    _mutation_lock.release()
        except (OSError, ValueError, requests.RequestException) as exc:
            print(f"sentineld DNS source maintenance failed: {exc}", flush=True)
        _stop_event.wait(30.0)


def _image_filter_reconcile_loop() -> None:
    """Retry model startup and enforce the saved setting, including disabled."""
    while not _stop_event.is_set():
        if _mutation_lock.acquire(blocking=False):
            try:
                image_filter_ops.apply_image_filtering()
            except (OSError, ValueError, RuntimeError) as exc:
                print(f"sentineld image-filter reconciliation failed: {exc}", flush=True)
            finally:
                _mutation_lock.release()
        _stop_event.wait(5.0)


def _run_daemon() -> None:
    """
    Run the Sentinel privileged daemon.
    """

    if os.geteuid() != 0:
        raise SystemExit("sentineld must run as root.")

    try:
        protected_uid = load_protected_uid()

        state = initialize_lock_state()
        initialize_guardian_auth(require_configured=state.mode is LockMode.GUARDIAN_LOCK)
        initialize_activity()
        from sentinel.daemon.operations.daily_screen_time_ops import initialize as initialize_daily_limit
        initialize_daily_limit()
        block_window_ops.initialize_block_windows()
        dns_ops.initialize_dns()
        from sentinel.daemon.operations.boot_media_ops import apply_boot_media_policy
        apply_boot_media_policy()
        vm_ops.apply_vm_policy()
        browser_ops.initialize_browser_policy()
        app_blocker_ops.initialize_app_blocker()
        guardian_app_ops.initialize_guardian_apps(protected_uid)

    except (
        ValueError,
        OSError,
    ) as exc:
        raise SystemExit(str(exc)) from exc

    _stop_event.clear()
    from sentinel.daemon.supervision import screen_time
    screen_time.initialize(protected_uid)
    initialize_screen_monitor(protected_uid, enabled=False)
    try:
        image_filter_ops.apply_image_filtering()
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc

    signal.signal(
        signal.SIGTERM,
        _signal_handler,
    )

    signal.signal(
        signal.SIGINT,
        _signal_handler,
    )

    try:
        extension_server, extension_thread = start_extension_server()
    except OSError as exc:
        raise SystemExit(f"Cannot bind Sentinel extension endpoint on 127.0.0.1:8765: {exc}. "
                         "Stop the old standalone extension server first.") from exc
    try:
        server = _prepare_socket()
    except BaseException:
        extension_server.shutdown()
        extension_server.server_close()
        extension_thread.join(timeout=3.0)
        raise

    watchdog_thread = threading.Thread(
        target=_screen_monitor_watchdog_loop,
        name="sentineld-screen-monitor-watchdog",
        daemon=True,
    )

    watchdog_thread.start()

    image_thread = threading.Thread(target=_image_filter_reconcile_loop,
                                    name="sentineld-image-filter-reconcile", daemon=True)
    image_thread.start()

    activity_thread = threading.Thread(target=activity_loop,
                                       name="sentineld-activity", daemon=True)
    activity_thread.start()

    screen_time_thread = threading.Thread(target=screen_time_loop, args=(protected_uid,),
                                         name="sentineld-screen-time", daemon=True)
    screen_time_thread.start()

    dns_thread = threading.Thread(target=_dns_reconcile_loop,
                                  name="sentineld-dns-reconcile", daemon=True)
    dns_thread.start()

    dns_sources_thread = threading.Thread(target=_dns_sources_loop,
                                         name="sentineld-dns-sources", daemon=True)
    dns_sources_thread.start()

    block_window_thread = threading.Thread(
        target=_block_window_reconcile_loop,
        name="sentineld-block-window-reconcile",
        daemon=True,
    )
    block_window_thread.start()

    steam_thread = threading.Thread(target=steam_watchdog_loop, args=(protected_uid,),
                                    name="sentineld-steam-watchdog", daemon=True)
    steam_thread.start()

    app_blocker_thread = threading.Thread(
        target=app_blocker_watchdog_loop,
        name="sentineld-app-blocker-watchdog",
        daemon=True,
    )
    app_blocker_thread.start()

    print(
        (f"sentineld listening on {SENTINELD_SOCKET}"),
        flush=True,
    )

    try:
        while not _stop_event.is_set():
            try:
                conn, _ = server.accept()

            except socket.timeout:
                continue

            _start_connection_worker(conn)

    finally:
        _stop_event.set()

        watchdog_thread.join(timeout=3.0)
        image_thread.join(timeout=3.0)
        activity_thread.join(timeout=3.0)
        screen_time_thread.join(timeout=3.0)
        dns_thread.join(timeout=3.0)
        dns_sources_thread.join(timeout=3.0)
        block_window_thread.join(timeout=3.0)
        steam_thread.join(timeout=5.0)
        app_blocker_thread.join(timeout=3.0)
        extension_server.shutdown()
        extension_server.server_close()
        extension_thread.join(timeout=3.0)

        server.close()

        SENTINELD_SOCKET.unlink(missing_ok=True)

        print(
            "sentineld stopped.",
            flush=True,
        )


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit('sentineld must run as root.')
    try:
        with instance_lock():
            _run_daemon()
    except (InstanceBusyError, OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
