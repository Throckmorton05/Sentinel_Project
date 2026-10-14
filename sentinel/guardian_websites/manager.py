"""Legacy module compatibility; normal management opens the Sentinel Portal."""
from __future__ import annotations

import sys

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)
from sentinel.daemon import client
from sentinel.daemon.operations.guardian_website_ops import normalize_website, MAX_DOMAINS
from sentinel.guardian_apps.manager import STYLE, prompt_pin
from sentinel.portal.main import main as open_portal


class Manager(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Sentinel — Guardian Website Access')
        self.resize(620, 600)
        layout = QVBoxLayout(self)
        intro = QLabel('Choose which websites this user can visit in Chrome.\n'
                       'Each domain also allows its subdomains. Other websites stay blocked.')
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.enabled = QCheckBox('Only allow selected websites')
        layout.addWidget(self.enabled)
        self.domains = QListWidget()
        layout.addWidget(self.domains)
        row = QHBoxLayout()
        self.domain = QLineEdit()
        self.domain.setPlaceholderText('Website domain, e.g. school.example.org')
        self.domain.returnPressed.connect(self.add_domain)
        row.addWidget(self.domain)
        add = QPushButton('Add website')
        add.clicked.connect(self.add_domain)
        row.addWidget(add)
        layout.addLayout(row)
        row = QHBoxLayout()
        kids = QPushButton('Add YouTube Kids')
        kids.clicked.connect(lambda: self.add_domain('youtubekids.com'))
        row.addWidget(kids)
        remove = QPushButton('Remove selected')
        remove.clicked.connect(self.remove_domain)
        row.addWidget(remove)
        layout.addLayout(row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        save = QPushButton('Save')
        save.clicked.connect(self.save)
        layout.addWidget(save)
        QShortcut(QKeySequence('Ctrl+Q'), self, activated=self.close)
        self.refresh()

    def refresh(self):
        policy = client.get_guardian_website_policy()
        self.enabled.setChecked(policy['enabled'])
        self.domains.clear()
        for domain in policy['domains']:
            self.domains.addItem(QListWidgetItem(domain))
        application = policy['browser_application']
        restrictions = policy['browser_restrictions']
        errors = [entry.get('error') for entry in (application, restrictions) if entry.get('error')]
        self.status.setText('Native Chrome is required. Close and reopen Chrome after saving.\n'
                            'Other browsers are restricted while this mode is on.\n'
                            'School sign-in pages on another domain need their own approval.\n'
                            + ('Changes are pending. ' + ' '.join(errors)
                               if application['status'] != 'applied' or restrictions['status'] == 'pending'
                               else 'Website selection saved.'))

    def add_domain(self, value=None):
        # clicked(bool) is not a website string.
        value = value if isinstance(value, str) else self.domain.text()
        try:
            value = normalize_website(value)
            present = {self.domains.item(i).text() for i in range(self.domains.count())}
            if value not in present:
                if len(present) >= MAX_DOMAINS:
                    raise ValueError(f'You can approve up to {MAX_DOMAINS} website domains.')
                self.domains.addItem(QListWidgetItem(value))
                self.domains.sortItems(Qt.SortOrder.AscendingOrder)
            self.domain.clear()
        except ValueError as exc:
            QMessageBox.warning(self, 'Website domain', str(exc))

    def remove_domain(self):
        for item in self.domains.selectedItems():
            self.domains.takeItem(self.domains.row(item))

    def save(self):
        pin = prompt_pin(self)
        if pin is None:
            return
        domains = [self.domains.item(i).text() for i in range(self.domains.count())]
        try:
            result = client.replace_guardian_websites(domains, self.enabled.isChecked(), guardian_pin=pin)
            self.refresh()
            pending = (result['browser_application']['status'] != 'applied'
                       or result['browser_restrictions']['status'] == 'pending')
            QMessageBox.information(self, 'Saved',
                'Website selection saved. ' + ('Enforcement is pending; see the status below.'
                                               if pending else 'Close and reopen Chrome to apply it.'))
        except client.SentineldError as exc:
            QMessageBox.warning(self, 'Sentinel', str(exc))


def main():
    return open_portal(['sentinel-portal', '--guardian'])


if __name__ == '__main__':
    sys.exit(main())
