# Sentinel update controls and image branding — 0.1.0-10

This revision adds `sen update-sen`, a separate **Update Sentinel** control in
Portal → System & desktop → Maintenance, and the requested ISO cleanup.

| Action | Behavior |
| --- | --- |
| `sen update-sen` / Update Sentinel | Refresh signed APT indexes, then upgrade the installed members of the six-package Sentinel release. |
| `sen update-sys` / Update system | Normal APT index refresh and upgrade of all installed packages, including Sentinel when available. |
| Automatic updates | The existing schedule still performs the normal system update, covering both Ubuntu and Sentinel. |

APT may upgrade dependencies needed by a newer Sentinel release. The dedicated
command does not request unrelated apps, add an absent platform package, accept
package/URL input, change repository trust, permit removals or allow downgrades.
Guardian Lock requires the real Guardian PIN. Updates remain available in
Self-Lock. User settings, PINs, account credentials and activity are retained by
the existing package lifecycle. Save work before updating; package restarts can
interrupt a desktop session. Actual package transactions and reboots need the VM
acceptance check below.

## Image and desktop changes

- Purge Discover/Manage Software and its backends, KMahjongg, KMines, KPatience
  (`kpat`) and KSudoku from fresh images. No automatic dependency removal is run.
- Purge Kubuntu, Plasma Workspace and Breeze stock wallpaper packages, then seed
  Sentinel Gradient and Sentinel Navy Waves. Other third-party/custom wallpaper
  packages are left alone. Run cleanup again after package installation so KDE
  recommends cannot reintroduce stock wallpapers during the build.
- Keep a single editable account-level Sentinel Konsole profile. New platform
  packages no longer register a second global profile. Only exact shipped
  legacy global copies are removed; custom profiles remain.
- Remove Fastfetch's **Theme** row. Starship itself did not supply this row, so
  its prompt and colors are preserved.
- Use a bright light-blue shield with a soft blue glow and blue spinner. The
  logo/wordmark and spinner have separate positions. The packaged Plymouth
  two-step plugin and its disk-password UI assets remain in use.
- Replace the compiled Kubuntu welcome screen with **Try Sentinel Linux** and
  **Install Sentinel Linux**. Keep the existing compositor/session wrapper,
  portal repair, desktop startup and checked Calamares elevation command. This
  welcome app refuses to run on an installed Sentinel system. Its initial screen
  focuses on the two choices; use Calamares for language/keyboard settings and
  the live desktop for Wi-Fi setup before installation.

These image changes require rebuilding the ISO. Updating application DEBs alone
cannot replace a bootable ISO snapshot or remove its stock packages.

## Apply and build on the development laptop

Extract the downloaded bundle under `~/Downloads`, then:

```bash
cd "$HOME/Sentinel_Project"
git apply --check "$HOME/Downloads/sentinel-update-controls-and-branding/source.patch"
git apply "$HOME/Downloads/sentinel-update-controls-and-branding/source.patch"
```

Use a complete successful application build directory as `--reuse-inputs`.
The small uploaded archives omit models, wheels and DEBs needed for a real build.
Do not use an incomplete/failed build directory:

```bash
bash tools/build_debs.sh --reuse-inputs '/full/path/to/last-successful-build'
```

Set `SENTINEL_DEBS` to the exact new successful output printed by that command.
Use the complete original permissions kit on your laptop for pinned vendor
inputs. Build a fresh platform and kit rather than editing a prepared kit's
checksum manifest:

```bash
SENTINEL_DEBS='/full/path/to/new-successful-build'
bash build.sh platform \
  --kit "$HOME/Sentinel_ISO_Build/sentinel-iso-kit-0.1.0-9-permissions" \
  --debs "$SENTINEL_DEBS" \
  --output "$HOME/Sentinel_ISO_Build/platform-0.1.0-10"
SENTINEL_PLATFORM="$HOME/Sentinel_ISO_Build/platform-0.1.0-10/sentinel-platform_0.1.0-10_amd64.deb"
bash build.sh prepare --debs "$SENTINEL_DEBS" \
  --platform "$SENTINEL_PLATFORM" \
  --output "$HOME/Sentinel_ISO_Build/sentinel-iso-kit-0.1.0-10"
bash build.sh verify "$HOME/Sentinel_ISO_Build/sentinel-iso-kit-0.1.0-10"
```

The live welcome integration checks the known `start-kubuntu-live-env` command
before changing it. It stops for review if that upstream command changed.
Cleanup simulates APT removal first and aborts if it would remove actual desktop,
installer or protection dependencies. Removal of the empty Kubuntu desktop
meta-package is allowed; its installed desktop dependencies are retained.

## Publish the update channel

The configured channel is:

`https://github.com/Throckmorton05/Sentinel_Project/releases/download/apt-beta/`

The public read-only channel check returned **404 / no Release file** during
this work. The source configuration and public signing key are present, but
public release metadata is not currently available at that address. These
commands cannot download Sentinel releases until the signed assets are public.

Follow [github-updates.md](github-updates.md), using the new `0.1.0-10` core and
platform DEBs. Retain the existing signing key and fixed channel URL. If an older
successful local channel snapshot exists, pass its exact path with `--previous`
when creating the new snapshot. Sign on your development laptop, upload the
verified snapshot, publish the channel draft if necessary, then run:

```bash
python3 packaging/iso/check_github_channel.py --config configs/updates
```

After the public check passes, copy the complete new kit into Cubic. In
**Cubic's root terminal**, use the actual copied path:

```bash
bash /root/sentinel-iso-kit-0.1.0-10/build.sh image
```

Publish first when reusing a customized Cubic image: its initial APT refresh may
already include the configured Sentinel source, and the current 404 would abort
that refresh. Do not bypass APT authentication to build around that failure.

The existing publisher/uploader is retained; no production signing, release
upload or repository settings changes were performed here. Publication and a
real VM package-upgrade check remain required before declaring updates ready
for public users. Signing metadata still needs periodic refresh before expiry.

## Existing VM appearance cleanup

The new platform release supplies the splash and removes known duplicate global
profile copies. It intentionally does not reseed existing account appearance.
After the new platform is installed, run as the VM's normal desktop user:

```bash
python3 /usr/lib/sentinel-iso/desktop_polish.py
```

This backs up changed files, removes the user Fastfetch theme row, and deletes
only identical user-level Sentinel profile copies, keeping the configured
default. It leaves custom profile settings and all unrelated preferences intact.
Close and reopen Konsole afterwards. For a VM without established APT trust,
use the documented one-time maintainer bootstrap in `github-updates.md`; do not
run image provisioning against an installed system.

## Validation

Passed focused CLI, authentication/policy, APT-runner, schedule, App Store,
Portal and ISO-helper tests. GUI workers remain off the Qt UI thread and duplicate
update clicks are disabled during package work. Verified rendered splash and
live-welcome previews. No packages were installed into the development host.

The container uses Python 3.12 instead of target Python 3.14. Signing-agent and
verified-AdGuard-binary integration tests are unavailable here. A complete DEB
build, real public channel check after publication, Cubic boot, installer
launch/return, encrypted-disk prompt and installed-VM upgrade must be tested on
your build laptop/VM. Verify both update buttons in unlocked, Guardian and
Self-Lock modes and confirm saved policy/PIN/activity survives the upgrade.

Upstream behavior checked against the Ubuntu `kubuntu-installer-prompt` 26.04.3
source and Plymouth's two-step source:

- https://archive.ubuntu.com/ubuntu/pool/universe/k/kubuntu-installer-prompt/
- https://gitlab.freedesktop.org/plymouth/plymouth/-/blob/main/src/plugins/splash/two-step/plugin.c
