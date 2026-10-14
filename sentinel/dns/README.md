# Sentinel DNS filtering

`sentineld` owns DNS policy and applies it to AdGuard Home. Desktop clients submit
settings or domain changes over the existing authenticated Unix socket. They do
not write settings, lists, generated rules, or correction timestamps.

## Managed settings

All of these names work with `sen setting get NAME` and `sen setting set NAME true|false`:

| Setting | Meaning | Default if absent |
| --- | --- | --- |
| `dns.enabled` | Enable optional DNS categories and durable custom lists | `true` |
| `dns.adult` | Explicit/adult category | `false` |
| `dns.adult_heavy_sites` | Packaged adult-heavy sites | `false` |
| `dns.gambling` | Gambling category | `false` |
| `dns.social_media` | Social-media category | `false` |
| `dns.unsafe_search_engines` | Packaged unsafe search engines | `false` |
| `dns.browsers_sans_safe_search` | Search services without SafeSearch | `false` |

Existing settings are preserved. The shipped config enables all categories.
The old spelling `dns.browsers_sans_safesearch` aliases the canonical SafeSearch
key and is normalized on write. Conflicting values are rejected.

Turning a protection on is a strengthen; turning it off is a weaken. Unlocked
allows either; Self-Lock denies weakening; Guardian Lock requires the actual PIN
for every mutation, including repeated/no-op requests. Client-supplied claims of
authentication or change strength have no authority.

`dns.enabled` controls optional categories and durable custom lists. The fixed
Google-only search baseline remains active; Google SafeSearch and YouTube
Moderate rewrites remain active while `safe_browsing` is true. The AdGuard
subscription therefore stays enabled even when optional categories are off.
See [supported browsing platform](../browser_policies/README.md) for deployment
and OS resolver routing.

## User lists and the correction window

```bash
sen dns status
sen dns block add example.com
sen dns block list
sen dns block remove example.com
sen dns allow add allowed.example
sen dns allow list
sen dns allow remove allowed.example
sen dns regenerate
sen dns regenerate --update-sources
```

Block additions are always classified as strengthens. Block removals are always
classified as weakens. A newly added domain has a **24-hour correction window
from its first addition**, even if it was added while Unlocked.

| Mode | Remove within 24 hours | Remove at or after 24 hours |
| --- | --- | --- |
| Unlocked | Allowed | Allowed |
| Self-Lock | Allowed by the narrow correction exception; still a weaken | Denied |
| Guardian Lock | PIN required | PIN required |

The deadline does not restart after a duplicate add, remove/re-add, daemon
restart, or lock-mode change. Deadline history is retained for removed domains.
Entries imported from the old blocklist have no new correction window and are
already locked in Self-Lock. Allowlist additions weaken protection; removing
exceptions strengthens it. There is no grace exception for adding allow entries.

Custom block rules use AdGuard's `$important` modifier so ordinary allow
exceptions, including parent/subdomain overlaps, cannot override them. DNS rules
cover the named domain and its subdomains. Inputs accept domain names, normalize
case/trailing dots and existing `www.` aliases, and reject URLs, IPs, wildcards,
paths, and injected filter syntax. International domains are converted to IDNA.

## Persistence and migration

- `/etc/sentinel/settings.toml`: category/master settings, root-owned and not writable by the desktop user.
- `/var/lib/sentinel/dns/user-policy.json`: authoritative lists, original deadlines, and a clock high-water mark; root-owned, `0600`.
- `/etc/sentinel/dns/user_blocklist.txt` and `user_allowlist.txt`: protected projections for compatibility. They are imported once when JSON policy is first created; later manual edits are overwritten from authority.
- `/usr/share/sentinel/dns/`: packaged static lists; root-owned.
- `/var/lib/sentinel/dns/`: generated category and merged lists; root-owned.
- `/var/cache/sentinel/dns/`: trusted source caches; root-owned.
- `/opt/AdGuardHome/userfilters/sentinel.txt`: generated AdGuard rules, written atomically.
- `/etc/sentinel/adguard.toml`: private local API connection settings, root-owned, `0600`.

First startup copies legacy `[adguard]` connection settings from
`/usr/share/sentinel/info.toml` into the private config if that config does not
already exist. It does not rotate the AdGuard password or remove the legacy
copy. The sample config contains development credentials; change them in AdGuard, update
the private file using `sudoedit`, and remove the old password from shared config
before relying on lock enforcement. The AdGuard config, installation, and API
credentials are part of the protected root service boundary. A user who already
knows AdGuard's admin password can change AdGuard directly.

Malformed policy, missing deadline metadata, user-writable files, and symlinks
are rejected. Existing blocked domains are never silently reimported with fresh
deadlines. The generator receives one validated backend settings snapshot rather than
reading `settings.toml` independently. CLI and Portal requests use the backend
client directly.

## Applying rules and handling outages

Accepted changes persist desired policy first, then generate rules and reload
AdGuard. The daemon registers or reenables its absolute-path filter subscription,
ensures required filtering/protection is active, forces a refresh, checks the
loaded subscription/rule count, and clears AdGuard's DNS cache.

A saved change whose enforcement fails returns `application.status = "pending"`
with an error, rather than claiming enforcement completed. The CLI reports the
saved/pending distinction and exits nonzero. `sen dns status` shows desired
settings and the most recent application result. Startup and a background loop
retry pending policy. The loop also probes backend state and restores the
subscription when it drifts, normally every 30 seconds when no other
administrative operation holds the daemon's mutation lock.

Backend reconciliation enforces accepted policy without a new PIN. Category
sources refresh on first startup and daily, with failed attempts retried after
15 minutes. An explicit `dns.regenerate` request still follows Guardian policy.

Missing/empty enabled source files abort generation before replacing active
runtime rules. Source updates use only packaged trusted URLs. Failed downloads
use the corresponding protected cache; without a cache the old category file is
retained and an error is reported. Normal settings/list changes do not download
sources. Social-media sources use HaGeZi's published social-network list.

### Social media without YouTube

The social category downloads [HaGeZi Social Networks](https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/social.txt).
It also includes Facebook, Instagram, TikTok, BeReal (bereal.com and bere.al),
and Threads (threads.com and threads.net) as Sentinel's core social domains.
A real snapshot ships in `sentinel/dns/data/social_media.txt`, so the existing
Social media protection works before the first successful download or offline.
No separate data installation or manual category-file creation is needed.

Normal rendering prefers the protected downloaded category file; missing or
empty social data falls back to the packaged list. Source refreshes prefer live
data, then the protected cache, then the packaged snapshot and report a download
error for the usual automatic retry. Other categories retain their existing
failure behavior. The downloaded social list updates on the existing daily
schedule; refreshing sources does not automatically enable the Social media toggle.

YouTube, its API/media domains, and broad rules that would cover those domains
are excluded from this category on download, cache fallback, and rendering.
Explicit user/scheduled blocks on YouTube still work normally, as does the
existing YouTube Restricted Mode policy.

The bundled HaGeZi data is licensed under GPL-3.0; its license ships alongside
the snapshot. BeReal and Threads are Sentinel additions to that snapshot.

Application failures are not a desktop/network blackout: previous AdGuard rules
may continue until the accepted policy can be applied. This subsystem does not
add firewall rules, browser DoH controls, or change the machine's DNS routing.

## RPCs

| Action | Parameters |
| --- | --- |
| `settings.get` | `name` |
| `settings.set` | `name`, boolean `value`, optional `guardian_pin` |
| `dns.status` | none |
| `dns.blocklist.list`, `dns.allowlist.list` | optional `offset` (default 0), `limit` (default 50, maximum 100) |
| `dns.blocklist.add`, `dns.blocklist.remove` | `domain`, optional `guardian_pin` |
| `dns.allowlist.add`, `dns.allowlist.remove` | `domain`, optional `guardian_pin` |
| `dns.regenerate` | optional boolean `update_sources`, optional `guardian_pin` |

List responses contain `entries`, `total`, and `next_offset`. Block entries expose
the protected expiry timestamp and remaining correction time. Pagination keeps
responses within the existing 64 KiB protocol limit. PINs use the CLI's existing
masked prompt and are never printed or accepted as a command-line argument.

## Validation

```bash
.venv/bin/python -m pytest sentinel/daemon/tests sentinel/dns/tests -q
```

Tests use isolated files, real policy persistence and PIN verification, the
Unix-socket client/server path, and a local HTTP API fixture. They cover expiry,
restarts, duplicate/re-added domains, policy denial without side effects,
allow/block precedence, backend outages, drift recovery, private credentials,
source retention, protected file handling, CLI prompts, and pending results.
Live AdGuard and OS DNS routing must also be tested on the target desktop.

References: [AdGuard Home API](https://github.com/AdguardTeam/AdGuardHome/blob/master/openapi/openapi.yaml),
[AdGuard DNS rule priority](https://adguard-dns.io/kb/general/dns-filtering-syntax/),
[HaGeZi social source](https://github.com/hagezi/dns-blocklists#social).
