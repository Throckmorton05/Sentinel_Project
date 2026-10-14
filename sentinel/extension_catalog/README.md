# Sentinel Extensions and optional New Tab

**Sentinel Extensions** is a required catalog extension with a pinned toolbar
shield. **Sentinel New Tab** is a separate optional companion: disable/re-enable
it in `chrome://extensions` without a Guardian PIN. Disabling the companion
returns Chrome's standard New Tab page and leaves catalog access available.
Bookmarks, home appearance, and bookmark-bar visibility remain user choices.

## Start page

The page provides Google Search, a slim **Add shortcut** bar, website shortcuts
with Chrome-provided favicons, and a catalog link. Up to 24 shortcuts are stored
in extension-origin localStorage and synchronized between open tabs. Only
HTTP/HTTPS links without embedded credentials are accepted. Navigation still
obeys existing browser/DNS policies. Unavailable storage reports session-only
changes. No third-party favicon service is used.

Themes: Dark, Light, Neon purple/green/red, Monochrome, Terminal, Catppuccin,
red/blue/purple terminals, Tokyo, and Dark Tokyo. Themes apply to both start and
catalog pages and persist at the catalog's original storage origin. The normal
outline shield has a solid visor; toolbar PNGs use Sentinel light blue.

The search box requests autofocus once. Chrome may keep focus in its address
bar for a new tab/window, where typing already works; the page does not repeatedly
take focus while editing shortcuts/themes. The companion has no API permissions
and opens the existing catalog-origin start page exposed only to its signed ID.

## Approved inventory and Guardian approvals

Source: `configs/browser/approved_extensions.toml`.
Installed: `/usr/share/sentinel/browser/approved_extensions.toml`, root-owned
under protected directories. The backend uses this one inventory for catalog
and Chrome policy; optional entries cannot override required extensions.

| Mode | Optional approved extensions |
| --- | --- |
| Unlocked / Self Lock | Install through the official Chrome Web Store link |
| Guardian Lock | PIN approval for each ID; approved entries become managed installs |

Unapproved optional entries are blocked in Guardian Lock, including direct Web
Store attempts. Approval and revocation verify the PIN in the backend; revocation
blocks the extension without deleting its data. Receipts survive restarts/mode
changes in `/var/lib/sentinel/browser/guardian-extensions.json` (0600), contain
IDs only, and cannot authorize entries missing from the packaged inventory.
Previously installed optional extensions need Guardian approval to stay enabled.
Approval grants no exemption from content filtering.

Four required extensions stay force-installed: Safe Steam, Safe Crunchyroll,
Sentinel Activity, and Sentinel Extensions. New Tab uses `normal_installed`,
never the required forcelist. The catalog's `favicon` permission supports shortcut
icons; it does not request history, bookmarks, management, tabs, or content access.

## Backend endpoints

`GET http://127.0.0.1:8765/api/browser/extensions` returns the validated inventory
intersected with written policy, including Guardian approval/pending states.
Invalid/unprotected data returns 503 and clears install links. Chrome consumes
policy asynchronously; a written policy is not proof of live installation.

The fixed POST routes `/api/browser/extensions/approve` and `/revoke` accept
only an extension ID and PIN, enforce loopback Host and catalog Origin, and call
`browser.extension.approve`/`.revoke`. They expose no general RPC gateway. PINs
are masked, cleared, and never saved. Catalog text uses `textContent`; store
URLs must be HTTPS on `chromewebstore.google.com` and end with their exact ID.

## Build, install, and update

Install existing public assets on an already configured runtime:

```bash
sudo bash tools/install_catalog_assets.sh
```

For HTML/CSS/JavaScript/icon changes, bump the affected manifest version and
rebuild with the **original catalog and New Tab signing keys**:

```bash
.venv/bin/python tools/build_catalog_extension.py
sudo bash tools/install_catalog_assets.sh
```

The builder uses Python/OpenSSL and stores private keys outside the checkout.
Back up `~/.local/share/sentinel-extension-keys/extension_catalog.pem` and
`~/.local/share/sentinel-extension-keys/newtab.pem` privately; override the directory
with `SENTINEL_EXTENSION_KEY_DIR` if needed. It can create keys for a new identity,
but refuses to replace missing keys when public release assets already exist.
Replacing a release key changes the ID. Install only CRX/XML/public metadata,
never PEM files. The installer also copies the approved inventory.

Backend-source changes require a protected wheel update and restart; see
[development runtime](../../README.md#development-runtime). Close/reopen native
Chrome, reload `chrome://policy`, and verify extension versions against the
current manifests/update XML. If needed, request Update in `chrome://extensions`
when the deployment permits it; opening new tabs alone does not update a CRX.

```bash
curl --fail http://127.0.0.1:8765/extensions/catalog/updates.xml
curl --fail --output /dev/null \
  http://127.0.0.1:8765/extensions/catalog/sentinel_extensions.crx
curl --fail http://127.0.0.1:8765/api/browser/extensions
```

To add reviewed catalog data, edit with
`nvim configs/browser/approved_extensions.toml`, then install it root-owned at
the path above. Direct protected edits use `sudoedit`. Each entry needs ID,
name, developer, category, description, permissions note, and matching store URL.
Inventory changes need no CRX rebuild; policy reconciliation normally runs every
30 seconds. There is no user-facing inventory-editing RPC.

## Validation

```bash
.venv/bin/python -m pytest sentinel/extension_catalog/tests \
  sentinel/daemon/tests/test_browser_ops.py \
  sentinel/daemon/tests/test_guardian_websites.py -q
```

Check live forced installation, Guardian approve/revoke, themes/shortcut icons,
search focus, and disabling/re-enabling New Tab on the target Chrome profile.
