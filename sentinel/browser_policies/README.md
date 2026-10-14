# Supported browser and managed policy

Sentinel supports **native Google Chrome Stable** (`google-chrome-stable`) and
Google Search. A fixed baseline restricts known alternative browser packages,
executables, Flatpak IDs, Snap paths, and search domains in every lock mode,
including when Safe Installs/optional filters are off.

The backend atomically writes its own root-owned policy file at
`/etc/opt/chrome/policies/managed/sentinel.json`. It applies policy at startup,
after authorized changes, and during reconciliation. `browser.status` reports
saved/applied/pending state; `browser.sync` requests authorized reapplication.
Written policy does not prove that an already-running Chrome has refreshed it.

## Policy

- Mandatory Google default search and URL restrictions for known other engines.
- System resolver (`DnsOverHttpsMode: off`), direct proxy mode, no Incognito/Guest.
- Four required extensions: Safe Steam, Safe Crunchyroll, Activity, Extensions
  catalog. They stay installed independently of filtering switches.
- Optional extensions from a protected inventory only; Guardian Lock additionally
  requires per-extension PIN approval. See [catalog](../extension_catalog/README.md).
- Sentinel New Tab is optional (`normal_installed`); bookmarks/home appearance
  remain user choices. Only the required catalog toolbar action is force-pinned.
- Guardian website selection restricts native Chrome navigation to selected
  domains and disables developer tools while active; it is not a network allowlist.

Native Chrome can be installed with `sen install chrome` once its trusted APT
repository exists. Flatpak Chrome is unsupported because it does not share this
policy path. Custom app removal cannot remove the fixed browser baseline.
This is a maintained inventory, not exhaustive execution/network confinement:
unknown/copied browsers, AppImages, VPNs, and arbitrary code require final OS controls.

## SafeSearch and DNS interaction

`safe_browsing = true` applies:

| Layer | Google | YouTube |
| --- | --- | --- |
| Chrome | `ForceGoogleSafeSearch: true` | `ForceYouTubeRestrict: 1` |
| AdGuard | Search hosts → `forcesafesearch.google.com` | Supported hosts → `restrictmoderate.youtube.com` |

YouTube value 1 means **Moderate**, not YouTube Kids or a sexual-content-only
filter; independent account/network policy may be stricter. DNS rewrites cover
`www.youtube.com`, `m.youtube.com`, `youtubei.googleapis.com`,
`youtube.googleapis.com`, and `www.youtube-nocookie.com`, not all video CDN or
Google API domains.

`dns.enabled` controls optional categories/durable custom lists. The Google-only
baseline remains when off; SafeSearch rewrites remain while Safe Browsing is on,
and active scheduled blocks remain. Custom/scheduled blocks take priority over
rewrites and ordinary allow exceptions. Allowlisting Google retains SafeSearch;
allowlisting another known engine cannot remove the baseline.

The backend verifies loaded rules and representative A/AAAA filter responses.
Outages/conflicting legacy rewrites report pending. Verification checks AdGuard,
not the machine's resolver routing. Source downloads/reconciliation are automatic.

## Development Firefox allowance

On an existing configured installation, stop the old daemon, update its wheel,
and launch one development instance:

```bash
sudo env SENTINEL_DEV_ALLOW_FIREFOX=1 \
  /usr/lib/sentinel/venv/bin/python -I -m sentinel.daemon.server
```

The override belongs on the backend, not the CLI. It temporarily exempts known
Firefox runtime/AppArmor baseline targets; custom/scheduled blocks still apply
and installation/catalog restrictions remain. **Active Guardian website selection
suppresses the Firefox exemption.** Firefox does not consume Chrome policies.
Omit this variable in the final distro. See
[development runtime](../../README.md#development-runtime).

## Target setup and DNS routing

Set native Chrome as the desktop browser as the normal user:

```bash
xdg-settings set default-web-browser google-chrome.desktop
xdg-mime default google-chrome.desktop x-scheme-handler/http
xdg-mime default google-chrome.desktop x-scheme-handler/https
xdg-mime default google-chrome.desktop text/html
```

AdGuard's private connection settings live in `/etc/sentinel/adguard.toml`;
edit with `sudoedit`. If AdGuard rejects the generated filter path, stop it,
use `sudoedit /opt/AdGuardHome/AdGuardHome.yaml`, append the exact
`/opt/AdGuardHome/userfilters/sentinel.txt` to `filtering.safe_fs_patterns`,
preserve existing values, and restart. Remove conflicting legacy rewrites rather
than layering a Strict YouTube rewrite over Sentinel's Moderate policy.

Generating rules does **not** configure OS DNS routing. Configure AdGuard to
listen on loopback TCP/UDP port 53 with an explicit external upstream. Verify it
before changing NetworkManager:

```bash
sudo ss -lntup 'sport = :53'
dig @127.0.0.1 www.google.com A
dig @127.0.0.1 www.youtube.com A
dig @127.0.0.1 www.youtube.com AAAA
dig @127.0.0.1 bing.com A
```

Expect SafeSearch/Moderate CNAMEs and Bing NXDOMAIN when those protections apply.
Scheduled YouTube blocks should return NXDOMAIN. AAAA must not resolve to an
unrestricted YouTube address.

For an existing Kubuntu NetworkManager profile, choose its actual active name.
Reconnect from a local terminal; the connection is briefly interrupted:

```bash
nmcli -f NAME,UUID,TYPE,DEVICE connection show --active
sentinel_connection='REPLACE WITH YOUR ACTIVE CONNECTION NAME'
sudo nmcli connection modify "$sentinel_connection" \
  ipv4.ignore-auto-dns yes ipv4.dns '127.0.0.1' ipv4.dns-priority -50 \
  ipv6.ignore-auto-dns yes ipv6.dns '' ipv6.dns-priority -50
sudo nmcli connection up "$sentinel_connection"
sudo resolvectl flush-caches
resolvectl status
dig www.google.com A
dig www.youtube.com A
dig bing.com A
```

Repeat for every network profile. systemd-resolved may still appear at 127.0.0.53;
verify its upstream is AdGuard. VPNs/new connections/external fallback require
separate distro routing restrictions. These commands update a target profile;
the backend does not perform them automatically.

Restart Chrome, reload `chrome://policy`, and confirm policy status OK. Inspect
Google SafeSearch and YouTube's content-restriction check page. Confirm actual
OS queries match loopback queries before relying on DNS enforcement.

## Validation

```bash
.venv/bin/python -m pytest sentinel/daemon/tests sentinel/dns/tests \
  sentinel/appstore/tests -q
sen dns status
sen app-block status
```

Tests cover authorization, baseline persistence, supported installers, exact-host
rewrites, category switches, and conflicting rules. Live Chrome refresh, KDE,
AppArmor, and resolver routing remain target-machine checks.
