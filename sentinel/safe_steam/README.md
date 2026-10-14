# Sentinel Safe Steam

Safe Steam filters Steam Store pages in native Steam's CEF browser and managed
native Chrome. Its policy comes from the backend at `127.0.0.1:8765`; missing,
invalid, timed-out, or unavailable policy keeps filtering enabled. Content scripts
refresh every five seconds and on visibility changes, without a persistent
browser-side disabled cache.

```bash
sen setting get safe_steam
sen setting set safe_steam true
sen setting set safe_steam false
```

The setting defaults to on. Enabling strengthens; Self Lock denies disabling;
Guardian Lock requires its PIN for changes. Disabling filtering leaves extension
installation/integrity repair active. It does not change native Steam installation.

## Filtering and managed assets

The extensions use Steam content descriptors and unsafe-search rules. Blocked
pages get an overlay that can be removed after an authorized disable or allowed
SPA navigation. Detection depends on site metadata; it is not classification
of every page/image. Block telemetry goes to [Activity](../activity/README.md).

| Asset | Protected installation |
| --- | --- |
| Native CEF source and integrity data | `/usr/share/sentinel/safe_steam/steam_extension/`, `provisioning/` |
| Signed Chrome package and update XML | `/usr/share/sentinel/extensions/safe-steam/` |

Native extension ID: `fbgjjhaoclncakjkdebkllglkgpffkfb`.
Chrome extension ID: `jdfobdpedcmlbmolbdfaoaidldkjjfnh`.
The daemon serves `/api/safe-steam`, `/extensions/safe-steam/updates.xml`, and
`/extensions/safe-steam/browser_extension.crx`. Safe Steam stays force-installed
independently of its filter switch and Safe Browsing.

The wheel carries the protected native payload/provisioning data and signed
Chrome bundle. The backend validates assets, stages root-owned copies, verifies
Chrome CRX identity/signature/update metadata with OpenSSL, and periodically
repairs missing/stale assets. It never accepts a user-provided asset source.

## Native Steam reconciliation

Checks run as the configured protected user in a graphical session using the
installed isolated runtime. Before protected asset replacement or profile repair,
a bounded stop command can close Steam; reopen it afterward. Steam home-profile
writes are performed as the user, never root. Failed/timed-out checks retry.
A fresh profile reports Waiting until Steam has been opened once.

Steam's Sniper container sees the host asset directory at
`/run/host/usr/share/sentinel/safe_steam/steam_extension`; direct host clients use
`/usr/share/sentinel/safe_steam/steam_extension`. The daemon inspects the webhelper
mount namespace and passes the verified load path to reconciliation. Registration
with a wrong path is repaired; protected hashes always use the host directory.

The Portal shows separate Chrome and Steam installation states. Chrome's ready
state confirms signed assets and written policy, not profile installation.
Steam's verified state confirms native assets/registration, not live page-filter
execution. A recent recognized-Origin policy read is informational connection
status and expires after 20 seconds; it is not authenticated filtering proof.

Development-only checks, as the desktop user after fully exiting Steam:

```bash
/usr/lib/sentinel/venv/bin/python -I -m sentinel.safe_steam.provision
/usr/lib/sentinel/venv/bin/python -I -m sentinel.safe_steam.watchdog --once
```

Standalone defaults to the container load path; known direct-host clients accept
`--extension-path /usr/share/sentinel/safe_steam/steam_extension`. Only the two
fixed protected paths are accepted. Do not run a second continuous watchdog
alongside the backend. `--once` exits 0 verified, 1 failed, 2 waiting for a fresh
profile, or 4 no supported native installation.

## Install/update Steam and the extension

```bash
sen install steam
sen uninstall steam
```

The alias installs trusted APT `steam-installer`, including in Self Lock.
Ubuntu multiverse and i386 dependency support must already be configured.
Removal is allowed in Unlocked, PIN-gated in Guardian Lock, and denied in Self
Lock. Install/removal wakes reconciliation; games/home profiles are not deleted
by ordinary package removal. The App Store's native Steam card uses these same
package RPCs with its separate `native:steam` identity.

Flatpak Steam installation is rejected in every mode; existing copies remain
listed for removal. Snap/Flatpak Steam are unsupported by this provisioner and
are not automatically removed or blocked merely by this install restriction.

After backend changes, update the protected wheel and restart its existing
instance; see [development runtime](../../README.md#development-runtime).
For browser-source changes, bump manifests and rebuild with retained private keys:

```bash
.venv/bin/python tools/build_safe_extensions.py
sudo bash tools/install_activity_assets.sh
```

The installer copies existing signed Activity/Safe Steam/Safe Crunchyroll public
assets only. Never distribute PEM keys or generate replacement release keys.
The original keys live outside the checkout in
`~/.local/share/sentinel-extension-keys/`, with `safe_steam.pem`, `activity.pem`,
and `safe_crunchyroll.pem`. `SENTINEL_EXTENSION_KEY_DIR` overrides that directory.
Chrome and native CEF source versions may differ; check their current manifests
and update XML rather than an old patch version. Reload Chrome policies and
extensions, reopen Steam, and verify live filtering after deployment.

```bash
.venv/bin/python -m pytest sentinel/safe_steam/tests sentinel/daemon/tests -q
```

Tests isolate provisioning, signatures, policy, and runtime paths; actual
Steam/CEF compatibility and live site behavior still require target checks.
