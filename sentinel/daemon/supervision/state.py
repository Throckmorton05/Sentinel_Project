"""Shared watchdog state and synchronization; no subsystem starts on import."""
from __future__ import annotations

import subprocess
import threading
from collections import deque
from dataclasses import dataclass, field


_stop_event = threading.Event()


@dataclass
class ScreenMonitorWatchdogState:
    enabled: bool = False

    uid: int | None = None

    pid: int | None = None

    last_heartbeat: float | None = None

    awaiting_registration_until: float | None = None

    healthy_since: float | None = None

    failures: deque[float] = field(default_factory=deque)

    tamper_cooldown_until: float | None = None

    tamper_blocker_pid: int | None = None

    overview_was_loaded: bool | None = None

    session_id: str | None = None

    # Keep the runuser parent so failed launches can be reaped/terminated.
    monitor_child: subprocess.Popen[bytes] | None = None

    launch_attempted: bool = False

    waiting_reason: str | None = None


_monitor_state = ScreenMonitorWatchdogState()


_monitor_state_lock = threading.RLock()

# Serialize watchdog launches against explicit image-filter enable/disable.
_monitor_lifecycle_lock = threading.RLock()
