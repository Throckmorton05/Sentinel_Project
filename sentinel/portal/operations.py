"""All socket work runs off the GUI thread; credentials exist for one request."""
from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLineEdit


class Signals(QObject):
    success = pyqtSignal(object)
    failed = pyqtSignal(str)
    finished = pyqtSignal()


class Job(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = Signals()

    def run(self):
        try:
            self.signals.success.emit(self.function())
        except Exception as exc:
            self.signals.failed.emit(str(exc))
        finally:
            self.function = None
            self.signals.finished.emit()


class Runner(QObject):
    busy_changed = pyqtSignal(bool)

    def __init__(self, parent):
        super().__init__(parent)
        self.jobs = set()

    @property
    def busy(self):
        return bool(self.jobs)

    def start(self, function, success, failure):
        job = Job(function)
        self.jobs.add(job)
        job.signals.success.connect(success)
        job.signals.failed.connect(failure)
        job.signals.finished.connect(lambda: self._finished(job))
        self.busy_changed.emit(True)
        QThreadPool.globalInstance().start(job)

    def _finished(self, job):
        self.jobs.discard(job)
        self.busy_changed.emit(self.busy)


def secret(parent, title='Guardian access', label='Guardian PIN'):
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    layout = QFormLayout(dialog)
    field = QLineEdit()
    field.setEchoMode(QLineEdit.EchoMode.Password)
    layout.addRow(label, field)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addRow(buttons)
    field.setFocus()
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    value = field.text()
    field.clear()
    return value if accepted else None
