"""Month calendar and timezone-aware block/focus window editor."""
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from PyQt6.QtCore import QDate, QDateTime, QTime, QTimeZone, QTimer, Qt
from PyQt6.QtGui import QColor, QFont, QTextCharFormat
from PyQt6.QtWidgets import (QComboBox, QDateTimeEdit,
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit, QScrollArea, QSplitter,
    QStackedWidget, QVBoxLayout, QWidget)
from sentinel.daemon import client
from .app_picker import AppPickerButton, app_icon, app_identity
from .models import MODE_LABELS, clock_time, month_range, overlaps_day, read_pages, wall_time
from .calendar_views import DayTimeline, MarkedCalendar
from .widgets import ToggleSwitch, button, description_box, label, page, setting_card


def local_zone():
    from PyQt6.QtCore import QTimeZone
    # Read the current OS setting rather than keeping a startup-only value.
    try:
        names = [str(Path('/etc/localtime').resolve().relative_to('/usr/share/zoneinfo'))]
    except (OSError, ValueError):
        names = []
    names.append(bytes(QTimeZone.systemTimeZoneId()).decode())
    for name in names:
        try:
            ZoneInfo(name)
            return name
        except (ValueError, KeyError):
            continue
    return 'UTC'


def qt_wall_time(value):
    """Keep editor fields as literal clock values; ZoneInfo owns DST conversion."""
    return QDateTime(QDate(value.date()), QTime(value.time()), QTimeZone.utc())


class WindowDialog(QDialog):
    def __init__(self, portal, day, policy, window=None):
        super().__init__(portal)
        self.portal = portal
        self.window = deepcopy(window)
        self.apps_data = deepcopy((window or {}).get('targets', {}).get('apps', []))
        self.setWindowTitle('Edit block / focus window' if window else 'Create block / focus window')
        self.resize(680, 700)
        root_layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        outer = QVBoxLayout(content)
        scroll.setWidget(content)
        root_layout.addWidget(scroll, 1)
        form = QFormLayout()
        outer.addLayout(form)
        self.name = QLineEdit((window or {}).get('name', 'Focus time'))
        self.name.setMaxLength(120)
        self.timezone = local_zone()
        self.start = QDateTimeEdit()
        self.end = QDateTimeEdit()
        zone = ZoneInfo(self.timezone)
        start = datetime.fromtimestamp(window['start_at'], zone) if window else datetime.combine(day, datetime.now(zone).time().replace(second=0, microsecond=0))
        end = datetime.fromtimestamp(window['end_at'], zone) if window else start + timedelta(hours=1)
        for field, value in ((self.start, start), (self.end, end)):
            field.setTimeZone(QTimeZone.utc())
            field.setDisplayFormat('ddd, dd MMM yyyy  hh:mm:ss AP')
            field.setCalendarPopup(True)
            font = QFont(field.font())
            font.setBold(True)
            field.setFont(font)
            style = QTextCharFormat()
            style.setForeground(QColor('#eef5ff'))
            style.setFontWeight(QFont.Weight.Bold)
            for weekday in Qt.DayOfWeek:
                field.calendarWidget().setWeekdayTextFormat(weekday, style)
            field.setDateTime(qt_wall_time(value))
        self.start_fold = QComboBox()
        self.end_fold = QComboBox()
        for field, value in ((self.start_fold, start), (self.end_fold, end)):
            field.addItems(['First occurrence', 'Second occurrence'])
            field.setCurrentIndex(value.fold)
        self.lock = QComboBox()
        allowed = policy['creation']['allowed_lock_types']
        if window and window['lock_type'] not in allowed:
            allowed = [window['lock_type'], *allowed]
        for mode in allowed:
            self.lock.addItem(MODE_LABELS[mode], mode)
        if window:
            self.lock.setCurrentIndex(self.lock.findData(window['lock_type']))
        self.enabled = ToggleSwitch('Enable this window')
        self.enabled.setChecked((window or {}).get('enabled', True))
        form.addRow('Name', self.name)
        self.block_type = QComboBox()
        self.block_type.addItem('Apps and websites', 'targets')
        self.block_type.addItem('Screen time — block the whole desktop', 'screen_time')
        self.block_type.setCurrentIndex(self.block_type.findData((window or {}).get('block_type', 'targets')))
        self.recurrence = QComboBox()
        for title, value in [('Does not repeat', 'none'), ('Daily', 'daily'), ('Weekly', 'weekly'), ('Monthly', 'monthly')]:
            self.recurrence.addItem(title, value)
        self.recurrence.setCurrentIndex(self.recurrence.findData((window or {}).get('recurrence', 'none')))
        form.addRow('Block', self.block_type)
        form.addRow('Repeat', self.recurrence)
        form.addRow('Starts', self.start)
        form.addRow('Starts during repeated DST hour', self.start_fold)
        form.addRow('Ends', self.end)
        form.addRow('Ends during repeated DST hour', self.end_fold)
        form.addRow('Commitment', self.lock)
        form.addRow(setting_card('Enable this window',
            'Apply this block during each scheduled occurrence.', self.enabled))
        self.domain_heading = label('Block websites (one domain per line)', True)
        outer.addWidget(self.domain_heading)
        self.domains = QPlainTextEdit('\n'.join((window or {}).get('targets', {}).get('domains', [])))
        self.domains.setPlaceholderText('youtube.com\nreddit.com')
        self.domains.setMaximumHeight(110)
        outer.addWidget(self.domains)
        self.app_heading = label('Apps to block', True)
        outer.addWidget(self.app_heading)
        self.apps = QListWidget()
        self.apps.setMaximumHeight(100)
        outer.addWidget(self.apps)
        row = QHBoxLayout()
        self.add_apps = AppPickerButton(self, lambda: self.apps_data)
        self.add_apps.app_selected.connect(self.add_app)
        row.addWidget(self.add_apps)
        self.remove = button('Remove selected app', self.remove_app)
        row.addWidget(self.remove)
        outer.addLayout(row)
        self.notes = QPlainTextEdit((window or {}).get('notes', ''))
        self.notes.setMaximumHeight(65)
        self.notes.setPlaceholderText('Notes (optional)')
        outer.addWidget(self.notes)
        self.message = description_box('Self-Locked windows can only be strengthened until they end. Website blocking uses Sentinel DNS.')
        self.screen_help = description_box('Screen time blocks cover every display. Self-Lock offers two minutes once per occurrence to disable this schedule in the calendar. Guardian bypass requires a PIN and applies only to the current occurrence. Monthly repeats use the last available day in shorter months.')
        outer.addWidget(self.screen_help)
        outer.addWidget(self.message)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        root_layout.addWidget(self.buttons)
        permissions = (window or {}).get('permissions', {})
        if permissions.get('self_lock_commitment_open') and not permissions.get('guardian_pin_required'):
            self.enabled.setEnabled(not window['enabled'])
            self.lock.setEnabled(False)
            self.remove.setEnabled(False)
            # Domain removals and time changes remain classified by the daemon.
        self.refresh_apps()
        self.block_type.currentIndexChanged.connect(self.update_block_type)
        self.update_block_type()
        self.saving = False
        self.initial_start = self.start.dateTime()
        self.initial_end = self.end.dateTime()
        self.initial_start_fold = self.start_fold.currentIndex()
        self.initial_end_fold = self.end_fold.currentIndex()
        self.time_form = form
        self.start.dateTimeChanged.connect(self.update_dst_fields)
        self.end.dateTimeChanged.connect(self.update_dst_fields)
        self.update_dst_fields()
        self.zone_timer = QTimer(self)
        self.zone_timer.setInterval(1000)
        self.zone_timer.timeout.connect(self.sync_timezone)
        self.zone_timer.start()
        self.finished.connect(self.zone_timer.stop)

    def update_block_type(self):
        screen_time = self.block_type.currentData() == 'screen_time'
        for widget in (self.domains, self.domain_heading, self.apps, self.app_heading, self.add_apps, self.remove):
            widget.setVisible(not screen_time)
        self.screen_help.setVisible(screen_time)

    def sync_timezone(self):
        zone = local_zone()
        if zone == self.timezone:
            return
        try:
            values = [wall_time(field.dateTime().toPyDateTime().replace(tzinfo=None), self.timezone, fold.currentIndex())
                for field, fold in ((self.start, self.start_fold), (self.end, self.end_fold))]
            initial = [wall_time(value.toPyDateTime().replace(tzinfo=None), self.timezone, fold)
                for value, fold in ((self.initial_start, self.initial_start_fold), (self.initial_end, self.initial_end_fold))]
        except (ValueError, KeyError) as exc:
            self.message.setText(str(exc))
            return
        self.timezone = zone
        for field, fold, instant in ((self.start, self.start_fold, values[0]), (self.end, self.end_fold, values[1])):
            value = datetime.fromtimestamp(instant, ZoneInfo(zone))
            field.blockSignals(True)
            field.setDateTime(qt_wall_time(value))
            fold.setCurrentIndex(value.fold)
            field.blockSignals(False)
        starts, ends = [datetime.fromtimestamp(value, ZoneInfo(zone)) for value in initial]
        self.initial_start, self.initial_end = [qt_wall_time(value) for value in (starts, ends)]
        self.initial_start_fold, self.initial_end_fold = starts.fold, ends.fold
        self.update_dst_fields()

    def update_dst_fields(self, *_):
        zone = self.timezone
        for time_field, fold_field in ((self.start, self.start_fold), (self.end, self.end_fold)):
            value = time_field.dateTime().toPyDateTime().replace(tzinfo=None)
            try:
                ambiguous = wall_time(value, zone, 0) != wall_time(value, zone, 1)
            except (ValueError, KeyError):
                ambiguous = False
            fold_field.setVisible(ambiguous)
            self.time_form.labelForField(fold_field).setVisible(ambiguous)

    def refresh_apps(self):
        self.apps.clear()
        for app in self.apps_data:
            self.apps.addItem(QListWidgetItem(app_icon(app), f"{app['name']} · {app.get('executable') or app.get('flatpak_id') or app.get('snap_instance')}"))

    def add_app(self, app):
        if any(x['key'] == app['key'] or app_identity(x) == app_identity(app) for x in self.apps_data):
            QMessageBox.warning(self, 'App', 'That app is already in this window.')
            return
        self.apps_data.append(deepcopy(app))
        self.refresh_apps()

    def remove_app(self):
        row = self.apps.currentRow()
        if row >= 0:
            self.apps_data.pop(row)
            self.refresh_apps()

    def payload(self):
        self.sync_timezone()
        zone = self.timezone
        # QDateTimeEdit is a wall-clock input. Never let Qt infer its offset.
        start = wall_time(self.start.dateTime().toPyDateTime().replace(tzinfo=None), zone, self.start_fold.currentIndex())
        end = wall_time(self.end.dateTime().toPyDateTime().replace(tzinfo=None), zone, self.end_fold.currentIndex())
        if self.window:
            if self.start.dateTime() == self.initial_start and self.start_fold.currentIndex() == self.initial_start_fold:
                start = self.window['start_at']
            if self.end.dateTime() == self.initial_end and self.end_fold.currentIndex() == self.initial_end_fold:
                end = self.window['end_at']
        if end <= start:
            raise ValueError('The end must be after the start.')
        domains = list(dict.fromkeys(x.strip() for x in self.domains.toPlainText().splitlines() if x.strip()))
        screen_time = self.block_type.currentData() == 'screen_time'
        if not self.name.text().strip() or (not screen_time and not (domains or self.apps_data)):
            raise ValueError('Enter a name and at least one app or website to block.')
        notes = self.notes.toPlainText()
        if len(notes) > 2000:
            raise ValueError('Notes must be at most 2,000 characters.')
        return dict(name=self.name.text().strip(), start_at=start, end_at=end,
                    timezone=self.window['timezone'] if self.window else zone,
                    lock_type=self.lock.currentData(), enabled=self.enabled.isChecked(),
                    apps=[] if screen_time else deepcopy(self.apps_data), domains=[] if screen_time else domains,
                    block_type=self.block_type.currentData(), recurrence=self.recurrence.currentData(), notes=notes)

    def save(self):
        try:
            params = self.payload()
        except (ValueError, KeyError) as exc:
            QMessageBox.warning(self, 'Schedule', str(exc))
            return
        if not self.window and params['lock_type'] == 'self_lock':
            if QMessageBox.question(self, 'Commit to this window',
                ('The overlay offers two minutes once per occurrence to disable this screen time schedule. Create it?'
                 if params['block_type'] == 'screen_time' else
                 'This window cannot be weakened or deleted before it ends. Recurring commitments remain locked between occurrences. Create it?'),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
        requires_pin = bool((self.window or {}).get('permissions', {}).get('guardian_pin_required'))
        pin = self.portal.pin(required=requires_pin)
        if pin is False:
            return
        self.saving = True
        self.buttons.setEnabled(False)
        def work():
            if self.window:
                return client.update_block_window(self.window['id'], params, guardian_pin=pin)
            return client.create_block_window(**params, guardian_pin=pin)
        def success(result):
            self.saving = False
            self.portal.saved(result)
            self.accept()
        def failure(message):
            self.saving = False
            self.buttons.setEnabled(True)
            self.message.setText(message)
        self.portal.runner.start(work, success, failure)

    def reject(self):
        if not self.saving:
            super().reject()


class CalendarPage(QWidget):
    def __init__(self, portal):
        super().__init__()
        self.portal = portal
        self.policy = None
        self.windows = []
        _, layout = page('Sentinel Calendar', 'Plan focused time with block windows. Right-click a day to add a window or delete an existing one. Times follow your computer’s timezone.')
        self.setLayout(layout)
        controls = QHBoxLayout()
        self.view = QComboBox()
        self.view.addItems(['Month', 'Day'])
        self.view.setAccessibleName('Calendar view')
        controls.addWidget(self.view)
        controls.addWidget(button('Previous', lambda: self.move_date(-1)))
        controls.addWidget(button('Next', lambda: self.move_date(1)))
        self.caption = label('')
        self.caption.setWordWrap(False)
        self.caption.setObjectName('settingTitle')
        controls.addWidget(self.caption)
        controls.addStretch()
        controls.addWidget(button('Today', self.today))
        controls.addWidget(button('New window', self.create, True))
        layout.addLayout(controls)
        splitter = QSplitter()
        self.calendar = MarkedCalendar(local_zone())
        self.views = QStackedWidget()
        self.views.addWidget(self.calendar)
        self.day_view = DayTimeline()
        self.day_scroll = QScrollArea()
        self.day_scroll.setWidgetResizable(True)
        self.day_scroll.setWidget(self.day_view)
        self.views.addWidget(self.day_scroll)
        splitter.addWidget(self.views)
        agenda = QWidget()
        agenda_layout = QVBoxLayout(agenda)
        self.day_label = label('')
        agenda_layout.addWidget(self.day_label)
        self.list = QListWidget()
        self.list.setObjectName('calendarAgenda')
        self.list.itemDoubleClicked.connect(lambda _: self.edit())
        agenda_layout.addWidget(self.list)
        row = QHBoxLayout()
        row.addWidget(button('Edit', self.edit))
        row.addWidget(button('Enable / disable', self.toggle))
        row.addWidget(button('Delete', self.delete))
        agenda_layout.addLayout(row)
        splitter.addWidget(agenda)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([750, 250])
        layout.addWidget(splitter, 1)
        self.details = description_box('Select a window to see its targets and commitment.')
        layout.addWidget(self.details)
        self.calendar.selectionChanged.connect(self.show_day)
        self.calendar.currentPageChanged.connect(lambda *_: self.reload())
        self.list.currentItemChanged.connect(lambda *_: self.show_details())
        self.view.currentIndexChanged.connect(self.change_view)
        for widget in (self.calendar, self.day_view):
            widget.contextRequested.connect(self.show_context_menu)
            widget.windowClicked.connect(self.select_window)
            widget.windowDoubleClicked.connect(self.edit_window)
        self.context_menu = None
        self.show_day()

    def change_view(self, index):
        self.views.setCurrentIndex(index)
        self.show_day()
        if index == 1:
            # Start near the day's first window, or the morning when empty.
            first = min((b['start'] for b in self.day_view.blocks), default=self.day_view.start+8*3600)
            self.day_scroll.verticalScrollBar().setValue(max(0, int((first-self.day_view.start)/3600*self.day_view.hour_height)-24))

    def move_date(self, direction):
        date = self.calendar.selectedDate()
        self.calendar.setSelectedDate(date.addDays(direction) if self.view.currentIndex() else date.addMonths(direction))

    def today(self):
        self.calendar.setSelectedDate(QDate(datetime.now(ZoneInfo(local_zone())).date()))
        self.reload()

    def reload(self):
        try:
            zone = local_zone()
            start, end = month_range(self.calendar.yearShown(), self.calendar.monthShown(), zone)
            # Include adjoining month cells and windows crossing month boundaries.
            start = (datetime.fromtimestamp(start, ZoneInfo(zone))-timedelta(days=7)).timestamp()
            end = (datetime.fromtimestamp(end, ZoneInfo(zone))+timedelta(days=7)).timestamp()
        except (ValueError, KeyError) as exc:
            self.portal.error(str(exc))
            return
        def work():
            policy = client.get_block_window_policy()
            # The protocol permits one full target-bearing window per page.
            windows = read_pages(lambda offset: client.list_block_windows(offset=offset, limit=25,
                range_start=start, range_end=end, include_targets=False), 'windows')
            return policy, windows, zone
        def loaded(result):
            self.policy, self.windows, zone = result
            self.calendar.windows = self.windows
            self.calendar.timezone = zone
            self.calendar.updateCells()
            self.show_day()
        self.refresh_timer = getattr(self, 'refresh_timer', None)
        if self.refresh_timer is None:
            self.refresh_timer = QTimer(self)
            self.refresh_timer.setInterval(5000)
            self.refresh_timer.timeout.connect(lambda: self.reload()
                if self.isVisible() and self.portal.connected and not self.portal.runner.busy else None)
            self.refresh_timer.start()
        self.portal.read(work, loaded)

    def show_day(self):
        selected = self.list.currentItem()
        previous_id = selected.data(Qt.ItemDataRole.UserRole)['id'] if selected else None
        date = self.calendar.selectedDate()
        self.caption.setText(date.toString('d MMM yyyy') if self.view.currentIndex()
                             else QDate(self.calendar.yearShown(), self.calendar.monthShown(), 1).toString('MMMM yyyy'))
        self.day_label.setText(date.toString('dddd, d MMMM yyyy'))
        self.day_label.setObjectName('settingTitle')
        self.day_view.render(date, self.calendar.timezone, self.windows)
        self.list.clear()
        for window in sorted(self.windows, key=lambda w: w['start_at']):
            if not overlaps_day(window, date.toPyDate(), self.calendar.timezone):
                continue
            zone = ZoneInfo(self.calendar.timezone)
            begins = datetime.fromtimestamp(window['start_at'], zone)
            ends = datetime.fromtimestamp(window['end_at'], zone)
            start = begins.strftime('%d %b ') + clock_time(begins)
            end = ends.strftime('%d %b ') + clock_time(ends)
            kind = 'Screen time' if window.get('block_type') == 'screen_time' else 'Apps / websites'
            repeat = window.get('recurrence', 'none')
            item = QListWidgetItem(f"{window['name']}\n{start} – {end}\n{kind} · {repeat if repeat != 'none' else 'once'}\n{window['state'].capitalize()} · {MODE_LABELS[window['lock_type']]}")
            item.setData(Qt.ItemDataRole.UserRole, window)
            self.list.addItem(item)
            if window['id'] == previous_id:
                self.list.setCurrentItem(item)
        if not self.list.count():
            item = QListWidgetItem('No block windows on this day.')
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(item)

    def select_window(self, window_id):
        for i in range(self.list.count()):
            item = self.list.item(i)
            window = item.data(Qt.ItemDataRole.UserRole)
            if window and window['id'] == window_id:
                self.list.setCurrentItem(item)
                return

    def edit_window(self, window_id):
        self.select_window(window_id)
        self.edit()

    def day_menu(self, day):
        menu = QMenu(self)
        add = menu.addAction('Add window')
        add.setEnabled(self.policy is not None)
        def create():
            self.calendar.setSelectedDate(day)
            self.create()
        add.triggered.connect(create)
        windows = sorted((w for w in self.windows if overlaps_day(w, day.toPyDate(), self.calendar.timezone)), key=lambda w: w['start_at'])
        if windows:
            menu.addSeparator()
            for window in windows:
                action = menu.addAction('Delete ' + window['name'])
                permissions = window['permissions']
                action.setEnabled(permissions['can_delete_without_pin'] or permissions['guardian_pin_required'])
                action.triggered.connect(lambda checked=False, w=window: self.delete_window(w))
        return menu

    def show_context_menu(self, day, position):
        if self.context_menu is not None:
            self.context_menu.close()
            self.context_menu.deleteLater()
        self.context_menu = self.day_menu(day)
        self.context_menu.popup(position)

    def selected(self):
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def show_details(self):
        window = self.selected()
        if window:
            targets = window['targets']
            if window.get('block_type') == 'screen_time':
                self.details.setText('Blocks the whole desktop. ' + ('You have two minutes to disable or delete this schedule.'
                    if window['permissions'].get('screen_time_disable_grace') else
                    'Self-Lock offers a two-minute extension; Guardian bypass requires a PIN.'))
                return
            self.details.setText(f"{targets['app_count']} apps · {targets['domain_count']} websites. "
                + ('Only strengthening changes are available until this window ends.'
                   if window['permissions']['self_lock_commitment_open'] else 'Edit to view and change targets.'))
        else:
            self.details.setText('Select a window to see its targets and commitment.')

    def create(self):
        if self.policy:
            self.open_dialog(None)

    def open_dialog(self, window):
        dialog = WindowDialog(self.portal, self.calendar.selectedDate().toPyDate(), self.policy, window)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            QTimer.singleShot(0, self.reload)
        dialog.deleteLater()

    def edit(self):
        window = self.selected()
        if window:
            self.portal.read(lambda: client.get_block_window(window['id']), lambda data: self.open_dialog(data['window']))

    def toggle(self):
        window = self.selected()
        if not window:
            return
        if window['enabled'] and not window['permissions']['can_weaken_without_pin'] and not window['permissions']['guardian_pin_required']:
            self.portal.error('This Self-Locked window cannot be disabled before it ends.')
            return
        self.portal.mutate(lambda pin: client.update_block_window(window['id'], {'enabled': not window['enabled']}, guardian_pin=pin),
            self.reload, requires_pin=window['permissions']['guardian_pin_required'])

    def delete(self):
        window = self.selected()
        if not window:
            return
        self.delete_window(window)

    def delete_window(self, window):
        if not window['permissions']['can_delete_without_pin'] and not window['permissions']['guardian_pin_required']:
            self.portal.error('This Self-Locked window cannot be deleted before it ends.')
            return
        if QMessageBox.question(self, 'Delete window', f"Delete {window['name']}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.portal.mutate(lambda pin: client.delete_block_window(window['id'], guardian_pin=pin),
                self.reload, requires_pin=window['permissions']['guardian_pin_required'])
