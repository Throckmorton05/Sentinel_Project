from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QPushButton,
    QHBoxLayout,
)

from PyQt6.QtCore import Qt, pyqtSignal


from ..operations import guardian_credentials


class UpdateCard(QWidget):
    update_finished = pyqtSignal()

    def __init__(
        self,
        name,
        description="",
        update_callback=None,
        *,
        operation_runner,
        policy=None,
    ):
        super().__init__()

        self.name = name
        self.description = description
        self.update_callback = update_callback

        self.operation_runner = operation_runner
        self.policy = policy

        self.setObjectName("UpdateCard")

        self.setMaximumWidth(500)

        self.setFixedHeight(150)

        self.setAttribute(
            Qt.WidgetAttribute.WA_StyledBackground,
            True,
        )

        layout = QVBoxLayout()

        name_label = QLabel(name)

        name_label.setStyleSheet("""
            background-color: transparent;
            color: #eef5ff;
            font-size: 16px;
            font-weight: bold;
        """)

        description_label = QLabel(description)

        description_label.setWordWrap(True)

        description_label.setStyleSheet("""
            background-color: transparent;
            color: #a6bbd5;
            font-size: 14px;
        """)

        self.update_button = QPushButton("Update")

        self.update_button.setMinimumWidth(300)

        self.update_button.clicked.connect(self.run_update)

        self.update_button.setStyleSheet("""
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

        button_layout = QHBoxLayout()

        button_layout.addWidget(self.update_button)

        layout.addWidget(name_label)

        layout.addWidget(description_label)

        layout.addStretch()

        layout.addLayout(button_layout)

        self.setLayout(layout)

        self.set_policy(policy)

        self.setStyleSheet("""
            #UpdateCard {
                border-radius: 6px;
                background-color: #1d304a;
                padding: 10px;
            }
        """)

    def set_policy(self, policy):
        self.policy = policy
        self.update_button.setEnabled(policy is not None)

    def run_update(self):
        if self.operation_runner.busy or self.update_callback is None:
            return
        ok, pin = guardian_credentials(self, operation_runner=self.operation_runner)
        if not ok:
            return
        self.update_button.setEnabled(False)
        self.operation_runner.start(
            "Installing update",
            lambda: self.update_callback(guardian_pin=pin),
            on_success=self.update_finished.emit,
            on_done=lambda: self.update_button.setEnabled(self.policy is not None),
        )
