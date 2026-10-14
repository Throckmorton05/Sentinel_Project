from __future__ import annotations

import threading
import time

from sentinel.daemon.client import (
    SentineldError,
    register_screen_monitor,
    screen_monitor_heartbeat,
)


HEARTBEAT_INTERVAL = 2.0

# If the actual monitor loop has made no progress for this long,
# stop sending heartbeats. sentineld will then treat the monitor
# as unhealthy even if the Python process itself is still alive.
MAIN_LOOP_STALL_LIMIT = 20.0


class MonitorHeartbeat:
    """
    Background heartbeat sender for the Sentinel screen monitor.

    The heartbeat thread only reports the monitor as healthy while
    the main scanning loop continues making progress.
    """

    def __init__(self) -> None:
        self._stop_event = threading.Event()

        self._thread: threading.Thread | None = None

        self._lock = threading.Lock()

        self._last_progress = time.monotonic()

        self._registered = False

        self._last_error: str | None = None

        self._stalled = False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return

        self._stop_event.clear()

        self._last_progress = time.monotonic()

        self._thread = threading.Thread(
            target=self._run,
            name="sentinel-monitor-heartbeat",
            daemon=True,
        )

        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

        if self._thread is not None:
            self._thread.join(timeout=3.0)

        self._thread = None

    def mark_progress(self) -> None:
        """
        Tell the heartbeat thread that the real monitor loop
        is still progressing.
        """

        with self._lock:
            self._last_progress = time.monotonic()

    def _progress_age(self) -> float:
        with self._lock:
            last_progress = self._last_progress

        return time.monotonic() - last_progress

    def _run(self) -> None:
        while not self._stop_event.is_set():
            progress_age = self._progress_age()

            if progress_age > MAIN_LOOP_STALL_LIMIT:
                if not self._stalled:
                    print(
                        "Sentinel watchdog: "
                        "screen monitor loop appears stalled; "
                        "heartbeats paused."
                    )

                    self._stalled = True

                self._stop_event.wait(HEARTBEAT_INTERVAL)

                continue

            if self._stalled:
                print("Sentinel watchdog: screen monitor loop recovered.")

                self._stalled = False

            try:
                if not self._registered:
                    register_screen_monitor()

                    self._registered = True

                    print("Sentinel watchdog: screen monitor registered.")

                else:
                    screen_monitor_heartbeat()

                if self._last_error is not None:
                    print("Sentinel watchdog: connection to sentineld restored.")

                    self._last_error = None

            except SentineldError as exc:
                self._registered = False

                error = str(exc)

                if error != self._last_error:
                    print(f"Sentinel watchdog: {error}")

                    self._last_error = error

            self._stop_event.wait(HEARTBEAT_INTERVAL)
