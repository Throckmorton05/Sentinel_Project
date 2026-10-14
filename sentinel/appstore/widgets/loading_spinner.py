"""Small loading wheel that animates only while visible."""
from PyQt6.QtCore import QRectF, QTimer, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget


class LoadingSpinner(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(52, 52)
        self.setAccessibleName("Loading applications")
        self.angle = 0
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self._advance)

    def _advance(self):
        self.angle = (self.angle + 12) % 360
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self.timer.start()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ring = QRectF(5, 5, self.width() - 10, self.height() - 10)
        pen = QPen(QColor("#345270"), 4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawEllipse(ring)
        pen.setColor(QColor("#67bfff"))
        painter.setPen(pen)
        painter.drawArc(ring, -self.angle * 16, 100 * 16)
