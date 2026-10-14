# Sentinel app blocker

The protected backend owns application block policy and enforcement. Desktop
clients submit authenticated requests; they do not write AppArmor profiles or
start a separate process monitor.

## Enforcement

- **AppArmor:** resolved executable profiles are generated under
  `/var/lib/sentinel/apparmor/profiles` and loaded with `apparmor_parser`.
- **Root process watchdog:** scans every 250 ms, matches native executables and
  Flatpak identity, and stops matching processes. Snap targets match kernel
  labels/mounted executable paths for the protected user; Sentinel does not
  replace Snap's own profiles.
- **Native Steam:** matches its supported launcher, user client/webhelpers, and
  exact launcher-script operand; selecting Steam blocks the client, not one game.

Runtime policy refreshes every two seconds and full profile reconciliation
runs every 30 seconds. Desired profiles reload after reboot; unresolved targets
retain existing profiles and expose pending errors. Legacy profiles are migrated
only when they match Sentinel's historical naming/template exactly.
Process polling may allow an app to run briefly; it is not universal pre-execution
confinement. Known alternative browsers are a separate fixed baseline.

## Policy and commands

The durable personal list is `/etc/sentinel/blocked_apps.toml`:

```toml
[apps.kate]
name = "Kate"
type = "native"
executable = "kate"

[apps.opera]
name = "Opera"
type = "flatpak"
flatpak_id = "com.opera.Opera"
```

```bash
sen app-block list
sen app-block status
sen app-block add-native kate kate --name Kate
sen app-block add-flatpak opera com.opera.Opera --name Opera
sen app-block remove kate
sen app-block sync
```

The Portal's searchable **Add apps +** picker also supports Snap instance targets.
There is no `sen app-block add-snap` command. New blocks strengthen protection;
removing active blocks weakens it. Unlocked permits both; Self Lock allows adds
and maintenance but denies removals; Guardian Lock requires its PIN for writes.
Removing a personal entry never removes browser-baseline or scheduled restrictions.

## API and modules

Reads: `appblock.list`, `appblock.status`, and local GUI `appblock.catalog`.
Writes: `appblock.add`, `appblock.remove`, `appblock.sync`. The backend validates
app identities and policy; clients cannot supply arbitrary parser paths, flags,
PIDs, shell commands, or trusted authentication booleans.

`resolver.py` resolves runtime targets. `AppArmor/` generates/loads profiles;
`process_monitor/` matches and stops processes.
`sentinel/daemon/operations/app_blocker_ops.py` owns policy/reconciliation and
`sentinel/daemon/supervision/app_blocker.py` owns continuous scanning.
The standalone `sentinel-process-monitor` entry point has been removed.

Guardian allow-only GUI selections are separate from personal blocks and do not
bypass them; see [Guardian apps](../guardian_apps/README.md). Scheduled target
unions use the same enforcement; see [block windows](../../docs/block_windows.md).

## Validation

```bash
.venv/bin/python -m pytest sentinel/daemon/tests -q
```

Tests isolate privileged commands and cover authorization, persistence, matching,
profile reload/retention, and unsupported standalone use. Verify real AppArmor,
Snap, Steam, and process behavior in the target installation before shipping.
