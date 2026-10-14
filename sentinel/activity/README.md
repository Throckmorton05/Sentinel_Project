# Sentinel activity and screen-time reports

The backend owns `/var/lib/sentinel/activity/events.sqlite3` (0600 in a 0700
directory). Desktop clients read through the authorized Unix socket; browser
endpoints accept bounded telemetry but do not expose reports.

## Records and privacy

| Kind | Source / recorded details |
| --- | --- |
| `search` | Activity extension: supported engine, submitted query, domain/path |
| `search_block` | Activity extension: supported query with explicit browser blocking error |
| `website_block` | Loopback AdGuard DNS queries or Safe Steam/Crunchyroll page blocks |
| `screen_block` | Registered monitor: KWin UUID, title, app/PID, model/reason/scores |
| Usage buckets | Separate KDE worker: active desktop/app time, foreground Chrome hostname time |

No screenshots, page contents, cookies, passwords, or complete browsing URLs
are stored. Queries and window titles can still contain sensitive information.
Search terms are limited to 512 Unicode characters; ordinary blocked-page query
strings are discarded and large model payloads are omitted.

Supported URL searches include Google.com, Bing, DuckDuckGo, Yahoo, Brave,
YouTube, and Steam; this does not exempt engines blocked by browser policy.
POST-only forms, unsupported sites/browsers, and navigation that never commits
can be missed. Blocked searches require `ERR_BLOCKED_BY_ADMINISTRATOR` or
`ERR_BLOCKED_BY_CLIENT`; SafeSearch result hiding and ordinary network errors
are not blocked-query evidence. Literal case-insensitive query filtering runs
in the database before pagination.

AdGuard must be the actual resolver with query logging enabled. Only loopback
clients are imported. DNS records include background queries, ads, and retries;
they do not prove user intent, unique visits, or per-window attribution.
SafeSearch rewrites are not counted as site blocks.

## Foreground usage

A backend-supervised KDE session worker loads `focus.js` and sends five-second
samples over its private stdout pipe. It runs independently of image filtering.
Idle (as reported by KDE/logind), locked, inactive, logged-out, suspended, and
screen-blocked time is excluded. Gaps/restarts/clock jumps never backfill time.
Focus changes can attribute about five seconds to the previous app.

The Activity extension reports the focused Chrome tab's hostname on focus/tab
changes and every 30 seconds; it expires after 40 seconds. Website time is a
subset of Chrome time, never counted twice in desktop totals. Other browsers
and full URLs are not tracked for website usage. Sampling and browser metadata
make these approximate statistics, not tamper-proof evidence.

Portal **Stats & info** shows a Sunday–Saturday week, total time, top ten apps/
sites, searches, and the enabled daily allowance. **Blocks** has separate event
pagination/date filters. The selected day chooses its containing week. Older
weeks may be incomplete because retention rolls continuously.

## Retention and outages

Events and detailed usage expire after a rolling **seven days**. Cleanup runs
at startup, every minute, on writes, and before reports/reads. No permanent
report archive is generated; caller-created exports are not automatically deleted.
Daily allowance accounting uses separate totals so detailed-record eviction
cannot restore today's allowance.

The database cap is 64 MiB, with limits of 100,000 events and 50,000 usage rows.
Oldest records are evicted at capacity and coverage warnings identify partial
reports. SQLite secure deletion is enabled with rollback journals; freed pages
are reused, and neither this nor seven-day expiry promises forensic SSD/backup
erasure. AdGuard separately owns its raw log rotation and deletion timing.

The DNS collector retains a restart-safe watermark and event identities; failure
leaves its watermark unchanged. Backlogs over 50,000 blocked queries per pass
report incomplete collection. Monitor retries use memory (256 events, one-day
maximum age); browser retries use session storage (500 events, one-day age).
Safe site-filter telemetry is best effort. Shutdown/outages can lose queued data.

`/var/log/sentinel/blocked.jsonl` is no longer written. Existing legacy files
are neither imported nor deleted. Browser endpoint Host/Origin checks restrict
ordinary web-page writes but do not authenticate local programs that can forge
headers. These records are accountability metadata, not an immutable audit trail.

## Commands and API

```bash
sen activity status
sen activity events --kind screen_block --limit 5
sen activity events --kind search --query 'example phrase'
sen activity events --kind search_block
sen activity report --period day --timezone America/Chicago
sen activity report --period week --timezone America/Chicago
sen activity screen-time --timezone America/Chicago
```

Events accept `--start`, `--end`, `--before-id`, and limits 1–20. Timestamps need
explicit offsets. Pagination uses newest received ID first; pass `next_before_id`
and keep the same time bounds. Late events do not invalidate that cursor.
Reports accept `--date YYYY-MM-DD`; undated `week` is the last seven days and
`day` is today so far in the requested zone. Screen-time reports instead show
the Sunday–Saturday week containing the chosen date. JSON results expose clipping,
collection status, and capacity/coverage limits.

RPCs: `activity.status`, `activity.events`, `activity.report`, `activity.usage`;
monitor-only ingestion is `activity.monitor_block`. Helpers in
`sentinel.daemon.client` include `get_activity_status`, `list_activity_events`,
`get_activity_report`, and `get_screen_time_report`. Reads obey socket authorization
in every mode; paired-device reads require `activity.read`. No generic append,
clear, or retention-change action is exposed to clients.

## Install and diagnose

Update the protected wheel and restart the backend using
[development runtime](../../README.md#development-runtime). Install existing
signed Activity/Safe Steam/Safe Crunchyroll assets:

```bash
sudo bash tools/install_activity_assets.sh
```

For browser-source changes, bump the manifest and rebuild with the **original**
private signing key before installing. For Activity alone:

```bash
.venv/bin/python tools/build_safe_extensions.py --only activity
```

Keep `~/.local/share/sentinel-extension-keys/activity.pem` private, outside the
checkout. `SENTINEL_EXTENSION_KEY_DIR` can select another external directory.
Prebuilt assets need no key to install. Reload Chrome policies/extensions and
verify installed versions against the current source manifest/update XML.
Test a supported search, foreground app/site usage, and harmless test blocks;
inspect `sen activity status` for collector gaps.

For AdGuard login failures, use `sudoedit /etc/sentinel/adguard.toml` and verify
the actual local web port/login, not the YAML bcrypt hash. For SQLite errors,
check filesystem space/inodes/mount health and service logs before attempting
repair; the database is never automatically deleted/reset.

```bash
.venv/bin/python -m pytest sentinel/daemon/tests sentinel/dns/tests -q
```

Tests isolate storage and privileged operations; live KDE usage, Chrome refresh,
and OS DNS routing still need target-machine checks.
