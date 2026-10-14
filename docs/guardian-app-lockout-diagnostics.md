# Diagnosing Guardian app lockout after login

Keep Guardian App Access disabled until the independent recovery smoke test
below succeeds on the target desktop. Automated tests and the diagnostic script
never enable host policy or alter host services. This runtime patch fixes the
global desktop-launch deny and approved-Konsole authorization conflict, and adds
authenticated offline recovery.

## Recover when KDE denies every app, including the recovery GUI

Switch to a text console with Ctrl+Alt+F3 and log in as the normal Linux user.
The installed disable-only helper can run from this console without KDE or its
recovery socket. Stop the main daemon first so it cannot race the offline write:

```bash
sudo systemctl stop sentineld.service
sudo /usr/lib/sentinel/venv/bin/python -I -c 'import getpass, json; from sentinel.guardian_apps.recovery import disable_offline; print(json.dumps(disable_offline(getpass.getpass("Guardian PIN: "), 0), indent=2))'
```

Confirm `enabled=false` and `errors=[]`. If the helper reports an error, keep
that error for diagnosis. After success, start sentineld again and reboot to
reload KDE's cached restrictions. Reboot ends the session and any unsaved work.

```bash
sudo systemctl start sentineld.service
sudo reboot
```

## What the tests check

Run from the project checkout, without sudo:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest sentinel.daemon.tests.test_guardian_apps_lockout sentinel.daemon.tests.test_guardian_recovery sentinel.daemon.tests.test_guardian_apps_gui -v
```

The tests recreate the six saved approvals from the reported incident: Dolphin,
Info Center/System Settings, Firefox Snap, Chrome, Rofi and Konsole. Policy files,
launcher overrides and KDE configuration are temporary fixtures. Process scans,
RPC connections, Qt dialogs and service probes are mocked.

| Test scenario | Result with the fixes | Meaning |
| --- | --- | --- |
| Cold daemon start with those six saved identities | All approvals and launch authorization survive | Selection loss is not reproduced by this fixture |
| Runtime process scan after restart | Approved processes survive; unapproved Kate is stopped | The app scan distinguishes the saved identities |
| Daemon socket disappears | Normal apps fail closed; recovery opens independently | Recovery has a fixed unprivileged entry point |
| Open the recovery manager directly while the daemon is down | Offline recovery window opens | Existing Guardian PIN verifies through the independent helper |
| Disable in persisted state, then start a fresh daemon | Generated overrides and Sentinel Kiosk block are removed; selections remain | Cleanup works in the isolated restart fixture |
| Old cached recovery override invokes the ordinary gate | Gate opens only the fixed recovery UI without RPC | The update does not depend on an immediate KDE cache reload |
| Approved Konsole declares `X-KDE-AuthorizeAction=shell_access` | No Sentinel shell restriction conflicts; revoking it restores the restriction | Approval is reflected in KDE authorization |
| KIO global desktop-launch check on six approvals and recovery | No Sentinel `run_desktop_files` deny | Installed apps pass KDE's prerequisite even without launcher authorization metadata |
| Cold start with the older Sentinel global deny block | Old deny removed, selections and unrelated configuration preserved | Migration repairs persisted configuration |
| Wrong PIN, other user, Self Lock, missing credentials or writable/symlink selection | No policy weakening | Recovery is narrow and authenticated |
| Live daemon or another recovery writer holds the instance lock | Offline write and duplicate daemon startup are rejected | Cached daemon policy cannot race offline changes |
| Diagnostic collection | State bytes remain unchanged; no reconcile or app launch | The report can be collected with the setting disabled |

Both formerly expected-failure tests now pass normally. With PyQt6 available the
combined run finishes with **44 tests, no failures**. Without PyQt6, GUI tests are
skipped. Tests use real temporary PIN hashes and kernel peer credentials over a
Unix socket pair. They do not simulate a full live Plasma or systemd session.

The Konsole test supplies the KDE authorization metadata explicitly. The report
checks the actual installed launcher to determine whether that conflict exists
on the affected PC. KDE documents that denied `X-KDE-AuthorizeAction` keys can
hide a launcher and prevent its execution:
https://develop.kde.org/docs/administration/kiosk/keys/

## Collect a report on the affected PC

From the checkout, run using the protected installed Python:

```bash
sudo /usr/lib/sentinel/venv/bin/python -I -B tools/guardian_app_diagnostics.py > ~/sentinel-guardian-diagnostics.json
```

After installing the runtime fix, the tool imports the installed Sentinel
runtime, compares it with this checkout and
prints JSON to stdout. The shell writes the report in your home directory. It
works with sentineld running or stopped, provided the protected Python/runtime
are installed. Sudo is needed to read the protected selection and configuration.

The report includes:

- RPC reachability, persisted selection and whether installed launcher identities
  still match saved approvals.
- Generated launcher gates/hidden flags, existing KDE action restrictions and
  conflicts predicted by the installed renderer even while disabled.
- Current launch authorization for each saved ID if RPC is reachable. This RPC
  only returns a command; the tool never executes it.
- Installed and development daemon unit status, persistent boot enablement,
  terminal-attached daemon processes and filtered lifecycle events from this boot.
- Model readiness on ports 8000–8003, separately from the app launch gate.
- Hash comparisons between installed modules and checkout files.
- The Firefox development exemption flag, without other process environments.
- Independent recovery socket availability and unit status.

Credentials, window titles and browsing/inference logs are not collected. The
report does contain app selections, local paths, service status and process IDs.

## Interpret the findings

`DAEMON_DOWN_WITH_GATED_LAUNCHERS` means overrides are still installed but their
required daemon connection is unavailable. This matches an all-app launch
failure even when the selections are saved correctly. A foreground development
daemon can disappear when its terminal/session closes. A transient development
unit lasts only until reboot; a running daemon alone does not prove persistent
boot startup.

`APPROVAL_CONFLICTS_WITH_KDE_AUTHORIZATION` identifies an installed launcher whose
required action is denied by the configuration currently on disk. The
`APPROVED_APP_WOULD_CONFLICT_WITH_KDE_AUTHORIZATION` finding checks the same
conflict against the mode's template without enabling it. This is a separate
possible explanation for Konsole; it does not explain every approved app.

`KDE_BLOCKS_ALL_DESKTOP_LAUNCHES` identifies `run_desktop_files=false` on disk.
KIO checks this globally before executing any installed launcher, including
recovery, and returns "You are not authorized to execute this file." Sentinel
approval and a running daemon cannot bypass that earlier KDE check. The older
tests checked only `X-KDE-AuthorizeAction` and missed this global restriction.
The updated tests cover it explicitly. `KDE_WOULD_BLOCK_ALL_DESKTOP_LAUNCHES`
detects an installed runtime that would write this rule when enabled, even if
the setting is currently disabled. Code reference:
https://sources.debian.org/src/kf6-kio/6.13.0-6/src/gui/applicationlauncherjob.cpp/

`CURRENT_RPC_DENIES_APPROVED_LAUNCH` identifies an additional launch denial, such
as an ordinary app block or a Block Window. `CHECKOUT_AND_RUNTIME_DIFFER` means
the protected launchers are running different code from the checkout.

An `enabled=false` report is a post-recovery snapshot. It cannot reconstruct
the previous enabled flag, old KDE caches or missing foreground-daemon logs.
Likewise `session_restart_required=true` is a sticky flag in this implementation,
not proof that the current session has stale restrictions. Use the report to
check the current prerequisites and failure paths; retain it for comparison
before attempting another enabled-session test.

## Install the runtime fix

Keep the restriction disabled. Apply the patch to the checkout first, then:

```bash
poetry build -f wheel
sudo systemctl stop sentineld.service
sudo /usr/lib/sentinel/venv/bin/python -I -m pip install --no-deps --force-reinstall dist/sen-0.1.0-py3-none-any.whl
sudo bash tools/install_guardian_app_access.sh
sudo systemctl start sentineld.service
systemctl is-active sentineld.service sentinel-guardian-recovery.socket
```

Stop any old manual daemon before installing. The new daemon prevents duplicate
instances, but an old daemon that predates the lock must not remain running.
The installer preserves existing sentineld service/drop-in settings, including
the development Firefox exemption. Both status lines should be `active`.

## Target-PC recovery smoke test

First run this with the restriction **still disabled**, so a failed test cannot
recreate the original app lockout. Guardian Lock must remain selected.

```bash
sudo systemctl stop sentineld.service
/usr/lib/sentinel/venv/bin/python -I -m sentinel.guardian_apps.manager
```

The recovery window should open. Click **Disable app restriction**, enter the
existing Guardian PIN, and confirm the disabled message. Close the window, then:

```bash
sudo systemctl start sentineld.service
sudo /usr/lib/sentinel/venv/bin/python -I -B tools/guardian_app_diagnostics.py > ~/sentinel-guardian-fixed-diagnostics.json
```

All package hashes should match; the recovery socket should be available; and
the recovery dependency/Konsole predicted conflict findings should be absent.
Keep sentineld enabled at boot. Then, if this smoke test succeeds, use the normal
manager to confirm Konsole and the desired apps are selected, enable, and sign
out/back in. Verify approved launches and an unapproved GUI app. Afterwards test
an outage from a retained terminal and recover through the app menu, then start
sentineld again and sign out/back in to reload KDE. Automated results do not
replace these target-session checks.

The recovery units were statically checked with `systemd-analyze verify` in an
isolated filesystem containing a stub at the production Python path; no service
was started during that check. Systemd's socket activation contract is described
at https://manpages.ubuntu.com/manpages/noble/man5/systemd.socket.5.html.
