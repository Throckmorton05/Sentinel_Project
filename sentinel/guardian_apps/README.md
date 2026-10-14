# Guardian app access

Guardian app access selects which installed GUI apps the protected desktop user
may run. It is separate from Safe Installs and personal app blocks. Initially
disabled, it is editable only in Guardian Lock with a freshly verified PIN.
Selections persist but are inactive in Unlocked/Self Lock.

## Use

In the Portal's Guardian > Apps tab, use the shared searchable picker, review
selected cards, turn on **Only allow selected apps**, and Save. Deleting a card
edits the draft; Save applies it. The Portal is the normal management interface;
the old standalone Guardian app/website menu entries and console commands have
been retired. CLI policy operations remain available:

```bash
sen guardian-apps list
sen guardian-apps status
sen guardian-apps approve org.kde.kate.desktop
sen guardian-apps revoke org.kde.kate.desktop
sen guardian-apps enable
sen guardian-apps disable
```

Use exact desktop IDs from `list`. Reads need no PIN; writes require Guardian
Lock and its PIN. Fresh unconfigured selections suggest Dolphin/System Settings
when available; they are not mandatory approvals. Saved unchecked defaults stay
unchecked. Required Sentinel management/recovery entries cannot be revoked.

If the backend is unavailable, open the Portal and choose **Guardian recovery**.
The existing independent helper verifies the Guardian PIN before disabling app
restrictions. A hidden system recovery entry is retained for backend validation
and cached launchers; it cannot be revoked and remains hidden in generated gates.
Old module launch paths open the Portal while online and authenticated recovery
while offline. The Portal's installed/generated launcher remains direct so a
backend outage cannot prevent opening recovery.

Enabling can terminate unselected applications and lose unsaved work, including
Konsole if it is not approved. Use the management GUI when testing without a
terminal. Sign out/back in after enabling or disabling to reload KDE Kiosk.
Approval never bypasses personal blocks, browser baseline, or active schedules.
[Guardian website selection](../../docs/guardian-website-access.md) is independent.

## Enforcement and scope

- Discover supported native ELF/system-script GUI launchers, protected-user/system
  Flatpaks, and Snap GUI exports. Group aliases for the same app; ordinary hidden,
  terminal-only, foreign-desktop, and session-helper entries are not choices.
- Root-generated launcher overrides hide unselected apps. Approved overrides
  consult the backend and launch as the desktop user; alternate actions and
  D-Bus activation are removed. Required recovery entries stay directly available.
- Runtime scans stop unselected known app processes for the protected UID.
  Flatpaks use mount metadata; Snaps use kernel labels/mounted paths. Native Steam
  includes its known client/webhelpers and exact launcher-script operand.
- System Settings and its `kcmshell5/6` GUI hosts follow the main Settings approval
  even when individual settings launchers are hidden. Session services are preserved.
- KDE Kiosk restricts KRunner, user launcher resources, and shell access.
  Approving an app that needs `shell_access` omits the global shell restriction.
  Sentinel does not deny `run_desktop_files`, which would block approved apps too.
  Inventory refreshes every two seconds; launcher/Kiosk reconciliation every 30.

This targets one desktop account. Machine-wide launcher/Kiosk changes and
process polling are not a complete default-deny OS sandbox. Unknown/user-writable
native code, scripts, interpreters, TTYs, service routes, and commands run inside
approved apps are not universally confined. Approved terminals/editors can be
powerful. Runtime rejection can occur after an app briefly starts.

## Files and API

Selections: `/etc/sentinel/guardian-apps.json`, protected and atomically written.
Native approvals retain executable identity; Flatpak uses app ID, Snap uses
instance name. User-source launcher arguments are fingerprinted, so editing
them requires fresh approval. Invalid/symlinked/user-writable state fails closed.

Overrides: `/usr/local/share/applications`, tracked in
`/var/lib/sentinel/guardian-desktop/launchers`. Kiosk: a reversible marked block
in `/etc/xdg/kdeglobals`. Only Sentinel-generated files/blocks are removed;
conflicting administrator overrides remain and report pending errors.
Disabling removes restrictions but retains selections.

RPCs: `guardian.apps.list`, `.policy`, `.authenticate`, `.approve`, `.revoke`,
`.enable`, `.replace`, `.launch`. Authentication grants no reusable token.
`replace` validates all desktop IDs before saving; `launch` returns authorized
argv for the unprivileged gate rather than executing apps as root.

## Installation and outage recovery

Update the protected wheel first; see
[development runtime](../../README.md#development-runtime). Before enabling,
install the management launcher and independent recovery socket:

```bash
sudo bash tools/install_guardian_app_access.sh
```

`sentinel-guardian-recovery.socket` starts a separate root helper only on demand.
It can **disable app selection only**, using the existing Guardian PIN and
verified protected UID during a daemon outage. It cannot change lock mode,
create credentials, edit selections, enable restrictions, or weaken other filters.
Five wrong PINs cause a persisted five-minute cooldown. It shares the daemon's
private instance lock, preventing races with a running backend or startup.

Ordinary gated launchers deny access during an outage; the recovery window
remains reachable. After recovery, sign out/back in to reload KDE. The Portal is also a reserved management exception. Independent outage recovery
remains in the standalone helper. See [diagnostics](../../docs/guardian-app-lockout-diagnostics.md).

## Validation

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest sentinel/daemon/tests -q
```

Tests isolate launcher/Kiosk/state changes and process scans. Verify real KDE menu,
Settings revocation, Snap/Steam discovery, approval, and outage recovery on the
target installation before shipping.
