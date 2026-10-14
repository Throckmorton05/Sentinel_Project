import asyncio
import json
import threading
from itertools import count
from dataclasses import asdict, dataclass
from typing import Any

from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method


BUS_NAME = "org.sentinel.KWinBridge"
OBJECT_PATH = "/org/sentinel/KWinBridge"
INTERFACE_NAME = "org.sentinel.KWinBridge"


@dataclass(frozen=True)
class WindowInfo:
    window_id: str
    title: str
    app_id: str
    resource_name: str

    pid: int

    x: int
    y: int
    width: int
    height: int

    focused: bool
    minimized: bool
    on_current_desktop: bool

    normal_window: bool
    dialog: bool


class SentinelKWinInterface(ServiceInterface):
    """
    D-Bus interface used by the Sentinel KWin script.

    KWin sends complete window snapshots through UpdateWindows().
    The latest snapshot is stored in a thread-safe registry.

    KWin also waits for Sentinel window-control commands through
    GetNextCommand().
    """

    def __init__(self) -> None:
        super().__init__(INTERFACE_NAME)

        self._windows: dict[str, WindowInfo] = {}
        self._lock = threading.RLock()

        # Lets the monitor know that KWin has sent at least one snapshot.
        self._has_update = threading.Event()

        # Used only from the asyncio/D-Bus thread.

        # Commands waiting for KWin to execute.
        self._commands: asyncio.Queue[dict[str, str]] = asyncio.Queue()

        # Give every command a unique ID.
        self._command_ids = count(1)

        # Results reported back by KWin.
        #
        # Example:
        # {
        #     "42": {
        #         "success": True,
        #         "message": "window minimized",
        #     }
        # }
        self._command_results: dict[str, dict[str, Any]] = {}

    @method()
    def UpdateWindows(self, payload: "s"):  # type: ignore
        payload_data = json.loads(payload)

        schema_version = payload_data.get("schema_version")

        if schema_version != 1:
            raise RuntimeError(
                f"Unsupported Sentinel KWin bridge schema: {schema_version}"
            )

        raw_windows: list[dict[str, Any]] = payload_data["windows"]

        windows: list[WindowInfo] = []

        for item in raw_windows:
            try:
                window = WindowInfo(
                    window_id=str(item["window_id"]),
                    title=str(item.get("title", "")),
                    app_id=str(item.get("app_id", "")),
                    resource_name=str(item.get("resource_name", "")),
                    pid=int(item.get("pid", -1)),
                    x=int(item.get("x", 0)),
                    y=int(item.get("y", 0)),
                    width=int(item.get("width", 0)),
                    height=int(item.get("height", 0)),
                    focused=bool(item.get("focused", False)),
                    minimized=bool(item.get("minimized", False)),
                    on_current_desktop=bool(item.get("on_current_desktop", True)),
                    normal_window=bool(item.get("normal_window", False)),
                    dialog=bool(item.get("dialog", False)),
                )

            except (TypeError, ValueError, KeyError) as exc:
                print(f"Skipping malformed KWin window: {exc}")
                continue

            windows.append(window)

        new_registry = {window.window_id: window for window in windows}

        with self._lock:
            self._windows = new_registry

        self._has_update.set()

    @method()
    def GetNextCommand(self) -> "s":  # type: ignore
        """
        Return the next queued KWin command immediately.

        An empty string means there is currently no command waiting.
        KWin polls this method on a short timer.
        """

        try:
            command = self._commands.get_nowait()

        except asyncio.QueueEmpty:
            return ""

        return json.dumps(command)

    def queue_command(
        self,
        action: str,
        window_id: str,
    ) -> str:
        """
        Add a command to the asyncio queue.

        Returns the unique command ID assigned to the command.
        """

        command_id = str(next(self._command_ids))

        self._commands.put_nowait(
            {
                "command_id": command_id,
                "action": action,
                "window_id": str(window_id),
            }
        )

        print(
            f"Sentinel KWin command queued: "
            f"id={command_id} action={action} window={window_id}"
        )

        return command_id

    @method()
    def QueueWindowCommand(
        self,
        action: "s",
        window_id: "s",
    ) -> "s":  # type: ignore
        """
        Queue a KWin window action.

        Returns the command ID.

        An empty string means the requested action was invalid.
        """

        if action not in {
            "minimize",
            "restore",
            "release",
            "close",
        }:
            return ""

        command_id = self.queue_command(
            action,
            window_id,
        )

        return command_id

    @method()
    def ReportCommandResult(
        self,
        command_id: "s",
        success: "b",
        message: "s",
    ):  # type: ignore
        """
        Called by the KWin script after it executes a command.
        """

        result = {
            "success": bool(success),
            "message": str(message),
        }

        with self._lock:
            self._command_results[str(command_id)] = result

        print(
            f"Sentinel KWin command completed: "
            f"id={command_id} success={success} message={message}"
        )

    @method()
    def GetCommandResult(
        self,
        command_id: "s",
    ) -> "s":  # type: ignore
        """
        Return the result for a completed KWin command.

        An empty string means KWin has not reported a result yet.
        """

        with self._lock:
            result = self._command_results.get(str(command_id))

        if result is None:
            return ""

        return json.dumps(result)

    @method()
    def GetWindowSnapshot(
        self,
        window_id: "s",
    ) -> "s":  # type: ignore
        """
        Return the latest KWin snapshot for one window.

        An empty string means the window no longer exists.
        """

        window = self.get_window(window_id)

        if window is None:
            return ""

        return json.dumps(asdict(window))

    def get_windows(self) -> list[WindowInfo]:
        with self._lock:
            return list(self._windows.values())

    def get_window(
        self,
        window_id: str,
    ) -> WindowInfo | None:
        with self._lock:
            return self._windows.get(window_id)

    def get_focused_window(self) -> WindowInfo | None:
        with self._lock:
            for window in self._windows.values():
                if window.focused:
                    return window

        return None

    def get_visible_windows(self) -> list[WindowInfo]:
        with self._lock:
            return [window for window in self._windows.values() if not window.minimized]

    def wait_for_update(
        self,
        timeout: float | None = None,
    ) -> bool:
        return self._has_update.wait(timeout)


class KWinBridge:
    """
    Embeddable KWin/D-Bus bridge.

    The D-Bus listener runs in a background thread so Sentinel's
    normal synchronous monitoring loop does not need to become async.
    """

    def __init__(self) -> None:
        self.interface = SentinelKWinInterface()

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None

        self._started = threading.Event()
        self._startup_error: BaseException | None = None

    def start(
        self,
        timeout: float = 5.0,
    ) -> None:
        if self._thread is not None and self._thread.is_alive():
            return

        self._started.clear()
        self._startup_error = None

        self._thread = threading.Thread(
            target=self._thread_main,
            name="sentinel-kwin-bridge",
            daemon=True,
        )

        self._thread.start()

        if not self._started.wait(timeout):
            raise RuntimeError("Timed out starting Sentinel KWin bridge")

        if self._startup_error is not None:
            raise RuntimeError(
                "Failed to start Sentinel KWin bridge"
            ) from self._startup_error

    def stop(self) -> None:
        if self._loop is None or self._stop_event is None:
            return

        self._loop.call_soon_threadsafe(self._stop_event.set)

        if self._thread is not None:
            self._thread.join(timeout=5.0)

        self._thread = None
        self._loop = None
        self._stop_event = None

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())

        except BaseException as exc:
            self._startup_error = exc
            self._started.set()

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()

        bus = await MessageBus().connect()

        bus.export(
            OBJECT_PATH,
            self.interface,
        )

        await bus.request_name(BUS_NAME)

        self._started.set()

        await self._stop_event.wait()

        bus.disconnect()

    def _queue_command(
        self,
        action: str,
        window_id: str,
    ) -> None:
        """
        Safely transfer a command from Sentinel's normal thread
        into the bridge's asyncio thread.
        """

        if self._loop is None:
            raise RuntimeError("Sentinel KWin bridge is not running")

        self._loop.call_soon_threadsafe(
            self.interface.queue_command,
            action,
            str(window_id),
        )

    def minimize_window(
        self,
        window_id: str,
    ) -> None:
        self._queue_command(
            "minimize",
            window_id,
        )

    def restore_window(
        self,
        window_id: str,
    ) -> None:
        self._queue_command(
            "restore",
            window_id,
        )

    def release_window(
        self,
        window_id: str,
    ) -> None:
        self._queue_command(
            "release",
            window_id,
        )

    def close_window(
        self,
        window_id: str,
    ) -> None:
        self._queue_command(
            "close",
            window_id,
        )

    def get_windows(self) -> list[WindowInfo]:
        return self.interface.get_windows()

    def get_window(
        self,
        window_id: str,
    ) -> WindowInfo | None:
        return self.interface.get_window(window_id)

    def get_focused_window(self) -> WindowInfo | None:
        return self.interface.get_focused_window()

    def get_visible_windows(self) -> list[WindowInfo]:
        return self.interface.get_visible_windows()

    def wait_for_update(
        self,
        timeout: float | None = None,
    ) -> bool:
        return self.interface.wait_for_update(timeout)


def main() -> None:
    """
    Standalone development/test mode.

    Production Sentinel will start KWinBridge from the screen monitor.
    """

    bridge = KWinBridge()

    bridge.start()

    print("Sentinel KWin D-Bus bridge started")

    try:
        while True:
            threading.Event().wait(3600)

    except KeyboardInterrupt:
        pass

    finally:
        bridge.stop()


if __name__ == "__main__":
    main()
