# Sentinel Safe Crunchyroll

The managed Chrome extension filters Crunchyroll watch pages using the backend's
`safe_crunchyroll` policy. The setting defaults to on; missing/malformed/unavailable
policy keeps filtering enabled. Pages refresh every five seconds and on visibility
changes, without a persistent disabled cache.

```bash
sen setting get safe_crunchyroll
sen setting set safe_crunchyroll true
sen setting set safe_crunchyroll false
```

Enabling strengthens protection; Self Lock denies disabling; Guardian Lock requires
the PIN for changes. The extension remains force-installed while filtering is off,
including when Safe Browsing is off.

## Rules and behavior

A watch page blocks if either is true:

- At least three distinct sexual advisory categories, regardless of age rating.
- Rating 16+ or 18+ with Sexual Content, Sexualized Imagery, or Nudity.

Counted categories: Sexual Content, Sexualized Imagery, Suggestive Dialogue, Nudity.
Case/extra whitespace and duplicate labels do not change the count. Violence,
Profanity, Smoking, and Slurs do not contribute. A 14+ title with one or two
sexual categories remains allowed; three block even without an age badge.

The script reads the smallest relevant advisory container rather than synopsis
keywords. Reliable series-page coverage remains future work. Blocking preserves
the DOM, overlays the page, pauses/mutes media, and prevents playback. Approved
disable restores prior mute/scroll state but leaves playback paused for the user.
SPA navigation is rechecked. Metadata/site changes can affect detection coverage.
Block telemetry goes to [Activity](../activity/README.md).

## Assets and update workflow

Permanent extension ID: `njcmpekfomicmkpnaihnkohgpoeooeoa`.
The backend serves `/api/safe-crunchyroll`,
`/extensions/safe-crunchyroll/updates.xml`, and
`/extensions/safe-crunchyroll/safe_crunchyroll.crx` on `127.0.0.1:8765`.
Public assets live under `/usr/share/sentinel/extensions/safe-crunchyroll/`.
The background worker accepts fixed requests from its own content scripts;
web pages receive no settings/PIN write API.

For browser-source changes, bump the manifest and rebuild with the original
private signing key, then install public assets:

```bash
.venv/bin/python tools/build_safe_extensions.py --only safe_crunchyroll
sudo bash tools/install_activity_assets.sh
```

Prebuilt signed assets need no key to install. The builder reads the private
`~/.local/share/sentinel-extension-keys/safe_crunchyroll.pem` key outside the
checkout, or the directory selected by `SENTINEL_EXTENSION_KEY_DIR`.
Keep PEM files private and retain
them for future updates; a replacement key changes the extension ID. Verify
current versions from source manifests/update XML instead of historical patch
instructions. Backend changes also require a protected wheel update/restart;
see [development runtime](../../README.md#development-runtime).

Reload Chrome policy, request an extension update when permitted, and reload
Crunchyroll tabs. Copying source files or changing a filter toggle does not
update an installed CRX. Verify real watch-page advisories, media blocking,
and allowed SPA navigation in native Chrome after deployment.
