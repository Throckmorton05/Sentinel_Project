from PyQt6.QtCore import Qt
from PyQt6.QtGui import (
    QCloseEvent,
    QGuiApplication,
    QScreen,
)
from PyQt6.QtSvgWidgets import QSvgWidget
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from sentinel.image_blocking.screen_blocker.styles import (
    PROGRESS_STYLE,
    WINDOW_STYLE,
)
from sentinel.settings.paths import SENTINEL_LOGO


class BlockOverlay(QWidget):
    """
    Fullscreen Sentinel privacy overlay for one physical display.

    During a recheck, the blocker creates one BlockOverlay for every
    connected monitor before restoring the original blocked window.
    """

    def __init__(
        self,
        screen: QScreen,
    ) -> None:
        super().__init__()

        self._screen = screen
        self._allow_close = False

        self.setWindowTitle(f"Sentinel Recheck - {screen.name()}")

        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )

        self.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground,
            False,
        )

        self.setStyleSheet(WINDOW_STYLE)

        layout = QVBoxLayout(self)

        layout.setContentsMargins(
            50,
            50,
            50,
            50,
        )

        # Sentinel logo.
        logo = QSvgWidget(str(SENTINEL_LOGO))

        logo.setFixedSize(
            400,
            250,
        )

        # Recheck message.
        title = QLabel("Rechecking...")

        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title.setStyleSheet(
            """
            font-size: 24px;
            font-weight: bold;
            """
        )

        # Indeterminate progress bar.
        progress = QProgressBar()

        progress.setStyleSheet(PROGRESS_STYLE)

        progress.setRange(
            0,
            0,
        )

        progress.setFixedWidth(400)

        layout.addStretch()

        layout.addWidget(
            logo,
            alignment=Qt.AlignmentFlag.AlignCenter,
        )

        layout.addSpacing(20)

        layout.addWidget(title)

        layout.addSpacing(30)

        layout.addWidget(
            progress,
            alignment=Qt.AlignmentFlag.AlignCenter,
        )

        layout.addStretch()

    def show_overlay(self) -> None:
        """
        Show this overlay fullscreen on its assigned physical display.
        """

        geometry = self._screen.geometry()

        print(
            "Creating overlay:"
            f" screen={self._screen.name()}"
            f" x={geometry.x()}"
            f" y={geometry.y()}"
            f" width={geometry.width()}"
            f" height={geometry.height()}"
        )

        # Associate the QWidget with the requested QScreen.
        self.setScreen(self._screen)

        # Explicitly assign the screen's virtual-desktop geometry.
        #
        # setScreen() alone does not necessarily move a top-level
        # window when the displays are virtual siblings.
        self.setGeometry(geometry)

        # Force creation of the underlying native QWindow so we can
        # explicitly associate that window with the target screen too.
        self.winId()

        native_window = self.windowHandle()

        if native_window is None:
            raise RuntimeError(
                f"Failed to create native window for display {self._screen.name()}"
            )

        native_window.setScreen(self._screen)

        native_window.setGeometry(geometry)

        # Show only after the target screen and geometry have both
        # been assigned.
        self.showFullScreen()

        self.raise_()

        QApplication.processEvents()

        actual_screen = self.screen()

        actual_screen_name = (
            actual_screen.name() if actual_screen is not None else "<none>"
        )

        print(
            "Overlay shown:"
            f" requested={self._screen.name()}"
            f" actual={actual_screen_name}"
        )

    def close_overlay(self) -> None:
        """
        Allow this overlay to close programmatically.
        """

        self._allow_close = True

        self.close()

    def closeEvent(
        self,
        a0: QCloseEvent | None,
    ) -> None:
        """
        Prevent users from dismissing the privacy overlay directly.
        """

        if a0 is None:
            return

        if self._allow_close:
            a0.accept()
            return

        a0.ignore()


class BlockOverlayManager:
    """
    Manages one fullscreen privacy overlay per connected display.
    """

    def __init__(self) -> None:
        self._overlays: list[BlockOverlay] = []

    def show(self) -> None:
        """
        Cover every connected display.
        """

        self.close()

        screens = QGuiApplication.screens()

        if not screens:
            raise RuntimeError("No displays are available for Sentinel overlays")

        print(f"Showing recheck overlays on {len(screens)} display(s)...")

        for index, screen in enumerate(
            screens,
            start=1,
        ):
            geometry = screen.geometry()

            print(
                f"Display {index}:"
                f" name={screen.name()}"
                f" x={geometry.x()}"
                f" y={geometry.y()}"
                f" width={geometry.width()}"
                f" height={geometry.height()}"
            )

            overlay = BlockOverlay(screen)

            self._overlays.append(overlay)

            overlay.show_overlay()

        QApplication.processEvents()

        print("Recheck overlays shown.")

    def close(self) -> None:
        """
        Remove every active privacy overlay.
        """

        if not self._overlays:
            return

        for overlay in self._overlays:
            overlay.close_overlay()
            overlay.deleteLater()

        self._overlays.clear()

        QApplication.processEvents()

        print("Recheck overlays closed.")

    def raise_all(self) -> None:
        """
        Raise every overlay again if KWin changes stacking order.
        """

        for overlay in self._overlays:
            overlay.raise_()

        QApplication.processEvents()

    def is_active(self) -> bool:
        return bool(self._overlays)
