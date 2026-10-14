# Obsolete-code audit before Debian packaging

Reviewed the 2026-10-10 project archive using Python imports, text references,
console entry points, scripts, packaging configuration, and existing tests.

## Removed

- `sen/install/installer.py`: uncalled direct APT/pipx installation and removal
  path predating the daemon's authoritative lock-mode policy.
- `sentinel/settings/config_loader.py`: unused local settings/PIN readers and
  compatibility RPC wrappers; its only caller was the retired installer.
- `load_info()` in `settings/info_loader.py`: unused public-info reader. Private
  AdGuard credential readers remain active.
- `sentinel/dns/dns_manager.py` and unused generator write wrappers: current
  DNS reconciliation renders and applies a validated daemon snapshot directly.
- X11 `hide_window.py`, `window_tracker.py`, `coordinate_mapper.py`, and
  `geometry.py`: no remaining callers after migration to KWin UUIDs and
  ScreenShot2. Removed `python-xlib`, `mss`, and the orphaned `six` dependency.
- `scripts/start_image_filter.sh`: manual service launcher superseded by the
  daemon's image-service lifecycle.
- `sentinel-extension-server` console entry point and its exit-only `main()`:
  the daemon already owns the active extension HTTP server.
- Uncalled update-list helpers, Flatpak runtime resolver compatibility alias,
  AppArmor unlink helper, Steam stat-signature helper, and OpenNSFW2 health
  client. Current reconciliation, runtime enforcement, and service health
  checks remain in their respective active implementations.
- Legacy Guardian mode/plaintext PIN keys from the packaged settings template
  and the nonexistent `sentinel/systemd` packaging exclusion.

## Retained or relocated

- `custom_installer.py` is **still required** by Neovim, REAPER, and Unity Hub.
  Moved it to `sentinel/daemon/operations/custom_installer.py`; the daemon now
  passes the authenticated user's Downloads path explicitly on every request.
  Installation steps and approved aliases remain supported.
- `sen/install/packages.py`, autostart helpers, and scheduled-update helpers
  are still used by the daemon or installed entry points.
- Legacy-state migrations and recovery launch handling remain necessary for
  existing installations and cached desktop entries.
- `safe_steam/steam_extension_tests` contains production extension assets
  despite its name; provisioning and integrity validation still need it.
- Model conversion, extension build, provisioning, and diagnostic tools remain
  supported maintenance tools.

## Documentation and checks

Replaced the outdated CLI and screen-blocker guides, corrected root README
examples, and removed the nonexistent NVIDIA command from `sen man`. The
manual now derives approved aliases from the package catalog.

Updated two stale test expectations: the current backend connection error
text and the catalog extension's exact `favicon` permission. Existing denial,
signature, and host-permission assertions remain intact. Added regression
coverage for custom-installer per-peer archive selection and lock/PIN policy.

This audit does not replace live installation and Wayland testing on Kubuntu.
It preserves active compatibility paths rather than assuming that an old name
alone makes code obsolete.
