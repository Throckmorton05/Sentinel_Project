# sentinel/image_blocking/screen_monitor/monitor.py

from __future__ import annotations

import subprocess
import signal
import sys
import time

import psutil
import requests

from sentinel.image_blocking.screen_monitor.capture import WindowCapture
from sentinel.image_blocking.screen_monitor.detector import WindowDetector
from sentinel.image_blocking.screen_monitor.hash_cache import WindowHashCache
from sentinel.image_blocking.screen_monitor.inference import analyze
from sentinel.image_blocking.screen_monitor.logger import log_block
from sentinel.wayland.kwin_bridge import KWinBridge

from sentinel.image_blocking.screen_monitor.heartbeat import MonitorHeartbeat


SCAN_INTERVAL = 2.0

# Wayland/KWin blocker is now enabled.
BLOCKER_ENABLED = True


def main() -> None:

    # An intentional daemon stop must run the cleanup below, including blocker
    # children and privacy overlays, rather than exit immediately on SIGTERM.
    def shutdown(signum, frame):
        raise KeyboardInterrupt

    previous_sigterm = signal.signal(signal.SIGTERM, shutdown)

    bridge = KWinBridge()

    heartbeat = MonitorHeartbeat()

    hash_cache = WindowHashCache()

    # One blocker process per KWin window UUID.
    blockers: dict[
        str,
        subprocess.Popen[bytes],
    ] = {}

    process = psutil.Process()
    process.cpu_percent()

    try:
        # ---------------------------------------------------------
        # Start KWin / Wayland bridge
        # ---------------------------------------------------------

        bridge.start()

        # Register with sentineld as soon as the Wayland bridge exists.
        heartbeat.start()

        print("Waiting for KWin window information...")

        while not bridge.wait_for_update(timeout=5.0):
            print("Still waiting for KWin window information...")

        heartbeat.mark_progress()

        detector = WindowDetector(bridge)

        capture = WindowCapture(detector)

        print("Screen monitor started.")
        print("Display backend: KWin / Wayland")
        print(f"Runtime: {sys.executable}")
        print("Press Ctrl+C to stop.\n")

        while True:
            heartbeat.mark_progress()

            scan_start = time.perf_counter()

            # -----------------------------------------------------
            # Clean up finished blocker processes
            # -----------------------------------------------------

            for window_id, blocker in list(blockers.items()):
                return_code = blocker.poll()

                if return_code is not None:
                    print(f"Blocker finished: window={window_id} exit={return_code}")

                    del blockers[window_id]

            # -----------------------------------------------------
            # Find windows Sentinel should scan
            # -----------------------------------------------------

            windows = capture.get_captureable_windows()

            active_window_ids: set[str] = set()

            captured_windows = 0
            analyzed_windows = 0

            print("-" * 80)

            for window in windows:
                heartbeat.mark_progress()

                active_window_ids.add(window.window_id)

                pipeline_start = time.perf_counter()

                # -------------------------------------------------
                # Skip windows currently owned by a blocker
                # -------------------------------------------------

                blocker = blockers.get(window.window_id)

                if blocker is not None and blocker.poll() is None:
                    print(f"  ⏸ Skipping blocked window: {window.title}")
                    continue

                # -------------------------------------------------
                # Capture
                # -------------------------------------------------

                capture_start = time.perf_counter()

                image = capture.capture_window(window)

                heartbeat.mark_progress()

                capture_ms = (time.perf_counter() - capture_start) * 1000

                if image is None:
                    continue

                captured_windows += 1

                # -------------------------------------------------
                # Hash cache
                # -------------------------------------------------

                if not hash_cache.should_analyze(
                    window.window_id,
                    image,
                ):
                    continue

                analyzed_windows += 1

                print(f"• {window.title}")

                print(f"  App: {window.app_id}")

                print(f"  Window ID: {window.window_id}")

                print(f"  Capture: {capture_ms:.2f} ms")

                # -------------------------------------------------
                # Inference
                # -------------------------------------------------

                try:
                    inference_result = analyze(image)

                    heartbeat.mark_progress()

                except requests.RequestException as exc:
                    print(f"  API request failed: {exc}")
                    continue

                if inference_result["blocked"]:
                    model = inference_result["model"]

                    print(f"  ❌ BLOCKED ({inference_result['reason']})")

                    # ---------------------------------------------
                    # Log block
                    # ---------------------------------------------

                    log_start = time.perf_counter()

                    log_block(
                        window=window,
                        model=model,
                        reason=inference_result["reason"],
                        scores=inference_result["result"],
                    )

                    log_ms = (time.perf_counter() - log_start) * 1000

                    print(f"  Logging: {log_ms:.2f} ms")

                    # ---------------------------------------------
                    # Start blocker
                    # ---------------------------------------------

                    if BLOCKER_ENABLED:
                        blocker = blockers.get(window.window_id)

                        if blocker is None or blocker.poll() is not None:
                            print(f"  Starting blocker for window {window.window_id}")

                            blocker = subprocess.Popen(
                                [
                                    sys.executable,
                                    "-m",
                                    ("sentinel.image_blocking.screen_blocker.blocker"),
                                    window.window_id,
                                ]
                            )

                            blockers[window.window_id] = blocker

                    else:
                        print("  ⚠ Blocker disabled.")

                else:
                    print("  ✅ Allowed")

                pipeline_time = (time.perf_counter() - pipeline_start) * 1000

                print(f"  Total: {pipeline_time:.2f} ms")

            # -----------------------------------------------------
            # Remove hash-cache entries for closed/hidden windows
            # -----------------------------------------------------

            hash_cache.cleanup(active_window_ids)

            # -----------------------------------------------------
            # Monitor statistics
            # -----------------------------------------------------

            scan_time = (time.perf_counter() - scan_start) * 1000

            cpu = process.cpu_percent(interval=None)

            ram = process.memory_info().rss / (1024 * 1024)

            print(f"Windows Found:     {len(windows)}")

            print(f"Windows Captured:  {captured_windows}")

            print(f"Windows Analyzed:  {analyzed_windows}")

            print(f"CPU Usage:         {cpu:.1f}%")

            print(f"RAM Usage:         {ram:.1f} MB")

            print(f"Scan Time:         {scan_time:.2f} ms")

            heartbeat.mark_progress()

            time.sleep(SCAN_INTERVAL)

    except KeyboardInterrupt:
        print("\nScreen monitor stopped.")

    finally:
        # The blocker depends on this process's KWin bridge.
        # Shut blocker children down before releasing the bridge.

        heartbeat.stop()

        for blocker in blockers.values():
            if blocker.poll() is None:
                blocker.terminate()

        for blocker in blockers.values():
            try:
                blocker.wait(timeout=2.0)

            except subprocess.TimeoutExpired:
                blocker.kill()

        bridge.stop()
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    main()
