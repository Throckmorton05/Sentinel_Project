"""sentineld-owned runtime application process enforcement."""

from __future__ import annotations

import time

from sentinel.app_blocker.process_monitor.monitor import scan_processes
from sentinel.daemon.operations.app_blocker_ops import (
    get_process_policy,
    refresh_process_policy,
)
from sentinel.daemon.operations import guardian_app_ops
from sentinel.daemon.supervision.state import _stop_event, _monitor_state, _monitor_state_lock


PROCESS_SCAN_INTERVAL = 0.25
PROCESS_POLICY_REFRESH_INTERVAL = 2.0


def _app_blocker_watchdog_tick() -> int:
    with _monitor_state_lock:
        uid = _monitor_state.uid
    return scan_processes(get_process_policy(), uid=uid) + guardian_app_ops.scan_processes()


def app_blocker_watchdog_loop() -> None:
    next_refresh = 0.0

    while not _stop_event.is_set():
        try:
            now = time.monotonic()
            if now >= next_refresh:
                refresh_process_policy()
                guardian_app_ops.refresh_inventory()
                next_refresh = now + PROCESS_POLICY_REFRESH_INTERVAL
            _app_blocker_watchdog_tick()
        except Exception as exc:
            # A transient /proc, config, or psutil failure must not kill
            # enforcement. The next watchdog iteration retries automatically.
            print(f"sentineld app blocker scan failed: {exc}", flush=True)
        _stop_event.wait(PROCESS_SCAN_INTERVAL)
