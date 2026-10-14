"""Shared Qt operation lifecycle and per-request Guardian PIN prompt."""
from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal
from PyQt6.QtWidgets import QDialog, QInputDialog, QLabel, QLineEdit, QMessageBox, QProgressBar, QVBoxLayout

from sentinel.daemon import client


class OperationThread(QThread):
    """Capture failures and always finish; never touch widgets from this thread."""
    def __init__(self, callback, parent=None):
        super().__init__(parent)
        self.callback = callback
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = self.callback()
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.callback = None  # Drop closures containing per-request credentials.


class BusyDialog(QDialog):
    allow_close = False

    def reject(self):
        if self.allow_close:
            super().reject()

    def closeEvent(self, event):
        if self.allow_close:
            super().closeEvent(event)
        else:
            event.ignore()


class OperationRunner(QObject):
    busy_changed = pyqtSignal(bool)

    def __init__(self, parent):
        super().__init__(parent)
        self.thread = None
        self.authorizing = False
        self.popup = None
        self.on_success = None
        self.on_done = None

    @property
    def busy(self):
        return self.authorizing or self.thread is not None or getattr(self.parent(), "read_thread", None) is not None

    def start(self, title, callback, *, on_success=None, on_done=None):
        if self.busy:
            return False
        self.on_success = on_success
        self.on_done = on_done
        self.popup = BusyDialog(self.parent())
        installing = title == "Installing application"
        self.popup.setWindowTitle("Installing" if installing else title)
        self.popup.setWindowModality(Qt.WindowModality.ApplicationModal)
        if installing:
            self.popup.setFixedSize(350, 120)
        else:
            self.popup.setFixedSize(380, 130)
        self.popup.setStyleSheet("""
            QDialog {
                background-color: #172338;
                border: 1px solid #345270;
                border-radius: 8px;
            }

            QLabel {
                background-color: transparent;
                color: #eef5ff;
                font-size: 16px;
                font-weight: bold;
            }
        """)
        layout = QVBoxLayout(self.popup)
        label = QLabel("Installing application..." if installing else f"{title}…")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        progress = QProgressBar()
        progress.setRange(0, 0)
        progress.setTextVisible(False)
        progress.setStyleSheet("""
            QProgressBar {
                border: none;
                border-radius: 6px;
                background-color: #172338;
                height: 14px;
            }

            QProgressBar::chunk {
                background-color: #67bfff;
                border-radius: 6px;
            }
        """)
        layout.addWidget(progress)
        self.popup.show()
        self.thread = OperationThread(callback, self)
        self.thread.finished.connect(self._complete)
        self.busy_changed.emit(True)
        self.thread.start()
        return True

    def _complete(self):
        thread = self.thread
        self.popup.allow_close = True
        self.popup.close()
        self.popup.deleteLater()
        self.popup = None
        success, done = self.on_success, self.on_done
        self.on_success = self.on_done = None
        if thread.error is not None:
            QMessageBox.warning(self.parent(), "Sentinel operation failed", thread.error)
        if done is not None:
            done()
        self.thread = None
        if thread.error is None and success is not None:
            success()
        self.busy_changed.emit(False)
        thread.deleteLater()


def guardian_credentials(parent, *, operation_runner, uninstall=False):
    """Fetch current state, then ask for credentials without reading local PINs.

    Return (continue, pin). The daemon rechecks policy on the actual mutation,
    so changing modes after this read cannot authorize an otherwise denied action.
    """
    if operation_runner.busy:
        return False, None
    operation_runner.authorizing = True
    try:
        return _guardian_credentials(parent, uninstall=uninstall)
    finally:
        operation_runner.authorizing = False


def prompt_guardian_pin(parent):
    """Show a password-masked prompt using the AppStore's existing palette."""
    dialog = QInputDialog(parent)
    dialog.setWindowTitle("Guardian PIN")
    dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
    dialog.setInputMode(QInputDialog.InputMode.TextInput)
    dialog.setLabelText("Enter the Guardian PIN to authorize this operation:")
    dialog.setTextEchoMode(QLineEdit.EchoMode.Password)
    dialog.setOkButtonText("Authorize")
    dialog.setCancelButtonText("Cancel")
    dialog.setMinimumWidth(420)
    dialog.setStyleSheet("""
        QInputDialog {
            background-color: #172338;
            color: #eef5ff;
        }

        QLabel, QDialogButtonBox {
            background-color: transparent;
            color: #eef5ff;
            font-size: 14px;
        }

        QLineEdit {
            background-color: #111c2e;
            color: #eef5ff;
            border: 1px solid #345270;
            border-radius: 6px;
            padding: 10px;
            font-size: 14px;
            selection-background-color: #295a82;
        }

        QLineEdit:focus {
            border-color: #78c8ff;
        }

        QPushButton {
            background-color: #284c70;
            color: #eef5ff;
            border: none;
            border-radius: 6px;
            padding: 8px 18px;
            font-weight: bold;
        }

        QPushButton:hover {
            background-color: #35638d;
        }

        QPushButton:pressed {
            background-color: #223b58;
        }
    """)
    try:
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        return (dialog.textValue(), True) if accepted else (None, False)
    finally:
        dialog.setTextValue("")
        dialog.deleteLater()


def _guardian_credentials(parent, *, uninstall=False):
    try:
        policy = client.get_appstore_policy()
    except client.SentineldError as exc:
        QMessageBox.warning(parent, "Sentinel unavailable", str(exc))
        return False, None
    if uninstall and not policy["can_uninstall"]:
        QMessageBox.warning(parent, "Self Lock", "Removing applications is disabled in Self Lock.")
        return False, None
    if not policy["guardian_required"]:
        return True, None
    pin, ok = prompt_guardian_pin(parent)
    return bool(ok), pin if ok else None
