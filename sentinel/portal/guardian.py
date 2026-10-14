from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QHBoxLayout, QLineEdit, QListWidget,
    QListWidgetItem, QTabWidget, QVBoxLayout, QWidget, QMessageBox)
from sentinel.daemon import client
from .access import EntryCard, add_card, card_list
from .app_picker import AppPickerButton, app_icon, app_identity
from .operations import secret
from .models import read_pages
from .widgets import ToggleSwitch, button, description_box, label, page, scroll, setting_card


def guardian_apps():
    """Use Guardian's inventory for permissions, and the app catalog for icons."""
    result = client.list_guardian_apps()
    try:
        catalog = read_pages(lambda offset: client.list_installed_gui_apps(offset=offset), 'apps')
    except client.SentineldError:
        catalog = []
    launchers = {app['desktop_id']: app for app in catalog}
    identities = {app_identity(app): app for app in catalog if app_identity(app)[1]}
    entries = []
    for entry in result['apps']:
        identity = app_identity(entry)
        metadata = launchers.get(entry['desktop_id']) or identities.get(identity, {})
        entries.append({**entry, 'key': entry['desktop_id'], 'type': entry.get('type', 'native'),
                        'icon': entry.get('icon') or metadata.get('icon', ''),
                        'selectable': bool(entry['supported'] and not entry['builtin']),
                        'reason': 'This launcher is not supported yet.' if not entry['supported'] else None})
    return {**result, 'apps': entries}


class GuardianPage(QWidget):
    def __init__(self, portal):
        super().__init__()
        self.portal = portal
        self.loaded = False
        self.dirty = False
        self.app_dirty = False
        self.website_dirty = False
        _, layout = page('Guardian controls', 'Choose the apps, websites, and extensions available to this user. A fresh guardian PIN is required when saving.')
        self.setLayout(layout)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)
        self.app_page = QWidget()
        app_layout = QVBoxLayout(self.app_page)
        self.apps_enabled = ToggleSwitch('Only allow selected apps')
        self.apps_enabled.clicked.connect(self.mark_apps_dirty)
        app_layout.addWidget(setting_card('Only allow selected apps',
            'Limit desktop apps to the choices below and required desktop components.', self.apps_enabled))
        self.apps_data = []
        self.app_cards = {}
        self.apps = card_list()
        self.apps.setMinimumHeight(180)
        self.add_apps = AppPickerButton(self, lambda: self.apps_data,
            load_apps=lambda: [app for app in guardian_apps()['apps'] if not app['builtin']],
            launcher_choices=True)
        self.add_apps.setEnabled(False)
        self.add_apps.app_selected.connect(self.add_app)
        app_layout.addWidget(self.add_apps)
        app_layout.addWidget(self.apps, 1)
        app_layout.addWidget(description_box('Desktop services and the Sentinel Portal stay available automatically. Sign out and back in after app access changes.'))
        app_layout.addWidget(button('Save app access', self.save_apps, True))
        tabs.addTab(scroll(self.app_page), 'Apps')
        websites = QWidget()
        website_layout = QVBoxLayout(websites)
        self.websites_enabled = ToggleSwitch('Only allow selected websites')
        self.websites_enabled.clicked.connect(self.mark_websites_dirty)
        website_layout.addWidget(setting_card('Only allow selected websites',
            'Limit native Chrome to the domains you allow below.', self.websites_enabled))
        self.domains = card_list()
        self.domains.setMinimumHeight(180)
        self.domain_cards = {}
        website_layout.addWidget(self.domains, 1)
        row = QHBoxLayout()
        self.domain = QLineEdit()
        self.domain.setPlaceholderText('school.example.org')
        self.domain.returnPressed.connect(self.add_domain)
        row.addWidget(self.domain)
        row.addWidget(button('Add website', self.add_domain))
        website_layout.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(button('Add YouTube Kids', lambda: self.add_domain('youtubekids.com')))
        website_layout.addLayout(row)
        website_layout.addWidget(description_box('Domains include subdomains. Add any separate domains needed for school sign-in. Close and reopen native Chrome after saving.'))
        website_layout.addWidget(button('Save website access', self.save_websites, True))
        tabs.addTab(scroll(websites), 'Websites')
        extensions = QWidget()
        extension_layout = QVBoxLayout(extensions)
        self.extensions = QListWidget()
        extension_layout.addWidget(self.extensions, 1)
        row = QHBoxLayout()
        row.addWidget(button('Approve selected extension', lambda: self.change_extension('approve')))
        row.addWidget(button('Revoke selected extension', lambda: self.change_extension('revoke')))
        extension_layout.addLayout(row)
        extension_layout.addWidget(description_box('Approved optional extensions are installed through managed Chrome policy. Required Sentinel extensions stay managed.'))
        tabs.addTab(extensions, 'Extensions')
        security = QWidget()
        security_layout = QVBoxLayout(security)
        security_layout.addWidget(button('Change guardian PIN', self.change_pin))
        security_layout.addWidget(button('Exit Guardian Lock', self.unlock))
        security_layout.addStretch()
        tabs.addTab(security, 'Guardian PIN')
        self.status = label('')
        layout.addWidget(self.status)
        layout.addWidget(button('Reload guardian choices', self.reload))

    def mark_dirty(self, *_):
        self.dirty = True
        self.status.setText('Unsaved guardian choices.')

    def mark_apps_dirty(self, *_):
        self.app_dirty = True
        self.mark_dirty()

    def mark_websites_dirty(self, *_):
        self.website_dirty = True
        self.mark_dirty()

    def render_apps(self):
        position = self.apps.verticalScrollBar().value()
        self.apps.clear()
        self.app_cards.clear()
        for entry in self.apps_data:
            key = entry['desktop_id']
            card = EntryCard(entry['name'], key, lambda k=key: self.remove_app(k), icon=app_icon(entry))
            self.app_cards[key] = card
            add_card(self.apps, entry['name'], key, card)
        self.apps.verticalScrollBar().setValue(position)

    def add_app(self, entry):
        if not self.loaded or entry.get('builtin') or not entry.get('supported'):
            return
        if entry['desktop_id'] in {app['desktop_id'] for app in self.apps_data}:
            return
        self.apps_data.append(dict(entry))
        self.render_apps()
        self.mark_apps_dirty()

    def remove_app(self, desktop_id):
        if not any(app['desktop_id'] == desktop_id for app in self.apps_data):
            return
        self.apps_data = [app for app in self.apps_data if app['desktop_id'] != desktop_id]
        self.render_apps()
        self.mark_apps_dirty()

    def render_domains(self, domains):
        position = self.domains.verticalScrollBar().value()
        self.domains.clear()
        self.domain_cards.clear()
        for domain in domains:
            card = EntryCard(domain, '', lambda d=domain: self.remove_domain(d))
            self.domain_cards[domain] = card
            add_card(self.domains, domain, domain, card)
        self.domains.verticalScrollBar().setValue(position)

    def reload(self, preserve=None):
        if preserve is None and self.dirty and QMessageBox.question(self, 'Reload choices', 'Discard unsaved guardian choices?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        def work():
            apps = guardian_apps()
            websites = client.get_guardian_website_policy()
            try:
                extensions = {'extensions': read_pages(lambda offset: client.request('browser.extensions.list', {'offset': offset, 'limit': 20}), 'extensions')}
            except client.SentineldError as exc:
                extensions = {'extensions': [], 'error': str(exc)}
            return apps, websites, extensions
        self.portal.read(work, lambda data: self.render(data, preserve))

    def render(self, data, preserve=None):
        apps, websites, extensions = data
        self.apps_enabled.setChecked(apps['policy']['enabled'])
        self.apps_data = [entry for entry in apps['apps'] if entry['selectable']
                          and (entry['approved'] or entry.get('suggested', False))]
        self.render_apps()
        self.websites_enabled.setChecked(websites['enabled'])
        self.render_domains(websites['domains'])
        self.extensions.clear()
        for entry in extensions['extensions']:
            status = 'Approved' if entry['guardian_approved'] else 'Not approved'
            item = QListWidgetItem(f"{entry['name']} · {status}")
            item.setData(Qt.ItemDataRole.UserRole, entry['id'])
            item.setToolTip(entry['description'])
            self.extensions.addItem(item)
        self.loaded = True
        self.add_apps.setEnabled(True)
        self.dirty = False
        self.app_dirty = self.website_dirty = False
        from .models import enforcement_message
        self.status.setText(enforcement_message({'apps': apps['policy'], 'websites': websites})
                            + (' ' + extensions['error'] if extensions.get('error') else ''))
        if preserve:
            if 'apps' in preserve:
                enabled, entries = preserve['apps']
                self.apps_enabled.setChecked(enabled)
                self.apps_data = entries
                self.render_apps()
                self.mark_apps_dirty()
            if 'websites' in preserve:
                enabled, domains = preserve['websites']
                self.websites_enabled.setChecked(enabled)
                self.render_domains(domains)
                self.mark_websites_dirty()

    def add_domain(self, value=None):
        value = value if isinstance(value, str) else self.domain.text()
        # UI validation only; sentineld repeats the authoritative normalization.
        from sentinel.daemon.operations.guardian_website_ops import normalize_website
        try:
            domain = normalize_website(value)
            domains = [self.domains.item(i).text() for i in range(self.domains.count())]
            if domain not in domains:
                self.render_domains([*domains, domain])
                self.mark_websites_dirty()
            self.domain.clear()
        except ValueError as exc:
            self.portal.error(str(exc))

    def remove_domain(self, domain):
        domains = [self.domains.item(i).text() for i in range(self.domains.count())]
        if domain in domains:
            self.render_domains([entry for entry in domains if entry != domain])
            self.mark_websites_dirty()

    def after_save(self, section=None):
        preserve = {}
        if self.app_dirty and section != 'apps':
            preserve['apps'] = (self.apps_enabled.isChecked(), [dict(entry) for entry in self.apps_data])
        if self.website_dirty and section != 'websites':
            preserve['websites'] = (self.websites_enabled.isChecked(), [self.domains.item(i).text() for i in range(self.domains.count())])
        self.reload(preserve=preserve)

    def save_apps(self):
        if not self.loaded:
            return
        ids = [entry['desktop_id'] for entry in self.apps_data]
        enabled = self.apps_enabled.isChecked()
        self.portal.mutate(lambda pin: client.replace_guardian_apps(ids, enabled, guardian_pin=pin),
                           lambda: self.after_save('apps'), requires_pin=True)

    def save_websites(self):
        if not self.loaded:
            return
        domains = [self.domains.item(i).text() for i in range(self.domains.count())]
        enabled = self.websites_enabled.isChecked()
        self.portal.mutate(lambda pin: client.replace_guardian_websites(domains, enabled, guardian_pin=pin),
                           lambda: self.after_save('websites'), requires_pin=True)

    def change_extension(self, operation):
        item = self.extensions.currentItem()
        if item:
            extension_id = item.data(Qt.ItemDataRole.UserRole)
            self.portal.mutate(lambda pin: client.request('browser.extension.' + operation,
                {'extension_id': extension_id}, guardian_pin=pin, timeout=30), self.after_save, requires_pin=True)

    def change_pin(self):
        old = secret(self.portal)
        if old is None:
            return
        new = secret(self.portal, 'New guardian PIN', 'New PIN')
        if new is None:
            return
        if secret(self.portal, 'Confirm new PIN', 'Repeat new PIN') != new:
            self.portal.error('The PINs do not match.')
            return
        self.portal.read(lambda: client.change_guardian_pin(guardian_pin=old, new_pin=new), self.portal.saved)

    def unlock(self):
        if QMessageBox.question(self, 'Exit Guardian Lock', 'Allow this user to change protection settings freely?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.portal.mutate(lambda pin: client.exit_guardian_lock(guardian_pin=pin),
                               self.portal.refresh_core, requires_pin=True)
