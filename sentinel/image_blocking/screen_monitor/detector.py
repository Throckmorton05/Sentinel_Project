# sentinel/image_blocking/screen_monitor/detector.py

from sentinel.wayland.kwin_bridge import KWinBridge, WindowInfo


MIN_WINDOW_WIDTH = 200
MIN_WINDOW_HEIGHT = 200


class WindowDetector:
    def __init__(self, bridge: KWinBridge) -> None:
        self.bridge = bridge

    def get_focused_window(self) -> WindowInfo | None:
        """
        Return the currently focused window.

        The focused window is returned even if it is smaller than the
        normal minimum scan size. This preserves the behavior of the
        old X11 detector.
        """

        window = self.bridge.get_focused_window()

        if window is None:
            return None

        if window.minimized:
            return None

        if not window.on_current_desktop:
            return None

        return window

    def get_visible_windows(self) -> list[WindowInfo]:
        """
        Return normal visible windows that Sentinel should consider
        for screen monitoring.
        """

        windows: list[WindowInfo] = []

        for window in self.bridge.get_windows():
            # Ignore minimized windows.
            if window.minimized:
                continue

            # Ignore windows belonging to another virtual desktop.
            if not window.on_current_desktop:
                continue

            # Ignore tiny utility/transient windows.
            if window.width < MIN_WINDOW_WIDTH or window.height < MIN_WINDOW_HEIGHT:
                continue

            windows.append(window)

        return windows

    def get_window_by_id(
        self,
        window_id: str,
    ) -> WindowInfo | None:
        """
        Look up a KWin window by its internal UUID.
        """

        window = self.bridge.get_window(window_id)

        if window is None:
            return None

        if window.minimized:
            return None

        if not window.on_current_desktop:
            return None

        return window


def main() -> None:
    bridge = KWinBridge()

    try:
        bridge.start()

        print("Waiting for KWin window information...")

        if not bridge.wait_for_update(timeout=5.0):
            print(
                "No KWin window update received. "
                "Trigger a window change or verify the KWin script "
                "is running."
            )
            return

        detector = WindowDetector(bridge)

        focused = detector.get_focused_window()

        print("\nFocused Window")
        print("-" * 50)

        if focused:
            print(f"Title: {focused.title}")
            print(f"App: {focused.app_id}")
            print(f"Resource: {focused.resource_name}")
            print(f"PID: {focused.pid}")
            print(f"ID: {focused.window_id}")
            print(f"Position: ({focused.x}, {focused.y})")
            print(f"Size: {focused.width}x{focused.height}")
        else:
            print("No focused window.")

        print("\nVisible Windows")
        print("-" * 50)

        for window in detector.get_visible_windows():
            print(
                f"{window.title} | "
                f"{window.app_id} | "
                f"{window.width}x{window.height} | "
                f"({window.x}, {window.y}) | "
                f"focused={window.focused}"
            )

    finally:
        bridge.stop()


if __name__ == "__main__":
    main()
