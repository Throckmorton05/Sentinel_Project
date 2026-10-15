# Cubic live repair A — 2026-10-15

## Full-screen welcome update

The latest bundle shows a full-screen welcome without a title bar over the
running Plasma desktop. Try closes the welcome. Opening Wi-Fi or the installer
minimizes it so those windows are accessible; closing Wi-Fi or an installer
failure restores the full-screen welcome. Small displays can scroll the card.
Plasma still starts first, so a brief desktop appearance during startup is
possible. This is a visual update, not a return to a separate welcome compositor.

If you already ran repair A, download the updated archive again, overwrite the
older archive inside Cubic and rerun the same two commands in section 2. No DEB
rebuild is required. Boot-menu branding does not need repeating if it is still
correct. On a previously patched source checkout, do not reapply the cumulative
source patch; use the incremental `sentinel-welcome-fullscreen.patch` instead.

This revision repairs the existing Sentinel Cubic image and updates the source
build workflow. It does not require rebuilding the five Sentinel DEBs or the
platform DEB for this test ISO. Apply the source patch before future kit/platform
builds so those builds retain the fixes.

## Changes

- Start Plasma through the retained Kubuntu wrapper before showing Sentinel's
  welcome through KDE autostart. The Try button closes the welcome and exposes
  the running desktop. The old code merely exited the welcome app inside a
  separate `kwin_wayland` process, relying on compositor shutdown to proceed.
  That is a plausible software cause of the reported black screen; hardware
  graphics problems still require a real boot test.
- Add Connect to Wi-Fi. It opens KDE's network applet in a separate window and
  minimizes welcome while you connect. The desktop panel remains accessible.
  NetworkManager status refreshes asynchronously. Local connectivity is not
  presented as independently verified internet access. Offline installation
  remains available; optional downloads still require a connection.
- Retain a syntactically valid, disabled `cdrom.sources` for Calamares' existing
  copy job. All media stanzas have `Enabled: no`; original inline signing keys
  and the build backup are preserved. Mixed online/media source files are
  refused. The existing target cleanup removes the active and backup files.
- Add `build.sh repair-live`, guarded to run only in an already configured
  Cubic image. It starts no services in the chroot. NetworkManager is enabled
  for subsequent boot. Installed-system lock and session protections remain.
- Add `build.sh boot-branding` for the HOST's Cubic `custom-disk` directory.
  It changes GRUB and Syslinux menu titles only, preserving boot paths, kernel
  arguments, IDs and search labels.

## 1. Save the permanent source patch on your main laptop

Download `sentinel-live-repair-a.patch` and `sentinel-live-repair-a.tar.gz` into
Downloads. In your actual Sentinel source checkout, run without sudo:

```bash
cd /home/carter/Sentinel_Project
git apply --check "$HOME/Downloads/sentinel-live-repair-a.patch"
git apply "$HOME/Downloads/sentinel-live-repair-a.patch"
bash build.sh --help
```

The patch targets `Sentinel_Project-current-20261014-094041.zip`. If your active
checkout is the newer `Sentinel_Public_Release/sentinel-fresh-history/Sentinel_Project`
directory, use that directory instead. If the check reports a mismatch, stop;
do not force it. The standalone bundle below can still repair the current
Cubic image independently of your checkout.

The previously saved ISO-kit ZIP omits the Sentinel DEBs, platform DEB and
Fastfetch DEB. It is not a complete input kit. This repair bundle contains only
the needed scripts and installer assets; do not run its `image` or `verify`
commands. Keep your original complete kit for future full builds.

## 2. Repair the existing image in Cubic

Open the SAME Cubic project used for the failed ISO and return to its virtual
terminal. Copy `sentinel-live-repair-a.tar.gz` into `/root` using Cubic's file
copy dialog. Run these commands INSIDE Cubic, where you are already root:

```bash
tar -xzf /root/sentinel-live-repair-a.tar.gz -C /root
bash /root/sentinel-live-repair/build.sh repair-live
```

Require a successful result before generating the ISO. This command checks
`image-built.json`, the normal installer launcher, the known live wrapper,
PyQt availability, the Ubuntu archive keyring, and network packages. Online APT
sources can use any filename; this repair does not change them. If it reports
missing network packages, install them inside Cubic and retry:

```bash
apt-get update
apt-get install -y --no-remove network-manager plasma-nm plasma-workspace
bash /root/sentinel-live-repair/build.sh repair-live
```

It deliberately does not run `apt upgrade` or replace installed Sentinel
packages. A new blank Cubic project must first be configured using a complete
kit and `build.sh image`; this repair command is for the existing image.

Optional checks inside Cubic:

```bash
cat /usr/lib/sentinel-iso/live-repair.json
cat /etc/apt/sources.list.d/cdrom.sources
cat /etc/xdg/autostart/sentinel-live-welcome.desktop
cat /usr/libexec/start-kubuntu-live-env
```

The media source must contain `Enabled: no`. The live wrapper should retain
its original Plasma startup and contain the comment `Sentinel welcome runs
after Plasma starts.` It must no longer launch the welcome inside a separate
`kwin_wayland --xwayland --no-lockscreen` invocation.

## 3. Rebrand the USB boot menu on the HOST

These files belong to Cubic's ISO tree outside its virtual-terminal root.
Extract the same repair archive on the main laptop, then set the actual Cubic
project directory (the folder containing `cubic.conf` and `custom-disk`):

```bash
mkdir -p "$HOME/Sentinel_ISO_Repair_A"
tar -xzf "$HOME/Downloads/sentinel-live-repair-a.tar.gz" \
  -C "$HOME/Sentinel_ISO_Repair_A"
CUBIC_PROJECT="/full/path/to/your/Cubic-project"
sudo bash "$HOME/Sentinel_ISO_Repair_A/sentinel-live-repair/build.sh" \
  boot-branding "$CUBIC_PROJECT/custom-disk"
```

Run this after image repair and before Cubic generates the ISO. If Cubic has
already loaded a Boot editor, reopen that page and check the resulting titles
before generating, so stale editor content does not overwrite the change.
If that Cubic version exposes a different disk layout, use Options → Boot to
change only the visible titles in `boot/grub/grub.cfg`, `boot/grub/loopback.cfg`,
and any BIOS menu files it exposes. Preserve the command lines underneath.

## 4. Generate and test

Keep Sentinel packages, Plasma, NetworkManager and `plasma-nm` on Cubic's Packages
page. Generate a new ISO such as `Sentinel-0.1.0-beta-repair-a-amd64.iso`. Flash
the new ISO onto the USB using your normal image writer; copying the archive
onto the old installer USB does not update its live filesystem.

Before another installation, verify:

1. The initial USB boot-menu titles say Sentinel Linux.
2. Plasma loads and the full-screen Sentinel welcome opens without a title bar.
3. Connect to Wi-Fi minimizes welcome, opens the network chooser and accepts a
   connection. Closing the chooser restores full-screen welcome.
4. Try Sentinel closes welcome and leaves a working desktop.
5. Install Sentinel minimizes welcome and opens Calamares. An offline test should get past the old
   media-source copy failure and finish, subject to any optional downloads.
6. After installation, reboot without the USB. Confirm Portal/backend health,
   DNS behavior and the normal installed account restrictions.

If the desktop still goes black, capture these from the LIVE session's Konsole
when accessible, or a live TTY after logging into the live account:

```bash
echo "$XDG_SESSION_TYPE"
pgrep -a -f 'start-kubuntu-live-env|startplasma|kwin_wayland|plasmashell|sentinel-live-welcome'
journalctl --user -b --no-pager -n 180
sudo journalctl -b -u sddm --no-pager -n 120
```

Do not launch another compositor from inside an existing Wayland session.
Those logs distinguish Plasma startup, missing desktop components and graphics
driver errors. This patch does not install GPU drivers based on a guess.

## Validation and limits

Focused regression tests exercise the real missing/present media-file copy,
repeated builds, embedded-key preservation, mixed-source rejection, execution
of the rewritten stock and old Sentinel wrappers reaching a fake Plasma
executable, and boot title changes with command preservation. These are local
file and shell tests, not proof of an actual Wayland boot or successful disk
installation. The four focused suites passed all 50 tests. A broader run of 71
tests had 66 passes, four skips and one dependency error (`tomli_w` is absent),
so the full suite is not claimed as passing. The environment has Python 3.12, not
the project's Python 3.14 runtime, and lacks PyQt6. Qt rendering, Wi-Fi secret
agent behavior, real Calamares completion and physical hardware remain untested.

Reference: APT's [deb822 source documentation](https://manpages.debian.org/trixie/apt/sources.list.5.en.html)
defines `Enabled: no`; KDE documents [testing applets with plasmawindowed](https://develop.kde.org/docs/plasma/widget/testing/).
