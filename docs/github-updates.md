# Sentinel updates hosted on GitHub Releases

For revision `0.1.0-10`, separate Sentinel/system update controls and current
ISO rebuild steps are documented in [update-controls-and-branding.md](update-controls-and-branding.md).
The older `-8` examples below describe the initial channel setup; use the newest
successful core/platform package version when publishing a new snapshot.

Apply this follow-up patch after `sentinel-updates-and-polish.patch`. It adds a
flat signed APT channel using GitHub Releases. No domain, Pages site or Cloudflare
account is required. The fixed public APT address is:

`https://github.com/Throckmorton05/Sentinel_Project/releases/download/apt-beta/`

That address becomes usable only after the channel release is published. Users
need no GitHub account or token to download updates from a public release. The
release must stay public, retain the `apt-beta` tag, and remain editable for
future index refreshes. Versioned `.deb` assets are never replaced; only signed
repository metadata and the public key download are refreshed.

## 1. Configure the public archive trust

Run in normal Konsole on the development laptop, inside `~/Sentinel_Project`:

```bash
export GNUPGHOME="$HOME/.local/share/sentinel-release-gnupg"
export GPG_TTY="$(tty)"
export SENTINEL_KEY_FPR="2783D1901D7826F83606DB5FF2225F1D294489F7"
export SENTINEL_GITHUB_REPO="Throckmorton05/Sentinel_Project"
python3 packaging/iso/repository_config.py \
  --github-repo "$SENTINEL_GITHUB_REPO" --github-tag apt-beta \
  --signing-key "$SENTINEL_KEY_FPR" --output configs/updates
```

The exporter copies only the public key. It creates a repository-specific
`Signed-By` source, package pins, and a metadata file. Flat APT sources use
`Suites: ./` with no `Components` field. The existing installed update mechanism
and package-policy guard still apply. If `configs/updates` already exists, stop
and inspect it rather than replacing existing trust blindly.

Save a secure backup of the private GPG directory/revocation certificate outside
the project and all publishing folders. Do not upload that private directory.
The signing passphrase remains on your development laptop.

## 2. Build the matching application and platform release

Follow the first patch's `docs/sentinel-updates.md` build steps. For this initial
release, the core changelog is already `0.1.0-8`:

```bash
bash tools/build_debs.sh --reuse-inputs dist/debs/0.1.0-20261012-194756
```

Set the exact newly printed successful build folder:

```bash
SENTINEL_DEBS='/full/path/to/new/successful/build'
bash build.sh platform \
  --kit "$HOME/Sentinel_ISO_Build/sentinel-iso-kit" \
  --debs "$SENTINEL_DEBS" \
  --output "$HOME/Sentinel_ISO_Build/platform-0.1.0-8"
SENTINEL_PLATFORM="$HOME/Sentinel_ISO_Build/platform-0.1.0-8/sentinel-platform_0.1.0-8_amd64.deb"
```

Use the original complete verified kit, not the small upload ZIP. Current helpers
and the GitHub public source/key configuration go into the platform package.
No new credentials or user settings go into it. Test the package upgrade using
the VM snapshot/acceptance steps from the first guide before a public release.

## 3. Create the signed channel snapshot

```bash
python3 packaging/iso/github_repository.py \
  --debs "$SENTINEL_DEBS" --platform "$SENTINEL_PLATFORM" \
  --config configs/updates \
  --output "$HOME/Sentinel_ISO_Build/github-channel-0.1.0-8"
```

GPG asks for your signing passphrase as needed. The folder contains all six
`.deb` files, `Packages`, `Packages.gz`, `Release`, `Release.gpg`, `InRelease`,
the public archive key, and a local `channel.json` receipt. The tool verifies its
own signature/checksum chain. It rejects mismatched core/platform versions,
packages at or above 2 GiB, and changed bytes under a retained package version.

No files are uploaded by this build command. The normal directory-layout
publisher remains available for a future conventional mirror/domain host.

## 4. Prepare the GitHub channel once

On the development laptop:

```bash
sudo apt-get install gh
gh auth login --hostname github.com --git-protocol https --web
gh auth status
```

Authenticate to the account with release-write access. Do not send the token,
login device code or key passphrase to chat. Keep the repository public so
installed computers can download without credentials.

Before creating/publishing the channel, check the repository's **Settings →
General → Releases**. `Enable release immutability` must be off for this mutable
channel. Published immutable releases cannot change assets. If you want immutable
normal software releases in your source repository, use a separate public
updates repository with mutable releases instead, and configure its OWNER/REPO
in step 1 before building the platform package. Do not alter a deployed channel
URL without a deliberate migration.

Prepare the local release notes (normal editor, no sudo):

```bash
nvim "$HOME/Sentinel_ISO_Build/apt-beta-notes.md"
```

Suggested text:

> Signed Sentinel Linux beta APT update channel for Kubuntu 26.04 amd64.
> This fixed channel carries versioned Debian packages and refreshed signed
> repository metadata. It is maintained for installed-system updates.

Create the draft only if it does not already exist:

```bash
gh release create apt-beta --repo "$SENTINEL_GITHUB_REPO" \
  --target main --draft --prerelease --latest=false \
  --title 'Sentinel beta APT update channel' \
  --notes-file "$HOME/Sentinel_ISO_Build/apt-beta-notes.md"
```

The channel tag is a persistent hosting address, not the version of its newest
package or a build attestation. Publish tested source changes/version tags
separately as part of normal source-release practice.

## 5. Upload the verified assets and publish the first draft

This command writes to the specified GitHub Release using your authenticated
`gh` session:

```bash
python3 packaging/iso/upload_github_channel.py \
  "$HOME/Sentinel_ISO_Build/github-channel-0.1.0-8" \
  --repo "$SENTINEL_GITHUB_REPO" --tag apt-beta \
  --fingerprint "$SENTINEL_KEY_FPR"
```

The uploader verifies the signed local snapshot, explicit repo/tag/fingerprint,
asset limits, and all existing package-name collisions before writing. It skips
identical existing packages and refuses different bytes under the same package
name/version. New packages are uploaded without `--clobber` and checked against
GitHub's SHA-256 digest (or a download if the API omits it). Metadata is uploaded
last, with `InRelease` last of all. Only the flat allowlisted publication files
are uploaded; private GPG files and the local `channel.json` are not uploaded.

After successful verification, publish the initial draft:

```bash
gh release edit apt-beta --repo "$SENTINEL_GITHUB_REPO" \
  --draft=false --prerelease --latest=false
python3 packaging/iso/check_github_channel.py --config configs/updates
```

The public check uses temporary APT configuration/state, verifies the signed
indexes, prints package candidates, and downloads the tiny `sentinel` meta-DEB.
It does not install packages, add a host APT source, or need sudo. This is the
check that confirms real GitHub HTTPS redirects/downloads work before we
bootstrap the installed VM.

**Publication is not atomic.** GitHub `--clobber` deletes an old metadata asset
before uploading its replacement. Flat Releases cannot expose APT's standard
`by-hash/` directory layout, so by-hash is disabled only for this flat source;
signature, expiry and package-checksum verification stay enabled. During a
publication or failed upload, APT may temporarily see missing/mismatched metadata
and abort its refresh. Complete/retry publication from the same signed snapshot
and rerun the public check. Never disable authentication to get past an error.
Publish from one machine/process at a time; no cross-machine publication lock
has been implemented.

## 6. Enable the installed VM without reinstalling

Use the first guide's one-time `bootstrap_updates.py` in an authorized root
maintenance shell or live-ISO target chroot, supplying this GitHub configuration
and your independently copied public fingerprint. The protected user's desktop
does not gain sudo or a generic root installer. We should choose mount/chroot
commands from the VM's actual partition layout. Reboot and verify versions,
protection and preserved state.

Subsequent Sentinel-only updates run through `sen update-sen` or the Portal's
**Update Sentinel** control. `sen update-sys` and the existing Portal schedule
cover all installed APT packages. APT verifies the signed index using the public key shipped with
the installation, compares versions, verifies archive checksums, and upgrades.
Ubuntu and Google repositories continue to provide their own packages.

## Future releases and metadata refreshes

Increment the Debian revision for changed packages (`-8` to `-9`, and so on),
build all five application packages plus the matching platform package, test,
and build a new signed GitHub snapshot. Pass `--previous` pointing to the complete
last successful local snapshot:

```bash
python3 packaging/iso/github_repository.py \
  --debs "$SENTINEL_DEBS" --platform "$SENTINEL_PLATFORM" \
  --config configs/updates \
  --previous "$HOME/Sentinel_ISO_Build/github-channel-0.1.0-8" \
  --output "$HOME/Sentinel_ISO_Build/github-channel-0.1.0-9"
```

The tool validates the previous signed index and all package bytes, retains old
packages, and refuses changed bytes under the same version. Upload the new
snapshot to the **same** `apt-beta` release using the uploader. Do not create a
new URL/tag for every APT update, delete the channel, or manually clobber DEBs.
Keep the new local snapshot for the following release. The release's 1000-asset
limit will eventually require reviewed retention/hosting changes; no automatic
package deletion is added here.

Metadata expires in 30 days. Refresh signatures/publication before expiry even
if application versions are unchanged (for example every two weeks). Reuse the
unchanged successful DEBs/platform inputs, use `--previous` and a new dated
output directory, then run the uploader/public check. No scheduled release
refresh job or GitHub Actions secret has been configured. The current matching
version dependencies mean runtime/models may be downloaded again on ordinary
Sentinel releases; separating their version cadence is future optimization.

## Validation and limits

Local checks use real isolated APT index acquisition and archive downloads,
real `dpkg-deb`/`apt-ftparchive`/`gpgv`, and disposable packages. The signing agent
cannot create sockets in this container, so the same temporary PGPy signing
adapter as the first patch supplied genuine test signatures. Upload operations
were mocked to verify ordering, collision rejection, mutable-release checks and
draft lookup; no account was authenticated and no GitHub release was changed.
Production Ed25519 signing, live HTTP downloads, a full package build and the
VM upgrade remain laptop/VM steps. Run the public check after publication.

References:

- [GitHub release limits](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)
- [GitHub CLI upload and replacement behavior](https://cli.github.com/manual/gh_release_upload)
- [GitHub immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases)
- [APT exact-path source syntax](https://manpages.debian.org/testing/apt/sources.list.5.en.html)
