# Debian packages: first Kubuntu 26.04 build

For the connected checkout, development runtime, packages, platform kit, and
Cubic workflow, start with [Development PC to ISO](development-to-iso.md).

These definitions target **Kubuntu/Ubuntu 26.04 amd64 and distro Python 3.14.2+**.
They build packages from the source checkout; they do not build an ISO, install
anything onto the build host, or select a desktop account automatically.
The first complete build and real KDE/Wayland installation must be verified in
a disposable VM before treating this as a release.

## Package layout

| Package | Contents |
| --- | --- |
| `sentinel-core` | Root-owned Python environment, backend, CLI, GUI modules, inference API code, systemd units, setup/lifecycle helpers |
| `sentinel-assets` | Public CRX/update assets, catalogs, branding, first-install defaults, DNS seed data |
| `sentinel-models` | Three ONNX classifiers and Hentai metadata; NudeNet's default detector comes from its runtime wheel |
| `sentinel-desktop` | Desktop launchers, system KWin script, screenshot authorization, KDE/Qt dependencies |
| `sentinel` | Metapackage depending on all four components |

The monolithic Python wheel remains intact inside the core package. Separating
public assets/models avoids distributing them inside the wheel. Versioned
component dependencies keep the installed package set consistent.

Chrome Stable, AdGuard Home, Steam, Rofi, Kate, and optional Neovim are separate
applications. Do not bundle their installers or your development environment.
Chrome and AdGuard Home are prerequisites for the supported complete runtime.
The current ISO/platform integration provisions them and resolver routing for
the installed distribution. These five application packages alone do not.

## Prepare the build machine

Run these on your Kubuntu **development host**, where you already use Poetry:

```bash
sudo apt update
sudo apt install build-essential debhelper dpkg-dev fakeroot lintian \
  binutils patchelf python3.14 python3.14-venv python3.14-dev desktop-file-utils
poetry self add poetry-plugin-export
```

If Poetry is managed by pipx instead of its own installer, use
`pipx inject poetry poetry-plugin-export` instead of `poetry self add`.

Do not run the build with sudo. Build tools use network access to obtain hashed,
locked dependency wheels. Package installation does not run pip or download
models. The script requires a compatible binary wheel for every runtime
dependency; it stops if one is unavailable rather than compiling an unreviewed
fallback or changing your lockfile.

## Required model and extension inputs

The complete build requires:

```text
sentinel/image_blocking/models/openNSFW2_ONNX_Model/open_nsfw.onnx
sentinel/image_blocking/models/anime_real_cls/mobilenetv3_v1.4_dist/model.onnx
packaging/models/hentai-vit/model.onnx
```

The Hentai model is intentionally absent from ordinary source ZIP exports.
Stage your **already converted ONNX artifact**, not the original safetensors
weights. If your model inputs are tracked through Git LFS, run `git lfs pull`.
The build rejects pointer files, symlinks, empty files, and obviously truncated
models. The APIs' actual load and preprocessing behavior must still be tested.

All five public extensions must have their existing pre-signed CRX3 packages
and matching update XML. The native Safe Steam bundle must match its integrity
manifest. Package assembly uses explicit file allowlists and rejects project
PEM/KEY files. The runtime retains Certifi's public `cacert.pem` CA bundles for
HTTPS verification, including Pip's vendored copy; the final audit permits only
those two exact paths, checks their PEM labels and parses their certificates.
Private-key PEM blocks remain forbidden.
Don't create new signing identities to bypass a missing asset.

## Build

```bash
cd ~/Sentinel_Project
bash tools/build_debs.sh --check
bash tools/build_debs.sh
```

Output goes into a new `dist/debs/DEBIAN_VERSION-YYYYMMDD-HHMMSS/` directory, containing
five `.deb` files, `.changes`/`.buildinfo`, SHA256SUMS, the exported requirements,
Poetry lockfile, dependency wheelhouse and freshly built project wheel.
`--output /absolute/path` changes the parent output directory. Outputs from
different builds are kept in separate directories, preventing accidental
mixing of package revisions during installation.

The wrapper exports only Poetry's main dependency group with hashes, downloads
matching binary wheels, builds a fresh Sentinel wheel and invokes:

```bash
dpkg-buildpackage -us -uc -b
```

The Debian install target stages the environment into the package, checks its
dependency consistency, rewrites entry points to the installed prefix, copies
the dedicated screen-monitor interpreter, and checks NudeNet's bundled model.
The wrapper validates desktop files and runs Lintian, failing on error-level
findings. Warnings remain visible and should be reviewed before publication.
Lintian runs verbosely without truncating repeated findings. The package build
sets library/imported-source modes to 0644, preserves native/console executable
modes, declares generic Python as well as the exact interpreter, and strips
unneeded ELF debug symbols with debhelper. Original wheel archives remain intact.
Stale manylinux build-machine search paths are removed with PatchELF; private
`$ORIGIN` paths are preserved and checked to remain inside the environment.
The changes are recorded in `/usr/lib/sentinel/elf-relocations.json`. Scientific
operations and key Qt, image and ONNX imports are exercised after relocation.

The private wheel runtime deliberately bundles upstream codec/font libraries.
Its embedded-library Lintian exception is restricted to the wheel site-packages
directory, plus Expat in the separately authorized copy of distro Python. The
package trigger refreshes that interpreter when Python updates. Bundled wheel
libraries require lockfile updates and a Sentinel rebuild to receive fixes.
No search-path, permissions, stripping or Python-dependency errors are overridden.
Completed build files are saved before these checks. If validation fails, the
output directory ends in `-unchecked` and contains `VALIDATION_FAILED.txt` with
the reason. Keep those files for diagnosis; do not install them. A successful
audit renames the directory to the normal checked output name.
To rerun validation after fixing an audit issue, reuse the retained build:

```bash
bash tools/build_debs.sh --audit dist/debs/0.1.0-YYYYMMDD-HHMMSS-unchecked
```

This verifies the saved checksums, extracts the existing packages into a
temporary directory, and reruns the payload, desktop-file and Lintian checks.
It does not download dependencies, rebuild packages or install anything.
The retained packages contain the build-time scripts; changing application or
packaging behavior still requires a rebuild. Only audit fixes can use this path.
To rebuild after packaging changes using an existing dependency wheelhouse:

```bash
bash tools/build_debs.sh --reuse-inputs dist/debs/0.1.0-YYYYMMDD-HHMMSS-unchecked
```

This verifies the retained checksums and requires an identical lockfile and
requirements export. It builds a fresh project wheel and new Debian packages,
using the saved dependency wheels without downloading them again. The install
step still verifies the locked dependency hashes and pip dependency consistency.
Wheel-private shared libraries are excluded from public `shlibs` metadata.

Running `dpkg-buildpackage` directly requires the wrapper's prepared inputs;
use the wrapper for ordinary builds. To make a new package revision, update
`debian/changelog` while retaining the upstream version from `pyproject.toml`.
For a new upstream release, update both files.

## Fresh VM installation

Create a clean Kubuntu 26.04 amd64 guest and take a powered-off snapshot.
Copy one build-output directory into the guest, then run **inside the guest**:

```bash
cd /path/to/copied/build-directory
sha256sum -c SHA256SUMS
sudo apt install ./*.deb
dpkg-query -W -f='${binary:Package}\t${Version}\t${Status}\n' \
  sentinel sentinel-core sentinel-assets sentinel-models sentinel-desktop
sudo dpkg --audit
```

Every package must report `install ok installed`, and `dpkg --audit` must produce
no output. A version-only query can also list unpacked or half-configured
packages. If APT fails during configuration, repair that failure before moving
on; later APT operations will otherwise retry the failed maintainer script.

The packages create the service account/group and protected directories. They
seed settings and empty custom blocklists only when the files are absent.
They do not package your development Kate/Opera blocks or any existing lock,
Guardian PIN, schedules, activity database, or pairing secrets.

Directory checks accept Ubuntu's root-owned `root:syslog` group-writable
`/var/log` system parent without changing its permissions. Sentinel's own
`/var/log/sentinel` and other protected directories must still be root-owned,
real directories with no group/world write permission. A world-writable,
symlinked, non-root-owned or unexpectedly group-writable parent is rejected.

The backend is enabled for boot but has a systemd condition requiring an
explicit `/etc/sentinel/daemon.toml`. Initial installation does not start it.
The four inference units are installed without independent boot enablement;
the backend starts/stops them according to the saved Image Filtering setting.

## AdGuard Home and browser prerequisites

Install native Google Chrome Stable in the guest through its official package
and repository. Install AdGuard Home using its official release/install process
at the paths expected by Sentinel:

```text
/opt/AdGuardHome/AdGuardHome
/opt/AdGuardHome/AdGuardHome.yaml
/opt/AdGuardHome/userfilters/sentinel.txt
```

Complete AdGuard's local setup, choose your own administrator credentials,
configure loopback DNS on TCP/UDP port 53 and an explicit external upstream,
and permit the Sentinel file via `filtering.safe_fs_patterns` if required by
your AdGuard release. The private connection template is supplied without a
username/password. Create it inside the VM:

```bash
sudo install -o root -g root -m 0600 \
  /usr/share/sentinel/defaults/adguard.toml.example /etc/sentinel/adguard.toml
sudoedit /etc/sentinel/adguard.toml
```

Set `url` to AdGuard's **actual local control endpoint** and enter the credentials
you created. The template's port 3000 is an example, not a promise about which
port your finished AdGuard setup uses.

Verify AdGuard itself answers DNS before directing the guest's network profile
to it. Follow [browser policies](../sentinel/browser_policies/README.md) for
NetworkManager routing, filter-path permissions, and DNS checks. Generating
filters or authenticating to AdGuard does not establish resolver routing.
Packages deliberately do not rewrite network profiles, install external
programs from the Internet in postinst, or silently set default passwords.

## Select the protected account

Use the **guest's actual username**, which may differ from your host account:

```bash
sudo sentinel-setup --user YOUR_GUEST_USERNAME
```

Setup checks installed runtime/model/KDE assets and AdGuard's local API, adds
that account to the control group, writes `daemon.toml` only if absent, and
enables the system-installed KWin bridge in that account's preferences.
It starts the backend and recovery socket. Log out and back into **KDE Wayland**
so the group membership and KWin preference take effect.

An existing account selection is preserved; setup refuses to silently replace
it. Re-running setup does not reset settings, PINs, lock modes, or schedules.
If the protected user has a development script/symlink under
`~/.local/share/kwin/scripts/sentinel-window-bridge`, move it aside as that user
before setup. It would shadow the protected system package.

For later image provisioning, `--no-start` configures the account and KWin
without starting services or requiring AdGuard credentials. Complete AdGuard
and routing setup before the first protected login. `sudo sentinel-setup --check`
verifies runtime and local API prerequisites without changing configuration.

The dedicated root-owned Python executable and hidden desktop entry authorize
KWin ScreenShot2 for the monitor. The monitor still runs as the protected desktop
user; the model APIs run as the non-login service account. A distro Python
update queues an atomic refresh of the dedicated interpreter so it does not
retain an outdated copy. Both screen-time and
anti-tamper layer-shell windows select the distribution's PyQt/Qt libraries.

Guardian management is consolidated in the Portal. The website manager's
desktop entry and both old console entry points are removed on package upgrade.
The app-access desktop ID remains as a hidden recovery entry because the backend
validates that independent route before enabling allow-only apps. Generated
Guardian overrides preserve its hidden state. During an outage, the Portal
offers the same PIN-protected recovery dialog; its launcher is always direct.
Legacy module launchers redirect to the Portal rather than opening duplicate
management windows. Existing Guardian selections and credentials are preserved.

In Guardian Lock, every Portal window first displays a masked PIN entry screen.
The backend verifies entry before any settings or other pages are requested or
shown. Wrong PINs keep it locked; closing cancels entry. Direct page shortcuts
use the same gate. Backend outages hide cached pages and preserve the recovery
button; reconnecting in Guardian Lock requires another login. Entry does not
replace PIN authorization for protected changes.

The custom `X-KDE-DBUS-Restricted-Interfaces` property uses KConfig's comma
list format, not an XDG semicolon list. The single monitor permission must be
exactly `org.kde.KWin.ScreenShot2`, with no trailing semicolon. Refresh the
desktop user's application cache with `kbuildsycoca6 --noincremental` after
repairing an older entry, then retry capture with Image Filtering enabled.

The App Store requires the default system Flathub remote. Inside a fresh guest,
provision it explicitly before testing the catalog:

```bash
sudo flatpak remote-add --system --if-not-exists flathub \
  https://dl.flathub.org/repo/flathub.flatpakrepo
sudo flatpak update --system --appstream flathub
```

AdGuard activity ingestion orders requests by timestamp and deduplication
fingerprint. Simultaneous A/AAAA requests must not stop the collector or lose
events; replaying a collection pass must not duplicate them.

## VM checks

```bash
echo "$XDG_SESSION_TYPE"
systemctl status sentineld.service --no-pager
journalctl -u sentineld.service -b --no-pager
sen state
sen dns status
sentinel-portal
sentinel-appstore
```

Verify all four model `/health` endpoints on ports 8000–8003, KWin snapshots and
real capture, quarantine/recheck/restore, Portal/App Store startup, Chrome
policies/extensions, DNS routing, app blocks, schedules, and Guardian access.
Open and close both GUI apps through KDE's application menu as well as their
terminal commands. Check idle close and close during a background read; an
active read/install/update must finish before the window closes. GUI entry
points join Python-backed Qt workers and destroy the window before QApplication
so background callbacks cannot race widget destruction. A KDE crash notification
or new application core dump means this smoke test failed, even if a separate
terminal launch exits successfully.
Use disposable snapshots for permanent Self Lock. Keep host VM protection off
while developing with virt-manager; restrictions inside the guest do not turn
off the host hypervisor.

Upgrade a configured guest to a new Debian revision and verify that existing
configuration, lock state, PIN authentication and schedules survive. The package
uses a separate systemd worker to wait for APT/dpkg's POSIX locks to be released
before restarting running APIs and the backend. This prevents a backend-driven
upgrade from killing its own APT process. Disabled/stopped services are not
started by that upgrade worker. The helper exits after one hour if APT remains
busy; inspect `journalctl -u sentinel-package-restart.service` in that case.

Test removal/purge from a root terminal on a disposable guest. Removing core
stops services, disables only the Sentinel AdGuard filter subscription, unloads
owned AppArmor profiles, removes marked Guardian launcher/Kiosk overrides and
boot-media/VM device rules, and removes Sentinel's Chrome policy. Cleanup errors
stop removal with a diagnostic so edited files are not discarded silently.
Configuration/state are retained on removal and erased only on core purge.
The non-login service account/group are retained to avoid reassigning file
ownership to recycled IDs. External AdGuard installation, network profiles,
Steam profile provisioning and user desktop preferences are retained; close
and restart Chrome/Steam after removing Sentinel. User-created managed services,
autologin and automatic-update configuration should be disabled through Sentinel
before removal. The package cleanup does not delete unrelated system services.

VM success does not replace hardware checks for NVIDIA, multiple monitors,
physical USB devices, or the final distro's independent privilege/boot boundary.

## Local packaging regression tests

```bash
python3 -m unittest discover -s packaging/debian/tests -v
```

The tests do not install packages, modify host policy or use the network. They
check input rejection, public-asset staging, config preservation, interpreter
relocation, POSIX lock detection and conservative lifecycle behavior. They are
also run by `debian/rules` during a real package build.

## Redistribution metadata

`debian/copyright` records the source archive's declared terms without inventing
a project-wide license. The archive does not currently declare one. Its
drawn/photo model attribution also lacks an exact OpenRAIL variant/license text.
Resolve those metadata gaps before publishing packages; the packaging does not
change upstream terms. Third-party runtime wheel metadata/licenses are retained.
