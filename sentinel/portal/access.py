from time import monotonic
from PyQt6.QtCore import QSize, QTimer, Qt
from PyQt6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QSizePolicy, QTabWidget, QVBoxLayout, QWidget)
from sentinel.daemon import client
from .app_picker import AppPickerButton, app_icon
from .models import enforcement_message, read_pages
from .widgets import button, description_box, label, page


class CardText(QLabel):
    def __init__(self, text):
        super().__init__(text)
        self.full_text = text
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setToolTip(text)
        self.setAccessibleName(text)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        super().setText(self.fontMetrics().elidedText(self.full_text, Qt.TextElideMode.ElideRight, self.width()))


class EntryCard(QFrame):
    def __init__(self, title, subtitle, callback, *, icon=None):
        super().__init__()
        self.setObjectName('accessCard')
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 12, 14, 12)
        row.setSpacing(12)
        if icon is not None:
            artwork = QLabel()
            artwork.setPixmap(icon.pixmap(36, 36))
            artwork.setFixedSize(40, 40)
            row.addWidget(artwork)
        text = QVBoxLayout()
        text.setSpacing(4)
        self.title = CardText(title)
        self.title.setObjectName('entryTitle')
        text.addWidget(self.title)
        if subtitle:
            self.subtitle = CardText(subtitle)
            self.subtitle.setObjectName('muted')
            text.addWidget(self.subtitle)
        row.addLayout(text, 1)
        self.delete = button('Delete', callback)
        self.delete.setObjectName('entryDelete')
        self.delete.setAccessibleName('Delete ' + title)
        row.addWidget(self.delete)


class CardList(QListWidget):
    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Qt otherwise defers Adjust layout, briefly clipping Delete controls
        # at the previous window width during a resize.
        self.doItemsLayout()


def card_list():
    choices = CardList()
    choices.setObjectName('accessList')
    choices.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
    choices.setResizeMode(QListWidget.ResizeMode.Adjust)
    choices.setSpacing(6)
    choices.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    return choices


def add_card(choices, title, entry, card):
    item = QListWidgetItem(title)
    item.setData(Qt.ItemDataRole.UserRole, entry)
    item.setSizeHint(QSize(0, 82))
    choices.addItem(item)
    choices.setItemWidget(item, card)


class AccessPage(QWidget):
    def __init__(self, portal):
        super().__init__()
        self.portal = portal
        _, layout = page('Apps and websites', 'Manage blocked apps and your personal DNS block and allow lists.')
        self.setLayout(layout)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)
        self.domain_lists = {}
        self.domain_cards = {}
        self.app_cards = {}
        self.mode = 'unlocked'
        for name, title in [('blocklist', 'Blocked websites'), ('allowlist', 'Allowed websites')]:
            widget = QWidget()
            body = QVBoxLayout(widget)
            choices = card_list()
            body.addWidget(choices, 1)
            row = QHBoxLayout()
            field = QLineEdit()
            field.setPlaceholderText('example.com')
            row.addWidget(field)
            add = button('Add domain', lambda n=name: self.add_domain(n))
            row.addWidget(add)
            field.returnPressed.connect(lambda n=name: self.add_domain(n))
            body.addLayout(row)
            self.domain_lists[name] = (choices, field, add)
            self.domain_cards[name] = {}
            tabs.addTab(widget, title)
        apps = QWidget()
        body = QVBoxLayout(apps)
        self.apps = card_list()
        self.apps_data = []
        body.addWidget(self.apps, 1)
        body.addWidget(description_box('Browser baseline restrictions are managed separately and are not removable from this list.'))
        row = QHBoxLayout()
        self.add_apps = AppPickerButton(self, lambda: self.apps_data)
        self.add_apps.app_selected.connect(self.add_app)
        row.addWidget(self.add_apps)
        row.addStretch()
        body.addLayout(row)
        tabs.addTab(apps, 'Blocked apps')
        self.status = label('Changes apply automatically. DNS sources update daily.', True)
        layout.addWidget(self.status)
        self.permission_timer = QTimer(self)
        self.permission_timer.setInterval(1000)
        self.permission_timer.timeout.connect(self.update_permissions)

    def showEvent(self, event):
        super().showEvent(event)
        self.permission_timer.start()

    def hideEvent(self, event):
        self.permission_timer.stop()
        super().hideEvent(event)

    def reload(self):
        def work():
            return (read_pages(lambda offset: client.list_dns_domains('blocklist', offset=offset), 'entries'),
                    read_pages(lambda offset: client.list_dns_domains('allowlist', offset=offset), 'entries'),
                    client.list_blocked_apps(), client.get_dns_status())
        def loaded(data):
            blocked, allowed, apps, dns = data
            loaded_at = monotonic()
            for name, entries in [('blocklist', blocked), ('allowlist', allowed)]:
                choices = self.domain_lists[name][0]
                position = choices.verticalScrollBar().value()
                choices.clear()
                self.domain_cards[name] = {}
                for entry in entries:
                    domain = entry['domain']
                    card = EntryCard(domain, '', lambda n=name, d=domain: self.remove_domain(n, d))
                    expires = loaded_at + entry.get('correction_remaining_seconds', 0)
                    self.domain_cards[name][domain] = (card, entry, expires)
                    add_card(choices, domain, entry, card)
                choices.verticalScrollBar().setValue(position)
            position = self.apps.verticalScrollBar().value()
            self.apps.clear()
            self.app_cards = {}
            self.apps_data = apps['apps']
            for app in apps['apps']:
                key = app['key']
                target = app.get('executable') or app.get('flatpak_id') or app.get('snap_instance')
                card = EntryCard(app['name'], target, lambda k=key: self.remove_app(k), icon=app_icon(app))
                self.app_cards[key] = card
                add_card(self.apps, app['name'], key, card)
            self.apps.verticalScrollBar().setValue(position)
            feedback = enforcement_message({'app_blocks': apps.get('application', {}),
                                            'DNS': dns.get('application', {}),
                                            'DNS_sources': dns.get('sources', {})})
            self.status.setText('Changes apply automatically. DNS sources update daily.' if feedback == 'Saved.'
                                else feedback.removeprefix('Saved. '))
            self.update_permissions()
        self.portal.read(work, loaded)

    def render_mode(self, mode):
        self.mode = mode
        self.update_permissions()
        for widget in self.domain_lists['allowlist'][1:]:
            widget.setEnabled(mode != 'self_lock')

    def update_permissions(self):
        for card in self.app_cards.values():
            card.delete.setEnabled(self.mode != 'self_lock')
            card.delete.setToolTip('App blocks cannot be removed in Self-Lock.' if self.mode == 'self_lock' else '')
        for name, cards in self.domain_cards.items():
            for card, entry, expires in cards.values():
                allowed = entry.get('removable_without_pin', False) or entry.get('guardian_pin_required', False)
                if self.mode == 'self_lock' and name == 'blocklist':
                    allowed = bool(entry.get('correction_remaining_seconds', 0) and monotonic() < expires)
                card.delete.setEnabled(bool(allowed))
                card.delete.setToolTip('The Self-Lock correction period has ended.' if not allowed and self.mode == 'self_lock'
                                       else 'Guardian PIN required.' if self.mode == 'guardian_lock' else '')

    def add_domain(self, name):
        field = self.domain_lists[name][1]
        value = field.text().strip()
        if value:
            def added():
                # Keep any new text entered while the request was running.
                if field.text().strip() == value:
                    field.clear()
                self.reload()
            self.portal.mutate(lambda pin: client.add_dns_domain(name, value, guardian_pin=pin), added)

    def remove_domain(self, name, domain):
        self.portal.mutate(lambda pin: client.remove_dns_domain(name, domain, guardian_pin=pin), self.reload)

    def add_app(self, app):
        if app['type'] == 'native':
            self.portal.mutate(lambda pin: client.add_blocked_native_app(app['key'], app['name'],
                app['executable'], guardian_pin=pin), self.reload)
        elif app['type'] == 'flatpak':
            self.portal.mutate(lambda pin: client.add_blocked_flatpak_app(app['key'], app['name'],
                app['flatpak_id'], guardian_pin=pin), self.reload)
        else:
            self.portal.mutate(lambda pin: client.add_blocked_snap_app(app['key'], app['name'],
                app['snap_instance'], guardian_pin=pin), self.reload)

    def remove_app(self, key):
        self.portal.mutate(lambda pin: client.remove_blocked_app(key, guardian_pin=pin), self.reload)
