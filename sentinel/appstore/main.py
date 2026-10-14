import sys

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from sentinel.appstore.windows.main_window import MainWindow
from sentinel.settings.paths import APPSTORE_ICON
from sentinel.gui_lifecycle import finish_gui


def main() -> int:
    app = QApplication(sys.argv)

    icon = QIcon(str(APPSTORE_ICON))

    app.setWindowIcon(icon)

    window = MainWindow()
    window.setWindowIcon(icon)
    window.show()

    try:
        return app.exec()
    finally:
        finish_gui(app, window)


if __name__ == "__main__":
    sys.exit(main())
