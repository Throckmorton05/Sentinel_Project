# styles.py

# -------------------------------------------------------
# Colors
# -------------------------------------------------------

BACKGROUND = "#1E1E1E"

BUTTON = "#4A4A4A"

BUTTON_HOVER = "#6B6B6B"

BUTTON_PRESSED = "#151515"

TEXT = "#FFFFFF"

SECONDARY_TEXT = "#D0D0D0"


# -------------------------------------------------------
# Fonts
# -------------------------------------------------------

TITLE_FONT_SIZE = 22

BODY_FONT_SIZE = 14

BUTTON_FONT_SIZE = 14


# -------------------------------------------------------
# Shared Stylesheets
# -------------------------------------------------------

WINDOW_STYLE = f"""
QWidget {{
    background-color: {BACKGROUND};
}}

QLabel {{
    color: {TEXT};
    background: transparent;
}}
"""


BUTTON_STYLE = f"""
QPushButton {{
    background-color: {BUTTON};

    color: {TEXT};

    border: none;

    border-radius: 12px;

    padding: 12px 24px;

    font-size: {BUTTON_FONT_SIZE}px;
}}

QPushButton:hover {{
    background-color: {BUTTON_HOVER};
}}

QPushButton:pressed {{
    background-color: {BUTTON_PRESSED};
}}
"""


PROGRESS_STYLE = """
QProgressBar {
    border: none;
    border-radius: 8px;
    background-color: "#4A4A4A";
    text-align: center;
}

QProgressBar::chunk {
    background-color: "#151515";
    border-radius: 8px;
}
"""
