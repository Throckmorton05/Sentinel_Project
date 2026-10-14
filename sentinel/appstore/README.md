# Sentinel App Store

The PyQt6 App Store manages system Flatpaks, native Steam, and system/Flatpak
updates through the protected Sentinel backend. Run it as the desktop user:

```bash
sentinel-appstore
# Source GUI:
.venv/bin/python -m sentinel.appstore.main
```

The GUI does not use sudo/pkexec or read a PIN from settings. Each protected
Guardian action supplies a fresh PIN for backend verification.

## Screenshots

### Store

Browse applications by category or search the catalog.

![App Store catalog with category navigation and application cards](../../docs/images/appstore/store.png)

### Installed apps

View installed applications and their management controls.

![Installed Apps page showing native Steam](../../docs/images/appstore/installed-apps.png)

### Updates

Install individual updates or use Update All.

![Updates page with available system updates and Update All](../../docs/images/appstore/updates.png)

## Catalog and lock policy

| Mode | Safe Installs on | Safe Installs off | Removal | Updates |
| --- | --- | --- | --- | --- |
| Unlocked | Approved catalog | Full supported Flathub catalog | Allowed | Allowed |
| Self Lock | Approved catalog | Full supported Flathub catalog | Denied | Allowed |
| Guardian Lock | Approved catalog; PIN to install | Full supported Flathub catalog; PIN to install | PIN required | PIN required |

`safe_installs` controls Flatpak catalog/install enforcement in every mode.
Self Lock preserves its current value, allows enabling it, and denies disabling
it afterward. Guardian Lock requires the PIN to change it; a PIN does not itself
permit unapproved apps while it is on. Missing keys default to enabled; unreadable,
unprotected, or invalid policy rejects installation.

With Safe Installs on, enabled entries in
`/usr/share/sentinel/approved_apps.toml` are checked against current app blocks
for each request. The backend validates the exact ID/ref rather than trusting
GUI filtering or client approval flags. Known alternative browsers, Flatpak
Chrome, and Flatpak Steam remain excluded even with Safe Installs off.

The built-in Steam card installs `steam-installer` through the same package RPC
as `sen install steam`; it follows CLI package policy and uses a distinct
`native:steam` identity. It never changes `safe_steam`. Native extension repair
can close Steam; reopen it afterward. Previously installed unsupported Flatpak
Steam remains visible for policy-controlled removal.

## Behavior and requirements

The initial load displays an animated blue wheel while catalog, installed apps,
and updates load. Failure stops it and offers retry. Background checks reconnect
and refresh policy automatically; an unavailable backend disables management.
Installed Apps includes applications outside the currently restricted catalog.
Closing waits for an active read, Guardian prompt, installation or update to
finish. On exit, Qt workers are joined before widgets and QApplication are
destroyed. Test normal close through both the terminal and KDE application menu.
Actions recheck policy before prompting, and the backend checks it again at execution.
Cancellation sends no mutation; operation failures do not emit success.

The backend uses the default **system** Flatpak installation and configured
system Flathub remote. Additional installations and per-user Flatpak management
are outside its scope. Native Steam requires Ubuntu multiverse/i386 support.
AppStream supplies descriptions/icons/categories; absent metadata does not
prevent the approved catalog from loading. Open launches apps as the user.

Administrative mutations are serialized, while bounded concurrent reads and
heartbeats continue. APT upgrades use `--no-remove` in Self Lock; individual
system-package upgrades use `--only-upgrade --no-remove`. Update All combines
system packages and system Flatpaks. Independent package/Polkit access still
needs final distribution enforcement.

## Modules and RPCs

- `windows/main_window.py`: catalog/navigation, asynchronous loading, refresh.
- `backend.py`: routes native Steam and Flatpak actions.
- `operations.py`: background jobs and masked Guardian prompts.
- `flatpak_backend.py`, `updates/update_manager.py`: backend client adapters.
- `app_loader.py`, `metadata/`, `widgets/`: catalog data and presentation.
- `sentinel/daemon/operations/appstore_ops.py`: Flatpak/update policy and execution.
- `sentinel/daemon/operations/package_ops.py`: native Steam package actions.

Reads: `appstore.policy`, `appstore.installed`, `appstore.updates`.
Mutations: `appstore.install`, `appstore.uninstall`, `appstore.update_flatpak`,
`appstore.update_system_package`, `appstore.update_all`; Steam instead uses
`package.install`/`package.uninstall`. Clients cannot choose arbitrary remotes,
installation scope, command paths, or flags.

## Validation

Update the installed wheel in both required environments and restart the backend;
see [development runtime](../../README.md#development-runtime).

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  sentinel/appstore/tests sentinel/daemon/tests -q
```

Tests isolate privileged commands. Verify live install/remove/update, Guardian
prompts, reconnection, and native Steam on the target machine before shipping.
