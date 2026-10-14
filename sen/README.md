# SEN command-line interface

`sen` runs as the normal desktop user. Administrative requests go to the
protected Sentinel backend, which verifies the socket peer, lock policy, and
Guardian PIN before acting.

```bash
sen man
sen --help
sen block-window create --help
```

The manual is `sen man`; `--help` supplies exact arguments and options. From a
source environment, replace `sen` with `.venv/bin/python -m sen.cli.sen`.

## Lock modes

| Mode | Package installation | Changes |
| --- | --- | --- |
| Unlocked | Valid APT names and supported aliases | Allowed by each operation's policy |
| Self Lock | Approved aliases and their exact APT package names | Maintain/strengthen; removal and weakening desktop/service changes denied |
| Guardian Lock | PIN required | Protected mutations require the PIN |

Known alternative browser packages remain restricted in every mode.
`safe_installs` governs App Store Flatpaks, not the CLI's package-mode rules.
Self Lock is permanent through the ordinary interface and can only be enabled
from Unlocked. Exit Guardian Lock with its PIN first.

Every entry from Unlocked into Guardian Lock creates and confirms a new
4–32 digit PIN, replacing the previous PIN without requiring it. No default PIN
exists. The new PIN also protects existing Guardian-owned schedules. Exiting or
changing the PIN requires the current PIN. PINs are prompted securely, never
accepted as command-line arguments or read from `settings.toml`.

## Command groups

| Commands | Purpose |
| --- | --- |
| `state`, `self-lock`, `guardian-lock`, `guardian-unlock`, `guardian-change-pin` | Lock management |
| `setting get/set` | Registered protection booleans, including image/boot-media filtering |
| `dns status/regenerate`, `dns block/allow list/add/remove` | DNS policy and domain lists |
| `app-block list/status/add-native/add-flatpak/remove/sync` | Personal application blocks |
| `guardian-apps`, `guardian-websites` | Guardian allow-only selections |
| `block-window` | One-time/recurring target or whole-desktop schedules |
| `activity status/events/report/screen-time` | Seven-day metadata and usage reports |
| `install`, `uninstall`, `update neovim/reaper`, `update-sys`, `update-sen`, `restart`, `shutdown` | Packages and system maintenance |

`sen update-sen` upgrades the installed Sentinel Debian release from its signed
APT channel. `sen update-sys` runs the normal APT index refresh and upgrade for
all installed packages, including Sentinel when an update is available. Both
are exposed under Portal → System & desktop → Maintenance. Guardian Lock
requires a PIN; updates remain available in Self-Lock. Neither action removes
packages or accepts a caller-selected repository.
| `add/start/stop/enable/disable/delete-service`, `service-status`, `ls-services` | Desktop-user programs in `sen-managed-*` units |
| `autostart-add/remove/ls`, `autologin-enable/disable/status` | Desktop startup preferences |
| `auto-update-enable/disable/status`, `auto-restart-enable/disable/status` | Scheduled system updates and reboot preferences |

Use the [Portal](../sentinel/portal/README.md) picker for Snap app blocks and
System & desktop for the daily allowance; these have no dedicated `sen` command.
[Block windows](../docs/block_windows.md) describes commitments, recurrence,
timezones, and two-minute screen-time exceptions. Guardian-owned schedules keep
PIN requirements outside Guardian Lock.

## Packages and automation

```bash
sen install git
sen install steam
sen install neovim  # Latest upstream stable .deb
sen update neovim   # Check for an updated stable release
sen install reaper   # Install official Cockos Linux release
sen update reaper    # Update existing /opt/REAPER install
sen install coding
sen uninstall git
sen auto-update-enable --daily --time 03:00
sen add-service server "$HOME/server.py"
sen autostart-add org.kde.kate.desktop
```

Native Steam uses `steam-installer`; Flatpak aliases use the App Store. Chrome
and Steam require their trusted repositories/dependency support. Bundle actions
ask for confirmation. The manual generates aliases and bundle contents from
`install/packages.py` so catalog changes appear automatically.

| Custom alias | Source | Installation |
| --- | --- | --- |
| `neovim` | Official stable GitHub `.deb` (downloaded by sentineld, SHA-256 checked) | APT-managed `neovim` package |
| `reaper` | Official Cockos stable HTTPS tarball | Extracts only application files to `/opt/REAPER`; tracks versions and preserves the old install as a backup |
| `unity` | Official Unity Hub APT repository | Repository/key setup and `unityhub` |

Neovim uses a fixed official GitHub release endpoint and verifies the size and
SHA-256 digest published in its release metadata; neither a local archive nor
an arbitrary URL is accepted. Updating requires an existing installation and
network access. Existing standalone /usr/local/bin/nvim binaries are backed up
under /usr/local/bin/nvim.sentinel-pre-deb-* after successful installation so
the packaged /usr/bin/nvim becomes active. The backup is not automatically
removed. Guardian Lock still requires its PIN. Release metadata and
the package both come from GitHub over HTTPS; SHA-256 is not an independent
publisher signature. The latest version is checked when the command runs, not
automatically in the background.

Poetry uses desktop-user pipx. Managed service programs must be inside the
requesting user's home; Python files use system Python, other files must be
executable. Services never run the user's program as root. Autologin enable
also verifies the desktop password. Automatic updates require exactly one of
`--daily`, `--weekly`, `--biweekly`, or `--monthly`; time defaults to 03:00.

## Implementation

`cli/sen.py` owns the manual and core commands; `cli/block_windows.py`,
`activity.py`, `guardian_apps.py`, and `guardian_websites.py` register groups.
`sentinel/daemon/operations/` owns privileged operations and policy. `autostart.py`
and `updates/auto_update.py` remain backend helpers. Lock state and credentials
belong to protected daemon stores, with no separate CLI verifier/installer.

See the [development runtime](../README.md#development-runtime) for wheel updates
and daemon startup. Read/status commands do not authorize a later mutation:
the backend rechecks every request. Saved-but-pending enforcement is distinct
from successful application.

REAPER updater: downloads from the fixed official Cockos HTTPS origin; validates
archive types, paths, and size before extraction; and never executes a
user-provided shell installer as root. It installs on x86_64 and aarch64.
Unlike Neovim's GitHub release, the REAPER download page does not currently
provide a cryptographically authenticated checksum manifest; SHA-256 recorded
during download is for auditing, not independent publisher verification.
Existing `/opt/REAPER` installations can be migrated and are retained in a
backup directory on update. Only Sentinel-managed installations can be
automatically uninstalled. Update/install requires network access.
