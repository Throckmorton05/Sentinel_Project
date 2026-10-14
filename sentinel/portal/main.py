import os
import sys
from PyQt6.QtGui import QIcon
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication
from .theme import LOGO
from .window import PortalWindow
from sentinel.gui_lifecycle import finish_gui


def main(argv=None):
    if os.geteuid() == 0:
        raise SystemExit('Run the Sentinel Portal as your desktop user, without sudo.')
    arguments = sys.argv if argv is None else argv
    app = QApplication(arguments)
    app.setApplicationName('Sentinel Portal')
    app.setOrganizationName('Sentinel')
    app.setDesktopFileName('sentinel-portal')
    app.setWindowIcon(QIcon(str(LOGO)))
    if '--guide' in arguments:
        # Read-only offline onboarding does not initialize Portal RPC workers.
        from .guide import GuideWindow
        app.setApplicationName('Sentinel Getting Started')
        app.setDesktopFileName('sentinel-getting-started')
        window = GuideWindow()
    else:
        window = PortalWindow()
    if '--guide' not in arguments and any(flag in arguments for flag in ('--calendar', '--system', '--guardian')):
        destination = ('Guardian' if '--guardian' in arguments else
                       'System & desktop' if '--system' in arguments else 'Calendar')
        def open_calendar():
            if window.connected:
                window.navigate(destination)
                timer.stop()
        timer = QTimer(window)
        timer.timeout.connect(open_calendar)
        timer.start(100)
    window.show()
    try:
        return app.exec()
    finally:
        finish_gui(app, window)


if __name__ == '__main__':
    sys.exit(main())
