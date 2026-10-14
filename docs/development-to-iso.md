# From a development PC to Debian packages and a Sentinel ISO

Start here to edit Sentinel, test it against a protected runtime, and build
release inputs. This guide connects the component instructions; it is not a
one-command installer. Commands marked **host** run on the development PC,
**guest** commands run in the disposable test system, and **Cubic** commands
run only in Cubic's extracted image terminal.

## 1. Choose the development environment

Before publishing source or artifacts, read [publication privacy](publication-privacy.md).
Project credits use a pseudonymous identity. Private extension/release keys stay
outside the checkout, and historical Git attribution requires a separate review.

The supported package-build host is Kubuntu/Ubuntu **26.04 amd64**, with distro
Python **3.14.2+ within 3.14**. Use KDE Plasma on Wayland to test native window
capture and blockers. Another environment may edit code, but is not the target
package/runtime environment. `pyproject.toml` and `poetry.lock` define Python
and project dependencies; do not resolve a new lockfile just to get started.

There are three distinct layers:

| Layer | Location | What it is for |
| --- | --- | --- |
| Editable checkout and project environment | `~/Sentinel_Project`, its `.venv` | Editing, tests, running development clients |
| Protected installed runtime | `/usr/lib/sentinel/venv`, `/etc/sentinel`, `/usr/share/sentinel` | Root backend, installed assets/models, desktop integration |
| ISO build inputs and extracted image | `~/Sentinel_ISO_Build`, Cubic's `custom-root` | Platform DEB, verified kit, installer image |

The installed root backend must not execute a desktop-user-writable checkout or
`.venv`. Conversely, do not edit installed Python files as your normal development
workflow. Edit source, build, and install the resulting artifact.

Use your existing configured PC for ordinary development. Test a first install,
package removal, permanent Self Lock, and distribution restrictions in a separate
disposable guest. VM success does not prove ScreenShot2 support, GPU behavior, or
multi-monitor privacy; native hardware checks remain necessary.

## 2. Get the checkout and project environment

On a new **host**, install distro prerequisites:

```bash
sudo apt update
sudo apt install git pipx build-essential debhelper dpkg-dev fakeroot lintian \
  binutils patchelf python3.14 python3.14-venv python3.14-dev \
  desktop-file-utils libpam0g libgl1 libegl1 libxkbcommon0 \
  libxcb-cursor0 libxcb-xinerama0
pipx ensurepath
```

Open a new terminal after `pipx ensurepath`. If Poetry is not already installed,
install it as your normal user, then add its export plugin:

```bash
pipx install poetry
pipx inject poetry poetry-plugin-export
```

Keep an existing working Poetry installation. For the official-installer method,
use `poetry self add poetry-plugin-export` instead of `pipx inject`. Upstream
[Poetry installation documentation](https://python-poetry.org/docs/#installation)
explains the two methods.

Clone only if you do not already have a checkout:

```bash
git clone https://github.com/Throckmorton05/Sentinel_Project.git "$HOME/Sentinel_Project"
cd "$HOME/Sentinel_Project"
poetry config virtualenvs.in-project true --local
poetry env use /usr/bin/python3.14
poetry install --with dev
poetry run python --version
```

For an existing checkout, begin with `git status` and keep local work intact;
do not replace it with a fresh archive. `poetry install` installs the project
and locked dependencies, but does not configure a daemon, DNS, models, system
services, or a protected account. If Poetry already uses an environment outside
the checkout, inspect `poetry env info --path`; use `poetry run` rather than
assuming `.venv` has moved.

Edit ordinary source with `nvim` or your GUI editor. Use `sudoedit` for protected
configuration, such as `/etc/sentinel/adguard.toml`. Keep Kate as the default
graphical editor; a terminal editor preference does not require changing GUI
file associations.

## 3. Test and run editable code

On the **host**, run a focused suite first:

```bash
QT_QPA_PLATFORM=offscreen poetry run python -m pytest \
  sentinel/daemon/tests/test_policy.py \
  sentinel/daemon/tests/test_image_filtering.py -q
```

Select other backend, Portal, App Store, or packaging suites for the changed area.
For example:

```bash
QT_QPA_PLATFORM=offscreen poetry run python -m pytest sentinel/portal/tests -q
poetry run python -m unittest discover -s packaging/debian/tests -v
poetry run python -m unittest discover -s packaging/iso/tests -v
```

The offline Start Here guide can be previewed without a backend:

```bash
poetry run sentinel-portal --guide
```

Against an existing configured protected runtime, run source clients normally:

```bash
poetry run sentinel-portal
poetry run sentinel-appstore
poetry run sen state
```

These clients talk to the installed daemon. Opening a source Portal proves your
GUI edits are loaded; it does not mean backend edits are loaded. Without a
configured backend, normal clients report backend unavailability. See
[daemon architecture](../sentinel/daemon/README.md) for the process boundary.

### Put backend edits into an existing development runtime

Use this quick loop only when the protected runtime, models, system assets, and
dependencies are already installed and compatible. For dependency, systemd,
model, or packaging changes, use a new Debian build instead.

For a **service-based development installation**, close clients, then:

```bash
cd "$HOME/Sentinel_Project"
poetry build -f wheel
sudo systemctl stop sentineld.service
sudo /usr/lib/sentinel/venv/bin/python -m pip install \
  --no-deps --force-reinstall dist/sen-0.1.0-py3-none-any.whl
sudo systemctl start sentineld.service
```

The filename uses the current upstream version; update it if `pyproject.toml`
changes. `--no-deps` does not install new requirements. If inference API code
changed, already-running model services also need a controlled restart while
preserving saved enabled/disabled intent; the Debian upgrade lifecycle handles
this more completely than a wheel-only replacement.

For your **foreground development daemon**, stop the old foreground process
with Ctrl+C instead of the service commands above, install the wheel, then use
the [existing development launch instructions](../README.md#development-runtime).
The `SENTINEL_DEV_ALLOW_FIREFOX=1` override is for development only; the supported
distribution baseline uses Chrome. Choose one daemon instance, not both launch
methods. Do not reset settings, PINs, or lock state to test source changes.

### First protected runtime on a new machine

Use the five application Debian packages in a clean test system. Follow
[fresh VM installation](debian-packaging.md#fresh-vm-installation), then
[AdGuard/browser prerequisites](debian-packaging.md#adguard-home-and-browser-prerequisites)
and [protected account selection](debian-packaging.md#select-the-protected-account).
Those steps install native Chrome, configure real local AdGuard credentials and
DNS routing, run `sudo sentinel-setup --user YOUR_GUEST_USERNAME`, and log into
KDE Wayland again for group/script changes. There is no default Guardian PIN.

The existing source-only asset installers assume prerequisites and are not a
complete fresh-PC bootstrap. Do not install `sentinel-platform` on an ordinary
development desktop just to obtain a backend: it includes ISO-specific startup,
APT, and session integration.

## 4. Build the five application Debian packages

On the **host**, obtain the complete public build inputs first:

- OpenNSFW2 and drawn/photo ONNX files at the source paths listed in
  [required model inputs](debian-packaging.md#required-model-and-extension-inputs).
- The converted Hentai ONNX file at `packaging/models/hentai-vit/model.onnx`;
  see [model staging](../packaging/models/hentai-vit/README.md).
- Existing signed public CRX3/update assets for all five extensions and the
  integrity-checked native Safe Steam bundle.

The small source archives omit models and prior build artifacts. Cloning alone
does not guarantee those files exist. Use the project's approved model/public
asset distribution or obtain the verified inputs from the maintainer. If the
project tracks an input through Git LFS, install Git LFS and fetch that input.
`download_models.py` is a placeholder. Do not generate replacement extension
signing identities merely to make a build pass.

Build **without sudo**:

```bash
cd "$HOME/Sentinel_Project"
bash tools/build_debs.sh --check
bash tools/build_debs.sh
```

For a subsequent build with an unchanged lockfile/requirements export, reuse a
retained complete wheelhouse:

```bash
bash tools/build_debs.sh --reuse-inputs '/absolute/path/to/retained/build'
```

Use the exact successful output printed by the builder. It contains five matching
DEBs, retained wheels, exported requirements, checksums, and build metadata.
Do not use an output marked `-unchecked`/`VALIDATION_FAILED.txt` as a release.
Version comes from `debian/changelog`; preserve unique package versions for
different released bytes. See [Debian packaging](debian-packaging.md) for audit,
input reuse, package contents, and guest install/upgrade/removal checks.

## 5. Build the platform DEB and verified ISO kit

The sixth package, `sentinel-platform`, carries distribution integration and
public update trust. It is separate from the five-package application build.

**Current prerequisite:** its builder requires a complete previously verified
ISO kit for pinned vendors, wallpapers, and migration provenance. A small kit
upload that excludes DEBs or large inputs is not sufficient. Obtain a complete
kit from the maintainer or retain your existing successful one.

There is a fresh-bootstrap gap in the current tooling: with `configs/updates`
present, `prepare` requires a platform DEB, while `platform` requires a verified
kit. This guide uses that existing complete kit rather than pretending a clean
clone can resolve the cycle. A documented, independent vendor/provenance bootstrap
is still needed for fully independent first-time ISO reproduction.

On the **host**, set real paths and use new output names for every attempt:

```bash
cd "$HOME/Sentinel_Project"
SENTINEL_DEBS='/absolute/path/to/new/successful/application-build'
SENTINEL_BASE_KIT='/absolute/path/to/complete/verified/prior-kit'
SENTINEL_BUILD_ID="$(date +%Y%m%d-%H%M%S)"
SENTINEL_VERSION="$(dpkg-parsechangelog -S Version)"
SENTINEL_PLATFORM_DIR="$HOME/Sentinel_ISO_Build/platform-$SENTINEL_BUILD_ID"
SENTINEL_KIT="$HOME/Sentinel_ISO_Build/sentinel-iso-kit-$SENTINEL_BUILD_ID"

bash build.sh verify "$SENTINEL_BASE_KIT"
bash build.sh platform --kit "$SENTINEL_BASE_KIT" \
  --debs "$SENTINEL_DEBS" --output "$SENTINEL_PLATFORM_DIR"
SENTINEL_PLATFORM="$SENTINEL_PLATFORM_DIR/sentinel-platform_${SENTINEL_VERSION}_amd64.deb"
bash build.sh prepare --debs "$SENTINEL_DEBS" \
  --platform "$SENTINEL_PLATFORM" --output "$SENTINEL_KIT"
bash build.sh verify "$SENTINEL_KIT"
tar -czf "$SENTINEL_KIT.tar.gz" \
  -C "$(dirname "$SENTINEL_KIT")" "$(basename "$SENTINEL_KIT")"
```

The prior kit supplies verified vendor/provenance inputs; current source supplies
the new helpers and relevant branding. All six DEBs must match the new version.
Do not edit prepared kit files or their manifest; change source and prepare a
new kit. Keep the original complete kit intact.

For signed updates, authorized maintainers follow
[GitHub channel publication](github-updates.md) with the new core/platform DEBs,
the existing release key, and the previous successful snapshot when available.
Ordinary contributors need no private signing key to edit/test code or build
packages. Public update verification is:

```bash
python3 packaging/iso/check_github_channel.py --config configs/updates
```

A public source repository is not sufficient: signed APT release assets must
also be published and pass this check. Reused customized Cubic roots can already
have that APT source, so publish/verify before their initial refresh. Do not
bypass APT signatures to work around an unavailable channel.

## 6. Let Cubic generate the bootable ISO

Start a new Cubic project from the original Kubuntu 26.04 amd64 Calamares ISO.
Copy the complete kit archive into `/root` using Cubic's file-copy interface.
Use its actual filename, then run only in the **Cubic root terminal**:

```bash
tar -xzf /root/sentinel-iso-kit-YOUR_BUILD_ID.tar.gz -C /root
bash /root/sentinel-iso-kit-YOUR_BUILD_ID/build.sh verify \
  /root/sentinel-iso-kit-YOUR_BUILD_ID
bash /root/sentinel-iso-kit-YOUR_BUILD_ID/build.sh image
```

Replace `YOUR_BUILD_ID` with the host build ID. The `image` operation is guarded
for the extracted installer environment; do not run it against an installed
desktop. It configures the root image and Calamares hooks. **Cubic creates the
actual bootable ISO** after that step. Keep the required package selections in
the [ISO guide](../packaging/iso/README.md#4-finish-cubic-and-generate-a-test-iso).

Install the generated ISO in a fresh guest, then run the
[ISO acceptance checks](../packaging/iso/README.md#vm-acceptance-checks-before-a-release).
Check installer branding, first boot, account restrictions, backend/client
startup, DNS, Chrome/extension policy, settings persistence, and update behavior.
Fresh ISO protection switches start off; old installed settings are preserved
on upgrade. Test native image capture and multi-monitor behavior on suitable
hardware before claiming those protections work.

Keep the source revision, package versions/checksums, vendor lock, verified kit,
signed channel snapshot, ISO checksum, and test results together for a release.
Documentation/headless checks are not evidence of a successful host package
build, Calamares install, or installed-system upgrade.
