from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QGroupBox, QVBoxLayout, QWidget, QMessageBox
from sentinel.daemon import client
from .models import MODE_LABELS, setting_editable, setting_label
from .operations import secret
from .widgets import ToggleSwitch, button, description_box, label, page, scroll, setting_card, label


class SettingsPage(QWidget):
    def __init__(self, portal):
        super().__init__()
        self.portal = portal
        self.controls = {}
        self.cards = {}
        self.signature = None
        self.safe_steam_status = None
        self.health_timer = QTimer(self)
        self.health_timer.setInterval(5000)
        self.health_timer.timeout.connect(self.refresh_health)
        self.health_timer.start()
        outer = QVBoxLayout(self)
        body, self.layout = page('Protection settings', 'Choose your protections. Changes are saved immediately and follow your lock mode.')
        outer.addWidget(scroll(body))

    def render(self, schema, snapshot, mode):
        definitions = schema['settings']
        signature = repr(definitions)
        if signature != self.signature:
            self.signature = signature
            self.controls.clear()
            self.cards.clear()
            while self.layout.count() > 2:
                item = self.layout.takeAt(2)
                if item.widget():
                    item.widget().deleteLater()
            groups = {}
            # Keep device protections together regardless of schema sort order.
            priority = {'image_filtering': 0, 'boot_media_protection': 1, 'vm_protection': 2}
            for definition in sorted(definitions, key=lambda entry: priority.get(entry['name'], 3)):
                name = definition['name']
                group_name = ('Image filtering' if name == 'image_filtering' else
                              'DNS filtering' if name.startswith('dns.') else 'Device and browser protection')
                if group_name not in groups:
                    group = QGroupBox(group_name)
                    groups[group_name] = QVBoxLayout(group)
                    self.layout.addWidget(group)
                checkbox = ToggleSwitch(setting_label(name))
                checkbox.setToolTip(definition.get('description', ''))
                card = setting_card(setting_label(name), definition.get('description', ''), checkbox)
                if name == 'dns.enabled':
                    groups[group_name].insertWidget(0, card)
                else:
                    groups[group_name].addWidget(card)
                self.cards[name] = card
                if name == 'safe_steam':
                    self.safe_steam_status = label('Checking Steam and Chrome…', True)
                    groups[group_name].addWidget(self.safe_steam_status)
                if definition.get('type') == 'boolean':
                    checkbox.clicked.connect(lambda checked, n=name: self.change(n, checked))
                else:
                    checkbox.setToolTip('This setting type needs an updated Portal.')
                self.controls[name] = (checkbox, definition)
            self.layout.addStretch()
        dns_default = next((entry.get('default') for entry in definitions
                            if entry['name'] == 'dns.enabled'), True)
        dns_enabled = snapshot['settings'].get('dns.enabled', dns_default) is True
        for name, (checkbox, definition) in self.controls.items():
            value = snapshot['settings'].get(name, definition.get('default'))
            checkbox.blockSignals(True)
            checkbox.setChecked(value is True)
            checkbox.blockSignals(False)
            category_inactive = name.startswith('dns.') and name != 'dns.enabled' and not dns_enabled
            self.cards[name].setEnabled(not category_inactive)
            editable = definition.get('type') == 'boolean' and setting_editable(definition, value, mode)
            checkbox.setEnabled(editable and not category_inactive)
            checkbox.setToolTip(definition.get('description', '') if definition.get('type') == 'boolean'
                                else 'This setting type needs an updated Portal.')
            if category_inactive:
                checkbox.setToolTip('Turn on DNS filtering to change this category. Your selection is saved.')
            elif mode == 'self_lock' and not editable:
                checkbox.setToolTip('Self-Lock keeps this protection enabled permanently.')

    def reload(self):
        self.portal.read(client.get_browser_status, self.render_health)

    def refresh_health(self):
        if self.isVisible() and self.portal.connected and not self.portal.runner.busy:
            self.reload()

    def render_health(self, data):
        if self.safe_steam_status is None:
            return
        health = data.get('application', {}).get('safe_steam')
        if not health:
            self.safe_steam_status.setText('Steam and Chrome status is unavailable.')
            return
        texts = ([] if health.get('enabled') else
                 ['Safe Steam filtering is off. Extension installation and repair remain active.'])
        for key, title in [('chrome', 'Chrome'), ('steam', 'Steam')]:
            item = health.get(key, {})
            state = item.get('status', 'waiting')
            if state == 'applied':
                text = 'Installed and verified' if key == 'steam' else 'Managed installation ready'
                text += ' · Connected' if item.get('runtime') == 'observed' else ' · Waiting for a live extension check'
            elif state == 'waiting':
                text = item.get('error') or 'Waiting for the app'
            elif state == 'not_installed':
                text = 'Not installed · ' + (item.get('error') or 'Install native Steam with sen install steam.')
            else:
                text = item.get('error') or 'Preparing protection…'
            texts.append(title + ': ' + text)
        self.safe_steam_status.setText('\n'.join(texts))

    def change(self, name, checked):
        # Restore the authoritative value until sentineld commits successfully.
        control = self.controls[name][0]
        control.setChecked(self.portal.snapshot['settings'][name])
        revision = self.portal.snapshot['revision']
        self.portal.mutate(lambda pin: client.set_setting(name, checked, guardian_pin=pin,
            expected_revision=revision), self.portal.refresh_core)


class LockPage(QWidget):
    def __init__(self, portal, self_only=False):
        super().__init__()
        self.portal = portal
        outer = QVBoxLayout(self)
        body, layout = page('Self-Lock' if self_only else 'Lock mode',
            'Self-Lock is permanent. Enabled protections can be strengthened and cannot be weakened.'
            if self_only else 'Choose who can change Sentinel protection settings.')
        self.mode = label('Connecting…')
        layout.addWidget(self.mode)
        descriptions = (
            ('unlocked',
             'You control protection settings and can turn them on or off without a Guardian PIN. '
             'Calendar windows still follow their own commitment rules.',
             'Set up Sentinel, try settings, and choose the protections that fit your routine '
             'before committing to a lock mode.'),
            ('self_lock',
             'Only changes that maintain or strengthen protection are allowed. Once you enable '
             'a protection, you cannot turn it off. Self-Lock is permanent: it cannot be disabled '
             'or replaced by another lock mode.',
             'Make a lasting personal commitment when you want your protections to stay in place '
             'without relying on a guardian.'),
            ('guardian_lock',
             'Changes to Sentinel require the guardian\'s PIN, whether they strengthen or weaken '
             'protection. Opening the Portal also requires the guardian\'s PIN. Guardian Lock can be turned off '
             'with the guardian\'s PIN.',
             'Let someone you trust approve changes while keeping the option to adjust your '
             'protections together.'),
        )
        self.mode_sections = {}
        for mode, behavior, purpose in descriptions:
            if self_only and mode != 'self_lock':
                continue
            section = QGroupBox(MODE_LABELS[mode])
            section_layout = QVBoxLayout(section)
            section_layout.addWidget(label('What it does: ' + behavior))
            section_layout.addWidget(label('What it\'s for: ' + purpose, True))
            self.mode_sections[mode] = section
            layout.addWidget(section)
        if not self_only:
            layout.addWidget(description_box('Each time you enable Guardian Lock, create and confirm a private guardian PIN. '
                'You do not need the previous PIN. The new PIN also protects existing Guardian-owned schedules.'))
        self.info = label('')
        layout.addWidget(self.info)
        layout.addStretch()
        self.content = scroll(body)
        outer.addWidget(self.content, 1)
        self.actions = QWidget()
        actions_layout = QVBoxLayout(self.actions)
        actions_layout.setContentsMargins(24, 0, 24, 20)
        self.self_button = button('Enable permanent Self-Lock', self.enter_self, True)
        self.guardian_button = button('Enable Guardian Lock', self.enter_guardian)
        actions_layout.addWidget(self.self_button)
        actions_layout.addWidget(self.guardian_button)
        outer.addWidget(self.actions)

    def render(self, mode):
        self.mode.setText('Current mode: ' + MODE_LABELS[mode])
        self.self_button.setVisible(mode == 'unlocked')
        self.guardian_button.setVisible(mode == 'unlocked')
        self.actions.setVisible(mode == 'unlocked')
        self.info.setText('Use Protection settings to enable additional protections. Each enabled protection becomes permanent. '
            'Individual calendar windows retain their own commitment level.' if mode == 'self_lock' else
            'The guardian must exit Guardian Lock with their PIN before Self-Lock can be enabled.'
            if mode == 'guardian_lock' else '')

    def enter_self(self):
        if self.portal.mode != 'unlocked':
            self.portal.error('Self-Lock can only be enabled from Unlocked. Exit Guardian Lock first.')
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle('Permanent Self-Lock')
        dialog.setText('Self-Lock cannot be turned off. You will only be able to maintain or strengthen enabled protections. Continue?')
        dialog.setStandardButtons(QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes)
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if dialog.exec() == QMessageBox.StandardButton.Yes:
            self.portal.mutate(lambda pin: client.enter_self_lock(guardian_pin=pin), self.portal.refresh_core)

    def enter_guardian(self):
        self.portal.read(client.get_guardian_pin_status, self._guardian_status)

    def _guardian_status(self, status):
        if status['mode'] != 'unlocked':
            self.portal.error('Guardian Lock can only be enabled from Unlocked.')
            QTimer.singleShot(0, self.portal.refresh_core)
            return
        new = secret(self.portal, 'Set up Guardian Lock', 'Create a guardian PIN (4–32 digits)')
        if new is None:
            return
        repeated = secret(self.portal, 'Confirm guardian PIN', 'Repeat new guardian PIN')
        if repeated is None:
            return
        if repeated != new:
            message = 'The guardian PIN did not match your first entry. Please try again.'
            self.portal.error(message)
            QMessageBox.warning(self, 'Guardian PIN did not match', message)
            return
        self.portal.read(lambda: client.enter_guardian_lock(new_pin=new, pin_confirmation=repeated),
                         self._entered)

    def _entered(self, result):
        self.portal.saved(result)
        QTimer.singleShot(0, self.portal.refresh_core)
