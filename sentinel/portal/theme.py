from pathlib import Path

LOGO = Path(__file__).parent / 'assets' / 'Sentinel_Shield.svg'
STYLE = '''
QWidget { background: #172338; color: #eef5ff; font-size: 14px; }
QMainWindow, QDialog { background: #172338; }
QLabel { background: transparent; font-weight: 400; }
QLabel#title { font-size: 27px; font-weight: 600; color: #78c8ff; }
QLabel#subtitle, QLabel#muted { color: #a6bbd5; }
QLabel#subtitle { font-weight: 700; }
QFrame#card, QGroupBox { background: #1d304a; border: 1px solid #304d6e; border-radius: 12px; }
QGroupBox { margin-top: 18px; padding: 18px; font-weight: 700; }
QGroupBox::title { subcontrol-origin: margin; left: 14px; color: #78c8ff; }
QFrame#settingCard { background: #17283e; border: 1px solid #345270; border-radius: 16px; }
QLabel#descriptionBox { color: #a6bbd5; background: #17283e; border: 1px solid #345270; border-radius: 16px; }
QFrame#settingCard QLabel, QWidget#settingText { background: transparent; border: none; }
QLabel#settingTitle { font-size: 15px; font-weight: 700; }
QFrame#settingCard:disabled { background: #152236; border-color: #30435d; }
QFrame#settingCard QLabel:disabled { color: #8a9bb1; }
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QDateTimeEdit, QDateEdit, QTimeEdit,
QSpinBox, QListWidget, QTableWidget { background: #111c2e; border: 1px solid #345270;
                          border-radius: 7px; padding: 7px; selection-background-color: #295a82; }
QLineEdit:focus, QTextEdit:focus { border-color: #78c8ff; }
QSpinBox::up-button, QSpinBox::down-button { background: #67bfff; border: none; width: 18px; }
QProgressBar#dailyAllowance { background: #111c2e; border: 1px solid #345270;
                            border-radius: 7px; text-align: center; color: #eef5ff; }
QProgressBar#dailyAllowance::chunk { background: #295a82; border-radius: 6px; }
QPushButton { background: #284c70; border: 1px solid #38688f; border-radius: 7px; padding: 9px 15px; font-weight: 600; }
QPushButton:hover { background: #35638d; }
QPushButton#primary { background: #67bfff; color: #0d2037; border-color: #67bfff; font-weight: 600; }
QPushButton#primary:hover { background: #96d4ff; }
QPushButton:disabled { color: #8a9bb1; }
QPushButton:disabled { background: #22334b; border-color: #30435d; }
QListWidget::item { padding: 9px; border-radius: 5px; }
QListWidget::item:selected { background: #295a82; }
QListWidget#navigation { background: #111c2e; border: none; font-size: 16px; }
QTabWidget::pane { border: 1px solid #304d6e; border-radius: 8px; }
QTabBar::tab { background: #111c2e; color: #adc3db; padding: 12px 20px; }
QTabBar::tab:selected { background: #284c70; color: #78c8ff; }
QHeaderView::section { background: #223b58; padding: 8px; border: none; }
QTableWidget { gridline-color: #304d6e; }
QCalendarWidget QWidget { alternate-background-color: #223b58; }
QToolButton { font-weight: 600; }
QCalendarWidget QToolButton { color: #eef5ff; background: #284c70; padding: 7px; }
QCalendarWidget QAbstractItemView { background: #172338; selection-background-color: #295a82; }
QCalendarWidget#windowCalendar QAbstractItemView { background: #142238; alternate-background-color: #142238; selection-background-color: transparent; }
QListWidget#calendarAgenda { font-weight: 600; }
QScrollArea { border: none; }
QScrollBar:vertical { background: #111c2e; width: 12px; }
QScrollBar::handle:vertical { background: #345270; border-radius: 5px; min-height: 25px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: #111c2e; height: 12px; }
QScrollBar::handle:horizontal { background: #345270; border-radius: 5px; min-width: 25px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QToolTip { color: #eef5ff; background: #284c70; border: 1px solid #67bfff; }
QMenu { color: #eef5ff; background: #1d304a; border: 1px solid #456d93; padding: 6px; }
QMenu::item { padding: 9px 18px; }
QMenu::item:selected { background: #295a82; border-radius: 6px; }
QMenu::item:disabled { color: #8a9bb1; }
QFrame#appPicker { background: #1d304a; border: 1px solid #456d93; border-radius: 12px; }
QFrame#appPicker QLabel { background: transparent; border: none; }
QFrame#accessCard { background: #1d304a; border: 1px solid #345270; border-radius: 14px; }
QFrame#accessCard QLabel { background: transparent; border: none; }
QLabel#entryTitle { font-size: 15px; font-weight: 700; }
QFrame#overviewCard { background: #1d304a; border: 1px solid #345270; border-radius: 14px; }
QFrame#overviewCard QLabel { background: transparent; border: none; }
QLabel#overviewCardTitle { color: #78c8ff; font-size: 14px; font-weight: 700; }
QLabel#overviewValue { color: #eef5ff; font-size: 23px; font-weight: 600; }
QLabel#overviewStatusValue { color: #eef5ff; font-size: 16px; font-weight: 600; }
QLabel#overviewSection { color: #78c8ff; font-size: 17px; font-weight: 700; }
QLabel#portalStatus { color: #a6bbd5; font-size: 12px; padding: 5px 4px; }
QPushButton#entryDelete { border-radius: 9px; padding: 8px 14px; }
QListWidget#accessList::item { padding: 0; border: none; background: transparent; }
QListWidget#appPickerList { background: #111c2e; border: none; padding: 4px; }
QListWidget#appPickerList::item { padding: 6px 10px; border-radius: 8px; }
QListWidget#appPickerList::item:hover { background: #223e5b; }
QListWidget#appPickerList::item:selected { background: #295a82; }
QListWidget#appPickerList::item:disabled { color: #8a9bb1; }
'''
