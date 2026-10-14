# Guardian PIN setup

Fresh installations have no default Guardian PIN. The protected credential
file records an unset state until the first entry into Guardian Lock.

In the Portal, choose **Enable Guardian Lock**. In the CLI, run:

```bash
sen guardian-lock
```

Every transition from Unlocked into Guardian Lock asks you to create and
confirm a PIN containing 4–32 ASCII digits. Any previous PIN is replaced; you
do not need to remember it. Canceling either prompt or entering mismatched PINs
leaves Sentinel unlocked and keeps existing credentials unchanged. The backend
validates both values and saves only a salted scrypt hash in a root-only file.

The setup and mode transition share one serialized backend request. Credentials
are saved before the Guardian lock state. If the credential write fails, the
mode stays Unlocked. If the lock-state write fails after saving the PIN, the
new PIN remains configured; retrying entry asks for a confirmed PIN again.

While Guardian Lock is active, the setup request cannot replace the PIN.
Exiting Guardian Lock or changing its PIN still requires the current PIN.
Self Lock cannot enter Guardian Lock.

## Portal entry

Opening the Portal in Guardian Lock requires the current Guardian PIN before
any settings, calendar, activity, or other pages load or become visible. Incorrect
PINs keep the entry screen locked; closing it cancels entry. Each new window
asks again, including direct page shortcuts. Unlocked and Self Lock open normally.

The PIN is cleared from the field when submitted and is never saved for later
operations. A disconnected Portal hides its cached pages and offers the existing
independent PIN-protected Guardian recovery. Reconnecting in Guardian Lock
requires a new login. Enabling Guardian Lock from another client locks an open
Portal on its next mode refresh.

Login authorizes viewing only in that Portal window. Protected changes still
require their own backend PIN verification. CLI and companion read access are
unchanged.

## Existing installations and schedules

Version 1 credential hashes remain valid during upgrade until a successful
new entry or authenticated PIN change. Re-entering Guardian Lock from Unlocked
replaces the saved PIN even on an upgraded installation.

The new PIN also authorizes existing Guardian-owned schedules and controls:
they use the same machine-wide Guardian credential. Their commitments,
targets, and ownership remain in place. While Unlocked, someone able to enable
Guardian Lock can choose this replacement PIN and then authenticate those
Guardian-owned controls.

An already Guardian-locked system requires a configured credential at startup.
Missing, malformed, insecure, or unset credentials are rejected; offline
recovery also refuses an unset credential store.

## Local API

- `guardian.pin_status` returns only `configured` and the current `mode`.
- `guardian.authenticate` verifies the submitted `guardian_pin` only while
  Guardian Lock is active. It returns `authenticated: true` without changing
  policy or issuing credentials for later requests. JSON authentication booleans
  never substitute for the PIN. This local action is serialized with PIN and
  mode changes, and is not granted to paired devices.
- Every `state.enter_guardian_lock` from Unlocked requires `new_pin` and
  `pin_confirmation`.
- Authenticated repeat requests while already locked retain the existing mode
  and cannot supply replacement PIN parameters.

Only the trusted local socket interface exposes setup. Future paired-device
permissions do not grant PIN setup or replacement. Clients cannot authenticate
themselves with a JSON boolean.
