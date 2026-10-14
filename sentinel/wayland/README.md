# Sentinel Wayland integration

Sentinel targets **KDE Plasma on Wayland/KWin**. Window discovery/control use
a KWin script and session D-Bus bridge; capture uses KWin's `ScreenShot2` API.
No X11 window IDs, `python-xlib`, or `mss` are used by this implementation.

## Ownership and data

- `kwin_script/contents/code/main.js` runs inside KWin, enumerates windows,
  sends periodic snapshots, polls commands, and reports results.
- `kwin_bridge.py` implements `org.sentinel.KWinBridge` at
  `/org/sentinel/KWinBridge`. The screen monitor owns the service.
- The detector consumes snapshots, capture consumes UUIDs, and blockers submit
  commands as D-Bus clients. A blocker must not start a second bridge.

Snapshots contain UUID, title, app/resource identifiers, PID, position/size,
focus, minimized/current-desktop state, and normal/dialog flags. Special,
deleted, unmanaged, popup, menu, notification, and similar windows are excluded.
Periodic snapshots allow the monitor to start without waiting for a focus change.

The script must be installed/enabled in the protected user's KWin session.
The package ID is `sentinel-window-bridge`; its source is `kwin_script/`.
During development, the existing user package/symlink typically lives at
`~/.local/share/kwin/scripts/sentinel-window-bridge`. A Python wheel alone does
not install/enable that KWin package. Check KDE's KWin Scripts settings and
reload/reconfigure KWin after installing changes.

## Commands and quarantine

| Action | Behavior |
| --- | --- |
| `minimize` | Save original flags once, minimize, and set skipTaskbar/skipSwitcher/skipPager |
| `restore` | Temporarily unminimize for covered recheck, retaining quarantine flags |
| `release` | Restore saved flags and unminimize a safe window |
| `close` | Ask KWin to close the exact UUID; the blocker confirms disappearance |

Commands get IDs and acknowledgements. The bridge exposes `UpdateWindows`,
`QueueWindowCommand`, `GetNextCommand`, `GetCommandResult`, and window snapshot
reads. The blocker waits for acknowledgements before removing privacy overlays.

While blocked, the app stays alive but is hidden from desktop/taskbar/switcher/
pager. Rechecks cover all displays before restoring and capturing it. A failure
returns to quarantine; a safe result releases its saved state. This replaces
X11 unmapping. See [screen blocker](../image_blocking/screen_blocker/README.md).

## Capture and diagnostics

`screen_monitor/capture.py` calls `org.kde.KWin.ScreenShot2`, passes a pipe file
descriptor, and builds a Pillow image from compositor pixels. Capture requires
the same supported KDE user session as the monitor.

```bash
busctl --user introspect org.sentinel.KWinBridge /org/sentinel/KWinBridge
journalctl --user-unit plasma-kwin_wayland -f
qdbus6 org.kde.KWin /KWin reconfigure
```

An idle desktop with no eligible windows is valid. A missing/stale bridge or
ScreenShot2 error needs session/script diagnosis, rather than starting the GUI
as root. See [image filtering](../image_blocking/README.md) for service lifecycle.

The separate Activity worker loads `sentinel/activity/focus.js` for foreground
usage independently of this image-monitor bridge. Switching image filtering off
does not stop screen-time collection.
