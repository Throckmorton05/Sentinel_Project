"""Interactive layer-shell blackout; only sentineld authorizes its release."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

if __name__ == '__main__':
    # This process alone uses system Qt, matching the native KDE layer-shell
    # plugin. Select it before the first PyQt import or GUI object is created.
    from sentinel.image_blocking.screen_blocker.system_qt import use_system_pyqt
    try:
        use_system_pyqt()
    except RuntimeError as exc:
        print(f'Sentinel screen time: {exc}', file=sys.stderr, flush=True)
        raise SystemExit(1)

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtProperty, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtQml import QQmlApplicationEngine

from sentinel.daemon import client
from sentinel.portal.operations import Runner
from sentinel.settings.paths import SENTINEL_LOGO


class Controller(QObject):
    windowsChanged = pyqtSignal()
    errorChanged = pyqtSignal()
    busyChanged = pyqtSignal()
    closingChanged = pyqtSignal()
    released = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._windows = []
        self._error = ''
        self._allowed_to_close = False
        self.runner = Runner(self)
        self.runner.busy_changed.connect(lambda _: self.busyChanged.emit())
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()

    @pyqtProperty('QVariantList', notify=windowsChanged)
    def windows(self):
        return self._windows

    @pyqtProperty(str, notify=errorChanged)
    def error(self):
        return self._error

    @pyqtProperty(bool, notify=busyChanged)
    def busy(self):
        return self.runner.busy

    @pyqtProperty(bool, notify=closingChanged)
    def allowedToClose(self):
        return self._allowed_to_close

    def failed(self, message):
        self._error = message
        self.errorChanged.emit()

    def received(self, status):
        if not status['blocking']:
            self.timer.stop()
            self._allowed_to_close = True
            self.closingChanged.emit()
            self.released.emit()
            return
        windows = [item for item in status['windows'] if item['blocking']]
        if windows != self._windows:
            self._windows = windows
            self.windowsChanged.emit()

    @pyqtSlot()
    def refresh(self):
        if not self.runner.busy:
            def read():
                status = client.get_screen_time_blocks()
                if status['blocking']:
                    client.register_screen_time_overlay()
                return status
            self.runner.start(read, self.received, self.failed)

    @pyqtSlot(str, str)
    def bypass(self, window_id, pin):
        if self.runner.busy:
            return
        self._error = ''
        self.errorChanged.emit()
        item = next((item for item in self._windows if item['id'] == window_id), {})
        def success(status):
            if item.get('mode') == 'self_lock' and not status['blocking']:
                try:
                    destination = '--system' if item.get('kind') == 'daily_limit' else '--calendar'
                    subprocess.Popen([sys.executable, '-I', '-m', 'sentinel.portal.main', destination],
                                     start_new_session=True)
                except OSError as exc:
                    print(f'Sentinel screen time: open the portal manually: {exc}', file=sys.stderr)
            self.received(status)
        self.runner.start(lambda: client.bypass_screen_time_block(
            window_id, guardian_pin=pin or None), success, self.failed)


def configure_qml_imports(engine, system_qml=Path('/usr/lib/x86_64-linux-gnu/qt6/qml')):
    """Use PyQt's Qt modules first, with KDE-only system modules as a fallback."""
    if system_qml.is_dir():
        # addImportPath prepends. A system Controls module can then require a
        # style version absent from PyQt, preventing the entire overlay loading.
        paths = [path for path in engine.importPathList() if path != str(system_qml)]
        engine.setImportPathList([*paths, str(system_qml)])


def main():
    if '--qt-info' in sys.argv:
        from PyQt6.QtCore import QLibraryInfo, qVersion
        print(f'Screen-time Qt: {qVersion()}')
        print(f'Qt plugins: {QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)}')
        print(f'QML modules: {QLibraryInfo.path(QLibraryInfo.LibraryPath.QmlImportsPath)}')
        return 0
    app = QGuiApplication(sys.argv)
    app.setApplicationName('Sentinel Screen Time')
    try:
        client.register_screen_time_overlay()
    except client.SentineldError as exc:
        print(f'Sentinel screen time: {exc}', file=sys.stderr)
        return 1
    controller = Controller(app)
    controller.released.connect(app.quit)
    engine = QQmlApplicationEngine()
    configure_qml_imports(engine)
    context = engine.rootContext()
    context.setContextProperty('screenTime', controller)
    logo = SENTINEL_LOGO if SENTINEL_LOGO.is_file() else Path(__file__).parents[2] / 'portal/assets/Sentinel_Shield.svg'
    context.setContextProperty('sentinelLogo', QUrl.fromLocalFile(str(logo)))
    engine.load(QUrl.fromLocalFile(str(Path(__file__).with_suffix('.qml'))))
    if not engine.rootObjects():
        return 1
    controller.refresh()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
