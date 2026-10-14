"""Offline, read-only onboarding; no backend, PIN storage, or settings writes."""
from __future__ import annotations

import subprocess
import sys

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QListWidget,
                            QMainWindow, QPushButton, QScrollArea, QStackedWidget,
                            QVBoxLayout, QWidget)

from .theme import LOGO, STYLE

# These are product instructions, not an automatic setup recipe. No action in
# this guide enables a protection or enters a lock mode.
SECTIONS = (
    ('Start here', 'Make Sentinel yours', 'Choose your protections. Then choose your commitment.', (
        ('Your first session',
         'A fresh Sentinel ISO installation starts <b>Unlocked</b>, with every protection switch off. '
         'Open Sentinel Portal, try the protections you want, and check that your normal apps and websites work before choosing a lock mode.'),
        ('A good first setup',
         '<b>1.</b> Open <b>Protections</b> in the Portal and enable the features you want.<br/><br/>'
         '<b>2.</b> Turn on DNS filtering before selecting its website categories. Category switches also start off.<br/><br/>'
         '<b>3.</b> Use <b>Apps &amp; websites</b> and <b>Calendar</b> to add your own limits.<br/><br/>'
         '<b>4.</b> Test everything while Unlocked. Decide whether Guardian Lock or permanent Self Lock fits your needs.'),
        ('The Sentinel desktop',
         'This is a managed Linux desktop: your account has no general sudo access. Chrome is the supported browser and Google is its search provider. '
         'Account, login, browser-management, and package-integrity rules still apply when the optional protection switches are off.'),
        ('Keep this guide handy',
         'This guide works offline and can be reopened from <b>Start Here — Sentinel</b> in the application menu. '
         'The desktop shortcut is yours to move or remove; it is not recreated at every login.'),
    )),
    ('Protections', 'Choose what Sentinel protects', 'Turn features on individually in Sentinel Portal → Protections.', (
        ('Image filtering',
         'Scans visible windows for explicit images. A blocked window is minimized while Sentinel checks it again. '
         'Turning this on starts the image services and screen monitor. If the Portal reports a startup or capture error, resolve it before relying on protection.'),
        ('DNS filtering',
         'Blocks domains in your selected categories and custom website list. Enable the master <b>DNS filtering</b> switch, then choose categories. '
         '<b>Adult content heavy websites</b> includes mixed-content communities and art-commission marketplaces, including Fiverr. '
         'These are whole-site blocks: safe pages and services on the same domain are blocked too.'),
        ('Safe browsing, Steam, and Crunchyroll',
         '<b>Safe browsing</b> enforces Google SafeSearch and YouTube Restricted Mode. '
         '<b>Safe Steam</b> filters supported Steam browsing surfaces. <b>Safe Crunchyroll</b> filters supported Crunchyroll pages. '
         'The managed extensions remain installed when their content-filtering switches are off.'),
        ('Safe Installs, boot media, and VM protection',
         '<b>Safe Installs</b> limits installs to approved software. <b>Boot media protection</b> restricts creating bootable removable media. '
         '<b>VM protection</b> restricts KVM and common virtual-machine tools. Shut down virtual machines before enabling VM protection.'),
    )),
    ('Lock modes', 'Choose your lock mode carefully', 'Lock modes control whether you can weaken the limits you have chosen.', (
        ('Unlocked',
         'Use this mode to configure and test Sentinel. You can enable or disable optional protections and adjust your limits. '
         'Unlocked does not give the desktop account administrator privileges.'),
        ('Guardian Lock',
         'A guardian controls changes that weaken protection with a PIN. On entry, create a PIN and confirm it; there is no default PIN. '
         'Keep it with your guardian. Optional Guardian app and website allowlists let them limit access to selected items. '
         'Leave Guardian Lock with the PIN before entering Self Lock.'),
        ('Permanent Self Lock',
         '<b>Self Lock is permanent. There is no ordinary unlock path.</b> You can strengthen protections, but cannot turn enabled protections off or weaken your commitment. '
         'Choose and test your protections while Unlocked first. Entering Self Lock is not a shortcut that configures every feature for you.'),
    )),
    ('Apps & websites', 'Set your own boundaries', 'Use Sentinel Portal → Apps & websites, or the sen command.', (
        ('Block applications',
         'Choose installed applications in the Portal’s app selector. Sentinel applies app blocks to the protected desktop account. '
         'Use the delete button to remove a block when your lock mode permits it.'),
        ('Block websites',
         'Add a domain, such as <b>example.com</b>, rather than a page URL. A domain block covers its subdomains. '
         'New custom blocks have a 24-hour correction window; later removal follows your lock policy. '
         'An allowlist exception can relax a category block, but does not override an explicit custom block.'),
        ('Install software',
         'Use <b>Sentinel App Store</b> or <b>sen install</b>. Safe Installs and your lock mode determine what is allowed; Guardian changes may ask for the PIN. '
         'Critical Sentinel and platform packages remain protected from removal.'),
    )),
    ('Focus & time', 'Plan time away from distractions', 'Set limits in Calendar and System & desktop.', (
        ('Focus windows',
         'In <b>Calendar</b>, create a window, choose its time and targets, and select the applicable lock rules. '
         'Windows can block apps, websites, or computer access. Recurring windows repeat on their selected schedule. '
         'Overlapping windows combine their blocked targets.'),
        ('Daily screen time',
         'Configure a daily allowance in <b>System &amp; desktop</b>. Overview shows time remaining. '
         'A Guardian-authorized bypass turns the daily limit off. Self Lock offers the supported two-minute grace period rather than an unrestricted unlock.'),
        ('Check the schedule',
         'Confirm the displayed timezone and start/end times before saving. Test a short window while Unlocked before making a lasting commitment.'),
    )),
    ('Activity', 'See how your time is used', 'Open Sentinel Portal → Activity.', (
        ('Your activity history',
         'Activity shows recorded app and website use, searches, and blocking events. Use its views and filters to find the events you need. '
         'The current history retention is one week; this is not a permanent archive.'),
        ('When something looks wrong',
         'Check Overview and the relevant protection page for pending changes or errors. Activity records depend on the supported collectors and active desktop session; '
         'a missing event is not proof that a site or app was blocked.'),
    )),
    ('Desktop', 'A desktop you can customize', 'Appearance settings are yours to edit.', (
        ('Launcher and terminal',
         'Press <b>Ctrl+Space</b> for Rofi. Konsole uses JetBrains Mono Nerd Font and starts with Starship and Fastfetch. '
         'Use Kate for graphical text editing; Neovim is the terminal editor.'),
        ('Editable configuration files',
         '<b>Rofi:</b> ~/.config/rofi/config.rasi<br/><br/>'
         '<b>Starship:</b> ~/.config/starship.toml<br/><br/>'
         '<b>Fastfetch:</b> ~/.config/fastfetch/config.jsonc<br/><br/>'
         '<b>Konsole:</b> ~/.local/share/konsole/Sentinel.profile<br/><br/>'
         'These user files can be edited without sudo. Back up a file before changing it. Konsole’s profile editor can also change the font and colors.'),
        ('Wallpapers and startup',
         'Choose Sentinel Gradient or Sentinel Navy Waves in KDE wallpaper settings. '
         'Configure app autostart and supported services in the Portal’s <b>System &amp; desktop</b> page.'),
    )),
    ('Updates & help', 'Keep Sentinel up to date', 'Use Sentinel’s own update controls.', (
        ('Update the installed system',
         'Use <b>Update Sentinel</b> in System & desktop, or run <b>sen update-sen</b>, '
         'for Sentinel releases. <b>Update system</b> and <b>sen update-sys</b> update all installed APT packages. '
         'Once the public update channel is available, signed Debian packages update Sentinel and its platform integration without reinstalling the ISO. '
         'Save your work and reboot when requested. Existing lock state, PIN, protections, and personal appearance settings are preserved.'),
        ('Schedules and errors',
         'Configure automatic updates in <b>System &amp; desktop</b>. If an update fails, keep the error text for diagnosis. '
         'Do not disable signature verification or ignore repository errors to force an update.'),
        ('Useful read-only commands',
         '<b>sen --help</b> — available commands<br/><br/>'
         '<b>sen state</b> — current lock mode<br/><br/>'
         '<b>sen setting get image_filtering</b> — image switch<br/><br/>'
         '<b>sen dns status</b> — DNS configuration and application status<br/><br/>'
         '<b>sen app-block status</b> — application blocking status'),
    )),
)


class GuideWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Start Here — Sentinel')
        self.setWindowIcon(QIcon(str(LOGO)))
        self.setStyleSheet(STYLE + '''
            QLabel#guideTitle { color: #78c8ff; font-size: 32px; font-weight: 700; }
            QLabel#guideCardTitle { color: #eef5ff; font-size: 17px; font-weight: 700; }
            QLabel#guideBody { font-size: 15px; line-height: 1.5; }
        ''')
        self.resize(1100, 780)
        self.setMinimumSize(820, 580)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 20, 24, 20)
        header = QHBoxLayout()
        shield = QLabel()
        shield.setPixmap(QIcon(str(LOGO)).pixmap(58, 58))
        header.addWidget(shield)
        name = QLabel('Sentinel  /  Getting started')
        name.setObjectName('title')
        header.addWidget(name)
        header.addStretch()
        version = QLabel('0.1.0 BETA · Offline guide')
        version.setObjectName('muted')
        header.addWidget(version)
        layout.addLayout(header)
        body = QHBoxLayout()
        body.setSpacing(22)
        layout.addLayout(body, 1)
        self.navigation = QListWidget()
        self.navigation.setObjectName('navigation')
        self.navigation.setFixedWidth(205)
        body.addWidget(self.navigation)
        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)
        for name, title, subtitle, cards in SECTIONS:
            self.navigation.addItem(name)
            content = QWidget()
            column = QVBoxLayout(content)
            column.setContentsMargins(4, 8, 14, 20)
            column.setSpacing(16)
            heading = QLabel(title)
            heading.setObjectName('guideTitle')
            heading.setWordWrap(True)
            column.addWidget(heading)
            summary = QLabel(subtitle)
            summary.setObjectName('muted')
            summary.setWordWrap(True)
            column.addWidget(summary)
            for heading, text in cards:
                card = QFrame()
                card.setObjectName('overviewCard')
                card_layout = QVBoxLayout(card)
                card_layout.setContentsMargins(20, 18, 20, 18)
                card_layout.setSpacing(10)
                label = QLabel(heading)
                label.setObjectName('guideCardTitle')
                label.setWordWrap(True)
                card_layout.addWidget(label)
                label = QLabel(text)
                label.setObjectName('guideBody')
                label.setTextFormat(Qt.TextFormat.RichText)
                label.setWordWrap(True)
                label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                card_layout.addWidget(label)
                column.addWidget(card)
            column.addStretch()
            scroller = QScrollArea()
            scroller.setWidgetResizable(True)
            scroller.setWidget(content)
            self.stack.addWidget(scroller)
        self.navigation.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.navigation.setCurrentRow(0)
        footer = QHBoxLayout()
        tip = QLabel('Choose protections while Unlocked. Test them before entering Self Lock.')
        tip.setObjectName('muted')
        tip.setWordWrap(True)
        footer.addWidget(tip, 1)
        self.portal_button = QPushButton('Open Sentinel Portal')
        self.portal_button.setObjectName('primary')
        self.portal_button.clicked.connect(self.open_portal)
        footer.addWidget(self.portal_button)
        close = QPushButton('Close guide')
        close.clicked.connect(self.close)
        footer.addWidget(close)
        layout.addLayout(footer)

    def open_portal(self):
        # This fixed unprivileged entry point still enforces Portal Guardian PIN
        # authorization. No guide text or launcher argument becomes a command.
        subprocess.Popen([sys.executable, '-I', '-B', '-m', 'sentinel.portal.main'],
                         start_new_session=True)
