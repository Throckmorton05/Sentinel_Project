"""Searchable installed-app dropdown shared by permanent and scheduled blocks."""
from copy import deepcopy
from functools import lru_cache
import os
from pathlib import Path
import shutil

from PyQt6.QtCore import QEvent, QPoint, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen
from PyQt6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QStyle, QVBoxLayout, QFrame)
from sentinel.daemon import client
from .models import read_pages
from .operations import Runner


def app_identity(app):
    kind = app.get('type')
    target = app.get({'native': 'executable', 'flatpak': 'flatpak_id', 'snap': 'snap_instance'}.get(kind, 'executable'))
    if kind == 'native' and target:
        resolved = shutil.which(target)
        if resolved or target.startswith('/'):
            target = str(Path(resolved or target).resolve())
    return kind, target


def app_value(app):
    """Only existing mutation fields leave the picker; icons are presentation."""
    field = {'native': 'executable', 'flatpak': 'flatpak_id', 'snap': 'snap_instance'}[app['type']]
    return {key: app[key] for key in ('key', 'name', 'type', field)}


@lru_cache(maxsize=1)
def configure_icon_paths():
    # Packaged PyQt can default to resource-only paths without KDE's platform
    # theme plugin. Include the installed desktop/Flatpak icon directories.
    home = Path.home()
    data_home = Path(os.environ.get('XDG_DATA_HOME', str(home / '.local/share')))
    data_dirs = [Path(p) for p in os.environ.get('XDG_DATA_DIRS', '/usr/local/share:/usr/share').split(':') if p.startswith('/')]
    shares = [data_home, *data_dirs, home / '.local/share/flatpak/exports/share',
              Path('/var/lib/flatpak/exports/share')]
    paths = [*QIcon.themeSearchPaths(), str(home / '.icons'), *(str(p / 'icons') for p in shares)]
    QIcon.setThemeSearchPaths(list(dict.fromkeys(paths)))
    QIcon.setFallbackSearchPaths(list(dict.fromkeys([
        *QIcon.fallbackSearchPaths(), *(str(p / 'pixmaps') for p in shares)])))
    if not QIcon.themeName():
        for theme in ('breeze', 'Adwaita', 'Humanity', 'hicolor'):
            if any((Path(p) / theme / 'index.theme').is_file() for p in paths):
                QIcon.setFallbackThemeName(theme)
                break


def app_icon(app):
    configure_icon_paths()
    icon_name = app.get('icon') or Path(app.get('executable') or app.get('flatpak_id') or app.get('snap_instance') or '').name
    icon = QIcon(icon_name) if icon_name.startswith('/') else QIcon.fromTheme(icon_name)
    if icon.isNull():
        icon = QIcon.fromTheme('application-x-executable')
    if icon.isNull():
        icon = QApplication.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
    return icon


class AppPickerPopup(QFrame):
    app_selected = pyqtSignal(object)

    def __init__(self, anchor, selected_apps, *, load_apps=None, launcher_choices=False):
        super().__init__(anchor, Qt.WindowType.Popup)
        self.anchor = anchor
        self.selected_apps = selected_apps
        self.load_apps = load_apps or (lambda: read_pages(
            lambda offset: client.list_installed_gui_apps(offset=offset), 'apps'))
        self.launcher_choices = launcher_choices
        self.entries = []
        self.identities = {}
        self.loading = False
        self.load_error = None
        self.runner = Runner(self)
        self.setObjectName('appPicker')
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAccessibleName('Installed apps')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)
        self.search = QLineEdit()
        self.search.setPlaceholderText('Search apps…')
        self.search.setAccessibleName('Search installed apps')
        self.search.setClearButtonEnabled(True)
        outer.addWidget(self.search)
        self.apps = QListWidget()
        self.apps.setObjectName('appPickerList')
        self.apps.setIconSize(QSize(32, 32))
        self.apps.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.apps.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.apps.setSpacing(3)
        self.apps.setAccessibleName('Installed GUI apps')
        outer.addWidget(self.apps, 1)
        footer = QHBoxLayout()
        self.status = QLabel('Loading installed apps…')
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setObjectName('muted')
        self.status.setWordWrap(True)
        footer.addWidget(self.status, 1)
        self.retry = QPushButton('Retry')
        self.retry.setVisible(False)
        self.retry.clicked.connect(self.reload)
        footer.addWidget(self.retry)
        outer.addLayout(footer)
        self.search.textChanged.connect(self.filter_apps)
        self.search.returnPressed.connect(self.choose_current)
        self.apps.itemClicked.connect(self.choose)
        self.apps.itemActivated.connect(self.choose)
        self.search.installEventFilter(self)
        self.apps.installEventFilter(self)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#456d93'), 1))
        painter.setBrush(QColor('#1d304a'))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 12, 12)

    def open(self):
        self.search.clear()
        self.filter_apps()
        screen = self.anchor.screen().availableGeometry()
        width = min(max(440, self.anchor.width()), screen.width() - 24)
        height = min(450, screen.height() - 32)
        self.resize(width, height)
        below = self.anchor.mapToGlobal(QPoint(0, self.anchor.height() + 6))
        above = self.anchor.mapToGlobal(QPoint(0, -height - 6))
        y = below.y() if below.y() + height <= screen.bottom() + 1 else above.y()
        self.move(max(screen.left() + 12, min(below.x(), screen.right() - width - 11)),
                  max(screen.top() + 12, min(y, screen.bottom() - height - 11)))
        self.show()
        self.search.setFocus()
        self.reload()

    def reload(self):
        if self.runner.busy:
            return
        self.retry.hide()
        self.loading = True
        self.load_error = None
        self.status.setText('Loading installed apps…')
        self.runner.start(self.load_apps, self.loaded, self.failed)

    def loaded(self, entries):
        self.loading = False
        self.load_error = None
        self.entries = entries
        self.identities = {app['key']: self.identity(app) for app in entries}
        self.retry.hide()
        self.filter_apps()

    def failed(self, message):
        self.loading = False
        self.load_error = 'Could not load apps: ' + message
        self.status.setText(self.load_error)
        self.retry.show()

    def identity(self, app):
        return ('desktop', app['desktop_id']) if self.launcher_choices else app_identity(app)

    def filter_apps(self, *_):
        selected = {self.identity(app) for app in self.selected_apps()}
        query = self.search.text().casefold().split()
        self.apps.clear()
        for app in self.entries:
            target = app.get('executable') or app.get('flatpak_id') or app.get('snap_instance') or ''
            haystack = f"{app['name']} {target} {app.get('desktop_id', '')}".casefold()
            if not all(word in haystack for word in query):
                continue
            identity = self.identities[app['key']]
            already = identity in selected and identity[1] is not None
            subtitle = 'Already added' if already else {'native': 'Native app', 'flatpak': 'Flatpak app', 'snap': 'Snap app'}.get(app['type'], 'App')
            if not app.get('selectable', False):
                subtitle += ' · Unavailable'
            item = QListWidgetItem(app_icon(app), f"{app['name']}\n{subtitle}")
            item.setSizeHint(QSize(0, 62))
            item.setData(Qt.ItemDataRole.UserRole, app)
            item.setToolTip(app.get('reason') or ('Already added to this list.' if already else target))
            if already or not app.get('selectable', False):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled & ~Qt.ItemFlag.ItemIsSelectable)
            self.apps.addItem(item)
        if self.load_error:
            status = self.load_error
        elif self.loading:
            status = 'Loading installed apps…'
        elif self.apps.count():
            status = f'{self.apps.count()} apps'
        else:
            status = 'No apps match your search.' if query else 'No GUI apps found.'
        self.status.setText(status)
        for index in range(self.apps.count()):
            if self.apps.item(index).flags() & Qt.ItemFlag.ItemIsEnabled:
                self.apps.setCurrentRow(index)
                break

    def choose(self, item):
        if not item or not item.flags() & Qt.ItemFlag.ItemIsEnabled:
            return
        app = item.data(Qt.ItemDataRole.UserRole)
        value = app if self.launcher_choices else app_value(app)
        # Recheck against the current editor/list, not just the rendered state.
        if self.identity(value) in {self.identity(app) for app in self.selected_apps()}:
            self.filter_apps()
            return
        self.hide()
        self.app_selected.emit(deepcopy(value))

    def choose_current(self):
        self.choose(self.apps.currentItem())

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self.hide()
                self.anchor.setFocus()
                return True
            if watched is self.search and event.key() == Qt.Key.Key_Down:
                self.apps.setFocus()
                return True
        return super().eventFilter(watched, event)


class AppPickerButton(QPushButton):
    app_selected = pyqtSignal(object)

    def __init__(self, parent, selected_apps, *, load_apps=None, launcher_choices=False):
        super().__init__('Add apps + ▾', parent)
        self.setAccessibleName('Add apps')
        self.picker = AppPickerPopup(self, selected_apps, load_apps=load_apps, launcher_choices=launcher_choices)
        self.picker.app_selected.connect(self.app_selected.emit)
        self.clicked.connect(self.picker.open)
