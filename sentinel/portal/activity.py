from datetime import date as Date, datetime, timedelta
from zoneinfo import ZoneInfo
from PyQt6.QtCore import QDate, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QComboBox, QDateEdit, QHBoxLayout, QLineEdit, QTabWidget, QVBoxLayout, QWidget
from sentinel.daemon import client
from .calendar import local_zone
from .daily_progress import DailyLimitProgress
from .widgets import button, fill_table, label, page, scroll, subtitle, table


def duration(seconds):
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours}h {minutes:02d}m' if hours else f'{minutes}m' if minutes else f'{int(seconds)}s'


class ScreenTimeChart(QWidget):
    def __init__(self):
        super().__init__()
        self.days = []
        self.setMinimumHeight(220)
        self.setAccessibleName('Daily screen time, Sunday through Saturday')

    def render(self, days):
        self.days = days
        self.setToolTip('\n'.join(f"{day['date']}: {duration(day['seconds'])}" for day in days))
        self.setAccessibleDescription(self.toolTip())
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor('#111c2e'))
        if not self.days:
            painter.setPen(QColor('#a6bbd5'))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, 'Loading weekly screen time…')
            return
        width = self.width() / 7
        plot_height = self.height() - 72
        maximum = max(3600, max(day['seconds'] for day in self.days))
        for index, day in enumerate(self.days):
            x = index * width
            height = day['seconds'] / maximum * plot_height
            painter.setPen(QColor('#a6bbd5'))
            painter.drawText(QRectF(x, self.height()-28, width, 24), Qt.AlignmentFlag.AlignCenter,
                             Date.fromisoformat(day['date']).strftime('%a'))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor('#67bfff'))
            painter.drawRoundedRect(QRectF(x+width*.25, self.height()-38-height, width*.5, max(2, height)), 5, 5)
            painter.setPen(QColor('#eef5ff'))
            painter.drawText(QRectF(x, self.height()-64-height, width, 24), Qt.AlignmentFlag.AlignCenter,
                             duration(day['seconds']))
        painter.end()


class ActivityPage(QWidget):
    def __init__(self, portal):
        super().__init__()
        self.portal = portal
        self.requests = {"search": 0, "block": 0}
        self.follow_today = True
        self.search_cursor = self.block_cursor = None
        self.search_events, self.block_events = [], []
        _, layout = page('Activity', 'Weekly screen time, search history, and Sentinel blocks.')
        self.setLayout(layout)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        stats = QWidget()
        content = QVBoxLayout(stats)
        week_row = QHBoxLayout()
        week_row.addWidget(label('Week containing'))
        self.date = QDateEdit(QDate.currentDate())
        self.date.setCalendarPopup(True)
        week_row.addWidget(self.date)
        week_row.addWidget(button('This week', lambda: self.date.setDate(QDate.currentDate())))
        week_row.addWidget(button('Refresh', self.reload))
        week_row.addStretch()
        content.addLayout(week_row)
        self.summary = label('Loading weekly screen time…')
        content.addWidget(self.summary)
        self.chart = ScreenTimeChart()
        content.addWidget(self.chart)
        self.daily_progress = DailyLimitProgress()
        content.addWidget(self.daily_progress)
        breakdown = QHBoxLayout()
        for name, heading in [('apps', 'Top 10 apps'), ('websites', 'Top 10 websites')]:
            column = QVBoxLayout()
            column.addWidget(subtitle(heading))
            widget = table(['App' if name == 'apps' else 'Website', 'Time'])
            widget.setMinimumHeight(200)
            widget.setMaximumHeight(340)
            setattr(self, name, widget)
            column.addWidget(widget)
            breakdown.addLayout(column)
        content.addLayout(breakdown)
        self.coverage = label('', True)
        content.addWidget(self.coverage)
        content.addWidget(subtitle('Search history'))
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('Find a word or phrase in search history…')
        self.search.setClearButtonEnabled(True)
        self.search.setMaxLength(512)
        self.search_scope = QComboBox()
        self.search_scope.addItems(['Last 7 days', 'Selected week'])
        row.addWidget(self.search, 1)
        row.addWidget(self.search_scope)
        content.addLayout(row)
        self.search_status = label('', True)
        content.addWidget(self.search_status)
        self.table = table(['Time', 'Engine', 'Search'])
        self.table.setMinimumHeight(220)
        content.addWidget(self.table)
        self.more = button('Load older searches', lambda: self.load_events('search', older=True))
        self.more.setEnabled(False)
        content.addWidget(self.more)
        self.tabs.addTab(scroll(stats), 'Stats && info')
        blocks = QWidget()
        blocks_layout = QVBoxLayout(blocks)
        blocks_layout.addWidget(subtitle('Block history'))
        filter_row = QHBoxLayout()
        self.block_kind = QComboBox()
        for title, value in [('All blocks', 'blocks'), ('Screen blocks', 'screen_block'),
                             ('Blocked searches', 'search_block'), ('Website blocks', 'website_block')]:
            self.block_kind.addItem(title, value)
        self.block_scope = QComboBox()
        self.block_scope.addItems(['Last 7 days', 'Selected week'])
        filter_row.addWidget(self.block_kind)
        filter_row.addWidget(self.block_scope)
        filter_row.addWidget(button('Refresh', lambda: self.load_events('block')))
        filter_row.addStretch()
        blocks_layout.addLayout(filter_row)
        self.block_status = label('', True)
        blocks_layout.addWidget(self.block_status)
        self.blocks = table(['Time', 'Type', 'Details', 'Reason'])
        blocks_layout.addWidget(self.blocks, 1)
        self.block_more = button('Load older blocks', lambda: self.load_events('block', older=True))
        self.block_more.setEnabled(False)
        blocks_layout.addWidget(self.block_more)
        self.tabs.addTab(blocks, 'Blocks')
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(350)
        self.debounce.timeout.connect(lambda: self.load_events('search'))
        self.search.textChanged.connect(self.search_changed)
        self.search_scope.currentIndexChanged.connect(lambda: self.load_events('search'))
        self.block_kind.currentIndexChanged.connect(lambda: self.load_events('block'))
        self.block_scope.currentIndexChanged.connect(lambda: self.load_events('block'))
        self.date.dateChanged.connect(self.select_week)
        self.refresh = QTimer(self)
        self.refresh.setInterval(30000)
        self.refresh.timeout.connect(self.auto_refresh)
        self.refresh.start()

    def select_week(self):
        self.follow_today = self.date.date() == QDate.currentDate()
        self.reload()

    def auto_refresh(self):
        if self.isVisible() and getattr(self.portal, 'connected', True) and not self.portal.runner.busy:
            if self.follow_today and self.date.date() != QDate.currentDate():
                self.date.setDate(QDate.currentDate())
                return
            if self.tabs.currentIndex() == 0:
                self.load_usage()
            else:
                self.load_events('block')

    def search_changed(self):
        self.requests["search"] += 1
        self.search_cursor = None
        self.more.setEnabled(False)
        self.debounce.start()

    def bounds(self, scope):
        if scope.currentIndex() == 0:
            return None, None
        zone = ZoneInfo(local_zone())
        selected = self.date.date().toPyDate()
        sunday = selected-timedelta(days=(selected.weekday()+1) % 7)
        start = datetime.combine(sunday, datetime.min.time(), zone)
        return start.isoformat(), (start+timedelta(days=7)).isoformat()

    def reload(self):
        if self.follow_today and self.date.date() != QDate.currentDate():
            self.date.setDate(QDate.currentDate())
            return
        self.load_usage()
        self.load_events('search')
        self.load_events('block')

    def load_usage(self):
        selected = self.date.date().toString('yyyy-MM-dd')
        def loaded(report):
            if self.date.date().toString('yyyy-MM-dd') != selected:
                return
            self.summary.setText(f"{report['week_start']} – {report['week_end']} · {duration(report['total_seconds'])} total screen time")
            self.chart.render(report['days'])
            fill_table(self.apps, [(item['label'], duration(item['seconds'])) for item in report['apps']])
            fill_table(self.websites, [(item['label'], duration(item['seconds'])) for item in report['websites']])
            state = report.get('collection', {}).get('state', 'waiting')
            state = {'active': 'Tracking', 'paused': 'Paused', 'waiting': 'Starting',
                     'unavailable': 'Unavailable', 'stale': 'Disconnected'}.get(state, state)
            message = ('Weeks reset on Sunday. Website time is included in browser time. '
                       f'Tracker: {state}.')
            if not report['total_seconds']:
                message += ' Screen-time collection begins after installing this update.'
            if report.get('retention_clipped'):
                message += ' Part of this week is outside the seven-day retention period.'
            if report.get('capacity_reached'):
                message += ' Some older usage was removed at the storage limit.'
            self.coverage.setText(message)
        self.portal.read(lambda: client.get_screen_time_report(date=selected, timezone=local_zone()), loaded)

    def load_events(self, target, older=False):
        search = target == 'search'
        scope = self.search_scope if search else self.block_scope
        start, end = self.bounds(scope)
        kind = 'search' if search else self.block_kind.currentData()
        query = self.search.text() if search else None
        cursor = (self.search_cursor if search else self.block_cursor) if older else None
        if older and cursor is None:
            return
        self.requests[target] += 1
        generation = self.requests[target]
        # Per-list identity also protects searches when another tab is loaded.
        identity = (start, end, kind, query)
        if search:
            self.search_identity = identity
        else:
            self.block_identity = identity
        def loaded(data):
            current_scope = self.search_scope if search else self.block_scope
            current = (*self.bounds(current_scope), 'search' if search else self.block_kind.currentData(),
                       self.search.text() if search else None)
            if current != identity or self.requests[target] != generation:
                return
            self.append(data, target, older)
        self.portal.read(lambda: client.list_activity_events(start=start, end=end, kind=kind,
            query=query, before_id=cursor, limit=20), loaded)

    def append(self, data, target='search', older=False):
        search = target == 'search'
        events = self.search_events if search else self.block_events
        if not older:
            events.clear()
        events.extend(data['events'])
        cursor = data.get('next_before_id')
        more = self.more if search else self.block_more
        if search:
            self.search_cursor = cursor
        else:
            self.block_cursor = cursor
        more.setEnabled(cursor is not None)
        rows = []
        names = {'screen_block': 'Screen block', 'search_block': 'Blocked search', 'website_block': 'Website block'}
        for event in events:
            details = event.get('details', {})
            value = datetime.fromisoformat(event['occurred_at'].replace('Z', '+00:00'))
            when = value.astimezone(ZoneInfo(local_zone())).strftime('%d %b %H:%M:%S')
            if search:
                rows.append((when, details.get('engine', ''), details.get('query', '')))
            else:
                description = details.get('query') or details.get('domain') or details.get('window_title') or details.get('app_id', '')
                if event['kind'] == 'screen_block' and details.get('app_id') and details.get('window_title'):
                    description = details['app_id'] + ' · ' + details['window_title']
                rows.append((when, names.get(event['kind'], event['kind']), description,
                             details.get('reason') or details.get('model', '')))
        fill_table(self.table if search else self.blocks, rows)
        status = self.search_status if search else self.block_status
        noun = ('matching search' if search else 'block') + ('' if len(events) == 1 else 'es' if search else 's')
        status.setText(f"{len(events)} {noun} loaded" +
                       (' · More available' if cursor else '') if events else
                       ('No matching searches.' if search else 'No blocks in this period.'))
