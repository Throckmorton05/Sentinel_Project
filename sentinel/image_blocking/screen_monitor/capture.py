# sentinel/image_blocking/screen_monitor/capture.py

from __future__ import annotations

import os
import traceback

from PIL import Image
from PyQt6.QtDBus import (
    QDBus,
    QDBusConnection,
    QDBusMessage,
    QDBusUnixFileDescriptor,
    QDBusVariant,
)

from typing import Any
from PyQt6.QtGui import QImage

from sentinel.image_blocking.screen_monitor.detector import WindowDetector
from sentinel.wayland.kwin_bridge import WindowInfo


MIN_WINDOW_WIDTH = 300
MIN_WINDOW_HEIGHT = 200

SCREENSHOT_SERVICE = "org.kde.KWin.ScreenShot2"
SCREENSHOT_PATH = "/org/kde/KWin/ScreenShot2"
SCREENSHOT_INTERFACE = "org.kde.KWin.ScreenShot2"


IGNORED_CLASSES = {
    "plasmashell",
    "ksmserver",
}

IGNORED_TITLES = {
    "Sentinel",
    "Sentinel Block Overlay",
}


class WindowCapture:
    def __init__(
        self,
        detector: WindowDetector | None = None,
    ) -> None:

        self.detector = detector
        self.bus = QDBusConnection.sessionBus()

        if not self.bus.isConnected():
            raise RuntimeError("Could not connect to the user D-Bus session.")

        if not QDBusUnixFileDescriptor.isSupported():
            raise RuntimeError("D-Bus Unix file descriptor passing is not supported.")

    def is_captureable(
        self,
        window: WindowInfo,
    ) -> bool:
        """
        Return True if this window should be scanned by Sentinel.
        """

        if window.minimized:
            return False

        if not window.on_current_desktop:
            return False

        if window.title in IGNORED_TITLES:
            return False

        if window.width < MIN_WINDOW_WIDTH or window.height < MIN_WINDOW_HEIGHT:
            return False

        app_id = window.app_id.lower()
        resource_name = window.resource_name.lower()

        if app_id in IGNORED_CLASSES or resource_name in IGNORED_CLASSES:
            return False

        # The KWin script should already filter most transient and
        # compositor-owned surfaces. Keep normal app windows and dialogs.
        if not window.normal_window and not window.dialog:
            return False

        return True

    def get_captureable_windows(
        self,
    ) -> list[WindowInfo]:
        if self.detector is None:
            raise RuntimeError(
                "WindowCapture requires a detector for window enumeration."
            )

        return [
            window
            for window in self.detector.get_visible_windows()
            if self.is_captureable(window)
        ]

    def capture_window(
        self,
        window: WindowInfo,
    ) -> Image.Image | None:
        """
        Capture one KWin-managed Wayland window.

        KWin returns metadata describing a raw QImage and writes
        the corresponding pixel buffer to a Unix pipe.
        """

        if not self.is_captureable(window):
            return None

        read_fd = -1
        write_fd = -1

        try:
            read_fd, write_fd = os.pipe()

            dbus_fd = QDBusUnixFileDescriptor(write_fd)

            options = {
                "include-decoration": False,
                "include-shadow": False,
                "native-resolution": True,
            }

            message = QDBusMessage.createMethodCall(
                SCREENSHOT_SERVICE,
                SCREENSHOT_PATH,
                SCREENSHOT_INTERFACE,
                "CaptureWindow",
            )

            message.setArguments(
                [
                    window.window_id,
                    options,
                    dbus_fd,
                ]
            )

            reply = self.bus.call(
                message,
                QDBus.CallMode.Block,
                4000,
            )

            # QDBusUnixFileDescriptor duplicates the descriptor,
            # so Sentinel still owns this original FD.
            os.close(write_fd)
            write_fd = -1

            if reply.type() == QDBusMessage.MessageType.ErrorMessage:
                raise RuntimeError(f"{reply.errorName()}: {reply.errorMessage()}")

            arguments = reply.arguments()

            if not arguments:
                raise RuntimeError("KWin returned no screenshot metadata.")

            metadata = self._normalize_metadata(arguments[0])

            image_type = str(metadata.get("type", ""))

            if image_type != "raw":
                raise RuntimeError(f"Unsupported KWin screenshot type: {image_type!r}")

            width = int(metadata.get("width", 0))

            height = int(metadata.get("height", 0))

            format_value = int(metadata.get("format", 0))

            if width <= 0 or height <= 0:
                raise RuntimeError(
                    f"KWin returned invalid screenshot size: {width}x{height}"
                )

            if format_value <= 0:
                raise RuntimeError(
                    f"KWin returned invalid QImage format: {format_value}"
                )

            return self._read_image(
                read_fd=read_fd,
                width=width,
                height=height,
                format_value=format_value,
            )

        except Exception:
            print(f"\nFailed to capture '{window.title}' ({window.window_id})")

            traceback.print_exc()

            return None

        finally:
            if read_fd >= 0:
                try:
                    os.close(read_fd)
                except OSError:
                    pass

            if write_fd >= 0:
                try:
                    os.close(write_fd)
                except OSError:
                    pass

    def capture_windows(
        self,
        windows: list[WindowInfo],
    ) -> list[tuple[WindowInfo, Image.Image]]:
        captured: list[tuple[WindowInfo, Image.Image]] = []

        for window in windows:
            image = self.capture_window(window)

            if image is None:
                continue

            captured.append(
                (
                    window,
                    image,
                )
            )

        return captured

    def _read_image(
        self,
        *,
        read_fd: int,
        width: int,
        height: int,
        format_value: int,
    ) -> Image.Image:
        """
        Read exactly one raw QImage from KWin's screenshot pipe.
        """

        try:
            qimage_format = QImage.Format(format_value)
        except ValueError as exc:
            raise RuntimeError(
                f"Unknown QImage format returned by KWin: {format_value}"
            ) from exc

        qimage = QImage(
            width,
            height,
            qimage_format,
        )

        if qimage.isNull():
            raise RuntimeError("Could not allocate QImage for screenshot.")

        expected_bytes = qimage.sizeInBytes()

        raw = self._read_exact(
            read_fd,
            expected_bytes,
        )

        if len(raw) != expected_bytes:
            raise RuntimeError(
                "Incomplete KWin screenshot: "
                f"expected {expected_bytes} bytes, "
                f"received {len(raw)}."
            )

        bits = qimage.bits()
        bits.setsize(expected_bytes)
        bits[:] = raw

        rgba = qimage.convertToFormat(QImage.Format.Format_RGBA8888)

        rgba_bytes = rgba.bits().asstring(rgba.sizeInBytes())

        image = Image.frombytes(
            "RGBA",
            (
                rgba.width(),
                rgba.height(),
            ),
            rgba_bytes,
            "raw",
            "RGBA",
            rgba.bytesPerLine(),
            1,
        )

        return image.convert("RGB")

    @staticmethod
    def _read_exact(
        fd: int,
        size: int,
    ) -> bytes:
        """
        Read exactly `size` bytes from a file descriptor,
        unless EOF occurs first.
        """

        chunks: list[bytes] = []
        remaining = size

        while remaining > 0:
            chunk = os.read(
                fd,
                min(
                    remaining,
                    1024 * 1024,
                ),
            )

            if not chunk:
                break

            chunks.append(chunk)
            remaining -= len(chunk)

        return b"".join(chunks)

    @staticmethod
    def _normalize_metadata(
        value: Any,
    ) -> dict[str, Any]:
        """
        Convert Qt D-Bus QVariant wrappers into normal Python values.
        """

        if isinstance(
            value,
            QDBusVariant,
        ):
            value = value.variant()

        if not isinstance(value, dict):
            raise RuntimeError(
                f"KWin returned unexpected screenshot metadata: {type(value).__name__}"
            )

        result: dict[str, Any] = {}

        for key, item in value.items():
            if isinstance(
                item,
                QDBusVariant,
            ):
                item = item.variant()

            result[str(key)] = item

        return result
