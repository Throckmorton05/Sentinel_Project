#!/usr/bin/python3
"""The live ISO's Try/Install screen; never runs in an installed account."""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import sys

from PyQt6.QtCore import QProcess, Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton,
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
        self.allow_close = False
        self.setWindowTitle('Try or install Sentinel Linux')
        self.setStyleSheet(STYLE)
        logo = QIcon('/usr/share/calamares/branding/sentinel/logo.svg')
        self.setWindowIcon(logo)
        outer = QVBoxLayout(self)
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
        self.status = QLabel(''); self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter); content.addWidget(self.status)
        outer.addWidget(card, alignment=Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch()

    def try_desktop(self):
        self.allow_close = True
        QApplication.instance().quit()  # The retained live wrapper starts Plasma.

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
        process.errorOccurred.connect(self.failed)
        process.finished.connect(self.finished)
        argv = shlex.split(self.installer_exec)
        process.start(argv[0], argv[1:])

    def failed(self, error):
        self.status.setText('Unable to open the installer. You can try the live desktop and launch it there.')
        self.try_button.setEnabled(True); self.install_button.setEnabled(True)
        if self.process is not None:
            self.process.deleteLater(); self.process = None

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
        raise SystemExit('The Sentinel welcome screen is only available on the live installer ISO.')
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
    backgrounds = []
    for screen in app.screens():
        if screen == app.primaryScreen():
            welcome.setGeometry(screen.geometry()); welcome.showFullScreen()
        else:
            background = QWidget(); background.setStyleSheet('background: #0b101b;')
            background.setGeometry(screen.geometry()); background.showFullScreen()
            backgrounds.append(background)
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
