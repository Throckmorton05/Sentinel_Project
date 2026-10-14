from __future__ import annotations

import time

import asyncio
import json
import sys
from enum import Enum, auto
from typing import Any

from dbus_next.aio import MessageBus
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from sentinel.image_blocking.screen_blocker.blocked_window import (
    BlockedWindow,
)
from sentinel.image_blocking.screen_blocker.overlay import (
    BlockOverlayManager,
)
from sentinel.image_blocking.screen_monitor.capture import (
    WindowCapture,
)
from sentinel.image_blocking.screen_monitor.inference import (
    analyze,
)
from sentinel.wayland.kwin_bridge import (
    BUS_NAME,
    INTERFACE_NAME,
    OBJECT_PATH,
    WindowInfo,
)

COMMAND_CONFIRM_INTERVAL_MS = 100
COMMAND_CONFIRM_TIMEOUT_SECONDS = 3.0

CLOSE_CONFIRM_INTERVAL_MS = 100
CLOSE_CONFIRM_TIMEOUT_SECONDS = 3.0

RECHECK_DELAY_MS = 1_000


class BlockerState(Enum):
    BLOCKED = auto()
    RECHECKING = auto()
    CLOSING = auto()
    EXITING = auto()


async def call_bridge_method(
    method_name: str,
    *args: str,
) -> Any:
    """
    Call a method on the already-running Sentinel KWin bridge.

    The screen monitor owns org.sentinel.KWinBridge. The blocker is
    only a D-Bus client and must never try to own the bridge itself.
    """

    bus = await MessageBus().connect()

    try:
        introspection = await bus.introspect(
            BUS_NAME,
            OBJECT_PATH,
        )

        proxy = bus.get_proxy_object(
            BUS_NAME,
            OBJECT_PATH,
            introspection,
        )

        interface: Any = proxy.get_interface(INTERFACE_NAME)

        if method_name == "queue_window_command":
            return await interface.call_queue_window_command(*args)

        if method_name == "get_command_result":
            return await interface.call_get_command_result(*args)

        if method_name == "get_window_snapshot":
            return await interface.call_get_window_snapshot(*args)

        raise ValueError(f"Unknown bridge method: {method_name}")

    finally:
        bus.disconnect()


def queue_window_command(
    action: str,
    window_id: str,
) -> str | None:
    """
    Ask the monitor-owned KWin bridge to perform a window action.

    Returns the command ID when the command was successfully queued.
    """

    try:
        command_id = asyncio.run(
            call_bridge_method(
                "queue_window_command",
                action,
                window_id,
            )
        )

    except Exception as exc:
        print(f"Failed to queue KWin command {action!r}: {exc}")
        return None

    if not command_id:
        return None

    command_id = str(command_id)

    print(f"KWin command queued: id={command_id} action={action} window={window_id}")

    return command_id


def get_command_result(
    command_id: str,
) -> dict[str, Any] | None:
    """
    Return KWin's result for a command.

    None means KWin has not reported a result yet.
    """

    try:
        payload = asyncio.run(
            call_bridge_method(
                "get_command_result",
                command_id,
            )
        )

    except Exception as exc:
        print(f"Failed to get KWin command result {command_id}: {exc}")
        return None

    if not payload:
        return None

    try:
        result = json.loads(payload)

    except (
        json.JSONDecodeError,
        TypeError,
    ) as exc:
        print(f"Invalid KWin command result {command_id}: {exc}")
        return None

    return result


def get_window_snapshot(
    window_id: str,
) -> WindowInfo | None:
    """
    Retrieve the latest KWin snapshot for one window.

    Returns None when the window no longer exists or the bridge
    cannot be reached.
    """

    try:
        payload = asyncio.run(
            call_bridge_method(
                "get_window_snapshot",
                window_id,
            )
        )

    except Exception as exc:
        print(f"Failed to get KWin window snapshot: {exc}")
        return None

    if not payload:
        return None

    try:
        data = json.loads(payload)

        return WindowInfo(
            window_id=str(data["window_id"]),
            title=str(data.get("title", "")),
            app_id=str(data.get("app_id", "")),
            resource_name=str(data.get("resource_name", "")),
            pid=int(data.get("pid", -1)),
            x=int(data.get("x", 0)),
            y=int(data.get("y", 0)),
            width=int(data.get("width", 0)),
            height=int(data.get("height", 0)),
            focused=bool(data.get("focused", False)),
            minimized=bool(data.get("minimized", False)),
            on_current_desktop=bool(
                data.get(
                    "on_current_desktop",
                    True,
                )
            ),
            normal_window=bool(
                data.get(
                    "normal_window",
                    False,
                )
            ),
            dialog=bool(
                data.get(
                    "dialog",
                    False,
                )
            ),
        )

    except (
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"Invalid KWin window snapshot: {exc}")
        return None


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: blocker.py <kwin_window_uuid>")
        sys.exit(1)

    # Wayland/KWin window IDs are UUID strings.
    window_id = str(sys.argv[1])

    app = QApplication([sys.argv[0]])

    capture = WindowCapture()

    overlay_manager = BlockOverlayManager()

    state = BlockerState.BLOCKED

    blocked_window: BlockedWindow | None = None

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def set_state(
        new_state: BlockerState,
    ) -> None:
        nonlocal state

        print(f"State: {state.name} -> {new_state.name}")

        state = new_state

    def wait_for_command_result(
        command_id: str,
        *,
        on_success,
        on_failure,
    ) -> None:
        """
        Wait asynchronously for KWin to acknowledge a command.
        """

        deadline = time.monotonic() + COMMAND_CONFIRM_TIMEOUT_SECONDS

        def poll() -> None:
            result = get_command_result(command_id)

            if result is not None:
                success = bool(
                    result.get(
                        "success",
                        False,
                    )
                )

                message = str(
                    result.get(
                        "message",
                        "",
                    )
                )

                print(
                    f"KWin command result: "
                    f"id={command_id} "
                    f"success={success} "
                    f"message={message}"
                )

                if success:
                    on_success(result)
                else:
                    on_failure(result)

                return

            if time.monotonic() >= deadline:
                print(f"Timed out waiting for KWin command {command_id}.")

                on_failure(
                    {
                        "success": False,
                        "message": "command acknowledgement timed out",
                    }
                )

                return

            QTimer.singleShot(
                COMMAND_CONFIRM_INTERVAL_MS,
                poll,
            )

        QTimer.singleShot(
            COMMAND_CONFIRM_INTERVAL_MS,
            poll,
        )

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------

    def exit_blocker() -> None:
        nonlocal blocked_window

        set_state(BlockerState.EXITING)

        if blocked_window is not None:
            blocked_window.close_blocked_screen()
            blocked_window.deleteLater()
            blocked_window = None

        overlay_manager.close()

        app.quit()

    # ------------------------------------------------------------------
    # BLOCKED state
    # ------------------------------------------------------------------

    def enter_blocked_state(
        *,
        initial: bool = False,
    ) -> None:
        nonlocal blocked_window

        set_state(BlockerState.BLOCKED)

        print("-" * 80)

        print(f"Blocking KWin window: {window_id}")

        command_id = queue_window_command(
            "minimize",
            window_id,
        )

        if command_id is None:
            print("Failed to queue window minimization.")

            if initial:
                print(
                    "Initial minimization could not be queued. "
                    "Keeping Sentinel blocker active."
                )

        if blocked_window is None:
            print("Showing Sentinel blocked screen...")

            blocked_window = BlockedWindow()

            blocked_window.recheck_requested.connect(request_recheck)

            blocked_window.close_requested.connect(request_close)

            blocked_window.showMaximized()

            print("Blocked screen shown.")

        else:
            blocked_window.reset_countdown()

            blocked_window.showMaximized()
            blocked_window.raise_()
            blocked_window.activateWindow()

        QApplication.processEvents()

        if command_id is not None:

            def initial_minimize_succeeded(
                result: dict[str, Any],
            ) -> None:
                print("KWin confirmed blocked target minimized.")

            def initial_minimize_failed(
                result: dict[str, Any],
            ) -> None:
                print(
                    "KWin failed to minimize blocked target: "
                    f"{result.get('message', '')}"
                )

            wait_for_command_result(
                command_id,
                on_success=initial_minimize_succeeded,
                on_failure=initial_minimize_failed,
            )

        print("Window is blocked. Waiting for recheck.")

    # ------------------------------------------------------------------
    # Close original window
    # ------------------------------------------------------------------

    def request_close() -> None:
        if state != BlockerState.BLOCKED:
            return

        print("=" * 80)
        print(f"Close requested for KWin window: {window_id}")

        set_state(BlockerState.CLOSING)

        command_id = queue_window_command(
            "close",
            window_id,
        )

        if command_id is None:
            print("Failed to queue close command.")

            set_state(BlockerState.BLOCKED)

            if blocked_window is not None:
                blocked_window.reset_countdown()

            return

        print(f"Close command {command_id} queued. Waiting for KWin acknowledgement...")

        def close_command_failed(
            result: dict[str, Any],
        ) -> None:
            print(f"KWin rejected the close command: {result.get('message', '')}")

            set_state(BlockerState.BLOCKED)

            if blocked_window is not None:
                blocked_window.reset_countdown()

        def close_command_succeeded(
            result: dict[str, Any],
        ) -> None:
            print(f"KWin accepted the close command: {result.get('message', '')}")

            close_deadline = time.monotonic() + CLOSE_CONFIRM_TIMEOUT_SECONDS

            def confirm_closed() -> None:
                window = get_window_snapshot(window_id)

                if window is None:
                    print(f"KWin window {window_id} has closed.")

                    exit_blocker()
                    return

                if time.monotonic() >= close_deadline:
                    print(
                        "KWin executed the close request, "
                        "but the target window still exists."
                    )

                    set_state(BlockerState.BLOCKED)

                    if blocked_window is not None:
                        blocked_window.reset_countdown()

                    return

                QTimer.singleShot(
                    CLOSE_CONFIRM_INTERVAL_MS,
                    confirm_closed,
                )

            QTimer.singleShot(
                CLOSE_CONFIRM_INTERVAL_MS,
                confirm_closed,
            )

        wait_for_command_result(
            command_id,
            on_success=close_command_succeeded,
            on_failure=close_command_failed,
        )

    # ------------------------------------------------------------------
    # Request recheck
    # ------------------------------------------------------------------

    def request_recheck() -> None:
        if state != BlockerState.BLOCKED:
            print(f"Ignoring recheck request because state is {state.name}.")
            return

        print("=" * 80)
        print("Recheck requested.")

        set_state(BlockerState.RECHECKING)

        begin_recheck()

    # ------------------------------------------------------------------
    # Begin RECHECKING state
    # ------------------------------------------------------------------

    def begin_recheck() -> None:
        """
        Cover every monitor before restoring the original window.
        """

        print("Showing privacy overlays...")

        try:
            overlay_manager.show()

        except Exception as exc:
            print(f"Failed to show privacy overlays: {exc}")

            set_state(BlockerState.BLOCKED)

            if blocked_window is not None:
                blocked_window.reset_countdown()
                blocked_window.showMaximized()

            return

        # The overlays now cover every display. The normal Sentinel
        # blocked window is no longer needed during the capture.
        if blocked_window is not None:
            blocked_window.hide()

        QApplication.processEvents()

        print("Restoring original window under privacy overlays...")

        command_id = queue_window_command(
            "restore",
            window_id,
        )

        if command_id is None:
            print("Failed to queue restore command.")

            return_to_blocked_state()
            return

        def restore_failed(
            result: dict[str, Any],
        ) -> None:
            print(
                f"KWin failed to restore original window: {result.get('message', '')}"
            )

            return_to_blocked_state()

        def restore_succeeded(
            result: dict[str, Any],
        ) -> None:
            print("KWin confirmed original window restored.")

            # Make sure overlays remain above the restored target.
            overlay_manager.raise_all()

            print("Waiting for KWin to redraw the original window...")

            QTimer.singleShot(
                RECHECK_DELAY_MS,
                perform_recheck,
            )

        wait_for_command_result(
            command_id,
            on_success=restore_succeeded,
            on_failure=restore_failed,
        )

    # ------------------------------------------------------------------
    # Return to BLOCKED
    # ------------------------------------------------------------------

    def return_to_blocked_state() -> None:
        """
        Re-hide the target before removing the privacy overlays.

        The overlays stay visible until Sentinel has requested
        minimization and restored the normal blocked screen.
        """

        print("Returning to blocked state...")

        command_id = queue_window_command(
            "minimize",
            window_id,
        )

        if command_id is None:
            print("Failed to queue minimization while returning to blocked state.")

            # Fail closed.
            # Leave privacy overlays active.
            return

        def minimize_failed(
            result: dict[str, Any],
        ) -> None:
            print(
                f"KWin failed to minimize blocked window: {result.get('message', '')}"
            )

            # Fail closed.
            # Keep privacy overlays visible.

        def minimize_succeeded(
            result: dict[str, Any],
        ) -> None:
            print("KWin confirmed blocked window minimized.")

            finish_return_to_blocked_state()

        wait_for_command_result(
            command_id,
            on_success=minimize_succeeded,
            on_failure=minimize_failed,
        )

    def finish_return_to_blocked_state() -> None:
        """
        Restore Sentinel's normal blocked page, then remove overlays.
        """

        set_state(BlockerState.BLOCKED)

        if blocked_window is not None:
            blocked_window.reset_countdown()

            blocked_window.showMaximized()
            blocked_window.raise_()
            blocked_window.activateWindow()

        # Make sure the blocked page has been submitted to KWin before
        # revealing the desktop again.
        QApplication.processEvents()

        overlay_manager.close()

        print("Window is blocked again. Waiting for recheck.")

    def release_safe_window() -> None:
        """
        Release a quarantined window after a successful recheck.

        Privacy overlays remain active until KWin confirms that the
        window's original taskbar/switcher/pager state has been restored.
        """

        print("Recheck passed. Releasing quarantined window...")

        command_id = queue_window_command(
            "release",
            window_id,
        )

        if command_id is None:
            print("Failed to queue release command. Returning to blocked state.")

            return_to_blocked_state()
            return

        def release_failed(
            result: dict[str, Any],
        ) -> None:
            print(f"KWin failed to release safe window: {result.get('message', '')}")

            # Fail closed. The privacy overlays are still active,
            # so quarantine the target again rather than expose it.
            return_to_blocked_state()

        def release_succeeded(
            result: dict[str, Any],
        ) -> None:
            print("KWin confirmed safe window released.")

            # The privacy overlays are still covering the displays.
            # exit_blocker() removes them only after KWin has confirmed
            # that the target has been fully released.
            exit_blocker()

        wait_for_command_result(
            command_id,
            on_success=release_succeeded,
            on_failure=release_failed,
        )

    # ------------------------------------------------------------------
    # Perform capture + inference
    # ------------------------------------------------------------------

    def perform_recheck() -> None:
        if state != BlockerState.RECHECKING:
            return

        print("=" * 80)
        print("Performing recheck...")

        window = get_window_snapshot(window_id)

        if window is None:
            print("Original window no longer exists.")

            exit_blocker()
            return

        print("Recapturing original window...")

        try:
            image = capture.capture_window(window)

        except Exception as exc:
            print(f"Window capture failed: {exc}")

            return_to_blocked_state()
            return

        if image is None:
            print("Window capture returned no image.")

            return_to_blocked_state()
            return

        print("Running inference...")

        try:
            result = analyze(image)

        except Exception as exc:
            print(f"Inference failed: {exc}")

            return_to_blocked_state()
            return

        print(result)

        is_blocked = bool(
            result.get(
                "blocked",
                False,
            )
        )

        if not is_blocked:
            print("Window is now safe.")

            release_safe_window()
            return

        print("Content is still blocked.")

        return_to_blocked_state()

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    initial_window = get_window_snapshot(window_id)

    if initial_window is None:
        print(
            "Target KWin window does not exist or the Sentinel bridge is unavailable."
        )
        sys.exit(1)

    enter_blocked_state(initial=True)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
