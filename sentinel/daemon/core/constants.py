"""Daemon executable paths, watchdog timing and anti-tamper defaults."""
from __future__ import annotations

import sys
from pathlib import Path


CONTROL_GROUP = "sentinel"

SYSTEMCTL = "/usr/bin/systemctl"

APT_GET = "/usr/bin/apt-get"

RUNUSER = "/usr/sbin/runuser"

ENV = "/usr/bin/env"

PIPX = "/usr/bin/pipx"

LOGINCTL = "/usr/bin/loginctl"

SCREEN_MONITOR_MODULE = "sentinel.image_blocking.screen_monitor.monitor"

SCREEN_MONITOR_EXECUTABLE = Path(sys.executable).with_name(
    "sentinel-screen-monitor-python"
)

MONITOR_HEARTBEAT_TIMEOUT = 15.0

MONITOR_CHECK_INTERVAL = 2.0

MONITOR_RESTART_GRACE = 45.0

# Give an existing monitor time to re-register after a daemon restart.
MONITOR_ADOPTION_GRACE = 6.0

MONITOR_FAILURE_WINDOW = 5 * 60.0

MONITOR_FAILURE_LIMIT = 3

MONITOR_HEALTHY_RESET = 10 * 60.0

TAMPER_BLOCKER_MODULE = "sentinel.image_blocking.screen_blocker.tamper_blocker"

TAMPER_COOLDOWN_SECONDS = 30.0

QDBUS6 = "/usr/bin/qdbus6"

KWIN_EFFECTS_OBJECT = "/Effects"

KWIN_EFFECTS_INTERFACE = "org.kde.kwin.Effects"

OVERVIEW_EFFECT = "overview"
