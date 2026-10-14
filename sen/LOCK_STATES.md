# Lock states through sen

`sen` collects input and calls the existing `sentinel.daemon.client.request`
Unix-socket client. `sentineld` verifies credentials, classifies changes and
enforces policy. Skipping the CLI or sending `guardian_authenticated: true`
does not bypass the daemon checks.

## Commands

```bash
sen state
sen self-lock
sen guardian-lock
sen guardian-unlock
sen guardian-change-pin
sen setting get safe_browsing
sen setting set safe_browsing true
sen setting set safe_browsing false
```

Self Lock asks for confirmation because it is permanent. `--yes` skips that
confirmation; it never skips daemon authentication or policy checks.
There is no self-unlock command or RPC.

Guardian Lock must be exited with a valid Guardian PIN before Self Lock can be
enabled from Unlocked. Direct Guardian Lock to Self Lock requests are denied,
even with a valid PIN. Repeating `guardian-lock` while already in Guardian Lock
requires the PIN. Every new entry from Unlocked creates and confirms a new PIN;
the previous PIN is not required. Once in Self Lock, every mode-transition
request is rejected, even if it includes a valid Guardian PIN.

The PIN prompts use Python 3.14's `getpass` with `echo_char="*"`, so each typed
character appears as an asterisk. Desktop user passwords remain fully hidden.
Changing the PIN prompts for
the current PIN, the new PIN, and confirmation. No PIN command-line option is
provided. If a terminal cannot disable echo, the CLI refuses to read a secret.
Uncaught CLI exception formatting does not display local variables.

Before ordinary administrative mutations, the CLI queries `state.get` and
prompts proactively when Guardian Lock is active. The daemon independently
checks the state again. A concurrent change to Guardian Lock can therefore
reject an already prepared unauthenticated request; rerun the command.
There is no automatic retry of privileged operations and no authentication
session or cached authorization. Bundles prompt once and submit the PIN with
each installation; every individual request is authenticated by the daemon.

## Installation policy

| Mode | Installation | Settings | Mode changes |
| --- | --- | --- | --- |
| Unlocked | Any valid APT package name | Any registered change | Enter either lock mode |
| Self Lock | Approved packages only | Maintain or strengthen | Denied |
| Guardian Lock | Any valid APT package name, with PIN | Every mutation requires PIN | Exit to Unlocked with PIN |

The approved `sen` package catalog is `APPROVED_PACKAGES` in
`sen/install/packages.py`. Its production installation must remain root-owned,
like the rest of Sentinel's code. This is the existing CLI catalog, distinct
from the App Store's `approved_apps.toml`.

Self Lock accepts approved aliases (`ninja`, `python`) and exact APT package
names contained in approved APT definitions (`ninja-build`, `python3`). Known
aliases retain their configured APT, pipx or custom installation methods.
Known Flatpak application aliases still direct users to the App Store.
Unrecognized names use APT in Unlocked or authenticated Guardian Lock.

The previous `safe_installs` setting no longer overrides these `sen` mode rules.
Package names are validated in the daemon, passed as individual subprocess
arguments after `--`, and never passed through a shell. Local DEB paths, APT
options, glob patterns and version selectors are outside this command's
interface. Self Lock APT installations use `--no-remove` so an approved install
cannot remove other packages to resolve a dependency conflict.

## Other administration

All existing administrative `sen` commands now submit their mutations through
the daemon, including autostart, autologin, auto-update and auto-restart
configuration. Autologin still requires the desktop user's password; PAM
verification now happens inside the daemon, using the authenticated socket UID.
Autostart file operations run as that user, never as root.

Guardian Lock requires a PIN for package removal, service mutations, desktop
configuration, system updates, restart and shutdown. Read-only status commands
do not require a PIN. Monitor registration, heartbeat and tamper-blocker
registration remain watchdog infrastructure operations and do not prompt.

For Self Lock, package removal and user-managed service/configuration mutations
are conservatively classified as weakening and denied. Arbitrary services do
not have a defined protection ordering, and package removal can remove
dependencies. System updates, restart and shutdown remain available. Registered
settings still use their existing strengthen/maintain/weaken definitions.

The generic settings CLI exposes registered settings only. This snapshot
registers `safe_browsing`; unregistered names remain rejected by the daemon.

Autostart entries reside in the user's own home directory. This patch enforces
policy for changes requested through `sen`; it does not make those session files
immutable or prevent the user from modifying their own files directly.

## Apply and verify

Save the patch in Downloads, then run from your source checkout:

```bash
cd ~/Sentinel_Project
git apply --check ~/Downloads/sentinel-sen-lock-states.patch
git apply ~/Downloads/sentinel-sen-lock-states.patch
.venv/bin/python -m unittest discover -s sentinel/daemon/tests -v
```

Restart the development daemon after applying the patch. Stop the old foreground
process with Ctrl+C, then start it from this checkout with your existing command:

```bash
cd ~/Sentinel_Project
sudo /usr/lib/sentinel/venv/bin/python -m sentinel.daemon.server
```

In a second terminal, use the source CLI so it matches the daemon's new protocol:

```bash
cd ~/Sentinel_Project
.venv/bin/python -m sen.cli.sen state
.venv/bin/python -m sen.cli.sen guardian-lock
.venv/bin/python -m sen.cli.sen setting get safe_browsing
.venv/bin/python -m sen.cli.sen setting set safe_browsing true
.venv/bin/python -m sen.cli.sen guardian-change-pin
.venv/bin/python -m sen.cli.sen guardian-unlock
```

Use your current PIN; applying the patch does not reset existing credentials.
Do not enter Self Lock on your development machine unless you intend to keep it
permanently locked. Testing package installation against a live daemon performs
a real installation.

For an installed `sen` command, rebuild and reinstall the updated wheel into
the same environment as that entry point. Installed daemon deployments also
need the updated wheel. Test with the source commands above before packaging.

The automated tests mock privileged package/service/configuration side effects
and use temporary state/credential files. Socket tests use a real AF_UNIX socket
pair with connection selection and peer authorization substituted. They cover
client encoding, framing, server dispatch, actual scrypt PIN verification,
policy denial, and persisted state transitions without touching live system
configuration. The creation environment does not permit a listening Unix socket;
the live-daemon commands above are the deployment verification step.
