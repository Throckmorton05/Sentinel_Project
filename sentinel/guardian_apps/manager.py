"""Legacy launcher compatibility and independently authenticated recovery."""
from __future__ import annotations

import sys

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QFormLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QVBoxLayout, QWidget, QAbstractItemView,
)
from sentinel.daemon import client
from sentinel.guardian_apps import recovery
from sentinel.gui_lifecycle import finish_gui


def open_portal(arguments):
    # Avoid importing the Portal again when it opens this recovery dialog.
    from sentinel.portal.main import main as portal_main
    return portal_main(arguments)

OFFLINE_RECOVERY_SUPPORTED = True

STYLE = '''
QWidget { background: #101820; color: #e6edf3; font-size: 14px; }
QLineEdit, QListWidget { background: #18232e; border: 1px solid #344656;
                       border-radius: 8px; padding: 8px; }
QPushButton { background: #263e50; border: none; border-radius: 8px;
              padding: 10px 16px; }
QPushButton:hover { background: #36576e; }
'''


def prompt_pin(parent=None) -> str | None:
    dialog = QDialog(parent)
    dialog.setWindowTitle('Guardian access')
    layout = QFormLayout(dialog)
    field = QLineEdit()
    field.setEchoMode(QLineEdit.EchoMode.Password)
    layout.addRow('Guardian PIN', field)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    return field.text() if dialog.exec() == QDialog.DialogCode.Accepted else None


def app_label(entry):
    package = {'snap': 'Snap', 'flatpak': 'Flatpak'}.get(entry.get('type'))
    return f"{entry['name']} ({package})" if package else entry['name']


class Manager(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Sentinel — Guardian App Access')
        self.resize(620, 600)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('Choose which installed apps this user can open.'))
        self.enabled = QCheckBox('Only allow selected apps')
        layout.addWidget(self.enabled)
        self.apps = QListWidget()
        layout.addWidget(self.apps)
        add = QPushButton('Add app…')
        add.clicked.connect(self.add_apps)
        layout.addWidget(add)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        save = QPushButton('Save')
        save.clicked.connect(self.save)
        layout.addWidget(save)
        websites = QPushButton('Manage allowed websites…')
        websites.clicked.connect(self.open_websites)
        layout.addWidget(websites)
        unlock = QPushButton('Exit Guardian Lock')
        unlock.clicked.connect(self.unlock)
        layout.addWidget(unlock)
        self.refresh()

    def open_websites(self):
        from sentinel.guardian_websites.manager import Manager as WebsiteManager
        try:
            pin = prompt_pin(self)
            if pin is None:
                return
            client.request('guardian.websites.authenticate', guardian_pin=pin)
            self.website_window = WebsiteManager()
            self.website_window.show()
        except client.SentineldError as exc:
            QMessageBox.warning(self, 'Sentinel', str(exc))

    def refresh(self):
        result = client.list_guardian_apps()
        policy = result['policy']
        self.enabled.setChecked(policy['enabled'])
        self.apps.clear()
        self.catalog = {entry['desktop_id']: entry for entry in result['apps'] if not entry['builtin']}
        for entry in self.catalog.values():
            if entry['approved'] or entry.get('default_app') or entry.get('suggested'):
                self._append_app(entry, entry['approved'] or entry.get('suggested', False))
        self.status.setText('Desktop services stay available automatically. '
                            'Add the apps this user may open; uncheck an app to restrict it. '
                            'Sign out and back in after changing this mode.\n'
                            + ('Dolphin and System Settings are suggested defaults; Save confirms your choices.\n'
                               if policy.get('defaults_pending') else '')
                            + '\n'.join(policy['application']['errors']))

    def _append_app(self, entry, checked=True):
        item = QListWidgetItem(app_label(entry))
        item.setData(Qt.ItemDataRole.UserRole, entry['desktop_id'])
        if entry['supported']:
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        else:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            item.setToolTip('This launcher is not supported yet.')
        self.apps.addItem(item)

    def add_apps(self):
        present = {self.apps.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.apps.count())}
        dialog = QDialog(self)
        dialog.setWindowTitle('Add allowed apps')
        dialog.resize(520, 440)
        layout = QVBoxLayout(dialog)
        search = QLineEdit()
        search.setPlaceholderText('Search installed apps…')
        layout.addWidget(search)
        choices = QListWidget()
        choices.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        layout.addWidget(choices)
        for key, entry in self.catalog.items():
            if key in present or not entry['supported']:
                continue
            item = QListWidgetItem(app_label(entry))
            item.setData(Qt.ItemDataRole.UserRole, key)
            choices.addItem(item)
        def filter_apps(text):
            for i in range(choices.count()):
                choices.item(i).setHidden(text.casefold() not in choices.item(i).text().casefold())
        search.textChanged.connect(filter_apps)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Add selected apps')
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            for item in choices.selectedItems():
                if not item.isHidden():
                    self._append_app(self.catalog[item.data(Qt.ItemDataRole.UserRole)])


    def save(self):
        pin = prompt_pin(self)
        if pin is None:
            return
        ids = [self.apps.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(self.apps.count())
               if self.apps.item(i).checkState() == Qt.CheckState.Checked]
        try:
            result = client.replace_guardian_apps(ids, self.enabled.isChecked(), guardian_pin=pin)
            self.refresh()
            QMessageBox.information(self, 'Saved', 'App selection saved. '
                                    + ('Desktop changes are pending; see the status below.'
                                       if result['application']['errors'] else 'Sign out and back in to refresh KDE.'))
        except client.SentineldError as exc:
            QMessageBox.warning(self, 'Sentinel', str(exc))

    def unlock(self):
        pin = prompt_pin(self)
        if pin is None:
            return
        try:
            client.exit_guardian_lock(guardian_pin=pin)
            self.close()
        except client.SentineldError as exc:
            QMessageBox.warning(self, 'Sentinel', str(exc))


class OfflineRecovery(QDialog):
    """The outage window needs no daemon state/PIN RPC to become visible."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Sentinel — Guardian recovery')
        self.resize(540, 240)
        layout = QVBoxLayout(self)
        message = QLabel('Guardian App Access is temporarily unavailable.\n'
                         'Your guardian can disable the app restriction using their PIN.\n'
                         'Saved app choices and other protections will be retained.')
        message.setWordWrap(True)
        layout.addWidget(message)
        self.disable_button = QPushButton('Disable app restriction')
        self.disable_button.clicked.connect(self.disable)
        layout.addWidget(self.disable_button)
        self.status = QLabel('')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def disable(self):
        pin = prompt_pin(self)
        if pin is None:
            return
        try:
            # If sentineld recovered since this window opened, use its normal
            # authenticated mutation. A policy/PIN denial never falls back.
            try:
                client.get_lock_state()
            except client.SentineldError:
                result = recovery.request_disable(pin)
            else:
                result = client.set_guardian_apps_enabled(False, guardian_pin=pin)
                result = {'errors': result['application']['errors']}
            self.disable_button.setEnabled(False)
            self.status.setText('App restriction disabled. Sign out and back in to refresh KDE.\n'
                                + '\n'.join(result.get('errors', [])))
        except (recovery.RecoveryError, client.SentineldError) as exc:
            QMessageBox.warning(self, 'Sentinel', str(exc))


def main():
    try:
        client.get_lock_state()
    except client.SentineldError:
        # This fixed hidden launcher must still open if the backend is down.
        # Recovery itself verifies the PIN through the independent helper.
        app = QApplication(sys.argv)
        app.setStyleSheet(STYLE)
        window = OfflineRecovery()
        window.show()
        try:
            return app.exec()
        finally:
            finish_gui(app, window)
    return open_portal(['sentinel-portal', '--guardian'])


if __name__ == '__main__':
    sys.exit(main())
