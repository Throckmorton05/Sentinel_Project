# Sentinel test ISO with Cubic

For the Wi-Fi welcome, Try-desktop black screen, missing `cdrom.sources`, and
USB GRUB titles, see [the Cubic repair guide](../../docs/cubic-live-repair.md).
`build.sh repair-live` updates an already configured Cubic image without DEB
reinstallation. `build.sh boot-branding PATH/custom-disk` runs on the host.
The welcome runs full-screen without a title bar over Plasma. Wi-Fi and installer
windows temporarily minimize it; Try dismisses it to the running desktop.

For the connected development PC → packages → platform → Cubic workflow, start
with [Development PC to ISO](../../docs/development-to-iso.md).
For signed updates of already installed systems, see
[docs/sentinel-updates.md](../../docs/sentinel-updates.md). The
`build.sh platform` command produces a sixth `sentinel-platform` DEB;
`prepare --platform PATH` includes it in new ISOs. Keep the original complete
kit as vendor/provenance input and generate a new kit for changed sources.
For GitHub Releases hosting, see [docs/github-updates.md](../../docs/github-updates.md).

These tools configure the extracted installer image. They do not
rebuild the five application Debian packages. Reuse
your fresh, successfully validated DEB build. The target is the normal Kubuntu
26.04 amd64 Calamares installer; OEM installation
is not supported.

## 1. Use the current source checkout

The repository already includes `build.sh` and `packaging/iso/`. On the host,
inspect the current command interface without applying an old patch:

```bash
cd "$HOME/Sentinel_Project"
bash build.sh --help
```

Ordinary project edits use `nvim` or your GUI editor;
root-owned configuration edits, when needed, use `sudoedit` on the development
machine. Cubic's terminal is already root and does not need sudo.

## 2. Prepare the input kit on your development system

Find the successful application build directory, not one ending in `-unchecked`:

```bash
cd "$HOME/Sentinel_Project"
ls -1d dist/debs/*/
```

The configured public update source makes a matching `sentinel-platform` DEB
required for new kits. Build it first using the current source, your new five
application DEBs, and an intact previously verified kit for vendor/provenance
inputs. A small upload that excludes DEBs or large inputs cannot serve as that
base. Follow the exact commands in
[platform and kit preparation](../../docs/development-to-iso.md#5-build-the-platform-deb-and-verified-iso-kit).

For reference, the preparation portion is below. Set `SENTINEL_DEBS`,
`SENTINEL_PLATFORM`, and a new `SENTINEL_KIT` output path as in that guide, and
run **without sudo**. Skip this block if preparation already succeeded there:

```bash
mkdir -p "$HOME/Sentinel_ISO_Build"
bash build.sh prepare \
  --debs "$SENTINEL_DEBS" --platform "$SENTINEL_PLATFORM" \
  --output "$SENTINEL_KIT"
bash build.sh verify "$SENTINEL_KIT"
tar -czf "$SENTINEL_KIT.tar.gz" \
  -C "$(dirname "$SENTINEL_KIT")" "$(basename "$SENTINEL_KIT")"
```

Preparation checks package names, architecture, matching versions, successful
build checksums, required graphics/configs, and all eight wallpaper dimensions.
It downloads checksum-pinned AdGuard Home, Starship, Fastfetch, and JetBrainsMono
Nerd Font assets. Downloaded archives are cached under `dist/iso/vendor-cache`.
The kit contains only selected inputs, not your entire checkout or development
home. Preparation needs Python 3.11 or newer, `dpkg-deb`, and HTTPS access to
GitHub release assets; those are normally present on your current build system.

Use a new output name for each attempt. If preparation fails, inspect its error;
do not reuse a partial kit. Do not edit a prepared kit, because its manifest will
reject changed or unlisted files. Edit source files and prepare a new kit instead.

The commands below use `sentinel-iso-kit` as an example archive/directory name.
Substitute the actual generated name, including its build ID.

## 3. Configure the extracted image inside Cubic

Continue with your extracted Kubuntu 26.04 amd64 ISO. Removing the Firefox and
Thunderbird snaps on Cubic's page is appropriate. The script also purges their
installed transitional DEB packages and Discover.

Before APT runs, the script also disables the known `file:///cdrom` media source
when present, preserving its contents as `cdrom.sources.disabled`. It checks
that this file contains only the media URI and refuses to disable mixed online
repositories. Kubuntu's late `rmcdrom` job is replaced with target-chroot cleanup
using `rm -f` for the active and disabled media-source files. At the end of image
setup the media source is staged at its original path with `Enabled: no` in every
stanza: Calamares can copy it without enabling the unavailable repository.
Rebuilding preserves the original backup and embedded signing key. If the stock
image has no media source, a complete disabled deb822 stanza is created.
Online repository signature checks stay enabled.

Use Cubic's file-copy dialog to copy `sentinel-iso-kit.tar.gz` from the host into
`/root` in the virtual terminal. Then run **inside Cubic**:

```bash
tar -xzf /root/sentinel-iso-kit.tar.gz -C /root
bash /root/sentinel-iso-kit/build.sh verify /root/sentinel-iso-kit
bash /root/sentinel-iso-kit/build.sh image
```

The image command refuses a normal host root filesystem and requires the
extracted Calamares image. It supports Cubic's shell-PID-1 environment by checking
the kernel's root mount field for a `custom-root` subtree, the Cubic hostname,
and absence of a running systemd instance. Traditional chroots retain the separate
PID-1-root check. It also refuses an already provisioned installation.
If it cannot establish Cubic's chroot boundary, stop and inspect the environment;
do not remove the guard. If PyYAML is missing, the script stops before image
changes; install it **inside Cubic** with `apt-get install python3-yaml` and retry.

APT and HTTPS must work inside Cubic. If package installation fails, retain the
error rather than generating an ISO from an incomplete image. The image command
temporarily inhibits package service startup and restores the original policy.
It enables service symlinks offline; it does not load firewall rules, replace
your host DNS, or launch the model services in the chroot.

The finalizer provisions only the installed target. Keep Cubic's normal live
installer functionality: the installed user's privilege restrictions do not
apply to the live rescue/install environment.

## 4. Finish Cubic and generate a test ISO

On Cubic's package-selection page, keep all five Sentinel packages, native Google
Chrome Stable, Steam, Neovim, Rofi, Fastfetch, KDE/SDDM/Wayland, Python/runtime/model
dependencies, and the networking/security packages installed by the script.
Do not remove them from the installed system. The Calamares app-selection jobs
are removed so the installer cannot restore Firefox or discard our bundled apps.
The locale, partition, bootloader, and other distro installation jobs remain.

Use a name such as `Sentinel-0.1.0-beta-test-amd64.iso`. This script prepares the
image; **Cubic generates the actual bootable ISO**. A generated ISO is not yet a
validated Sentinel release.

## What the image ships

| Area | Behavior |
| --- | --- |
| Apps | Your five DEBs; native Chrome Stable; Ubuntu's native Steam installer and i386 graphics dependencies; Neovim; Kate; Konsole; Rofi; Flatpak/Flathub |
| Terminal | Starship and Fastfetch settings; editable user-local Sentinel Konsole profile with navy background, 60% opacity, blur and white cursor; JetBrainsMono Nerd Font; shared Fastfetch logo; `EDITOR`/`VISUAL` set to nvim; Kate remains the graphical default |
| Desktop | Sentinel shield launcher icon; top, floating, opaque KDE panel; Breeze Dark with blue accent; blur enabled as an editable default; wobbly windows disabled; Konsole, Sentinel App Store, Dolphin, System Settings, Sentinel Portal and Chrome pinned in that order and seeded as menu favorites |
| Launcher | Compact 11-point, single-column, scrollable Rofi config, with an 18em by 24em window; `Ctrl+Space` shortcut using Plasma 6's service shortcut format |
| Wallpapers | Both selected designs as KDE wallpaper packages, each in 3840x2160, 3840x2400, 5120x2160, and 5120x1440; Gradient is the initial desktop/lock-screen background |
| Installer | Sentinel name, shield, navy/blue styling and onboarding slideshow; normal password-protected desktop user; no auto-login or administrator groups; explicit `${USER}` passed to the Sentinel finalizer immediately before unmount |
| Onboarding | Start Here — Sentinel offline guide in the application menu and a removable shortcut in the new user's localized desktop folder; protection switches start off |
| Privileges | Locked root password; explicit denial of sudo/sudoedit for the desktop account; narrow polkit allowlist for ordinary local desktop functions; SSH and Xrdp login disabled; console/root graphical login denied |
| Chrome | Root-owned native package; installed-system APT guard rejects removal, removal caused by dependency changes, and downgrades of Chrome and critical platform packages |
| DNS | Unique AdGuard API credentials and bcrypt hash per installation; private local control endpoint; non-login DNS account; local resolver; root-owned nftables rules redirect ordinary IPv4/IPv6 DNS and reject common encrypted-DNS transports |
| Sessions | SDDM offers KDE Wayland only; PAM session limit plus root logind guard keeps the earliest normal KDE Wayland session and closes additional or unsupported login sessions |
| Startup | Display manager requires DNS policy application, plus healthy model services only when image filtering is enabled; the session guard closes SDDM if required platform services stop |
| Boot | GRUB editing restricted with a per-install random credential; recovery entries disabled; ordinary Linux boot remains password-free; APT hook reapplies this integration after package transactions |

The font, AdGuard, Starship, and Fastfetch releases are pinned in
`vendor-lock.json`. Chrome, Steam, Neovim, and distro dependencies are obtained
through their configured signed APT repositories, so those versions can change
between builds. The kit manifest records the staged inputs. All vendor refreshes
require a reviewed lock-file update and another test build.

The pictures are static desktop/lock-screen wallpapers. This patch does not add
an animated screen saver. Rofi keeps the navy/blue palette and 90% background
opacity with tighter padding, smaller icons and a shorter hint. Each new user's
`~/.config/rofi/config.rasi` is a normal editable copy. Konsole's profile and
palette are also copied into the user's data directory and explicitly selected
after the installer creates the account. No appearance config is periodically
reapplied or marked immutable. The protected KWin monitor bridge remains locked.

To preview the defaults in an existing VM, run `desktop_defaults.py --assets
/path/to/assets` as the normal user from the VM's Konsole. It saves existing
target configs under `~/.local/state/sentinel/desktop-backups`, changes only the
requested user settings, refreshes the KDE app cache, and pins the applications
through Plasma's live scripting API. It briefly restarts the user's shortcut
service if running; it never stops system services or the monitor. Close all
Konsole windows and reopen it afterwards. Existing Kickoff favorites remain;
the new favorite defaults apply before its first-use import. VM rendering and
shortcut behavior still need runtime verification.

Reference: https://develop.kde.org/docs/administration/menu/ documents the
editable `kicker-extra-favoritesrc` distribution defaults. Plasma 6's
`kglobalacceld` service-action implementation uses `[services][application.desktop]`
and `X-KDE-Shortcuts` for application shortcuts.

## VM acceptance checks before a release

Create a fresh VM with a snapshot, working virtual GPU/Wayland, networking, and
sufficient memory for all four image models. Install to its disposable disk.
Test both UEFI and legacy BIOS if you intend to support both. Ordinary boot must
not request the discarded GRUB editing credential. Keep the original installer
ISO and VM snapshot for repairs; the installed desktop has no administrator
recovery account.

After installation and reboot, run as the installed desktop user:

```bash
id
echo "$XDG_SESSION_TYPE"
loginctl list-sessions
systemctl --no-pager --full status \
  sentinel-first-boot.service sentinel-dns.service \
  sentinel-dns-firewall.service sentineld.service \
  sentinel-session-guard.service
sen state
sen dns status
```

Confirm the account is outside all administrative/device/VM groups, the session
is Wayland, the services are active, and DNS policy reports `applied`. Open Portal
and App Store; check that approved app installation works without exposing a root
shell or polkit administrator prompt. Chrome and Steam must launch normally;
the native Steam installer downloads its client for the user on first launch.
Verify the forced Chrome extensions, Safe Steam integration, KWin bridge, screen
monitor, and the actual model-driven screen-blocking behavior.

Check the top panel and icon, blue dark scheme, both wallpaper choices, Konsole
font/colors/transparency, Starship segments, Fastfetch shield, and `Meta+Space`
launcher. Confirm the Rofi menu scrolls and is a single column. Compare at normal
and high DPI settings, and with a second monitor attached.

Expected privilege-denial checks:

```bash
sudo -n /usr/bin/id -u
sudoedit -n /etc/sentinel/settings.toml
pkexec /usr/bin/id -u
sen uninstall chrome
```

These must fail without granting root or removing Chrome. In a disposable VM,
also test protected dependency removal, approved app updates, a kernel/GRUB
update, and reboot after the update. The first installed user must remain
unprivileged after all package transactions.

Test root graphical login, TTY login, a second SDDM session, and remote login.
These login paths must stay denied or be closed. Stop a protection service from
a separate maintenance/test harness and verify that the user desktop closes.
Test model failure and DNS startup failure; the display manager must not expose
an unprotected desktop. Verify the monitor resumes correctly after suspend.

Test queries to an explicitly selected external DNS server over both IPv4 and
IPv6: they must still receive Sentinel's policy through local redirection.
Check blocklisted names, ordinary allowed names, and the Chrome safe-search
baseline. Do not use mere internet connectivity as evidence of DNS enforcement.
Inspect nftables rules in the maintenance harness; runtime nftables behavior
has not been exercised in this development container.

Make two clean installations and have the maintenance harness compare their
private credential files without printing passwords; the credentials and GRUB
hashes must differ. Reboot the same installation and confirm credentials, user,
settings, and lock state are preserved.

## Security scope and remaining release work

This is a baseline integration for a **test ISO**, not a proof of a bypass-free
general-purpose Linux desktop. The scripts restrict supported administrative,
installer, resolver, and login paths. They do not stop arbitrary user code from
using HTTPS DNS, an HTTPS proxy/tunnel, static server IPs, user-installed apps,
or manually launched/nested compositors that do not create a normal logind login
session. A mandatory application/network policy and compositor/monitor tamper
review are needed to meet the stronger "non-bypassable" requirement. A transport
redirect alone cannot distinguish ordinary HTTPS from DNS carried over HTTPS.

The existing Sentinel product lock policy is preserved: initial state is
Unlocked, with every registered protection switch explicitly off, including
DNS and its categories, image filtering, browser content protections, Safe
Installs, boot-media protection, and VM protection. These defaults are copied
from `fresh-install-settings.toml` only into an unprovisioned image. Startup and
package updates never reset a user's saved choices. Account, login, native
Chrome/Google, resolver, and package-integrity platform rules still apply.
This build does not irreversibly enter Self Lock.
Unlocked-mode authorized settings changes remain possible through Sentinel.
Choose and validate the product's intended onboarding/lock behavior before a
public release; do not treat absence of sudo as equivalent to Self Lock.

For the `0.1.0-9` rebuild and fresh-install acceptance checks, see
[`docs/iso-onboarding.md`](../../docs/iso-onboarding.md).

GRUB restrictions do not secure firmware settings or prevent external boot,
offline disk edits, or hardware attacks. Secure Boot, firmware control, disk
encryption/recovery design, distribution/update signing, hardware driver
coverage, and security review remain release decisions. Native Chrome/Steam and
all redistributed components also need release/distribution review; the image
includes the bundled font/AdGuard/Starship/Fastfetch notices available to this
pipeline. Keep upstream source and license obligations in the release process.

No host DEB build, Cubic image build, installed VM boot, Calamares installation,
GPU/model-monitor operation, or runtime firewall test was executed here.

## Validation included with this patch

```bash
python3 -B -m unittest discover -s packaging/iso/tests -v
```

The standard suite uses real disposable DEB metadata and temporary files to test
manifest tampering, host/chroot refusal, incomplete/mixed/modified package builds,
Calamares sequence preservation and idempotence, APT protocol validation,
protected removal/downgrade rejection, approved dependencies, session selection,
and GRUB generator adaptation. It never installs a package or alters host policy.

The optional real-vendor test additionally requires PyYAML and a checksum-verified
AdGuard binary supplied in `SENTINEL_TEST_ADGUARD_BINARY`. It starts that binary
with temporary config/state, random high loopback ports, and a dead loopback
upstream, then checks authenticated API access and local filter registration.
No production DNS service or port-53 listener is used.

During development, that optional test passed against the pinned AdGuard
release; all pinned archive checksums were verified; kit preparation passed with
the real assets and disposable five-package fixtures; and the real pinned
Starship/Fastfetch binaries accepted your configs. Shell, Python, and panel JS
syntax passed. Systemd dependency ordering passed a static check with executable
paths substituted by harmless stubs. These results support trying the test ISO;
they do not replace the VM checks above.

Upstream integration references:

- Calamares shellprocess configuration: https://github.com/calamares/calamares/blob/master/src/modules/shellprocess/shellprocess.conf
- Calamares users implementation: https://github.com/calamares/calamares/blob/master/src/modules/users/Config.cpp
- AdGuard configuration and local filter allowlist: https://github.com/AdguardTeam/AdGuardHome/wiki/Configuration
- KDE panel scripting: https://develop.kde.org/docs/plasma/scripting/api/
- Flatpak system remote setup: https://docs.flatpak.org/en/latest/using-flatpak.html
