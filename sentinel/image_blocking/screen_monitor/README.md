# Screen monitor

The monitor connects KWin window information, Wayland capture, local model
inference, and per-window blockers. It runs as the protected desktop user;
the root daemon supervises its process and heartbeat. It does not authorize
settings changes or own model-service lifecycle.

Read the [image-filter overview](../README.md), [model APIs](../screen_filter/README.md),
[Wayland bridge](../../wayland/README.md), and
[daemon lifecycle](../../daemon/IMAGE_FILTERING.md) for the cooperating pieces.

## Files and responsibilities

| File | Responsibility |
| --- | --- |
| [monitor.py](monitor.py) | Scan loop, process ownership, blocker children, cleanup |
| [detector.py](detector.py) | Select windows from KWin snapshots |
| [capture.py](capture.py) | KWin `ScreenShot2` capture and raw image decoding |
| [hash_cache.py](hash_cache.py) | Per-window change detection |
| [inference.py](inference.py) | Gate → route → final classifier sequence |
| [api_clients/](api_clients/) | PNG multipart requests to local model APIs |
| [heartbeat.py](heartbeat.py) | Registration, progress checks, periodic heartbeat |
| [logger.py](logger.py) | Bounded asynchronous block-event delivery |

## Startup and ownership

The installed entry point is `sentinel-screen-monitor`; the daemon launches the
expected module through its protected monitor interpreter. Launch identity and
adoption checks live in
[`daemon/supervision/screen_monitor.py`](../../daemon/supervision/screen_monitor.py).
The protected UID is configured by the daemon, not selected by a registration
payload.

The monitor starts the KWin bridge and heartbeat worker, waits for window
updates, and then scans. It owns `org.sentinel.KWinBridge`; blocker children
communicate with that existing bridge. The diagnostic `detector.py` entry point
also starts a bridge, so do not run it alongside the managed monitor as a second
owner. A D-Bus name or process being present does not prove capture is working.

## One scan

1. Mark progress and reap exited blocker children.
2. Get visible windows from the current KWin snapshot. The ordinary detector
   excludes minimized windows, other desktops, and dimensions below 200 × 200.
3. Apply capture exclusions: only ordinary windows/dialogs, at least 300 × 200,
   excluding known shell/Sentinel windows. The larger capture minimum is the
   effective minimum for ordinary scans.
4. Skip windows already owned by a live blocker child.
5. Capture and decode the window image. A failed capture returns `None` and the
   monitor moves on to another window.
6. Compute the per-UUID image hash. Skip an unchanged resized image.
7. Call the local inference pipeline. A below-threshold OpenNSFW2 gate allows;
   otherwise the drawn/photo router selects Hentai or NudeNet's final decision.
8. For a final positive result, enqueue bounded block metadata and spawn one
   `sentinel.image_blocking.screen_blocker.blocker` child for the UUID.
9. Remove hash entries absent from the current capturable-window set, report
   scan/resource statistics, mark progress, and sleep for two seconds.

The period is **scan work plus the two-second sleep**, not a fixed two-second
deadline. Windows are processed sequentially; slow captures or requests delay
later windows. Detection is periodic and has coverage gaps.

## Capture contract

[`capture.py`](capture.py) uses the session D-Bus service and interface
`org.kde.KWin.ScreenShot2` at `/org/kde/KWin/ScreenShot2`, calling `CaptureWindow`
with the exact UUID and a Unix file descriptor. Capture options exclude window
decoration and shadows and request native resolution.

The returned width, height, format, and stride describe the raw bytes read from
the pipe. Qt converts supported image formats to RGBA, then Pillow receives an
RGB image. Respect `bytesPerLine`; assuming tightly packed pixels can corrupt
captures. File descriptors are cleaned up even on failure.

The D-Bus method call has a four-second timeout. That does **not** establish a
four-second bound for the entire pipe read and image-processing path. Capture
exceptions print diagnostics and return `None`; the ordinary monitor does not
automatically quarantine a window just because it could not be captured.

## Hash cache and failure behavior

The cache hashes a 256 × 256 RGB bilinear resize using BLAKE3. It identifies
changes visible at that resolution, not byte-identical original captures. It
stores the new hash **before** inference and has no periodic expiry for an
unchanged active window; the stored timestamp is not an expiry policy.

Consequently, an HTTP inference failure can leave the failed capture's hash
cached. An identical later capture is skipped until the image changes, the
entry is removed, or the cache is cleared. Do not assume it retries that same
image on the next scan. Treat this as a current retry limitation when planning
reliability work.

Each model client has a ten-second Requests timeout. Request exceptions are
reported and skipped by the scan loop; these timeouts are not a single total
pipeline deadline. Unexpected exceptions can terminate the monitor and enter
daemon recovery.

Failure behavior differs between ordinary scanning and an already blocked
window: the [blocker](../screen_blocker/README.md) keeps/reinstates quarantine
when a recheck cannot capture or classify safely. It covers every display
during restoration/recheck and waits for KWin acknowledgements. Preserve that
distinction when changing either path.

## Heartbeat and shutdown

[`heartbeat.py`](heartbeat.py) registers with the daemon and sends heartbeats
every two seconds while the main loop keeps making progress. If progress is
older than 20 seconds, it pauses heartbeats even if the Python process is alive.
Daemon timeout, startup/adoption grace, and repeated-failure handling are
separate rules in [`daemon/core/constants.py`](../../daemon/core/constants.py)
and the supervisor. A heartbeat indicates loop progress, not model accuracy or
successful capture of every window.

SIGTERM raises `KeyboardInterrupt` so the monitor's `finally` block runs:

- Stop the heartbeat thread.
- Terminate blocker children, wait up to two seconds per child, then kill any
  that have not exited.
- Stop the KWin bridge and restore the previous SIGTERM handler.

Children stop before the bridge because their cleanup depends on it. Authorized
disable is handled by the daemon as intentional shutdown and clears tamper
state. Repeated unexpected monitor termination can instead trigger the separate
[anti-tamper screen](../../daemon/IMAGE_FILTERING.md#anti-tamper-screen).

## Block events and privacy

`logger.py` sends events through the daemon client on a background thread rather
than delaying protection. Events include UUID, title/app identity, PID, model,
reason, time, and bounded scores; they contain no captured image bytes.
The in-memory queue holds at most 256 events, drops its oldest entry when full,
retries daemon errors, and expires pending events older than one day. It is not
a durable delivery queue, so monitor termination can lose pending events.

The backend validates and stores accepted metadata in its
[Activity database](../../activity/README.md), with its own retention and size
limits. Titles and app identifiers can still be sensitive metadata. The legacy
`blocked.jsonl` path is not used by this event sender.

## Development checks

Use the read-only daemon status check in
[native verification](../../daemon/IMAGE_FILTERING.md#native-verification) before
trying to launch another monitor. Check saved intent, model readiness, session
availability, registration, and heartbeat separately.

The existing lifecycle suite includes SIGTERM cleanup and registration checks:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  sentinel/daemon/tests/test_image_filtering.py -q
```

This does not validate real ScreenShot2 pixels or model decisions. On the target
KDE session, verify changed/unchanged captures, excluded windows, API failure,
blocker exit, safe/unsafe rechecks, multiple displays, intentional disable, and
unexpected monitor termination. Follow installed runtime instructions in the
[root README](../../../README.md#development-runtime); source edits alone do
not update the supervised executable.
