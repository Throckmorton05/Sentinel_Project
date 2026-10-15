#!/usr/bin/python3
"""The live ISO's Try/Install screen; never runs in an installed account."""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import sys

from PyQt6.QtCore import QProcess, QProcessEnvironment, QTimer, Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

STYLE = '''
QWidget { background: #0b101b; color: #e6edf7; font-family: sans-serif; }
QFrame#card { background: #121c2c; border: 1px solid #273c56; border-radius: 24px; }
QFrame#card QLabel { background: transparent; border: none; }
QLabel#title { font-size: 32px; font-weight: 700; }
QLabel#subtitle { color: #a9bdd7; font-size: 16px; }
QPushButton { background: #1d304b; border: 1px solid #395879; border-radius: 14px;
              padding: 17px 22px; color: #e6edf7; font-size: 17px; }
QPushButton:hover { background: #294568; }
QPushButton#install { background: #769ff0; color: #0b1626; font-weight: 700; }
QPushButton#install:hover { background: #9cbcff; }
QPushButton:disabled { background: #263449; color: #8a9bb2; }
'''


class Welcome(QWidget):
    def __init__(self, installer_exec):
        super().__init__()
        self.installer_exec = installer_exec
        self.process = None
        self.network_process = None
        self.allow_close = False
        self.setWindowTitle('Try or install Sentinel Linux')
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setStyleSheet(STYLE)
        logo = QIcon('/usr/share/calamares/branding/sentinel/logo.svg')
        self.setWindowIcon(logo)
        # Small laptop screens and display scaling must not hide the buttons.
        scroll = QScrollArea(self); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget(); scroll.setWidget(body)
        frame = QVBoxLayout(self); frame.setContentsMargins(0, 0, 0, 0)
        frame.addWidget(scroll)
        outer = QVBoxLayout(body)
        outer.addStretch()
        card = QFrame(); card.setObjectName('card'); card.setMaximumWidth(760)
        content = QVBoxLayout(card); content.setContentsMargins(42, 36, 42, 36)
        content.setSpacing(24)
        mark = QLabel(); mark.setPixmap(logo.pixmap(116, 116)); mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content.addWidget(mark)
        title = QLabel('Welcome to Sentinel Linux'); title.setObjectName('title')
        title.setAlignment(Qt.AlignmentFlag.AlignCenter); content.addWidget(title)
        subtitle = QLabel('0.1.0 Beta'); subtitle.setObjectName('subtitle')
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter); content.addWidget(subtitle)
        text = QLabel('Try the live desktop, or install Sentinel Linux on this computer.')
        text.setWordWrap(True); text.setAlignment(Qt.AlignmentFlag.AlignCenter); content.addWidget(text)
        choices = QHBoxLayout(); choices.setSpacing(18)
        self.try_button = QPushButton('Try Sentinel Linux'); self.try_button.clicked.connect(self.try_desktop)
        self.install_button = QPushButton('Install Sentinel Linux'); self.install_button.setObjectName('install')
        self.install_button.clicked.connect(self.install)
        choices.addWidget(self.try_button); choices.addWidget(self.install_button); content.addLayout(choices)
        self.network_button = QPushButton('Connect to Wi-Fi')
        self.network_button.clicked.connect(self.open_network)
        content.addWidget(self.network_button)
        self.network_status = QLabel('Checking network connection…')
        self.network_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content.addWidget(self.network_status)
        offline = QLabel('Internet is optional for installation. Extra downloads need a connection.')
        offline.setWordWrap(True); offline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content.addWidget(offline)
        self.status = QLabel(''); self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter); content.addWidget(self.status)
        outer.addWidget(card, alignment=Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch()
        self.network_check = QProcess(self)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert('LC_ALL', 'C')
        self.network_check.setProcessEnvironment(environment)
        self.network_check.finished.connect(self.network_checked)
        self.network_check.errorOccurred.connect(lambda _: self.network_status.setText('Network status unavailable'))
        self.network_timeout = QTimer(self); self.network_timeout.setSingleShot(True)
        self.network_timeout.timeout.connect(self.network_check.kill)
        self.network_timer = QTimer(self); self.network_timer.timeout.connect(self.check_network)
        self.network_timer.start(5000); self.check_network()

    def check_network(self):
        if self.network_check.state() == QProcess.ProcessState.NotRunning:
            self.network_check.start('/usr/bin/nmcli', ['-t', '-f', 'STATE', 'general'])
            self.network_timeout.start(3000)

    def network_checked(self, code, status):
        self.network_timeout.stop()
        state = bytes(self.network_check.readAllStandardOutput()).decode(errors='replace').strip()
        text = {'connected': 'Connected — NetworkManager reports internet access',
                'connected (local only)': 'Connected to a local network — internet unverified',
                'connected (site only)': 'Connected to a network — internet limited',
                'connecting': 'Connecting…', 'disconnected': 'Not connected',
                'asleep': 'Networking is disabled'}.get(state, 'Internet access unverified')
        self.network_status.setText(text if code == 0 else 'Network status unavailable')

    def open_network(self):
        if self.network_process is not None:
            return
        process = QProcess(self); self.network_process = process
        process.errorOccurred.connect(lambda _: self.network_closed(True))
        process.finished.connect(lambda code, status: self.network_closed(code != 0))
        # Plasma has already started, including its NetworkManager secret agent.
        process.start('/usr/bin/plasmawindowed', ['org.kde.plasma.networkmanagement'])
        self.network_button.setEnabled(False)
        self.showMinimized()  # The panel and network dialog remain accessible.

    def network_closed(self, failed=False):
        process, self.network_process = self.network_process, None
        if process is None:
            return
        process.deleteLater(); self.network_button.setEnabled(True)
        self.showFullScreen(); self.raise_(); self.activateWindow(); self.check_network()
        if failed:
            self.status.setText('Choose Try Sentinel Linux, then use the desktop network icon to connect to Wi-Fi.')

    def try_desktop(self):
        self.allow_close = True
        QApplication.instance().quit()  # Plasma is already running underneath.

    def install(self):
        if self.process is not None:
            return
        self.try_button.setEnabled(False); self.install_button.setEnabled(False)
        self.status.setText('Opening the installer…')
        # Match the upstream prompt's root XWayland access. The actual installer
        # command, including its existing elevation wrapper, comes from the
        # checked root-owned desktop file at image-build time.
        if Path('/usr/bin/xhost').is_file():
            access = QProcess(self)
            access.start('/usr/bin/xhost', ['si:localuser:root'])
            access.waitForFinished(5000)
        process = QProcess(self); self.process = process
        process.started.connect(self.showMinimized)
        process.errorOccurred.connect(self.failed)
        process.finished.connect(self.finished)
        argv = shlex.split(self.installer_exec)
        process.start(argv[0], argv[1:])

    def failed(self, error):
        self.status.setText('Unable to open the installer. You can try the live desktop and launch it there.')
        self.try_button.setEnabled(True); self.install_button.setEnabled(True)
        if self.process is not None:
            self.process.deleteLater(); self.process = None
        self.showFullScreen(); self.raise_(); self.activateWindow()

    def finished(self, code, status):
        if code == 0 and status == QProcess.ExitStatus.NormalExit:
            self.try_desktop()
        else:
            self.failed(None)

    def closeEvent(self, event):
        if self.allow_close:
            event.accept()
        else:
            event.ignore()


def main():
    if ('boot=casper' not in Path('/proc/cmdline').read_text().split()
            or Path('/etc/sentinel/iso-installed.json').exists()):
        return 0  # The autostart file is harmless after installation.
    configuration = Path('/usr/lib/sentinel-iso/live-welcome.json')
    info = configuration.lstat()
    if configuration.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit('Unprotected live installer configuration.')
    command = json.loads(configuration.read_text())['installer_exec']
    if not isinstance(command, str) or not shlex.split(command):
        raise SystemExit('Invalid live installer command.')
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    welcome = Welcome(command)
    screen = app.primaryScreen()
    if screen is not None:
        welcome.setGeometry(screen.geometry())
    welcome.showFullScreen()  # Plasma keeps running behind the live welcome.
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
