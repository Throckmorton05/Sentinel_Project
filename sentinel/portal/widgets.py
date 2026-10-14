from PyQt6.QtCore import Qt, QRectF, QPropertyAnimation, QEasingCurve, pyqtProperty
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (QLabel, QPushButton, QVBoxLayout, QWidget, QScrollArea, QFrame, QHBoxLayout,
                            QTableWidget, QAbstractItemView, QHeaderView, QTableWidgetItem,
                            QAbstractButton)


class LockBadge(QFrame):
    """A compact, noninteractive lock-mode indicator."""
    def __init__(self):
        super().__init__()
        self.setObjectName('lockBadge')
        self.setFixedHeight(40)
        row = QHBoxLayout(self)
        row.setContentsMargins(15, 0, 14, 0)
        row.setSpacing(12)
        self.title = label('Connecting…')
        self.title.setWordWrap(False)
        row.addWidget(self.title)
        self.dot = QLabel()
        self.dot.setFixedSize(9, 9)
        self.dot.setObjectName('lockDot')
        row.addWidget(self.dot)
        self.render(None)

    def render(self, mode):
        title, background, foreground, dot = {
            'unlocked': ('Unlocked', '#22334b', '#a6bbd5', '#0d1726'),
            'self_lock': ('Self Lock', '#111c2e', '#78c8ff', '#67bfff'),
            'guardian_lock': ('Guardian Lock', '#111c2e', '#78c8ff', '#65d49e'),
            None: ('Connecting…', '#22334b', '#a6bbd5', '#0d1726'),
        }[mode]
        self.setProperty('mode', mode or 'unavailable')
        self.title.setText(title)
        self.setAccessibleName('Lock mode: ' + title)
        border = '1px solid #78c8ff' if mode in ('self_lock', 'guardian_lock') else 'none'
        self.setStyleSheet(
            f'QFrame#lockBadge {{ background: {background}; border: {border}; border-radius: 20px; }}'
            f'QFrame#lockBadge QLabel {{ background: transparent; color: {foreground}; font-weight: 600; }}'
            f'QFrame#lockBadge QLabel#lockDot {{ background: {dot}; border-radius: 4px; }}')

    def unavailable(self):
        self.render(None)
        self.title.setText('Unavailable')
        self.setAccessibleName('Sentinel backend unavailable')


class ToggleSwitch(QAbstractButton):
    """Chunky blue switch with an animated rounded-square white thumb."""
    def __init__(self, text='', parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setText(text)
        self.setAccessibleName(text)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(78, 48)
        self._position = 0.0
        self.animation = QPropertyAnimation(self, b'position', self)
        self.animation.setDuration(230)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def setChecked(self, checked):
        super().setChecked(checked)
        # Snapshot updates block signals to avoid triggering a write. Keep the
        # painted thumb animated even when the toggled signal is suppressed.
        if self.signalsBlocked():
            self._animate(self.isChecked())

    @pyqtProperty(float)
    def position(self):
        return self._position

    @position.setter
    def position(self, value):
        self._position = value
        self.update()

    def _animate(self, checked):
        target = float(checked)
        # Repeated snapshots must not restart an animation already headed to
        # the authoritative value.
        if self.animation.state() == QPropertyAnimation.State.Running and self.animation.endValue() == target:
            return
        self.animation.stop()
        if not self.isVisible() or abs(self.position - target) < 0.001:
            self.position = target
            return
        self.animation.setStartValue(self.position)
        self.animation.setEndValue(target)
        self.animation.start()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.55)
        painter.setPen(Qt.PenStyle.NoPen)
        off, on = ((QColor('#345270'), QColor('#67bfff')) if self.isEnabled()
                   else (QColor('#3d4653'), QColor('#7c8693')))
        color = QColor(*(round(a + (b - a) * self.position) for a, b in
                         zip(off.getRgb()[:3], on.getRgb()[:3])))
        painter.setBrush(color)
        painter.drawRoundedRect(QRectF(2, 4, 74, 40), 11, 11)
        painter.setBrush(QColor('#ffffff'))
        painter.drawRoundedRect(QRectF(6 + 34 * self.position, 8, 32, 32), 8, 8)
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor('#a5dcff'), 1, Qt.PenStyle.DashLine))
            painter.drawRoundedRect(QRectF(0.5, 1.5, 77, 45), 12, 12)


def label(text, muted=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    if muted:
        widget.setObjectName('muted')
    return widget


def subtitle(text):
    widget = label(text)
    widget.setObjectName('subtitle')
    return widget


def button(text, callback, primary=False):
    widget = QPushButton(text)
    if primary:
        widget.setObjectName('primary')
    widget.clicked.connect(lambda _checked=False: callback())
    return widget


def setting_card(title, description, control=None):
    card = QFrame()
    card.setObjectName('settingCard')
    row = QHBoxLayout(card)
    row.setContentsMargins(18, 14, 18, 14)
    row.setSpacing(20)
    text = QWidget()
    text.setObjectName('settingText')
    text_layout = QVBoxLayout(text)
    text_layout.setContentsMargins(0, 0, 0, 0)
    text_layout.setSpacing(5)
    heading = label(title)
    heading.setObjectName('settingTitle')
    text_layout.addWidget(heading)
    if description:
        text_layout.addWidget(label(description, True))
    row.addWidget(text, 1)
    if control is not None:
        row.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
    return card


def description_box(text):
    widget = label(text, True)
    widget.setObjectName('descriptionBox')
    widget.setContentsMargins(18, 14, 18, 14)
    return widget


def page(title, description, *, boxed_description=True):
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(24, 20, 24, 20)
    heading = label(title)
    heading.setObjectName('title')
    layout.addWidget(heading)
    layout.addWidget(description_box(description) if boxed_description else label(description, True))
    return widget, layout


def scroll(widget):
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setWidget(widget)
    return area


def table(headers):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    widget.verticalHeader().hide()
    return widget


def fill_table(widget, rows):
    widget.setRowCount(len(rows))
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            widget.setItem(i, j, QTableWidgetItem(str(value)))
