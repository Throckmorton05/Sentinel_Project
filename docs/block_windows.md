# Sentineld Block Windows

Sentineld owns one-time and recurring block periods for apps, websites, and screen time.
The authoritative state is stored in `/var/lib/sentinel/block-windows.json` and
is writable only by the daemon.

## Lock behavior

A block window has its own `lock_type`, independent of Sentinel's machine-wide
lock mode.

| Machine mode | New window choices |
|---|---|
| `unlocked` | `unlocked`, `self_lock` |
| `self_lock` | `unlocked`, `self_lock` |
| `guardian_lock` | forced to `guardian_lock` |

`unlocked` block windows may be edited, disabled, shortened, or deleted freely,
even if the machine later enters Self Lock. `self_lock` windows may only be
maintained or strengthened until their end time (or indefinitely when recurring).
Screen time has the two-minute disable exception described below. `guardian_lock` windows require
a valid Guardian PIN for mutations. Any window edited while the machine is in
Guardian Lock is promoted to `guardian_lock`.

Guardian-locked windows cannot be created as one-off password schedules on an
Unlocked or Self-Locked machine.

## Enforcement

Active scheduled apps are merged into the effective sentineld app policy and
therefore use the same process-kill and AppArmor enforcement as permanent app
blocks. The schedule does not modify `/etc/sentinel/blocked_apps.toml`.

Active scheduled domains are merged into generated AdGuard rules and do not
modify the user's permanent DNS blocklist. A scheduled domain remains enforceable
even when normal DNS filtering is disabled; in that case only temporary schedule
rules are enabled.

A sentineld reconciliation loop checks schedule boundaries twice per second and
re-applies enforcement when the active set changes.

## RPC API

Read operations:

- `blockwindow.policy` — lock choices, limits, and whether Guardian auth is required.
- `blockwindow.list` — paginated calendar-ready summaries; accepts `range_start`,
  `range_end`, `include_ended`, and `include_targets`.
- `blockwindow.get` — complete details and targets for one window.
- `blockwindow.effective` — bounded summary of the schedule restrictions active now.

Mutation operations:

- `blockwindow.create`
- `blockwindow.update`
- `blockwindow.delete`

Create fields include `name`, `start_at`, `end_at`, `timezone`, `lock_type`,
`apps`, `domains`, `enabled`, `notes`, `block_type`, and `recurrence`. `start_at` and `end_at` accept Unix
timestamps or ISO-8601 strings. Naive ISO times are interpreted using the supplied
IANA `timezone`.

Application targets use the same shape as sentineld's app blocker:

```json
{
  "key": "opera",
  "name": "Opera",
  "type": "flatpak",
  "flatpak_id": "com.opera.Opera"
}
```

or:

```json
{
  "key": "kate",
  "name": "Kate",
  "type": "native",
  "executable": "kate"
}
```

Calendar/list responses expose normalized UTC and local start/end values,
duration, current state (`scheduled`, `active`, `ended`, or `disabled`), seconds
to transitions, target counts, current edit/delete permissions, lock information,
revision numbers, and the next daemon transition time. Full target lists should
normally be fetched with `blockwindow.get` so calendar pages stay below the RPC
message-size limit.
## Screen time and repeating schedules

The Portal calendar's **New window** editor supports **Apps and websites** or
**Screen time**, with **Does not repeat**, **Daily**, **Weekly**, and **Monthly**
choices. Screen time covers every connected display with a dark grey Sentinel
overlay, independently of the image filtering setting.

Guardian bypass requires the PIN and releases only the selected occurrence;
future repeats remain scheduled. Self-Lock grants one two-minute extension per
occurrence and opens the Portal calendar. During those two minutes the user may
disable or delete that screen time schedule. If they leave it enabled, the
overlay returns when the extension expires. This exception never permits
weakening other Sentinel protections or normal Self-Locked app/website windows.
Overlapping desktop blocks must each be bypassed before access returns.

Repeats use the schedule's saved IANA timezone and local start/end clock times.
Weekly windows repeat on the original weekday. Monthly windows retain the
original day, clamp to the month's last day when necessary, and return to the
original day in later months. Missing DST clock times move forward by the gap;
an occurrence consumed entirely by the gap is skipped. The original repeated
DST-hour choice is preserved. Daily windows may last at most one local day,
weekly windows seven, and monthly windows 28, preventing overlapping instances
of the same schedule. Calendar queries expand at most 366 days at a time.

Normal recurring Self-Locked commitments remain locked between occurrences.
Calendar edits, disabling, and deleting affect the entire series, not just the
selected calendar occurrence. No recurrence end date is implied.

The daemon saves extension usage and Guardian bypasses in root-owned schedule
state. Restarting sentineld does not renew the extension. The desktop supervisor
recreates terminated overlays. Overlay failures log an error and retry; screen
time never locks the graphical session, terminates Rofi, or changes KWin effects.
Layer-shell coverage and exclusive keyboard input require KDE Wayland
and the same `org.kde.layershell` QML module used by the anti-tamper screen.

```bash
sen block-window create "Evening break" \
  --start '2026-10-09T21:00:00' --end '2026-10-09T22:00:00' \
  --timezone America/Chicago --screen-time --lock self_lock --repeat daily

sen block-window create "Weekly focus" \
  --start '2026-10-09T10:00:00' --end '2026-10-09T11:00:00' \
  --timezone America/Chicago --domain youtube.com --repeat weekly

sen block-window bypass WINDOW_ID
sen block-window disable WINDOW_ID
```

After applying the patch, stop the running dev daemon, install the updated wheel,
and restart it:

```bash
cd ~/Sentinel_Project
.venv/bin/python -m pytest sentinel/daemon/tests sentinel/portal/tests -q
poetry build -f wheel
sudo systemctl stop sentineld.service
sudo /usr/lib/sentinel/venv/bin/python -I -m pip install --no-deps --force-reinstall \
  dist/sen-0.1.0-py3-none-any.whl
sudo /opt/AdGuardHome/AdGuardHome -s start
sudo env SENTINEL_DEV_ALLOW_FIREFOX=1 \
  /usr/lib/sentinel/venv/bin/python -I -m sentinel.daemon.server
```

For a manual Wayland check, create a short **unlocked** screen time window first.
Confirm every monitor is covered, PIN typing works in Guardian Lock, a wrong PIN
keeps coverage, one bypass leaves an overlapping window blocked, and killing
the overlay causes it to be recreated. Then verify the Self-Lock extension and
schedule disable within its two-minute grace.
