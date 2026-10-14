import re

from PyQt6.QtCore import QTime, QSize, QTimer
from PyQt6.QtGui import QValidator
from PyQt6.QtWidgets import (QComboBox, QFormLayout, QGroupBox,
                             QTimeEdit, QVBoxLayout, QWidget, QMessageBox, QSpinBox)
from sen.autostart import list_autostart_apps, list_managed_autostart_apps
from sentinel.daemon import client
from .operations import secret
from .app_picker import AppPickerButton, app_icon
from .access import EntryCard
from .widgets import ToggleSwitch, button, label, page, scroll, setting_card


class DailyDuration(QSpinBox):
    """A duration in HH:MM, including 24:00 without clock-time wrapping."""

    def textFromValue(self, minutes):
        return f'{minutes // 60:02d}:{minutes % 60:02d}'

    def valueFromText(self, text):
        if re.fullmatch(r'[0-9]{2}:[0-5][0-9]', text):
            hours, minutes = map(int, text.split(':'))
            return hours * 60 + minutes
        return self.value()

    def validate(self, text, position):
        state = QValidator.State.Invalid
        if re.fullmatch(r'[0-9]{0,2}(:[0-9]{0,2})?', text):
            state = QValidator.State.Intermediate
            if (re.fullmatch(r'[0-9]{2}:[0-5][0-9]', text)
                    and self.minimum() <= self.valueFromText(text) <= self.maximum()):
                state = QValidator.State.Acceptable
        return state, text, position


class SystemPage(QWidget):
    def __init__(self, portal):
        super().__init__()
        self.portal = portal
        self.loaded = False
        outer = QVBoxLayout(self)
        body, layout = page('System and desktop', 'Manage screen time, startup preferences, and system maintenance.', boxed_description=False)
        layout.setSpacing(22)
        outer.addWidget(scroll(body))
        self.status = label('')
        layout.addWidget(self.status)
        self.daily_loaded = False
        self.daily_data = {}
        daily = QGroupBox('Daily screen-time limit')
        daily_layout = QVBoxLayout(daily)
        daily_layout.setSpacing(18)
        daily_layout.addWidget(label('Set a total allowance for active desktop use each day. '
            'When it runs out, Sentinel shows the screen-time overlay until midnight. '
            'Idle, locked, suspended, and blocked time do not count.', True))
        self.daily_enabled = ToggleSwitch('Enable daily screen-time limit')
        daily_layout.addWidget(setting_card('Enable daily limit',
            'This allowance applies every day, independently of calendar block windows.', self.daily_enabled))
        self.daily_minutes = DailyDuration()
        self.daily_minutes.setRange(1, 1440)
        self.daily_minutes.setValue(120)
        self.daily_minutes.setAccessibleName('Daily screen-time allowance in hours and minutes')
        self.daily_minutes.setToolTip('Enter HH:MM, from 00:01 to 24:00.')
        daily_layout.addWidget(setting_card('Daily allowance',
            'Enter HH:MM. For example, 02:30 gives you two hours and thirty minutes.', self.daily_minutes))
        self.daily_status = label('Loading daily allowance…')
        daily_layout.addWidget(self.daily_status)
        daily_layout.addWidget(label('The overlay can turn this limit off; enable it and Save here to turn it back on. '
            'Guardian Lock requires a PIN to change or turn off the limit. '
            'Self-Lock lets you lower it; once reached, the overlay offers two minutes once per day '
            'to raise or disable this limit. Other protections stay locked.', True))
        self.daily_save = button('Save daily limit', self.save_daily_limit, True)
        self.daily_save.setEnabled(False)
        daily_layout.addWidget(self.daily_save)
        layout.addWidget(daily)
        self.admin_groups = []
        updates = QGroupBox('Automatic updates')
        update_layout = QVBoxLayout(updates)
        update_layout.setSpacing(18)
        update_layout.addWidget(label('Keep operating system and Sentinel packages up to date on a schedule. Save your changes below to apply the schedule and restart preference.', True))
        self.enabled = ToggleSwitch('Enable automatic updates')
        self.restart = ToggleSwitch('Restart automatically when required')
        update_layout.addWidget(setting_card('Enable automatic updates',
            'Install system updates at the cadence and time you choose.', self.enabled))
        update_layout.addWidget(setting_card('Restart automatically when required',
            'Allow scheduled updates to restart the computer when a reboot is needed. Save your work before the scheduled update time.', self.restart))
        schedule = setting_card('Update schedule', 'The update time uses your computer’s system timezone.')
        form = QFormLayout()
        form.setVerticalSpacing(14)
        self.cadence = QComboBox()
        self.cadence.addItems(['daily', 'weekly', 'biweekly', 'monthly'])
        self.time = QTimeEdit(QTime(3, 0))
        self.time.setDisplayFormat('HH:mm')
        form.addRow('Cadence', self.cadence)
        form.addRow('Time (system timezone)', self.time)
        schedule.layout().addLayout(form, 1)
        update_layout.addWidget(schedule)
        update_layout.addWidget(button('Save automatic updates', self.save_updates, True))
        layout.addWidget(updates)
        self.admin_groups.append(updates)
        login = QGroupBox('Automatic login')
        login_layout = QVBoxLayout(login)
        login_layout.setSpacing(18)
        self.autologin = ToggleSwitch('Automatic desktop login')
        login_layout.addWidget(setting_card('Automatic desktop login',
            'Sign in to your desktop account automatically when the computer starts. Enabling this requires your desktop account password when you save.', self.autologin))
        login_layout.addWidget(button('Save automatic login', self.save_login))
        layout.addWidget(login)
        self.admin_groups.append(login)
        autostart = QGroupBox('Autostart applications')
        autostart_layout = QVBoxLayout(autostart)
        autostart_layout.setSpacing(18)
        autostart_layout.addWidget(label('Choose an installed application to launch each time you sign in to the desktop. Add or remove its Sentinel autostart entry below.', True))
        self.autostart_app = None
        self.autostart_apps = []
        self.autostart_cards = {}
        self.autostart_choice = label('No application selected')
        self.autostart = AppPickerButton(self, lambda: self.autostart_apps, load_apps=list_autostart_apps, launcher_choices=True)
        self.autostart.setText('Choose app ▾')
        self.autostart.setAccessibleName('Choose autostart application')
        self.autostart.setIconSize(QSize(24, 24))
        self.autostart.app_selected.connect(self.select_autostart)
        application = setting_card('Autostart application', 'Search installed apps and choose one from the list.', self.autostart)
        autostart_layout.addWidget(application)
        autostart_layout.addWidget(self.autostart_choice)
        self.autostart_add = button('Add autostart', self.add_autostart)
        self.autostart_add.setEnabled(False)
        autostart_layout.addWidget(self.autostart_add)
        self.autostart_list = QWidget()
        self.autostart_list.setObjectName('settingText')
        self.autostart_list_layout = QVBoxLayout(self.autostart_list)
        self.autostart_list_layout.setContentsMargins(0, 0, 0, 0)
        self.autostart_list_layout.setSpacing(12)
        self.autostart_list.hide()
        autostart_layout.addWidget(self.autostart_list)
        layout.addWidget(autostart)
        self.admin_groups.append(autostart)
        maintenance = QGroupBox('Maintenance')
        maintenance_layout = QVBoxLayout(maintenance)
        maintenance_layout.setSpacing(18)
        maintenance_layout.addWidget(label('Run system maintenance or power actions when you need them. Update, restart, and shutdown actions ask for confirmation.', True))
        for title, detail, text, action in [
            ('Sentinel updates', 'Update the installed Sentinel packages from the signed release channel. Save your work before continuing.', 'Update Sentinel', 'sentinel_update'),
            ('System updates', 'Run a normal APT update for all installed packages, including Sentinel when a newer release is available.', 'Update system', 'update'),
            ('Restart', 'Restart the computer. Save your work before continuing.', 'Restart computer', 'restart'),
            ('Shut down', 'Turn off the computer. Save your work before continuing.', 'Shut down', 'shutdown')]:
            maintenance_layout.addWidget(setting_card(title, detail,
                button(text, lambda a=action: self.system_action(a))))
        maintenance_layout.addWidget(setting_card('Chrome policy',
            'Reapply Sentinel’s managed browser settings to native Chrome.',
            button('Apply Chrome policy', lambda: self.portal.mutate(
                lambda pin: client.sync_browser_policy(guardian_pin=pin), self.reload))))
        layout.addWidget(maintenance)
        layout.addWidget(button('Reload desktop status', self.reload))
        layout.addStretch()

    def render_mode(self, mode):
        for group in self.admin_groups:
            group.setEnabled(mode != 'self_lock')
            group.setToolTip('Desktop administration cannot change during permanent Self-Lock.' if mode == 'self_lock' else '')
        self.render_daily_permissions()

    def reload(self):
        def work():
            return client.request('desktop.status'), list_managed_autostart_apps(), client.get_daily_screen_time_limit()
        def loaded(result):
            data, apps, daily = result
            self.loaded = True
            updates = data['updates']
            self.enabled.setChecked(updates['enabled'])
            self.restart.setChecked(updates['auto_restart'])
            self.cadence.setCurrentText(updates['cadence'])
            self.time.setTime(QTime.fromString(updates['time'], 'HH:mm'))
            self.autologin.setChecked(data['autologin_enabled'])
            self.status.setText('Last successful automatic update: ' + (updates['last_success'] or 'Not yet run'))
            self.render_autostart(apps)
            self.portal.daily_limit_received(daily, reset_system=True)
        self.portal.read(work, loaded)

    def render_daily_limit(self, data, reset_controls=False):
        # Follow external changes (such as the overlay turning the limit off)
        # when controls are untouched, while preserving actual unsaved edits.
        toggle_untouched = (not self.daily_loaded or
            self.daily_enabled.isChecked() == self.daily_saved_controls[0])
        reset_controls = (reset_controls or not self.daily_loaded or
            (self.daily_enabled.isChecked(), self.daily_minutes.text(), self.daily_timezone)
            == self.daily_saved_controls)
        self.daily_loaded = True
        self.daily_data = data
        if reset_controls:
            self.daily_revision = data['revision']
            self.daily_timezone = data['timezone']
            self.daily_enabled.setChecked(data['enabled'])
            self.daily_minutes.setMaximum(1440)
            self.daily_minutes.setValue(data['minutes'])
            self.daily_saved_controls = (data['enabled'], self.daily_minutes.text(), data['timezone'])
        elif toggle_untouched:
            # An allowance draft must not hide an external on/off change.
            self.daily_enabled.setChecked(data['enabled'])
            self.daily_saved_controls = (data['enabled'], *self.daily_saved_controls[1:])
        def duration(seconds):
            minutes = int(seconds // 60)
            return f'{minutes // 60}h {minutes % 60}m'
        state = ('Two-minute extension active' if data['grace_until'] else
                 'Daily allowance used up' if data['blocking'] else
                 'Daily limit enabled' if data['enabled'] else 'Daily limit off')
        text = f"{state} · Used today: {duration(data['used_seconds'])}"
        if data['enabled']:
            text += f" · Remaining: {duration(data['remaining_seconds'])}"
        text += f"\nResets at midnight · {self.daily_timezone}"
        collection = data['collection']
        if collection.get('state') in ('waiting', 'unavailable', 'stale'):
            text += '\n' + collection.get('reason', 'Waiting for the desktop usage collector.')
        self.daily_status.setText(text)
        self.render_daily_permissions()

    def render_daily_permissions(self):
        if not self.daily_loaded:
            return
        weaken = (self.portal.mode != 'self_lock'
                  or self.daily_data['permissions']['can_weaken_without_pin'])
        self.daily_enabled.setEnabled(weaken or not self.daily_data['enabled'])
        maximum = 1440 if weaken or not self.daily_data['enabled'] else self.daily_data['minutes']
        if self.daily_minutes.maximum() != maximum:
            self.daily_minutes.setMaximum(maximum)
        self.daily_save.setEnabled(True)

    def refresh_daily_usage(self):
        self.portal.refresh_daily_status()

    def save_daily_limit(self):
        if not self.daily_loaded:
            return
        enabled, minutes = self.daily_enabled.isChecked(), self.daily_minutes.value()
        zone, revision = self.daily_timezone, self.daily_revision
        self.portal.mutate(lambda pin: client.set_daily_screen_time_limit(enabled, minutes, zone,
            expected_revision=revision, guardian_pin=pin),
            lambda: self.portal.read(client.get_daily_screen_time_limit,
                                    lambda data: self.portal.daily_limit_received(data, reset_system=True)))

    def reload_autostart(self):
        self.portal.read(list_managed_autostart_apps, self.render_autostart)

    def render_autostart(self, apps):
        self.autostart_apps = apps
        while self.autostart_list_layout.count():
            widget = self.autostart_list_layout.takeAt(0).widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self.autostart_cards.clear()
        for app in apps:
            desktop_id = app['desktop_id']
            card = EntryCard(app['name'], 'Launches when you sign in',
                lambda name=desktop_id: self.remove_autostart(name), icon=app_icon(app))
            card.setMinimumHeight(78)
            self.autostart_cards[desktop_id] = card
            self.autostart_list_layout.addWidget(card)
        self.autostart_list.setVisible(bool(apps))
        self.update_autostart_choice()

    def update_autostart_choice(self):
        already_added = bool(self.autostart_app and any(
            app['desktop_id'] == self.autostart_app['desktop_id'] for app in self.autostart_apps))
        self.autostart_add.setEnabled(self.autostart_app is not None and not already_added)
        if self.autostart_app:
            self.autostart_choice.setText('Selected: ' + self.autostart_app['name']
                + (' · Already added' if already_added else ''))

    def save_updates(self):
        if not self.loaded:
            return
        enabled, restart = self.enabled.isChecked(), self.restart.isChecked()
        cadence, time = self.cadence.currentText(), self.time.time().toString('HH:mm')
        def work(pin):
            first = client.request('updates.enable' if enabled else 'updates.disable',
                {'cadence': cadence, 'time': time} if enabled else {}, guardian_pin=pin, timeout=30)
            try:
                return client.request('updates.restart_enable' if restart else 'updates.restart_disable', guardian_pin=pin, timeout=30)
            except client.SentineldError as exc:
                raise client.SentineldError('Update schedule saved, but restart preference failed: ' + str(exc)) from exc
        self.portal.mutate(work, self.reload)

    def save_login(self):
        if not self.loaded:
            return
        enabled = self.autologin.isChecked()
        password = secret(self.portal, 'Automatic desktop login', 'Desktop account password') if enabled else None
        if enabled and password is None:
            return
        self.portal.mutate(lambda pin: client.request('autologin.enable' if enabled else 'autologin.disable',
            {'password': password} if enabled else {}, guardian_pin=pin, timeout=30), self.reload)

    def select_autostart(self, app):
        self.autostart_app = app
        self.autostart.setIcon(app_icon(app))
        self.update_autostart_choice()

    def add_autostart(self):
        if self.autostart_app and self.autostart_add.isEnabled():
            name = self.autostart_app['desktop_id']
            self.portal.mutate(lambda pin: client.request('autostart.add', {'name': name}, guardian_pin=pin, timeout=30),
                self.reload_autostart)

    def remove_autostart(self, name):
        self.portal.mutate(lambda pin: client.request('autostart.remove', {'name': name}, guardian_pin=pin, timeout=30),
            self.reload_autostart)

    def system_action(self, action):
        if QMessageBox.question(self, 'System maintenance', {'update': 'Update operating system packages now?',
            'sentinel_update': 'Update Sentinel packages now? Save your work first. A restart may be needed.',
            'restart': 'Restart the computer now?', 'shutdown': 'Shut down the computer now?'}[action],
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            method = {'update': client.update_system, 'sentinel_update': client.update_sentinel,
                      'restart': client.restart_system, 'shutdown': client.shutdown_system}[action]
            outcome = {}
            def work(pin):
                result = method(guardian_pin=pin)
                outcome.update(result)
                return result
            def finished():
                if action in {'update', 'sentinel_update'}:
                    message = 'Sentinel update complete.' if action == 'sentinel_update' else 'System update complete.'
                    if outcome.get('reboot_required'):
                        message += ' Restart the computer to finish applying updates.'
                    self.portal.banner.setText(message)
            self.portal.mutate(work, finished)
            if self.portal.runner.busy and action in {'update', 'sentinel_update'}:
                self.portal.banner.setText('Updating Sentinel packages…' if action == 'sentinel_update'
                                           else 'Updating system packages…')
