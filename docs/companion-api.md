# Sentinel client and companion API foundation

This API serves the desktop settings portal now and prepares a future guardian
companion. All actions currently use `/run/sentinel/sentineld.sock`. This patch
does not start a LAN listener, pair devices, change firewall rules, or contact
hosted services. Existing CLI requests and protocol version 1 remain compatible.

## Discovery and settings

| Action | Parameters | Result |
| --- | --- | --- |
| `api.capabilities` | none | API/protocol versions, features, limits, companion action permissions, and caller grants |
| `settings.schema` | none | Canonical names, boolean types, defaults, descriptions, aliases, and protection effects |
| `settings.snapshot` | none | All registered settings and an opaque `revision` |
| `settings.get` | `name` | Existing single-setting result |
| `settings.set` | `name`, `value`, optional `expected_revision` | Existing setting-change result and enforcement status where supported |
| `state.get` | none | Existing authoritative lock state |

The snapshot covers registered settings in `settings.toml`. DNS lists, guardian
app/website selections, block windows, and enforcement status retain their own
existing actions, listed by `api.capabilities`. It does not imply a transaction
across those subsystems. The schema omits duplicate legacy names but lists their
aliases; existing aliases remain accepted for reads/writes.

A revision is a SHA-256 content token for the validated, canonical registered
values, not a numeric counter. It survives daemon restarts and notices changes
to those values made outside the API. Equal values have equal revisions,
including after a change is undone. Clients must treat the token as opaque.
It describes saved configuration, not whether every enforcement backend has
successfully applied it. Check the existing application/status responses.

Load a snapshot, then send its revision with a setting update. Comparison and
writing are performed under the same settings lock. A mismatch returns error
code `revision_conflict` without writing or applying the requested change.
Reload and show the latest values before asking the user to retry; do not
silently overwrite a newer configuration. Fetch a fresh snapshot after a
successful update. Requests omitting the revision retain existing behavior.
This protects competing API clients; arbitrary root programs writing the file
are outside the daemon's locking protocol.

```python
from sentinel.daemon import client

capabilities = client.get_api_capabilities()
schema = client.get_settings_schema()
snapshot = client.get_settings_snapshot()

# Example only: this changes a protection setting if current policy permits it.
try:
    result = client.set_setting(
        "dns.adult", True,
        expected_revision=snapshot["revision"],
        # guardian_pin=... when the local client is in Guardian Lock
    )
except client.SentineldError as error:
    if error.code == "revision_conflict":
        snapshot = client.get_settings_snapshot()
    else:
        raise
```

Never use schema metadata or disabled GUI controls as authorization. The daemon
validates types, classifies changes, and enforces the current lock state on
every write. Self-Lock remains permanent.

## Resumable activity synchronization

`activity.sync` accepts `cursor` (omit or use null for the initial request) and
`limit` (integer 1–20). Python helper:

```python
page = client.sync_activity_events(cursor=saved_cursor, limit=20)
```

The result includes:

* `store_id`, `events`, `next_cursor`, and `has_more`.
* `history_gap`, `gap_reasons`, and `cursor_reset`.
* `oldest_available_id`, `latest_received_id`, and `retention_days`.

Events arrive in ascending receipt-ID order, including delayed events whose
occurrence time is older. No kind/time filters are accepted for this sync
contract, so filtering cannot advance a cursor past unseen events.
Responses are also bounded by serialized size; a page may contain fewer events
than requested. Continue with `next_cursor` while `has_more` is true.

The cursor shape is `{"store_id": "<32 lowercase hex characters>", "after_id": 123}`.
Persist events and the returned cursor together before requesting another page.
Repeated requests are safe: deduplicate using `(store_id, event.id)`.
There are no server-side acknowledgements or per-companion retention extensions.
Activity remains subject to the existing seven-day and capacity limits; sync
never includes screenshots.

`history_removed` indicates potentially missed events due to retention or
capacity eviction, including removals in the middle of the IDs. It also applies
to initial sync when the store has already removed history. Migration from an
older database conservatively reports gaps when IDs are missing. A cursor from
a replaced database causes `store_reset` and returns currently retained events
from the replacement; start a new deduplication namespace. A cursor ahead of
the same store is rejected. A fresh response after consuming a reported gap
does not repeatedly report that old gap.

## Future paired-device transport

`security.caller_context.CallerContext` is an **internal trusted-server API**.
Only after verifying a paired device and loading its permissions from protected
daemon-owned state may an adapter supply a device context to `_handle_request`.
No current socket request can select a caller context. UID/PID continue to
identify the real local peer or adapter; they are not remote device credentials.

The dispatcher uses an immutable context isolated per request/thread. It checks
the explicit `DEVICE_ACTION_PERMISSIONS` allowlist before routing. Supported
device scopes are `api.read`, `settings.read`, `settings.write`, `activity.read`,
`block_windows.read`, and `block_windows.write`. Grant read/write scopes
separately; a write grant does not automatically grant reads.

Device authorization is required even when the machine is Unlocked. A verified
device with an appropriate write grant satisfies guardian authentication for
that permitted action; normal operation-specific lock rules still apply.
Device requests cannot supply `guardian_pin` or `guardian_authenticated`.
Local guardian PIN verification remains unchanged. Request JSON identity fields
cannot create grants or elevate a local caller.

The companion allowlist excludes package installation, service administration,
system shutdown/restart, lock transitions, PIN changes, desktop app launching,
monitor/tamper registration, activity ingestion, and extension approval.
Unsupported/new actions are denied by default. Capability discovery reports
dispatch availability, not a promise that the current policy will allow a write.

Before enabling a LAN listener later, implement encrypted mutual device
authentication, guardian-authorized short-lived pairing, protected key/grant
storage, revocation checked on every request, rate/connection limits, and audit
records with the verified device identity. Never forward arbitrary network
requests through the local socket with the adapter's privileges or trust device
grants supplied by the network client.

## Validation

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest sentinel/daemon/tests -q
```

Tests use temporary settings/activity stores and mocked enforcement operations;
they do not change live Sentinel protections or require a running daemon.
