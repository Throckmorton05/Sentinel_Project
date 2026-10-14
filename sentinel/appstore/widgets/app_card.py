from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QPushButton,
    QHBoxLayout,
    QMenu,
    QMessageBox,
)


from PyQt6.QtGui import QPixmap
from PyQt6.QtCore import Qt, pyqtSignal
from ..models import AppEntry


from ..backend import (
    install_app,
    uninstall_app,
    launch_app,
)


from ..operations import guardian_credentials


class AppCard(QWidget):
    install_finished = pyqtSignal()

    def __init__(self, app: AppEntry, installed: bool, *, operation_runner, policy=None):
        super().__init__()
        self.app = app
        self.operation_runner = operation_runner
        self.policy = policy
        self.setObjectName("AppCard")
        self.setMaximumWidth(600)
        self.setMaximumHeight(200)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        layout = QVBoxLayout()

        icon_label = QLabel()
        icon_label.setStyleSheet(" background-color: #1d304a;")

        if app.icon_path:
            pixmap = QPixmap(app.icon_path)

            icon_label.setPixmap(
                pixmap.scaled(
                    55,
                    55,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

        name_label = QLabel(app.name)
        name_label.setStyleSheet(
            "font-size: 16px; font-weight: bold; background-color: #1d304a;"
        )

        summary_label = QLabel(app.summary)
        summary_label.setStyleSheet("background-color: #1d304a;")
        summary_label.setWordWrap(True)

        self.install_button = QPushButton("Install")
        self.install_button.setMinimumWidth(300)

        button_layout = QHBoxLayout()

        if installed:
            self.install_button.setText("Installed")
            self.install_button.setEnabled(False)

            self.install_button.setStyleSheet("""
                QPushButton {
                background-color: #22334b;
                color: #8a9bb1;
                border: none;
                border-radius: 6px;
                padding: 8px;
                font-weight: bold;
        }
            """)

            self.menu_button = QPushButton("...")

            self.menu_button.setMinimumWidth(150)

            self.menu_button.setStyleSheet("""
                QPushButton {
                background-color: #284c70;
                color: #eef5ff;
                border: none;
                border-radius: 6px;
                padding: 8px;
                font-weight: bold;
                }

                QPushButton:hover {
                    background-color: #35638d;
                }
                QPushButton:pressed{
                    background-color: #223b58;
                }

            """)
            self.menu_button.clicked.connect(self.show_app_menu)

            button_layout.addWidget(self.install_button)
            button_layout.addStretch()
            button_layout.addWidget(self.menu_button)

        else:
            self.install_button.clicked.connect(self.handle_install)

            self.install_button.setStyleSheet("""
                QPushButton {
                    background-color: #284c70;
                    color: #eef5ff;
                    border: none;
                    border-radius: 6px;
                    padding: 8px;
                    font-weight: bold;
                }

                QPushButton:hover {
                    background-color: #35638d;
                }

                QPushButton:pressed {
                    background-color: #223b58;
                }
            """)

            button_layout.addWidget(self.install_button)

        layout.addWidget(name_label)
        layout.addWidget(icon_label)
        layout.addWidget(summary_label)
        layout.addLayout(button_layout)

        self.setLayout(layout)

        self.set_policy(policy)

        self.setStyleSheet("""
            #AppCard {
                border-radius: 6px;
                background-color: #1d304a;
                padding: 10px;
            }
        """)

    def set_policy(self, policy):
        self.policy = policy
        if self.install_button.text() == "Install":
            self.install_button.setEnabled(policy is not None)

    def handle_install(self):
        if self.operation_runner.busy:
            return
        ok, pin = guardian_credentials(self, operation_runner=self.operation_runner)
        if not ok:
            return
        self.install_button.setEnabled(False)
        self.operation_runner.start(
            "Installing application",
            lambda: install_app(self.app, guardian_pin=pin),
            on_success=self.install_finished.emit,
            on_done=lambda: self.install_button.setEnabled(self.install_button.text() == "Install"),
        )

    def handle_uninstall(self):
        if self.operation_runner.busy:
            return
        ok, pin = guardian_credentials(self, operation_runner=self.operation_runner, uninstall=True)
        if not ok:
            return
        self.operation_runner.start(
            "Removing application",
            lambda: uninstall_app(self.app, guardian_pin=pin),
            on_success=self.install_finished.emit,
        )

    def show_app_menu(self):

        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #1d304a;
                color: #eef5ff;
                border: 1px solid #456d93;
            }

            QMenu::item {
                padding: 8px 20px;
            }

            QMenu::item:selected {
                background-color: #295a82;
            }

            QMenu::item:disabled {
                color: #8a9bb1;
            }
        """)
        open_action = menu.addAction("Open")

        uninstall_action = menu.addAction("Uninstall")
        uninstall_action.setEnabled(self.policy is not None and self.policy["can_uninstall"])
        if self.policy is not None and not self.policy["can_uninstall"]:
            uninstall_action.setToolTip("Removing applications is disabled in Self Lock.")

        action = menu.exec(
            self.menu_button.mapToGlobal(self.menu_button.rect().bottomLeft())
        )

        if action == open_action:
            try:
                launch_app(self.app)
            except OSError as exc:
                QMessageBox.warning(self, "Unable to launch application", str(exc))

        elif action == uninstall_action:
            self.handle_uninstall()
