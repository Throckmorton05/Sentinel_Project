# Sentinel ISO 0.1.0: onboarding and installer polish

This incremental change follows the update/polish, GitHub channel, and GitHub
CLI compatibility patches. The Debian revision advances to `0.1.0-9`.

## Behavior

- Fresh ISO installs start Unlocked. Every registered protection switch is
  explicitly false: image filtering, boot media, VM protection, Safe Installs,
  Safe Browsing, Safe Steam, Safe Crunchyroll, DNS filtering and all DNS categories.
- `packaging/iso/fresh-install-settings.toml` is applied only by the guarded
  image builder. Ordinary project defaults and existing installs are preserved;
  boot/update hooks do not reset saved settings, lock mode or PIN. The installed
  daemon stays available for opt-in configuration.
- Account/login restrictions, managed native Chrome/Google, local DNS transport,
  required extension installation, and package-integrity rules remain platform
  boundaries independent of the optional switches. There is no general sudo
  access. SafeSearch/YouTube enforcement follows Safe Browsing's switch.
- Fiverr, VGen, Artistree, Artists&Clients, Skeb, and Commishes join the existing
  adult-heavy website category. Parent-domain rules cover subdomains. Both the
  DNS master switch and category must be enabled for these category blocks.
  Whole-domain blocks include ordinary art and other safe services. This list
  is curated coverage, not exhaustive coverage of commission platforms.
  Review links are in `configs/dns/adult_heavy_sites.txt`.
- Calamares gets Sentinel names, shield assets, navy/light-blue styling, and a
  five-slide presentation. Partition/account/bootloader install jobs retain their
  existing order. The live launcher keeps its original elevation command and
  flags while its displayed name/icon are rebranded.
  Both Kubuntu's `calamares-launch-normal` wrapper and its `sh -c` entry are
  recognized. The unsupported OEM entry is hidden from the live installer menu.
- Start Here — Sentinel is an offline Qt guide in the application menu and on
  the new user's desktop. It covers protections, lock modes, apps/sites, focus
  windows, screen time, activity, editable desktop settings and updates. It makes
  no backend requests or settings changes. Its Portal button still follows the
  Portal's Guardian PIN rules. Guardian's built-in guide entry uses a fixed route;
  it does not execute commands supplied by a launcher.
- The installer initializes XDG folders with the target locale, then creates one
  executable, user-owned desktop shortcut. Users can move/delete it; boot/update
  hooks do not recreate it. Existing installs get the menu entry on update,
  without a forced desktop shortcut.

## Apply and rebuild on the development laptop

After extracting the bundle into Downloads:

```bash
cd "$HOME/Sentinel_Project"
git apply --check "$HOME/Downloads/sentinel-final-iso-polish/sentinel-final-iso-polish.patch"
git apply "$HOME/Downloads/sentinel-final-iso-polish/sentinel-final-iso-polish.patch"
bash tools/build_debs.sh --reuse-inputs \
  "$HOME/Sentinel_Project/dist/debs/0.1.0-20261013-180235"
```

The saved dependency wheels are reused, but the project wheel and all five DEBs
are rebuilt. Keep the earlier `-8` package bytes and signed snapshot. After a
successful build, set `SENTINEL_DEBS` to the **new exact output folder**; all five
packages must report version `0.1.0-9`.

To preview just the guide on your development laptop, without changing settings:

```bash
poetry run sentinel-portal --guide
```

```bash
export SENTINEL_DEBS='/replace/with/the/new/successful/build/folder'
bash build.sh platform \
  --kit "$HOME/Sentinel_ISO_Build/sentinel-iso-kit" \
  --debs "$SENTINEL_DEBS" \
  --output "$HOME/Sentinel_ISO_Build/platform-0.1.0-9"
export SENTINEL_PLATFORM="$HOME/Sentinel_ISO_Build/platform-0.1.0-9/sentinel-platform_0.1.0-9_amd64.deb"

bash build.sh prepare \
  --debs "$SENTINEL_DEBS" \
  --platform "$SENTINEL_PLATFORM" \
  --output "$HOME/Sentinel_ISO_Build/sentinel-iso-kit-0.1.0-9"
bash build.sh verify "$HOME/Sentinel_ISO_Build/sentinel-iso-kit-0.1.0-9"
```

The platform builder uses the existing complete verified kit for locked vendors,
and current patched project files for runtime/installer artwork. Preparing the
new kit stages current image scripts and the fresh settings template. Do not
edit old manifest-listed kit files in place.

## Build and test a new ISO

Use a fresh Cubic project from the original Kubuntu 26.04 amd64 installer ISO,
avoiding a private Sentinel source or provisioned state from an earlier root.
Stage the **new `-9` kit** as `/root/sentinel-iso-kit` in Cubic. Inside its terminal:

```bash
cd /root/sentinel-iso-kit
bash build.sh image
```

Generate the test ISO, retaining the packages listed in the ISO README. Do not
run `image` or `provision_install` against an existing installation: they are
build/install operations, not upgrades. The GitHub repository can stay private
while testing; APT cannot use its unauthenticated release URLs until it is public.
If an old Cubic root reports a private Sentinel URL error, use the clean base
above instead of bypassing authentication.

Fresh-install acceptance in a new disposable VM:

1. Verify Sentinel installer names, welcome art, sidebar and slideshow, then
   complete locale, disk selection, account creation, bootloader and finish.
   These require a real Cubic/Calamares VM test in addition to local QML preview.
2. Verify the installed first session is Unlocked and every Protection switch is
   off. DNS category controls become editable when DNS filtering is enabled.
   The image monitor and model services should be inactive while its switch is off.
3. Open Start Here — Sentinel from the desktop without an execute/trust prompt;
   read each section and check small-window scrolling. Disconnect networking
   and reopen the guide. Its Portal button must open the normal Portal. Check
   that a non-English install puts the shortcut in its localized desktop folder.
4. Enable selected protections while Unlocked, reboot, and verify they stay on.
   Test image capture on suitable hardware; the current VM's missing KWin
   capture interface may prevent that part of the test.
5. With DNS filtering and adult-heavy websites enabled, confirm Fiverr and the
   other five additions resolve as blocked through the local resolver. Turn the
   category off while Unlocked and verify these category rules disappear. Other
   categories, explicit blocks or focus windows may still block a site independently.
6. Move/delete the guide's desktop shortcut, then reboot and confirm it is not
   recreated. Its menu entry remains. In Guardian allow-only app mode, verify the
   guide opens and the Portal button still requires Guardian authorization.

Read-only spot checks as the normal installed user:

```bash
sen state
sen setting get image_filtering
sen setting get safe_browsing
sen setting get dns.enabled
sen dns status
```

An existing VM upgraded to `-9` keeps its old enabled protections and lock state.
A fresh installation is necessary to test the new all-off defaults.

## Publish after testing

Build a new signed channel, retaining the prior snapshot's package versions:

```bash
export GNUPGHOME="$HOME/.local/share/sentinel-release-gnupg"
export GPG_TTY="$(tty)"
python3 packaging/iso/github_repository.py \
  --debs "$SENTINEL_DEBS" \
  --platform "$SENTINEL_PLATFORM" \
  --config configs/updates \
  --previous "$HOME/Sentinel_ISO_Build/github-channel-0.1.0-8" \
  --output "$HOME/Sentinel_ISO_Build/github-channel-0.1.0-9"

python3 packaging/iso/upload_github_channel.py \
  "$HOME/Sentinel_ISO_Build/github-channel-0.1.0-9" \
  --repo Throckmorton05/Sentinel_Project \
  --tag apt-beta \
  --fingerprint 2783D1901D7826F83606DB5FF2225F1D294489F7
```

After you choose to make the repository public, run:

```bash
python3 packaging/iso/check_github_channel.py --config configs/updates
```

That public signature/index/download check must pass before installed-system
updates use the channel. See `github-updates.md` for signing expiry and refresh.
Keep release keys, tokens and personal installed state outside the source repo.
