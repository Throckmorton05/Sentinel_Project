# Sentinel project map

This is a starting point for developers and LLM-assisted work. Read it alongside
[AGENTS.md](AGENTS.md), then follow the component links for the task at hand.
It describes the current repository; it is not a promise that every protection
or distribution integration has been verified on a deployed machine.

## What Sentinel is

Sentinel is a Linux desktop protection project with a Typer CLI (`sen`), a Qt
Portal, a Qt App Store, and a privileged Python backend (`sentineld`). The image
monitoring and window-blocking path targets KDE Plasma / Wayland with KWin.
Chrome policies, DNS filtering, application controls, schedules, local image
inference, and Guardian/Self Lock policy form separate cooperating components.
The [main README](README.md) describes user-facing features and screenshots.

## Processes and authority

The CLI and GUI clients run as the desktop user. They call
[`sentinel/daemon/client.py`](sentinel/daemon/client.py), which talks to the
root daemon over `/run/sentinel/sentineld.sock`. The daemon authenticates local
Unix peers and authorizes each operation against the current lock policy.
Membership in the `sentinel` group permits local control access; it does not
replace Guardian authentication or make every mutation permissible.

The daemon is configured for one protected desktop account through root-owned
`/etc/sentinel/daemon.toml`. It supervises desktop helpers in that user's
session. Image model APIs run as the `sentinel` system account on loopback ports
8000–8003. The screen monitor runs as the protected desktop user and owns the
KWin bridge; per-window blockers use that bridge and do not become root GUIs.

Read [daemon architecture](sentinel/daemon/README.md) before changing RPC,
authorization, persistence, or process supervision.

## Find the right code

| Task | Start here | Detailed guide |
| --- | --- | --- |
| CLI commands and installed entry points | `sen/cli/sen.py`, `pyproject.toml` | [CLI](sen/README.md) |
| Portal settings, schedules, authentication | `sentinel/portal/` | [Portal](sentinel/portal/README.md) |
| Approved software and package operations | `sentinel/appstore/`, `sentinel/daemon/operations/package_ops.py` | [App Store](sentinel/appstore/README.md) |
| Privileged RPC, locks, Guardian PIN | `sentinel/daemon/rpc/`, `security/`, `core/` | [Daemon](sentinel/daemon/README.md) |
| Settings schema and filesystem paths | `sentinel/settings/`, `sentinel/daemon/operations/settings_ops.py` | [Daemon settings](sentinel/daemon/README.md#configuration-and-state) |
| Image classification and preprocessing | `sentinel/image_blocking/screen_filter/APIs/` | [Model APIs](sentinel/image_blocking/screen_filter/README.md) |
| Capture, scan loop, hashes, heartbeat | `sentinel/image_blocking/screen_monitor/` | [Screen monitor](sentinel/image_blocking/screen_monitor/README.md) |
| KWin quarantine, overlays, rechecks | `sentinel/wayland/`, `sentinel/image_blocking/screen_blocker/` | [Wayland](sentinel/wayland/README.md), [blocker](sentinel/image_blocking/screen_blocker/README.md) |
| Image service lifecycle and tamper recovery | `sentinel/daemon/operations/image_filter_ops.py`, `supervision/screen_monitor.py` | [Lifecycle](sentinel/daemon/IMAGE_FILTERING.md) |
| DNS, Chrome baseline, domain policy | `sentinel/dns/`, `sentinel/browser_policies/` | [DNS](sentinel/dns/README.md), [browser policies](sentinel/browser_policies/README.md) |
| Guardian app and website access | `sentinel/guardian_apps/`, daemon operations | [Apps](sentinel/guardian_apps/README.md), [websites](docs/guardian-website-access.md) |
| Schedules, daily limits, desktop blocks | daemon `operations/` and `supervision/screen_time.py` | [Block windows](docs/block_windows.md) |
| Activity storage and usage collection | `sentinel/activity/` | [Activity](sentinel/activity/README.md) |
| Extensions and site-specific protection | `sentinel/extension_catalog/`, `safe_steam/`, `safe_crunchyroll/` | [Catalog](sentinel/extension_catalog/README.md), [Steam](sentinel/safe_steam/README.md), [Crunchyroll](sentinel/safe_crunchyroll/README.md) |
| Debian packages, updates, release signing | `debian/`, `packaging/`, daemon `operations/system_ops.py` | [Debian packaging](docs/debian-packaging.md), [GitHub updates](docs/github-updates.md) |
| ISO branding, provisioning, migration | `packaging/iso/`, `assets/`, `configs/` | [Onboarding](docs/iso-onboarding.md), [updates and branding](docs/update-controls-and-branding.md) |
| Future paired-device integration | `sentinel/daemon/security/caller_context.py` | [Companion API](docs/companion-api.md) |

Paths in the table's “Start here” column are relative to the repository; shortened
paths continue under the preceding component. Companion caller permissions are
an internal foundation, not a completed remote authentication or pairing service.

## Policy and runtime invariants

- `unlocked` allows ordinary changes; `self_lock` allows maintenance and
  strengthening while denying weakening; `guardian_lock` requires backend
  Guardian authentication for mutations. Some administrative actions have
  additional restrictions. Follow each handler, not just this broad rule.
- Changing a persisted setting and making the runtime healthy are separate
  steps. Read pending status and errors instead of treating “enabled” as proof
  of working protection.
- Image filtering is periodic detection. Capture exclusions, polling, models,
  and failures limit coverage. An OpenNSFW2 positive result only routes onward;
  Hentai or NudeNet makes the final decision.
- Intentional authorized shutdown must not count as tampering. Runtime ownership
  checks and exact UUIDs prevent unrelated processes/windows being targeted.
- DNS configuration alone does not route OS traffic through AdGuard. Installed
  enforcement and firmware/session escape routes require distribution validation.
- `sen update-sen` upgrades installed Sentinel Debian packages through the
  configured authenticated APT channel. `sen update-sys` performs the normal
  system upgrade. Follow [update documentation](docs/sentinel-updates.md).

## Development and verification

Use `pyproject.toml` and `poetry.lock` for supported dependencies. The Python
range is `>=3.14,!=3.14.1,<3.15`. Follow the
[development-to-ISO guide](docs/development-to-iso.md) for first setup and the
connected package/platform/Cubic workflow, or the
[development runtime instructions](README.md#development-runtime) for the
existing protected installation; installing a wheel with `--no-deps` presumes
those dependencies already exist. Models and enforcement assets are separate
from the wheel. Do not overwrite live settings with sample configurations.

Run only the relevant suites initially, from the repository environment:

```bash
# Backend authorization and image lifecycle
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  sentinel/daemon/tests/test_policy.py \
  sentinel/daemon/tests/test_admin_policy.py \
  sentinel/daemon/tests/test_image_filtering.py -q

# GUI changes: select the relevant component
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest sentinel/portal/tests -q
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest sentinel/appstore/tests -q

# ISO tooling
.venv/bin/python -m unittest discover -s packaging/iso/tests
```

The repository has more backend and component tests; choose them by the affected
operation. Headless tests do not demonstrate real model accuracy, KWin capture,
multi-monitor privacy, DNS routing, or boot behavior. Report native checks and
skips separately. The model API `tests/` directory currently has no actual test
cases; do not describe running that directory as classifier validation.

Before a release, follow the signed-channel and ISO guides rather than assuming
GitHub access, model redistribution, or native installation has been verified.
Keep task-specific details in component READMEs so this map stays easy to read.
