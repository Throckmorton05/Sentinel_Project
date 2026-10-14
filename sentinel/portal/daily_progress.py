"""Today's allowance, shared by Overview and Activity."""
from PyQt6.QtWidgets import QProgressBar, QVBoxLayout, QWidget
from .widgets import label, subtitle


def hours_minutes(seconds):
    minutes = max(0, int(seconds // 60))
    return f'{minutes // 60:02d}:{minutes % 60:02d}'


class DailyLimitProgress(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.addWidget(subtitle("Today's screen-time allowance"))
        self.bar = QProgressBar()
        self.bar.setObjectName('dailyAllowance')
        self.bar.setAccessibleName("Today's daily screen-time allowance used")
        self.bar.setRange(0, 1000)
        self.bar.setMinimumHeight(24)
        layout.addWidget(self.bar)
        self.detail = label('', True)
        layout.addWidget(self.detail)
        self.hide()

    def render(self, data):
        self.setVisible(data['enabled'])
        if not data['enabled']:
            return
        used = max(0, data['used_seconds'])
        allowance = data['minutes'] * 60
        remaining = max(0, allowance - used)
        self.bar.setValue(min(1000, int(used * 1000 / allowance)))
        text = (f'{hours_minutes(used)} of {hours_minutes(allowance)} used'
                f' · {hours_minutes(remaining)} remaining')
        if used >= allowance:
            text += ' · Daily allowance reached'
        if data['grace_until']:
            text += ' · Two-minute extension active'
        collection = data['collection']
        if collection.get('state') in ('waiting', 'unavailable', 'stale'):
            text += '\n' + collection.get('reason', 'Waiting for the desktop usage collector.')
        self.detail.setText(text)
        self.bar.setAccessibleDescription(text)

    def unavailable(self, message):
        self.detail.setText('Daily allowance status unavailable: ' + message)
        self.bar.setAccessibleDescription(self.detail.text())
