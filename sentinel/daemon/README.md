# Sentinel privileged daemon

`sentineld` is the root backend for Sentinel's local control interface. The CLI,
Portal, and App Store call [`client.py`](client.py); they do not run their GUI as
root or directly mutate protected configuration. The daemon authenticates local
peers, authorizes requested operations, persists policy, and coordinates
enforcement and supervised desktop processes.

## Code map

| Area | Responsibility |
| --- | --- |
| [server.py](server.py) | Socket, peer handling, startup, worker limits, reconciliation threads |
| [client.py](client.py) | Typed client helpers and request timeouts |
| [rpc/protocol.py](rpc/protocol.py) | Versioned newline-delimited JSON and message bounds |
| [rpc/request_handler.py](rpc/request_handler.py) | Action dispatch, input validation, mutation locking, admin authorization |
| [rpc/capabilities.py](rpc/capabilities.py) | Published API capability information |
| [security/](security/) | Local peer identity, Guardian authentication, change policy, caller context |
| [core/](core/) | Protected user configuration, lock state, session discovery, constants |
| [operations/](operations/) | Settings, packages, DNS, browsers, schedules, updates, and other backend actions |
| [supervision/](supervision/) | Image-monitor readiness/watchdog, desktop app and screen-time supervision |
| [tests/](tests/) | Backend behavior, authorization, lifecycle, and client contract tests |

## Local request path

The Unix socket is `/run/sentinel/sentineld.sock`, owned by root and the
`sentinel` group with mode `0660`. The server additionally reads Linux
`SO_PEERCRED` and checks root/control-group authorization before accepting a
request. Socket access is only the first boundary; each action still has policy
checks. Local group members are not automatically Guardian-authenticated.

The protocol uses version 1 JSON objects, bounded to 64 KiB, terminated by a
newline. There is one request/response exchange per connection. Requests carry
an ID, an action, and a parameters object; responses identify the request and
return a result or structured error. Use `client.py` and
[`rpc/responses.py`](rpc/responses.py) for the exact envelopes instead of
inventing a separate protocol in a GUI.

The server limits connection workers to 32 and uses a ten-second socket read
timeout. A shared nonblocking mutation lock serializes actions that require
exclusive mutation. Selected reads and runtime messages can run concurrently;
other requests may return `daemon_busy`. The allowed concurrent action list is
in `request_handler.py`. Long-running operations use operation-specific bounds
and client timeouts; the socket read timeout is not their execution deadline.

## Authorization and lock modes

[`security/policy.py`](security/policy.py) classifies changes as read, maintain,
strengthen, or weaken:

| Mode | General rule |
| --- | --- |
| `unlocked` | Normal operations may change policy |
| `self_lock` | Read, maintenance, and strengthening allowed; weakening denied |
| `guardian_lock` | Reads allowed; mutations require Guardian authentication verified by the daemon |

Some administrative operations have additional restrictions and classification
rules in the request handler. Follow the concrete handler before adding an
action. A no-op is not automatically exempt from Guardian authentication.
The Portal's login gate can be stricter than the backend's local read policy.

Guardian verification belongs in the backend. A client cannot grant itself an
authenticated status by sending a boolean. Similarly, paired-device identity
and grants must come from trusted server code, not request JSON.
[`security/caller_context.py`](security/caller_context.py) provides a closed
device-action permission map, but does not implement network listening,
credential verification, or pairing. See [Companion API](../../docs/companion-api.md).

## Configuration and state

[`core/config.py`](core/config.py) resolves one protected, non-root account from
`/etc/sentinel/daemon.toml`:

```toml
protected_user = "your-desktop-user"
```

The file and its parent must be root-owned and not writable by other users; the
file must be regular and is opened without following symlinks. The named user
must exist and belong to the `sentinel` control group. Registration cannot change
that protected identity.

Canonical paths live in [`sentinel/settings/paths.py`](../settings/paths.py).
Important locations include:

| Location | Purpose |
| --- | --- |
| `/etc/sentinel/settings.toml` | Persisted settings |
| `/var/lib/sentinel/lock-state.json` | Lock state |
| `/var/lib/sentinel/guardian-auth.json` | Protected Guardian authentication state |
| `/var/lib/sentinel/block-windows.json` | Persisted scheduled blocks |
| `/var/lib/sentinel/activity/events.sqlite3` | Bounded activity/event history |
| `/usr/share/sentinel/` | Packaged policy, models, and enforcement assets |
| `/usr/lib/sentinel/venv` | Installed protected Python runtime |

These have different owners and update semantics from a developer checkout.
Do not overwrite configured user policy, authentication state, or activity with
sample files during deployment. Treat executable runtime and enforcement
assets as part of the protection boundary.

## Startup and reconciliation

`server.py` requires root, loads the protected UID, initializes lock and Guardian
state and policy stores, and reconciles enforcement before serving clients.
It starts the extension endpoint on `127.0.0.1:8765` as well as the control socket.
Background workers reconcile scheduled policy, DNS/browser/application controls,
DNS source refresh, image-filter lifecycle, Activity, screen time, and Steam/app
supervision. Read the startup code for the current list and shutdown ordering.

Persisted desired state is distinct from successful runtime application. For
example, image filtering can remain enabled in settings while models or the
desktop session are unavailable; status reports pending readiness and errors.
The reconciler retries. Intentional disable stops supervision before stopping
the monitor and model services, so it does not become a tamper penalty.
See [image-filter lifecycle](IMAGE_FILTERING.md) and
[screen monitor](../image_blocking/screen_monitor/README.md).

Desktop helpers run in the protected user's session, model services as the
`sentinel` system account, and privileged operations in the daemon. Preserve
exact process/interpreter checks and bounded fixed commands when editing this
division. Window-blocking uses exact KWin UUIDs; generic process/window matching
is not an equivalent replacement.

## Where to extend behavior

For a new control operation, follow an existing related action through the
client helper, RPC dispatch, input validation, authorization, operation, and
tests. Classify its effect on protection and check all relevant lock modes,
denied-state preservation, concurrent behavior, and errors. Add companion grants
only when the device caller is intended to access that exact operation.

Keep user-provided values out of shell commands and root service names. Existing
operations use explicit arguments, allowlists, and validation; preserve their
constraints. Return useful structured results/errors so a client can distinguish
policy denial, backend unavailability, pending enforcement, and completed work.

Useful neighboring guides:

- [Settings/CLI](../../sen/README.md), [Portal](../portal/README.md), [App Store](../appstore/README.md)
- [DNS](../dns/README.md), [browser policies](../browser_policies/README.md)
- [Guardian apps](../guardian_apps/README.md), [Guardian websites](../../docs/guardian-website-access.md)
- [Schedules](../../docs/block_windows.md), [Activity](../activity/README.md)
- [Boot media](BOOT_MEDIA_PROTECTION.md), [virtual machines](VM_PROTECTION.md)
- [System/Sentinel updates](../../docs/sentinel-updates.md), [signed GitHub channel](../../docs/github-updates.md)

## Tests and installed diagnostics

Run focused tests from the repository environment, without starting a second
root daemon:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  sentinel/daemon/tests/test_daemon.py \
  sentinel/daemon/tests/test_policy.py \
  sentinel/daemon/tests/test_admin_policy.py \
  sentinel/daemon/tests/test_guardian_rpc.py \
  sentinel/daemon/tests/test_image_filtering.py -q
```

Select additional operation suites for the change. Mocked/substituted system
dependencies do not verify native enforcement; test actual KDE, DNS, AppArmor,
browser, package, and boot integration as applicable.

For an installed service, inspect:

```bash
systemctl status sentineld.service --no-pager
journalctl -u sentineld.service -n 100 --no-pager
sen setting get image_filtering
```

The last command reads saved intent. Read runtime image status through
`client.get_image_filtering_status()` as shown in
[native verification](IMAGE_FILTERING.md#native-verification). Keep credentials
and personal activity out of shared diagnostic output.

Follow the [development runtime guide](../../README.md#development-runtime) when
replacing the installed wheel. Choose the existing service or one foreground
development daemon; the daemon includes a single-instance guard, and running a
second instance is not a way to test a checkout change.
