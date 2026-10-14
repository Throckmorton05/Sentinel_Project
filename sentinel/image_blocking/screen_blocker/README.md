# Sentinel screen blocker

The screen monitor launches `blocker.py` with a KWin window UUID when image
analysis detects blocked content. The current desktop target is KDE Plasma
on Wayland.

The monitor owns `org.sentinel.KWinBridge`. The blocker is a D-Bus client:
it queues window actions and waits for their acknowledgements. Captures use
KWin ScreenShot2 through `screen_monitor/capture.py`.

## Screenshots

### Content blocked

The target window stays quarantined while this page offers an automatic recheck
countdown, a manual Recheck button, and Close Window.

![Content Blocked page with recheck countdown and window controls](../../../docs/images/blocking/blocked-window.png)

### Rechecking

Privacy overlays cover the desktop while the target is restored underneath
for capture and analysis.

![Rechecking privacy overlay with a progress indicator](../../../docs/images/blocking/rechecking.png)

For the separate anti-tamper restoration screen, see
[the monitor lifecycle guide](../../daemon/IMAGE_FILTERING.md#anti-tamper-screen).

## Window lifecycle

| State | Behavior |
|---|---|
| `BLOCKED` | Quarantine/minimize the target and show the blocked page with a 30-second automatic recheck countdown and manual Recheck/Close controls |
| `RECHECKING` | Cover every monitor, restore the target underneath, wait for acknowledgement and redraw, then capture and analyze it |
| `CLOSING` | Request close through KWin and poll until the target disappears; a timeout restores the blocked UI |
| `EXITING` | Close the blocker UI and privacy overlays after the target is gone or KWin confirms release of a safe window |

An unsafe result, capture failure, or inference failure returns to blocking.
Privacy overlays remain until KWin confirms the target has been minimized
again. Successful analysis releases the target's quarantine only after the
release command is acknowledged. Rechecks use timers and state transitions.

## Files

- `blocker.py`: controller, D-Bus requests, acknowledgement timers, capture,
  inference, and close confirmation.
- `blocked_window.py`: blocked page, countdown, and user actions.
- `overlay.py`: privacy overlay manager spanning every connected screen.
- `styles.py`: shared Qt styles.
- `tamper_blocker.py` and `tamper_blocker.qml`: daemon-requested integrity
  blocking UI.
- `screen_time_blocker.py` and `screen_time_blocker.qml`: scheduled and daily-allowance
  full-desktop screen-time blocking UI.
- `system_qt.py`: launches the QML helpers using the system Qt environment.

Window discovery/quarantine is implemented in
[`sentinel/wayland`](../../wayland/README.md). Image services and monitor
supervision are described in [the daemon lifecycle guide](../../daemon/IMAGE_FILTERING.md).

The old X11 hide/track/coordinate helpers have been removed. Neither
`python-xlib` nor `mss` is required by the Wayland implementation.

## Other full-desktop blocks

Anti-tamper and screen-time helpers are launched/supervised by the backend,
separately from per-window image blockers. Scheduled screen-time windows support
one-time and recurring occurrences; the daily allowance has its own midnight
reset and controls. Both contribute to the screen-time overlay, and releasing
one restriction never bypasses another. The system-QML helpers use the target's
Qt/layer-shell environment; they require a real KDE user session. See
[Portal screen time](../../portal/README.md#daily-screen-time-allowance) and
[block windows](../../../docs/block_windows.md).
