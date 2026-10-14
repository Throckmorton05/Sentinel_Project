# Guardian Website Access

Guardian Lock now has independent app and website selections. A guardian can
allow native Google Chrome Stable in Guardian App Access, then turn on
**Only allow selected websites** and approve `youtubekids.com` plus a school's
domain. Regular `youtube.com`, Google Search, and all other unapproved web
destinations remain blocked. Nothing is automatically approved.

## Interface

Open **Sentinel Portal > Guardian > Websites** while in Guardian Lock.
Save verifies a fresh Guardian PIN. The old standalone website menu app has
been retired; cached module launchers redirect to the Portal's Guardian page.
Add plain domains without `https://`, paths, ports,
or wildcards. **Add YouTube Kids** adds only `youtubekids.com`.

Every entry allows that domain and its subdomains. `school.example.org` does
not approve its parent `example.org`; `www.school.example.org` does not approve
`school.example.org`. Up to 450 domains are supported. Internationalized names
are stored in their ASCII IDNA form. An enabled empty selection blocks every
HTTP(S) website.

Close all Chrome windows and reopen Chrome after saving. For inspection,
`chrome://policy` → **Reload policies** should show `URLBlocklist` beginning
with `*`, and `URLAllowlist` containing only HTTP and HTTPS entries for selected
domains. The daemon's `applied` status confirms the policy file was written;
it does not confirm that an already-running browser or open tab has refreshed.

If a school login redirects to a different domain, explicitly approve that
domain too. Existing DNS blocks, category filtering, SafeSearch, screen
filtering, and Block Windows continue to apply; website approval is not a DNS
exception and does not override these protections.

## CLI

```bash
sen guardian-websites status
sen guardian-websites list
sen guardian-websites approve youtubekids.com
sen guardian-websites approve school.example.org
sen guardian-websites enable
sen guardian-websites revoke school.example.org
sen guardian-websites disable
```

All writes require Guardian Lock and the existing masked Guardian PIN prompt,
including disabling the feature or removing an entry. Reads need no PIN.
Choices are retained when disabled or when Guardian Lock is exited; reentering
Guardian Lock reapplies a previously enabled selection. This mode is inactive
in Unlocked and Self Lock.

## Enforcement and limitations

This is native Chrome website navigation policy, not a systemwide network
allowlist. Chrome enterprise `URLBlocklist`/`URLAllowlist` enforce the selection;
only HTTP(S) exceptions are emitted, and Chrome developer tools are disabled
while the mode is active. Chrome's own internal pages and installed extensions
remain governed by its existing enterprise policies. The built-in Sentinel
management launchers remain available when guardian app selection is enabled.

While website selection is active, `SENTINEL_DEV_ALLOW_FIREFOX=1` does **not**
exempt Firefox. Existing unsupported-browser AppArmor and process enforcement
is reconciled immediately and periodically. Disabling website selection or
exiting Guardian Lock restores the explicit development exception. Allow
native Chrome in the app selection before enabling this feature.

Other approved applications, embedded web views, allowed extensions, and
arbitrary user code are outside this website policy's scope. An approved
website can itself serve or embed content: choose trusted sites and retain
the existing content protections. Do not approve terminals, browser substitutes,
or tools that provide unrestricted browsing in a child profile. This patch
does not add a firewall or claim complete OS confinement.

Policy state is `/etc/sentinel/guardian-websites.json`, written atomically by
sentineld. Startup rejects malformed, symlinked, or desktop-writable state
instead of resetting it. Failed Chrome writes remain pending and are retried
by the daemon's existing reconciliation loop. Stopping sentineld does not
remove the last Chrome policy; restart it to make normal PIN-authenticated
changes. The app restriction's independent recovery service remains unchanged.

## Install from the patch

Run from the project, after stopping your foreground development daemon
(Ctrl+C) and any system service instance:

```bash
cd ~/Sentinel_Project
git apply --check ~/Downloads/sentinel-guardian-websites.patch
git apply ~/Downloads/sentinel-guardian-websites.patch

.venv/bin/python -m pytest \
  sentinel/daemon/tests/test_guardian_websites.py \
  sentinel/daemon/tests/test_guardian_websites_gui.py -q

sudo systemctl stop sentineld.service
poetry build -f wheel
sudo /usr/lib/sentinel/venv/bin/python -m pip install --no-deps --force-reinstall \
  dist/sen-0.1.0-py3-none-any.whl
sudo bash tools/install_guardian_website_access.sh

sudo /opt/AdGuardHome/AdGuardHome -s start
sudo env SENTINEL_DEV_ALLOW_FIREFOX=1 \
  /usr/lib/sentinel/venv/bin/python -m sentinel.daemon.server
```

In a second terminal:

```bash
cd ~/Sentinel_Project
.venv/bin/python -m sen.cli.sen guardian-websites approve youtubekids.com
.venv/bin/python -m sen.cli.sen guardian-websites approve school.example.org
.venv/bin/python -m sen.cli.sen guardian-websites enable
.venv/bin/python -m sen.cli.sen guardian-websites status
```

Replace `school.example.org` with the actual school domain. These commands
assume Guardian Lock is already active. To use the GUI instead:

```bash
/usr/lib/sentinel/venv/bin/python -I -m sentinel.guardian_websites.manager
```

Test a wrong PIN (no changes), YouTube Kids, the approved school website,
regular YouTube, Google Search, an unrelated site, and an IP-address URL.
Verify the latter four are blocked. Try launching Firefox even with the
development environment flag. Restart sentineld and Chrome and repeat the
allow/block checks. Disable using the correct PIN and reopen Chrome to restore
normal browsing under the existing Sentinel protections.

Google policy references:

- https://chromeenterprise.google/policies/url-allowlist/
- https://support.google.com/chrome/a/answer/9942583
