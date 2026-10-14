from PyQt6.QtCore import QEvent, QTimer, Qt
from PyQt6.QtGui import QAction, QIcon, QKeySequence
from PyQt6.QtWidgets import (QApplication, QFrame, QGridLayout, QHBoxLayout, QListWidget, QMainWindow,
    QLineEdit, QStackedWidget, QVBoxLayout, QWidget, QMessageBox)
from sentinel.daemon import client
from .access import AccessPage
from .activity import ActivityPage
from .calendar import CalendarPage
from .daily_progress import DailyLimitProgress
from .guardian import GuardianPage
from .models import MODE_LABELS, enforcement_message
from .operations import Runner, secret
from .settings import LockPage, SettingsPage
from .system import SystemPage
from .theme import LOGO, STYLE
from .widgets import LockBadge, button, description_box, label, page, scroll


class OverviewPage(QWidget):
    def __init__(self, portal):
        super().__init__()
        self.portal = portal
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        body, layout = page('Your Sentinel', 'Your protection at a glance.', boxed_description=False)
        outer.addWidget(scroll(body), 1)
        layout.setSpacing(16)
        summary = QHBoxLayout()
        summary.setSpacing(16)
        self.summary, self.mode = label(''), label('Connecting…')
        self.mode_detail = label('', True)
        for heading, value, detail in (
            ('Protections enabled', self.summary, label('Choose your protections in Settings.', True)),
            ('Lock mode', self.mode, self.mode_detail),
        ):
            card = QFrame()
            card.setObjectName('overviewCard')
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(18, 15, 18, 15)
            title = label(heading)
            title.setObjectName('overviewCardTitle')
            value.setObjectName('overviewValue')
            card_layout.addWidget(title)
            card_layout.addWidget(value)
            card_layout.addWidget(detail)
            summary.addWidget(card, 1)
        layout.addLayout(summary)
        self.daily_progress = DailyLimitProgress()
        layout.addWidget(self.daily_progress)
        heading = label('Protection status')
        heading.setObjectName('overviewSection')
        layout.addWidget(heading)
        self.health = QWidget()
        grid = QGridLayout(self.health)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(12)
        self.health_cards = {}
        for index, name in enumerate(('Image filtering', 'DNS filtering', 'App protection', 'Focus windows', 'Activity history')):
            card = QFrame()
            card.setObjectName('overviewCard')
            row = QVBoxLayout(card)
            row.setContentsMargins(18, 14, 18, 14)
            title = label(name)
            title.setObjectName('overviewCardTitle')
            value = label('Checking…')
            value.setObjectName('overviewStatusValue')
            row.addWidget(title)
            row.addWidget(value)
            grid.addWidget(card, index // 2, index % 2)
            self.health_cards[name] = value
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        layout.addWidget(self.health)
        layout.addWidget(description_box('Changes made from another Sentinel client are reflected automatically. '
            'Calendar windows can overlap; every active window contributes its blocked targets.'))
        layout.addStretch()
        actions = QWidget()
        actions_layout = QVBoxLayout(actions)
        actions_layout.setContentsMargins(24, 0, 24, 20)
        actions_layout.addWidget(button('Plan a focus window', lambda: portal.navigate('Calendar'), True))
        actions_layout.addWidget(button('Open protection settings', lambda: portal.navigate('Protections')))
        outer.addWidget(actions)

    def render(self):
        self.mode.setText({'unlocked': 'Unlocked', 'self_lock': 'Self Lock', 'guardian_lock': 'Guardian Lock'}[self.portal.mode])
        self.mode_detail.setText({
            'unlocked': 'Settings are yours to adjust.',
            'self_lock': 'Enabled protections stay on.',
            'guardian_lock': 'Changes need your guardian’s PIN.',
        }[self.portal.mode])
        values = self.portal.snapshot['settings']
        self.summary.setText(f"{sum(value is True for value in values.values())} / {len(values)}")

    def reload(self):
        def work():
            results = {}
            for name, method in [('Image filtering', client.get_image_filtering_status), ('DNS filtering', client.get_dns_status),
                                  ('App protection', client.get_appblock_status), ('Focus windows', client.get_effective_block_windows),
                                  ('Activity history', client.get_activity_status)]:
                try:
                    result = method()
                    if name == 'Focus windows':
                        text = f"{result.get('active_window_count', len(result.get('active_window_ids', [])))} active windows"
                    elif name == 'Activity history':
                        text = f"{result.get('retained_events', 0)} events recorded"
                    else:
                        application = result.get('application', {})
                        status = application.get('status', 'ready')
                        text = {'applied': 'Active', 'pending': 'Applying changes', 'failed': 'Needs attention',
                                'error': 'Needs attention', 'ready': 'Ready'}.get(status, status.capitalize())
                        if name == 'Image filtering':
                            if not result.get('enabled'):
                                text = 'Off' + (' · Stopping' if application.get('status') == 'pending' else '')
                            elif application.get('monitor', {}).get('healthy'):
                                text = 'On · Protecting your screen'
                                if application.get('status') == 'pending':
                                    text += ' · Starting protection'
                            else:
                                text = 'On · ' + (application.get('monitor', {}).get('waiting_reason') or 'Starting protection')
                        if name == 'DNS filtering' and result.get('settings', {}).get('enabled') is False:
                            text = ('Off · Focus window active' if application.get('scheduled_domain_count')
                                    else 'Off')
                        if application.get('error'):
                            text += ': ' + application['error']
                        if application.get('errors'):
                            text += ': ' + '; '.join(application['errors'])
                        if application.get('unresolved'):
                            text += ': ' + '; '.join(application['unresolved'].values())
                    results[name] = client.user_message(text)
                except client.SentineldError as exc:
                    results[name] = str(exc)
            return results
        def loaded(results):
            for name, text in results.items():
                self.health_cards[name].setText(text)
        self.portal.read(work, loaded)


class PortalWindow(QMainWindow):
    def __init__(self, autoload=True):
        super().__init__()
        self.setWindowTitle('Sentinel Portal')
        self.setWindowIcon(QIcon(str(LOGO)))
        self.setStyleSheet(STYLE)
        self.resize(1280, 840)
        self.setMinimumSize(960, 680)
        self.mode = 'unlocked'
        self.snapshot = {'settings': {}, 'revision': None}
        self.connected = False
        # Entry authorization belongs to this window only; never retain a PIN.
        self._guardian_authenticated = False
        self._login_required = False
        self.runner = Runner(self)
        self.runner.busy_changed.connect(self.busy_changed)
        # Status polling must not disable inputs, remove focus, or wait for
        # unrelated page reads. One snapshot keeps all three pages in sync.
        self.daily_runner = Runner(self)
        self.daily_snapshot = None
        root = QWidget()
        self.content = root
        self.entry_stack = QStackedWidget()
        self.setCentralWidget(self.entry_stack)
        self.entry_page = QWidget()
        entry_layout = QVBoxLayout(self.entry_page)
        entry_layout.addStretch()
        card = QFrame()
        card.setObjectName('card')
        card.setMaximumWidth(440)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(28, 28, 28, 28)
        entry_logo = label('')
        entry_logo.setPixmap(QIcon(str(LOGO)).pixmap(90, 64))
        entry_logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(entry_logo)
        entry_title = label('Sentinel Portal')
        entry_title.setObjectName('title')
        card_layout.addWidget(entry_title)
        self.entry_status = label('Connecting to Sentinel backend…', True)
        card_layout.addWidget(self.entry_status)
        self.login_pin = QLineEdit()
        self.login_pin.setEchoMode(QLineEdit.EchoMode.Password)
        self.login_pin.setMaxLength(32)
        self.login_pin.setPlaceholderText('Guardian PIN')
        self.login_pin.setAccessibleName('Guardian PIN')
        self.login_pin.returnPressed.connect(self.login)
        card_layout.addWidget(self.login_pin)
        self.login_button = button('Unlock Portal', self.login, True)
        card_layout.addWidget(self.login_button)
        self.retry_button = button('Retry connection', self.refresh_core)
        card_layout.addWidget(self.retry_button)
        self.recovery_button = button('Guardian recovery', self.open_guardian_recovery)
        card_layout.addWidget(self.recovery_button)
        card_layout.addWidget(button('Close', self.close))
        entry_layout.addWidget(card, 0, Qt.AlignmentFlag.AlignHCenter)
        entry_layout.addStretch()
        self.entry_stack.addWidget(self.entry_page)
        self.entry_stack.addWidget(root)
        self.show_entry('Connecting to Sentinel backend…')
        layout = QVBoxLayout(root)
        header = QHBoxLayout()
        logo = label('')
        logo.setPixmap(QIcon(str(LOGO)).pixmap(90, 64))
        header.addWidget(logo)
        title = label('Sentinel Portal')
        title.setObjectName('title')
        header.addWidget(title)
        header.addStretch()
        self.mode_label = LockBadge()
        header.addWidget(self.mode_label)
        layout.addLayout(header)
        self.banner = label('Connecting to Sentinel backend…', True)
        self.banner.setObjectName('portalStatus')
        self.banner.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        body = QHBoxLayout()
        layout.addLayout(body, 1)
        self.navigation = QListWidget()
        self.navigation.setObjectName('navigation')
        self.navigation.setFixedWidth(205)
        body.addWidget(self.navigation)
        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)
        self.pages = {
            'Overview': OverviewPage(self), 'Protections': SettingsPage(self),
            'Calendar': CalendarPage(self), 'Apps & websites': AccessPage(self),
            'Activity': ActivityPage(self), 'System & desktop': SystemPage(self),
            'Lock mode': LockPage(self), 'Self-Lock': LockPage(self, True),
            'Guardian': GuardianPage(self),
        }
        for widget in self.pages.values():
            self.stack.addWidget(widget)
        footer = QHBoxLayout()
        footer.addWidget(self.banner, 1)
        layout.addLayout(footer)
        self.navigation.currentTextChanged.connect(self.select_page)
        quit_action = QAction('Quit', self)
        quit_action.setShortcut(QKeySequence('Ctrl+Q'))
        quit_action.triggered.connect(self.close)
        self.addAction(quit_action)
        self.timer = QTimer(self)
        self.timer.setInterval(15000)
        self.timer.timeout.connect(lambda: self.refresh_core(silent=True))
        self.daily_timer = QTimer(self)
        self.daily_timer.setInterval(5000)
        self.daily_timer.timeout.connect(self.refresh_daily_status)
        self.daily_timer.start()
        if autoload:
            self.timer.start()
            QTimer.singleShot(0, self.refresh_core)
        self.busy_changed(False)

    def busy_changed(self, busy):
        self.navigation.setEnabled(not busy and self.connected)
        self.stack.setEnabled(not busy and self.connected)
        self.login_pin.setEnabled(not busy and self._login_required)
        self.login_button.setEnabled(not busy and self._login_required)
        self.retry_button.setEnabled(not busy)

    def show_entry(self, message, *, login=False, offline=False):
        self.entry_stack.setCurrentWidget(self.entry_page)
        if message is not None:
            self.entry_status.setText(message)
        if not self._login_required or not login:
            self.login_pin.clear()
        self._login_required = login
        self.login_pin.setVisible(login)
        self.login_button.setVisible(login)
        self.retry_button.setVisible(offline)
        self.recovery_button.setVisible(offline)
        if login:
            self.login_pin.setFocus()

    def login(self):
        if not self._login_required or self.runner.busy:
            return
        pin = self.login_pin.text()
        self.login_pin.clear()
        if not pin.isascii() or not pin.isdigit() or not 4 <= len(pin) <= 32:
            self.entry_status.setText('Enter a Guardian PIN containing 4–32 digits.')
            return
        self.entry_status.setText('Checking Guardian PIN…')
        def success(result):
            if result.get('authenticated') is not True:
                failure('Guardian authentication failed.')
                return
            self._guardian_authenticated = True
            self.entry_status.setText('Loading Portal…')
            QTimer.singleShot(0, self.refresh_core)
        def failure(message):
            self._guardian_authenticated = False
            # Only show the backend's safe error text, never the submitted PIN.
            self.entry_status.setText(client.user_message(message))
            self.login_pin.setFocus()
        self.runner.start(lambda: client.authenticate_guardian(guardian_pin=pin), success, failure)

    def refresh_daily_status(self):
        if self.connected and not self.daily_runner.busy:
            self.daily_runner.start(client.get_daily_screen_time_limit,
                                    self.daily_limit_received, self.daily_limit_failed)

    def daily_limit_received(self, data, reset_system=False):
        if self.daily_snapshot and data['revision'] < self.daily_snapshot['revision']:
            return
        self.daily_snapshot = data
        self.pages['System & desktop'].render_daily_limit(data, reset_controls=reset_system)
        for name in ('Overview', 'Activity'):
            self.pages[name].daily_progress.render(data)

    def daily_limit_failed(self, message):
        for name in ('Overview', 'Activity'):
            self.pages[name].daily_progress.unavailable(message)

    def changeEvent(self, event):
        super().changeEvent(event)
        if (event.type() == QEvent.Type.ActivationChange and self.isActiveWindow()
                and hasattr(self, 'daily_timer')):
            self.refresh_daily_status()

    def read(self, function, success, failure=None):
        if not self.connected:
            return
        if self.runner.busy:
            # Reads requested from a completion callback start after that job ends.
            QTimer.singleShot(25, lambda: self.read(function, success, failure))
            return
        self.runner.start(function, success, failure or self.error)

    def pin(self, required=False):
        if required or self.mode == 'guardian_lock':
            value = secret(self)
            return False if value is None else value
        return None

    def mutate(self, function, after=None, requires_pin=False):
        if not self.connected or self.runner.busy:
            return
        pin = self.pin(requires_pin)
        if pin is False:
            return
        self.banner.setText('Applying your change…')
        def success(result):
            self.saved(result)
            if after:
                QTimer.singleShot(0, (lambda: self.refresh_core(silent=True)) if after == self.refresh_core else after)
        def failure(message):
            self.error(message)
            # A rejected change may mean another client changed mode/revision.
            QTimer.singleShot(0, lambda: self.refresh_core(silent=True))
        self.runner.start(lambda: function(pin), success, failure)

    def saved(self, result):
        self.banner.setText(enforcement_message(result))

    def error(self, message):
        self.banner.setText(client.user_message(message))

    def refresh_core(self, silent=False):
        if self.runner.busy or QApplication.activeModalWidget():
            return
        if not silent:
            self.banner.setText('Loading current Sentinel settings…')
        authenticated = self._guardian_authenticated and self.mode == 'guardian_lock'
        def work():
            state = client.get_lock_state()
            if state.get('mode') not in MODE_LABELS:
                raise ValueError('Unsupported Sentinel lock mode. Update the Portal.')
            if state['mode'] == 'guardian_lock' and not authenticated:
                # No settings, schedules, activity or other page reads before login.
                return state, None, None
            return state, client.get_settings_schema(), client.get_settings_snapshot()
        def loaded(data):
            state, schema, snapshot = data
            old_mode, was_connected = self.mode, self.connected
            self.mode = state['mode']
            if self.mode != 'guardian_lock':
                self._guardian_authenticated = False
            if schema is None:
                self.connected = False
                self._guardian_authenticated = False
                self.show_entry(None if self._login_required else
                    'Guardian Lock is active. Enter your guardian’s PIN to open the Portal.', login=True)
                return
            self.snapshot = snapshot
            self.connected = True
            self._login_required = False
            self.login_pin.clear()
            self.entry_stack.setCurrentWidget(self.content)
            self.recovery_button.hide()
            self.refresh_daily_status()
            self.mode_label.render(self.mode)
            self.pages['Protections'].render(schema, self.snapshot, self.mode)
            for name in ('Lock mode', 'Self-Lock'):
                self.pages[name].render(self.mode)
            self.pages['Overview'].render()
            self.pages['Apps & websites'].render_mode(self.mode)
            self.pages['System & desktop'].render_mode(self.mode)
            if old_mode != self.mode or not was_connected:
                self.pages['Guardian'].loaded = False
                self.rebuild_navigation()
            if not silent:
                self.banner.setText('Ready. Settings are up to date.')
            if was_connected and old_mode == self.mode and (not silent or self.stack.currentWidget() in (self.pages['Overview'], self.pages['Calendar'], self.pages['Apps & websites'])):
                QTimer.singleShot(0, self.reload_current)
        def failed(message):
            self.connected = False
            self._guardian_authenticated = False
            self.show_entry('Cannot connect to Sentinel backend.', offline=True)
            self.mode_label.unavailable()
            self.banner.setText('Cannot connect to Sentinel backend.')
        self.runner.start(work, loaded, failed)

    def rebuild_navigation(self):
        current = self.navigation.currentItem().text() if self.navigation.currentItem() else 'Overview'
        names = ['Overview', 'Protections', 'Calendar', 'Apps & websites', 'Activity', 'System & desktop', 'Lock mode']
        if self.mode == 'self_lock':
            names.append('Self-Lock')
        elif self.mode == 'guardian_lock':
            names.append('Guardian')
        self.navigation.blockSignals(True)
        self.navigation.clear()
        self.navigation.addItems(names)
        self.navigation.setCurrentRow(names.index(current) if current in names else 0)
        self.navigation.blockSignals(False)
        self.select_page(self.navigation.currentItem().text())

    def navigate(self, name):
        if not self.connected:
            return
        matches = self.navigation.findItems(name, Qt.MatchFlag.MatchExactly)
        if matches:
            self.navigation.setCurrentItem(matches[0])

    def select_page(self, name):
        if self.connected and name in self.pages:
            self.stack.setCurrentWidget(self.pages[name])
            QTimer.singleShot(0, self.reload_current)

    def reload_current(self):
        if not self.connected:
            return
        widget = self.stack.currentWidget()
        if widget is self.pages['Guardian'] and (widget.loaded or self.mode != 'guardian_lock'):
            return
        if hasattr(widget, 'reload'):
            widget.reload()

    def open_guardian_recovery(self):
        from sentinel.guardian_apps.manager import OfflineRecovery
        dialog = OfflineRecovery(self)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def closeEvent(self, event):
        if self.runner.busy or self.daily_runner.busy:
            self.banner.setText('Wait for the current operation to finish before closing the Portal.')
            if self.entry_stack.currentWidget() is self.entry_page:
                self.entry_status.setText('Wait for the current operation to finish before closing the Portal.')
            event.ignore()
            return
        guardian = self.pages['Guardian']
        if self.connected and guardian.dirty and QMessageBox.question(self, 'Unsaved guardian choices', 'Discard unsaved guardian choices and close?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            event.ignore()
            return
        self.timer.stop()
        self.daily_timer.stop()
        self.login_pin.clear()
        self._guardian_authenticated = False
        event.accept()
