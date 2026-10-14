from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtSvgWidgets import QSvgWidget
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sentinel.image_blocking.screen_blocker.styles import (
    BUTTON_STYLE,
    WINDOW_STYLE,
)
from sentinel.settings.paths import SENTINEL_LOGO


class BlockedWindow(QMainWindow):
    """
    Sentinel's blocked-content screen.

    This window contains no compositor-specific window-control logic.

    It only emits requests. The blocker controller is responsible for
    minimizing, restoring, closing, and rechecking the original KWin
    window.
    """

    recheck_requested = pyqtSignal()
    close_requested = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()

        self.countdown = 30
        self._allow_close = False
        self._recheck_pending = False

        self.setMinimumSize(600, 400)
        self.setWindowTitle("Sentinel")
        self.setStyleSheet(WINDOW_STYLE)

        # Keep the normal KDE title bar, minimize, and maximize buttons,
        # but do not expose a normal close button.
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
        )

        central_widget = QWidget()
        layout = QVBoxLayout(central_widget)

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

        # Main blocked message.
        label = QLabel("Content Blocked")

        label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        label.setStyleSheet(
            """
            font-size: 24px;
            font-weight: bold;
            """
        )

        # Countdown text.
        self.countdown_label = QLabel("Rechecking in 30 seconds...")

        self.countdown_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.countdown_label.setStyleSheet(
            """
            font-size: 16px;
            """
        )

        # Recheck button.
        self.recheck_button = QPushButton("Recheck")

        self.recheck_button.setStyleSheet(BUTTON_STYLE)

        self.recheck_button.setFixedSize(
            220,
            50,
        )

        self.recheck_button.clicked.connect(self.request_recheck)

        # Close-original-window button.
        self.close_button = QPushButton("Close Window")

        self.close_button.setStyleSheet(BUTTON_STYLE)

        self.close_button.setFixedSize(
            220,
            50,
        )

        self.close_button.clicked.connect(self.request_close)

        button_layout = QHBoxLayout()

        button_layout.addStretch()

        button_layout.addWidget(self.recheck_button)

        button_layout.setSpacing(20)

        button_layout.addWidget(self.close_button)

        button_layout.addStretch()

        layout.addStretch()

        layout.addWidget(
            logo,
            alignment=Qt.AlignmentFlag.AlignCenter,
        )

        layout.addSpacing(20)

        layout.addWidget(label)

        layout.addSpacing(15)

        layout.addWidget(self.countdown_label)

        layout.addSpacing(40)

        layout.addLayout(button_layout)

        layout.addStretch()

        self.setCentralWidget(central_widget)

        # Automatic recheck countdown.
        self.countdown_timer = QTimer(self)

        self.countdown_timer.setInterval(1000)

        self.countdown_timer.timeout.connect(self.update_countdown)

        self.countdown_timer.start()

        self.showMaximized()

    def update_countdown(self) -> None:
        """
        Update the automatic recheck countdown.
        """

        if self._recheck_pending:
            return

        self.countdown -= 1

        if self.countdown <= 0:
            self.request_recheck()
            return

        self.countdown_label.setText(f"Rechecking in {self.countdown} seconds...")

    def request_recheck(self) -> None:
        """
        Ask the blocker controller to recheck the original window.
        """

        if self._recheck_pending:
            return

        self._recheck_pending = True

        self.countdown_timer.stop()

        self.countdown_label.setText("Rechecking...")

        self.recheck_button.setEnabled(False)

        self.recheck_requested.emit()

    def request_close(self) -> None:
        """
        Ask the blocker controller to close the original KWin window.
        """

        self.countdown_timer.stop()

        self.recheck_button.setEnabled(False)

        self.close_button.setEnabled(False)

        self.close_requested.emit()

    def reset_countdown(
        self,
        seconds: int = 30,
    ) -> None:
        """
        Reset the blocked screen after a failed recheck.
        """

        self.countdown = seconds
        self._recheck_pending = False

        self.countdown_label.setText(f"Rechecking in {seconds} seconds...")

        self.recheck_button.setEnabled(True)

        self.close_button.setEnabled(True)

        self.countdown_timer.start()

    def close_blocked_screen(self) -> None:
        """
        Allow the Sentinel blocked screen to close normally.
        """

        self.countdown_timer.stop()

        self._allow_close = True

        self.close()

    def closeEvent(
        self,
        a0: QCloseEvent | None,
    ) -> None:
        """
        Prevent the blocked screen from being dismissed directly.

        blocker.py must explicitly call close_blocked_screen().
        """

        if a0 is None:
            return

        if self._allow_close:
            a0.accept()
            return

        a0.ignore()
