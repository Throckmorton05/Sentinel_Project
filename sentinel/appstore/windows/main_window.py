from PyQt6.QtWidgets import (
    QAbstractScrollArea,
    QMainWindow,
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QListWidget,
    QLineEdit,
    QLabel,
    QScrollArea,
    QGridLayout,
    QSizePolicy,
    QStackedWidget,
    QPushButton,
)


from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QAction, QIcon, QKeySequence

from sentinel.daemon import client
from ..operations import OperationRunner, OperationThread, guardian_credentials
from ..models import AppEntry

from sentinel.settings.paths import APPSTORE_ICON

from ..app_loader import load_apps
from ..widgets.app_card import AppCard
from ..widgets.update_card import UpdateCard
from ..widgets.loading_spinner import LoadingSpinner
from ..flatpak_backend import get_installed_entries
from ..updates.update_manager import (
    UpdateCheckError,
    get_all_updates,
    update_flatpak_app,
    update_system_package,
    update_all,
)


APPS_PER_BATCH = 25
SCROLL_THRESHOLD = 300


def style_scrollbar(scroll_area: QAbstractScrollArea) -> None:
    scrollbar = scroll_area.verticalScrollBar()
    assert scrollbar is not None

    scrollbar.setStyleSheet("""
        QScrollBar:vertical {
            background: #111c2e;
            width: 14px;
            border: none;
        }

        QScrollBar::handle:vertical {
            background: #345270;
            border-radius: 6px;
            min-height: 30px;
        }

        QScrollBar::handle:vertical:hover {
            background: #35638d;
        }

        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical {
            height: 0px;
        }

        QScrollBar::add-page:vertical,
        QScrollBar::sub-page:vertical {
            background: none;
        }
    """)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sentinel AppStore")

        self.setWindowIcon(QIcon(str(APPSTORE_ICON)))

        self.resize(1500, 900)

        self.quit_action = QAction("Quit", self)
        self.quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        self.quit_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self.quit_action.triggered.connect(self.close)
        self.addAction(self.quit_action)

        self.installed_apps = set()
        self.installed_entries = []
        self.policy = None
        self.available_updates = []
        self.update_error = None
        self.read_thread = None
        self._content_ready = False
        self.operation_runner = OperationRunner(self)
        self.operation_runner.busy_changed.connect(self._operation_busy_changed)

        # ============================================================
        # Here we setup the basic GUI elements
        # ============================================================

        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        self.setStyleSheet("""
            QMainWindow {
                background-color: #172338;
            }

            QWidget {
                background-color: #172338;
                color: #eef5ff;
            }

            QToolTip {
                color: #eef5ff;
                background-color: #284c70;
                border: 1px solid #67bfff;
            }
        """)

        main_layout = QHBoxLayout()
        central_widget.setLayout(main_layout)

        sidebar_widget = QWidget()
        sidebar_widget.setMinimumWidth(150)
        sidebar_widget.setMaximumWidth(200)
        sidebar_layout = QVBoxLayout()

        sidebar_widget.setLayout(sidebar_layout)

        self.navigation_sidebar = QListWidget()
        self.navigation_sidebar.setMaximumHeight(180)

        self.navigation_sidebar.addItem("Store")
        self.navigation_sidebar.addItem("Installed Apps")
        self.navigation_sidebar.addItem("Updates")

        self.navigation_sidebar.setStyleSheet("""
            QListWidget {
                background-color: #111c2e;
                border: none;
                border-radius: 6px;
                font-size: 18px;
                padding: 8px;
            }

            QListWidget::item {
                padding: 12px;
                border-radius: 6px;
            }

            QListWidget::item:selected {
                background-color: #295a82;
                color: #eef5ff;
            }

            QListWidget::item:hover {
                background-color: #223e5b;
            }

            QListWidget::item:selected:hover {
                background-color: #295a82;
            }
        """)

        self.navigation_sidebar.setCurrentRow(0)

        self.pages = QStackedWidget()

        store_page = QWidget()
        store_layout = QVBoxLayout()
        store_page.setLayout(store_layout)

        self.category_sidebar = QListWidget()

        for label, category in (
            ("All", "all"),
            ("Browsers", "browsers"),
            ("Games", "gaming"),
            ("Emulation", "emulation"),
            ("Education", "education"),
            ("Development", "development"),
            ("Coding", "coding"),
            ("Music", "music"),
            ("Media", "media"),
            ("Utilities", "utilities"),
        ):
            self.category_sidebar.addItem(label)
            item = self.category_sidebar.item(self.category_sidebar.count() - 1)
            item.setData(Qt.ItemDataRole.UserRole, category)

        self.category_sidebar.setMinimumWidth(150)
        self.category_sidebar.setMaximumWidth(200)
        self.category_sidebar.setMinimumHeight(120)
        self.category_sidebar.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding,
        )
        self.category_sidebar.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.category_sidebar.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.category_sidebar.setStyleSheet("""
            QListWidget {
                background-color: #111c2e;
                border: none;
                border-radius: 6px;
                font-size: 18px;
                padding: 8px;
            }

            QListWidget::item {
                padding: 12px;
                border-radius: 6px;
            }

            QListWidget::item:selected {
                background-color: #295a82;
                color: #eef5ff;
            }

            QListWidget::item:hover {
                background-color: #223e5b;
            }

            QListWidget::item:selected:hover {
                background-color: #295a82;
            }
        """)
        style_scrollbar(self.category_sidebar)

        self.category_sidebar.setCurrentRow(0)

        self.category_sidebar.itemClicked.connect(
            lambda item: self.filter_by_category(item.text())
        )

        sidebar_layout.addWidget(self.category_sidebar, 1)
        sidebar_layout.addWidget(self.navigation_sidebar)

        self.search_bar = QLineEdit()
        self.search_bar.setStyleSheet("""
            QLineEdit {
                background-color: #111c2e;
                color: #eef5ff;
                border: 2px solid #345270;
                border-radius: 8px;
                padding: 10px;
                font-size: 16px;

                selection-background-color: #295a82;
                selection-color: #eef5ff;
            }

            QLineEdit:focus {
                border: 2px solid #78c8ff;
            }
        """)

        self.search_timer = QTimer()
        self.search_timer.setSingleShot(True)
        self.search_timer.timeout.connect(
            lambda: self.filter_apps(self.search_bar.text())
        )
        self.search_bar.textChanged.connect(self.schedule_search)
        self.search_bar.setPlaceholderText("Search applications...")
        store_layout.addWidget(self.search_bar)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        style_scrollbar(self.scroll_area)

        scroll_widget = QWidget()
        scroll_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )

        self.grid_layout = QGridLayout()
        self.grid_layout.setSpacing(20)
        self.grid_layout.setContentsMargins(12, 12, 12, 12)
        self.grid_layout.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignBottom
        )

        scroll_widget.setLayout(self.grid_layout)

        apps = []

        # ==============================================================================
        # Loading in the app cards:
        # ==============================================================================

        self.apps = apps
        self.filtered_apps = self.apps
        self.current_index = 0

        self.load_more_apps()

        self.scroll_area.setWidget(scroll_widget)
        scrollbar = self.scroll_area.verticalScrollBar()
        assert scrollbar is not None

        scrollbar.valueChanged.connect(self.check_scroll_position)

        self.store_main_area = QWidget()
        store_main_layout = QVBoxLayout()
        self.store_main_area.setLayout(store_main_layout)

        store_main_layout.addWidget(self.search_bar)
        store_main_layout.addWidget(self.scroll_area)

        store_layout.addWidget(self.store_main_area)

        self.pages.addWidget(store_page)

        installed_page = QWidget()

        installed_layout = QVBoxLayout()

        installed_page.setLayout(installed_layout)

        self.installed_scroll_area = QScrollArea()

        self.installed_scroll_area.setWidgetResizable(True)

        self.installed_scroll_area.setStyleSheet("""
            QScrollArea {
                background-color: #1d304a;
                border: none;
            }
        """)

        installed_scroll_widget = QWidget()

        self.installed_grid = QGridLayout()

        self.installed_grid.setSpacing(20)

        self.installed_grid.setContentsMargins(12, 12, 12, 12)

        self.installed_grid.setAlignment(Qt.AlignmentFlag.AlignTop)

        installed_scroll_widget.setLayout(self.installed_grid)

        self.installed_scroll_area.setWidget(installed_scroll_widget)

        installed_layout.addWidget(self.installed_scroll_area)

        self.pages.addWidget(installed_page)
        self.load_installed_page()

        updates_page = QWidget()
        updates_layout = QVBoxLayout()
        updates_page.setLayout(updates_layout)

        self.update_all_button = QPushButton("Update All")

        self.update_all_button.setFixedHeight(45)

        self.update_all_button.setStyleSheet("""
            QPushButton {
                background-color: #284c70;
                color: #eef5ff;
                border: none;
                border-radius: 6px;
                padding: 8px;
                font-weight: bold;
                font-size: 16px;
            }

            QPushButton:hover {
                background-color: #35638d;
            }

            QPushButton:pressed {
                background-color: #223b58;
            }
        """)

        self.update_all_button.clicked.connect(self.run_update_all)

        updates_layout.addWidget(self.update_all_button)

        self.updates_scroll = QScrollArea()
        self.updates_scroll.setWidgetResizable(True)
        style_scrollbar(self.updates_scroll)

        updates_widget = QWidget()

        self.updates_container = QVBoxLayout()
        self.updates_container.setAlignment(Qt.AlignmentFlag.AlignTop)

        updates_widget.setLayout(self.updates_container)
        self.updates_scroll.setWidget(updates_widget)
        updates_layout.addWidget(self.updates_scroll)

        self.pages.addWidget(updates_page)
        self.load_updates_page()

        main_layout.addWidget(sidebar_widget)
        self.content = QStackedWidget()
        self.loading_page = QWidget()
        loading_layout = QVBoxLayout(self.loading_page)
        loading_layout.addStretch()
        self.loading_spinner = LoadingSpinner()
        loading_layout.addWidget(self.loading_spinner, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.loading_label = QLabel("Loading applications…")
        self.loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_label.setWordWrap(True)
        loading_layout.addWidget(self.loading_label)
        self.retry_button = QPushButton("Try again")
        self.retry_button.clicked.connect(self.refresh_data)
        self.retry_button.hide()
        loading_layout.addWidget(self.retry_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        loading_layout.addStretch()
        self.content.addWidget(self.loading_page)
        self.content.addWidget(self.pages)
        main_layout.addWidget(self.content, 1)
        self.update_all_button.setEnabled(False)

        self.navigation_sidebar.currentRowChanged.connect(self.change_page)

        self.policy_timer = QTimer(self)
        self.policy_timer.setInterval(5000)
        self.policy_timer.timeout.connect(self.refresh_policy)
        self.policy_timer.start()
        QTimer.singleShot(0, self.refresh_data)

    def _operation_busy_changed(self, busy):
        if not busy:
            self.refresh_data()

    def _read_async(self, callback, on_success):
        if self.read_thread is not None or self.operation_runner.busy:
            return
        if not self._content_ready:
            self.loading_label.setText("Loading applications…")
            self.retry_button.hide()
            self.loading_spinner.show()
        self.read_thread = OperationThread(callback, self)
        self._read_success = on_success
        self.read_thread.finished.connect(self._read_complete)
        self.read_thread.start()

    def _read_complete(self):
        thread = self.read_thread
        callback = self._read_success
        self.read_thread = None
        self._read_success = None
        if thread.error is not None:
            self._apply_policy(None)
            if not self._content_ready:
                self.loading_spinner.hide()
                self.loading_label.setText("Unable to load the App Store.\n" + client.user_message(thread.error))
                self.retry_button.show()
        else:
            callback(thread.result)
        thread.deleteLater()

    def refresh_policy(self):
        self._read_async(client.get_appstore_policy, self._policy_received)

    def _policy_received(self, policy):
        changed = self.policy is None or self.policy["approved_only"] != policy["approved_only"]
        self._apply_policy(policy)
        if changed:
            self.refresh_data()

    def _apply_policy(self, policy):
        self.policy = policy
        for card in self.findChildren(AppCard) + self.findChildren(UpdateCard):
            card.set_policy(policy)
        self.update_all_button.setEnabled(policy is not None)

    def refresh_data(self):
        def read():
            policy = client.get_appstore_policy()
            apps = load_apps(policy["approved_only"])
            installed = get_installed_entries()
            try:
                updates, error = get_all_updates(), None
            except UpdateCheckError as exc:
                updates, error = [], str(exc)
            return policy, apps, installed, updates, error
        self._read_async(read, self._data_received)

    def _data_received(self, result):
        policy, self.apps, self.installed_entries, self.available_updates, self.update_error = result
        self.installed_apps = {entry["id"] for entry in self.installed_entries}
        self._apply_policy(policy)
        self.apply_filters()
        self.load_installed_page()
        self.refresh_updates_page()
        self._content_ready = True
        self.content.setCurrentWidget(self.pages)

    def closeEvent(self, event):
        # Keep Qt threads and their callbacks alive until requests complete.
        if self.operation_runner.busy or self.read_thread is not None:
            event.ignore()
        else:
            self.policy_timer.stop()
            super().closeEvent(event)

    # ======================================================
    # Helper functions for filrering apps:
    # ======================================================

    def filter_apps(self, text):

        self.apply_filters()

    def filter_by_category(self, category):

        self.navigation_sidebar.setCurrentRow(0)
        self.pages.setCurrentIndex(0)

        self.apply_filters()

    def apply_filters(self):

        search_text = self.search_bar.text().lower()

        current_item = self.category_sidebar.currentItem()

        if current_item:
            category = current_item.data(Qt.ItemDataRole.UserRole)
        else:
            category = "all"

        matching_apps = []

        for app in self.apps:
            app_name = app.name.lower()
            app_summary = app.summary.lower()
            app_categories = [c.lower() for c in app.categories]

            category_match = category == "all" or category in app_categories

            search_match = (
                search_text in app_name
                or search_text in app_summary
                or any(search_text in c for c in app_categories)
            )

            if category_match and search_match:
                matching_apps.append(app)

        self.filtered_apps = matching_apps

        self.reset_grid()

        self.current_index = 0

        self.load_more_apps()

        scrollbar = self.scroll_area.verticalScrollBar()

        if scrollbar is not None:
            scrollbar.setValue(0)

    def schedule_search(self):

        self.search_timer.start(200)

    # ========================================================
    # Helper function for making the app cards
    # ========================================================

    def build_cards(self, apps):

        cards = []

        for app in apps:
            card = AppCard(
                app,
                installed=app.installation_id in self.installed_apps,
                operation_runner=self.operation_runner,
                policy=self.policy,
            )
            card.install_finished.connect(self.refresh_install_state)

            cards.append(card)

        return cards

    def load_installed_page(self):

        catalog = {app.installation_id: app for app in self.apps}
        installed_entries = [
            catalog.get(entry["id"]) or AppEntry.from_installed(entry)
            for entry in self.installed_entries
        ]

        cards = self.build_cards(installed_entries)

        self.populate_installed_grid(cards)

    def populate_installed_grid(self, cards):

        for i in reversed(range(self.installed_grid.count())):
            item = self.installed_grid.itemAt(i)

            if item is None:
                continue

            widget = item.widget()

            if widget:
                widget.hide()
                self.installed_grid.removeWidget(widget)
                widget.deleteLater()

        row = 0
        column = 0

        for card in cards:
            self.installed_grid.addWidget(
                card,
                row,
                column,
            )

            column += 1

            if column > 2:
                column = 0
                row += 1

    def load_more_apps(self):

        if self.current_index >= len(self.filtered_apps):
            return

        start = self.current_index
        end = start + APPS_PER_BATCH

        apps_to_load = self.filtered_apps[start:end]

        cards = self.build_cards(apps_to_load)

        self.append_cards(cards)

        self.current_index = end

    def append_cards(self, cards):

        current_count = self.grid_layout.count()

        row = current_count // 3
        column = current_count % 3

        for card in cards:
            card.show()

            self.grid_layout.addWidget(
                card,
                row,
                column,
            )

            column += 1

            if column > 2:
                column = 0
                row += 1

    def check_scroll_position(self):

        scrollbar = self.scroll_area.verticalScrollBar()
        assert scrollbar is not None

        if scrollbar.value() > scrollbar.maximum() - SCROLL_THRESHOLD:
            self.load_more_apps()

    def reset_grid(self):

        for i in reversed(range(self.grid_layout.count())):
            item = self.grid_layout.itemAt(i)

            if item is None:
                continue

            widget = item.widget()

            if widget:
                widget.hide()
                self.grid_layout.removeWidget(widget)
                widget.deleteLater()

    def refresh_install_state(self):
        self.refresh_data()

    # ===============================================
    # Page functions:
    # ===============================================

    def change_page(self, index):

        self.pages.setCurrentIndex(index)

        if index != 0:
            self.category_sidebar.clearSelection()

    def load_updates_page(self):

        updates = self.available_updates
        if self.update_error:
            self.update_all_button.setVisible(False)
            label = QLabel(f"Unable to check for updates: {self.update_error}")
            label.setWordWrap(True)
            self.updates_container.addWidget(label)
            return
        if self.policy is None:
            self.update_all_button.setVisible(False)
            self.updates_container.addWidget(QLabel("Loading updates…"))
            return

        self.update_all_button.setVisible(bool(updates))

        if not updates:
            no_updates_label = QLabel("Your system is up to date.")

            no_updates_label.setAlignment(
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter
            )
            no_updates_label.setFixedSize(400, 100)

            no_updates_label.setStyleSheet("""
                QLabel {
                    font-size: 22px;
                    font-weight: bold;
                    color: #eef5ff;
                    background-color: #1d304a;
                    border-radius: 12px;
                    padding: 30px;
                }
            """)

            self.updates_container.addWidget(
                no_updates_label,
                alignment=Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter,
            )

            return

        for update in updates:
            if update["type"] == "Flatpak":
                callback = lambda guardian_pin=None, ref=update["id"]: update_flatpak_app(ref, guardian_pin=guardian_pin)

            else:
                callback = lambda guardian_pin=None, name=update["id"]: update_system_package(name, guardian_pin=guardian_pin)

            card = UpdateCard(
                name=update["name"],
                description=f"{update['type']} update available",
                update_callback=callback,
                operation_runner=self.operation_runner,
                policy=self.policy,
            )

            card.update_finished.connect(self.refresh_data)

            self.updates_container.addWidget(card)

    def run_update_all(self):
        if self.operation_runner.busy:
            return
        ok, pin = guardian_credentials(self, operation_runner=self.operation_runner)
        if not ok:
            return
        self.operation_runner.start(
            "Installing all updates", lambda: update_all(guardian_pin=pin),
            on_success=self.refresh_data,
        )

    def refresh_updates_page(self):

        while self.updates_container.count():
            item = self.updates_container.takeAt(0)

            if item is not None:
                widget = item.widget()

                if widget is not None:
                    widget.deleteLater()

        self.load_updates_page()

    def closeEvent(self, event):
        # The window owns QThreads. Never destroy them during a read, install,
        # update or Guardian prompt; let their normal completion handlers run.
        if self.operation_runner.busy:
            event.ignore()
            return
        self.policy_timer.stop()
        self.search_timer.stop()
        self.loading_spinner.timer.stop()
        super().closeEvent(event)
