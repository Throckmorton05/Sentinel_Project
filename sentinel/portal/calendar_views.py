"""Rounded month cells and an elapsed-time day view of saved block windows."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from PyQt6.QtCore import QDate, QEvent, QPoint, QPointF, QRectF, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QTextCharFormat
from PyQt6.QtWidgets import QCalendarWidget, QTableView, QWidget
from .models import clock_time, overlaps_day


def day_range(day, timezone):
    zone = ZoneInfo(timezone)
    return (datetime.combine(day, time.min, zone).timestamp(),
            datetime.combine(day + timedelta(days=1), time.min, zone).timestamp())


def ordered_day_windows(windows, day, timezone):
    """Use the same chronological color order in the month and day views."""
    return sorted((window for window in windows if overlaps_day(window, day, timezone)),
                  key=lambda window: (window['start_at'], window.get('name', ''), window['id']))


def block_colors(window, index, *, month=False):
    """Cycle normal blocks blue/pink/green; screen time has its own style."""
    if window.get('block_type') == 'screen_time':
        return '#111c2e', '#78c8ff'
    if not window['enabled']:
        return '#30435d', None if month else '#456078'
    fills = ('#467da3' if month else '#295a82', '#8b4d73', '#3f7867')
    outlines = ('#78c8ff', '#efa5cb', '#95d2b6')
    return fills[index % 3], None if month else outlines[index % 3]


def day_blocks(windows, day, timezone):
    """Clip at midnight and place simultaneous windows in separate columns."""
    start, end = day_range(day, timezone)
    blocks = [{'window': window, 'start': max(start, window['start_at']),
               'end': min(end, window['end_at']), 'color_index': index}
              for index, window in enumerate(ordered_day_windows(windows, day, timezone))]
    blocks.sort(key=lambda block: (block['start'], block['end'], block['window']['id']))
    group, group_end = [], start

    def assign(entries):
        lanes = []
        for block in entries:
            lane = next((i for i, until in enumerate(lanes) if until <= block['start']), len(lanes))
            if lane == len(lanes):
                lanes.append(block['end'])
            else:
                lanes[lane] = block['end']
            block['lane'] = lane
        for block in entries:
            block['lanes'] = len(lanes)

    for block in blocks:
        if group and block['start'] >= group_end:
            assign(group)
            group = []
        group.append(block)
        group_end = max(group_end, block['end'])
    assign(group)
    return blocks


class ColumnGuides(QWidget):
    """Continuous column dividers over the weekday header and date rows."""
    def __init__(self, table):
        super().__init__(table.viewport())
        self.table = table
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(QPen(QColor('#456d93'), 1))
        columns = [i for i in range(self.table.model().columnCount()) if not self.table.isColumnHidden(i)]
        for column in columns[1:]:
            x = self.table.columnViewportPosition(column)+0.5
            painter.drawLine(QPointF(x, 0), QPointF(x, self.height()))


class MarkedCalendar(QCalendarWidget):
    contextRequested = pyqtSignal(QDate, QPoint)
    windowClicked = pyqtSignal(str)
    windowDoubleClicked = pyqtSignal(str)

    def __init__(self, timezone):
        super().__init__()
        self.setObjectName('windowCalendar')
        self.windows = []
        self.timezone = timezone
        self.cells = {}
        self.chips = {}
        self.wheel_delta = 0
        self.setGridVisible(False)
        self.setNavigationBarVisible(False)
        self.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.setMinimumSize(280, 240)
        style = QTextCharFormat()
        style.setForeground(QColor('#eef5ff'))
        style.setBackground(QColor('#111c2e'))
        style.setFontWeight(QFont.Weight.Bold)
        for weekday in Qt.DayOfWeek:
            self.setWeekdayTextFormat(weekday, style)
        self.currentPageChanged.connect(self.clear_cells)
        table = self.findChild(QTableView)
        self.table = table
        self.viewport = table.viewport()
        self.column_guides = ColumnGuides(table)
        table.horizontalHeader().sectionResized.connect(lambda *_: self.refresh_guides())
        self.refresh_guides()
        self.viewport.setMouseTracking(True)
        self.viewport.installEventFilter(self)
        self.refresh_month_rows()

    def clear_cells(self, *_):
        self.cells.clear()
        self.chips.clear()
        self.refresh_month_rows()

    def refresh_month_rows(self):
        first = QDate(self.yearShown(), self.monthShown(), 1)
        leading = (first.dayOfWeek()-self.firstDayOfWeek().value) % 7 or 7
        for row in range(1, self.table.model().rowCount()):
            start = (row-1)*7
            self.table.setRowHidden(row, start+7 <= leading or start >= leading+first.daysInMonth())

    def setFirstDayOfWeek(self, weekday):
        super().setFirstDayOfWeek(weekday)
        if hasattr(self, 'table'):
            self.clear_cells()

    def refresh_guides(self):
        self.column_guides.setGeometry(self.viewport.rect())
        self.column_guides.raise_()
        self.column_guides.update()

    def paintCell(self, painter, rect, date):
        if date.year() != self.yearShown() or date.month() != self.monthShown():
            return
        key = date.toJulianDay()
        self.cells[key] = QRectF(rect)
        self.chips[key] = []
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        cell = QRectF(rect).adjusted(3, 3, -3, -3)
        selected = date == self.selectedDate()
        painter.setPen(QPen(QColor('#67bfff' if selected else '#304d6e'), 1))
        painter.setBrush(QColor('#142238'))
        painter.drawRoundedRect(cell, 10, 10)
        font = QFont(self.font())
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor('#eef5ff'))
        painter.drawText(cell.adjusted(9, 5, -8, 0), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, str(date.day()))
        if date == QDate(datetime.now(ZoneInfo(self.timezone)).date()):
            painter.setPen(QPen(QColor('#78c8ff'), 2))
            painter.drawLine(int(cell.left()+9), int(cell.top()+25), int(cell.left()+28), int(cell.top()+25))
        items = ordered_day_windows(self.windows, date.toPyDate(), self.timezone)
        capacity = max(1, int((cell.height() - 31) // 23))
        font.setPixelSize(11)
        painter.setFont(font)
        zone = ZoneInfo(self.timezone)
        for i, window in enumerate(items[:capacity]):
            block = QRectF(cell.left()+5, cell.top()+29+i*23, cell.width()-10, 20)
            if block.bottom() > cell.bottom()-3:
                break
            fill, outline = block_colors(window, i, month=True)
            painter.setPen(QPen(QColor(outline), 1.5) if outline else Qt.PenStyle.NoPen)
            painter.setBrush(QColor(fill))
            painter.drawRoundedRect(block, 5, 5)
            painter.setPen(QColor('#eef5ff'))
            begins = datetime.fromtimestamp(window['start_at'], zone)
            title = window['name'] if begins.date() == date.toPyDate() else '↳ ' + window['name']
            suffix = f'+{len(items)-capacity}' if i == capacity-1 and len(items) > capacity else ''
            reserve = painter.fontMetrics().horizontalAdvance(suffix)+8 if suffix else 0
            text = painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, max(1, int(block.width()-8-reserve)))
            painter.drawText(block.adjusted(4, 0, -4-reserve, 0), Qt.AlignmentFlag.AlignVCenter, text)
            if suffix:
                painter.drawText(block.adjusted(4, 0, -4, 0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, suffix)
            self.chips[key].append((block, window))
        painter.restore()

    def eventFilter(self, watched, event):
        if watched is self.viewport:
            if event.type() == QEvent.Type.Wheel:
                self.wheel_delta += event.angleDelta().y() or event.pixelDelta().y()*3
                steps = int(self.wheel_delta/120)
                self.wheel_delta -= steps*120
                if steps:
                    month = QDate(self.yearShown(), self.monthShown(), 1).addMonths(-steps)
                    self.setSelectedDate(QDate(month.year(), month.month(),
                        min(self.selectedDate().day(), month.daysInMonth())))
                event.accept()
                return True
            if event.type() == QEvent.Type.Resize:
                self.refresh_guides()
                QTimer.singleShot(0, self.refresh_guides)
            if event.type() == QEvent.Type.ContextMenu:
                for key, rect in self.cells.items():
                    if rect.contains(QPointF(event.pos())):
                        self.contextRequested.emit(QDate.fromJulianDay(key), event.globalPos())
                        return True
                return True
            if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick, QEvent.Type.MouseButtonRelease):
                if not any(rect.contains(event.position()) for rect in self.cells.values()):
                    return True
            if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick) and event.button() == Qt.MouseButton.LeftButton:
                for key, chips in self.chips.items():
                    for rect, window in chips:
                        if rect.contains(event.position()):
                            self.setSelectedDate(QDate.fromJulianDay(key))
                            signal = self.windowDoubleClicked if event.type() == QEvent.Type.MouseButtonDblClick else self.windowClicked
                            signal.emit(window['id'])
                            return True
            if event.type() == QEvent.Type.MouseMove:
                names = []
                for key, rect in self.cells.items():
                    if rect.contains(event.position()):
                        day = QDate.fromJulianDay(key).toPyDate()
                        names = [w['name'] for w in self.windows if overlaps_day(w, day, self.timezone)]
                        break
                self.viewport.setToolTip('\n'.join(names))
        return super().eventFilter(watched, event)


class DayTimeline(QWidget):
    contextRequested = pyqtSignal(QDate, QPoint)
    windowClicked = pyqtSignal(str)
    windowDoubleClicked = pyqtSignal(str)
    hour_height = 56
    top = 24
    gutter = 126

    def __init__(self):
        super().__init__()
        self.day = QDate.currentDate()
        self.timezone = 'UTC'
        self.blocks = []
        self.rects = []
        self.setMinimumWidth(360)
        self.setMouseTracking(True)
        self.setAccessibleName('Day timeline')

    def render(self, day, timezone, windows):
        self.day, self.timezone = day, timezone
        self.start, self.end = day_range(day.toPyDate(), timezone)
        self.blocks = day_blocks(windows, day.toPyDate(), timezone)
        self.setMinimumWidth(max(360, self.gutter+120*max((b['lanes'] for b in self.blocks), default=1)))
        self.setFixedHeight(int((self.end-self.start)/3600*self.hour_height)+self.top+24)
        self.update()

    def block_rect(self, block):
        width = (self.width()-self.gutter-16)/block['lanes']
        return QRectF(self.gutter+block['lane']*width+3,
            self.top+(block['start']-self.start)/3600*self.hour_height+2,
            width-6, max(18, (block['end']-block['start'])/3600*self.hour_height-4))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont(self.font())
        font.setBold(True)
        painter.setFont(font)
        zone = ZoneInfo(self.timezone)
        hours = int((self.end-self.start)/3600)
        for hour in range(hours+1):
            y = self.top+hour*self.hour_height
            painter.setPen(QPen(QColor('#304d6e'), 1))
            painter.drawLine(self.gutter, y, self.width()-12, y)
            if hour < hours:
                value = datetime.fromtimestamp(self.start+hour*3600, zone)
                text = clock_time(value) + ' ' + value.tzname()
                painter.setPen(QColor('#eef5ff'))
                painter.drawText(QRectF(0, y-10, self.gutter-10, 20), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, text)
        self.rects = []
        for block in self.blocks:
            rect = self.block_rect(block)
            window = block['window']
            fill, outline = block_colors(window, block['color_index'])
            painter.setPen(QPen(QColor(outline), 1.5 if window.get('block_type') == 'screen_time' else 1))
            painter.setBrush(QColor(fill))
            painter.drawRoundedRect(rect, 9, 9)
            painter.setPen(QColor('#eef5ff'))
            title = painter.fontMetrics().elidedText(window['name'], Qt.TextElideMode.ElideRight, max(1, int(rect.width()-16)))
            painter.save()
            painter.setClipRect(rect.adjusted(5, 2, -5, -2))
            painter.drawText(rect.adjusted(8, 5, -8, 0), Qt.AlignmentFlag.AlignTop, title)
            if rect.height() >= 48:
                start = clock_time(datetime.fromtimestamp(window['start_at'], zone))
                end = clock_time(datetime.fromtimestamp(window['end_at'], zone))
                text = painter.fontMetrics().elidedText(start+' – '+end, Qt.TextElideMode.ElideRight, max(1, int(rect.width()-16)))
                painter.drawText(rect.adjusted(8, 28, -8, 0), Qt.AlignmentFlag.AlignTop, text)
            painter.restore()
            self.rects.append((rect, window))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            for rect, window in reversed(self.rects):
                if rect.contains(event.position()):
                    self.windowClicked.emit(window['id'])
                    return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            for rect, window in reversed(self.rects):
                if rect.contains(event.position()):
                    self.windowDoubleClicked.emit(window['id'])
                    return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        self.contextRequested.emit(self.day, event.globalPos())

    def mouseMoveEvent(self, event):
        self.setToolTip(next((w['name'] for rect, w in reversed(self.rects) if rect.contains(event.position())), ''))
        super().mouseMoveEvent(event)
