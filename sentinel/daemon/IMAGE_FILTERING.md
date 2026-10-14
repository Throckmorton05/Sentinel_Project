# Image Filtering lifecycle

The registered boolean setting `image_filtering` controls the screen monitor
and all four installed model API services:

| Service | Model | Port |
| --- | --- | --- |
| sentinel-opennsfw.service | OpenNSFW2 | 8000 |
| sentinel-drawn.service | Drawn/photo classifier | 8001 |
| sentinel-hentai.service | Hentai classifier | 8002 |
| sentinel-nudenet.service | NudeNet | 8003 |

The Portal generates the blue Image Filtering switch from the daemon schema.
It appears first on the Protection settings page. No separate GUI preference
or privileged GUI process is involved.

## Behavior

- **Enabled:** sentineld starts missing/inactive model services. The monitor
  starts in the protected user's Wayland session once KWin and every model's
  health endpoint are ready. Models can take time to load.
- **Disabled:** sentineld disables supervision before stopping the monitor,
  its launch parent, and registered anti-tamper blocker; it then stops all
  active model services. Intentional shutdown clears failure counters and does
  not trigger a tamper penalty. Disabled monitor registration/heartbeats are
  rejected. SIGTERM now runs monitor cleanup for blocker children and overlays.
- **Persistence:** the choice is stored in `/etc/sentinel/settings.toml` and is
  reconciled at daemon startup and every five seconds. Missing keys default to
  true to preserve the existing always-on behavior.
- **Failures:** the requested setting stays saved, with pending runtime status
  and errors returned to the client. Reconciliation retries startup/shutdown.
  A saved enabled setting is not a claim that models have loaded successfully.
- **Lock policy:** false→true strengthens protection; true→false weakens it.
  Self-Lock cannot disable an enabled filter. Guardian Lock needs its PIN for
  any change, including no-op retry requests.

The lifecycle lock prevents a watchdog launch from racing a disable request.
Disabling after a daemon restart also discovers monitors from the protected
UID using the expected interpreter and exact Python module operand. Initial
startup retains the existing monitor-adoption grace to avoid duplicate launches.

The APIs run as the `sentinel` system account. The monitor runs as the protected
KDE user; root does not run its GUI. Fixed systemd unit names are used with
bounded subprocess timeouts, without a shell or client-provided commands.

## Anti-tamper screen

Repeated termination of the screen monitor can trigger a separate full-desktop
block while Sentinel restores protected monitoring services. Intentional
shutdown through an authorized image-filtering setting change does not trigger
this penalty.

![Anti-tamper block showing protection restoration and a countdown](../../docs/images/blocking/anti-tamper.png)

This is separate from the per-window Content Blocked page and its rechecking
privacy overlay; see [the screen blocker screenshots](../image_blocking/screen_blocker/README.md#screenshots).

## API / CLI

- `settings.get` / `settings.set`, name `image_filtering`.
- `settings.schema` and `settings.snapshot` include it automatically.
- `imagefilter.status` is a pin-free local read returning saved intent,
  four service states, cached model health, and live heartbeat/waiting status.
- `client.get_image_filtering_status()` wraps the status call.
- Settings writes use a 60-second client timeout for this setting.

Existing `sen` commands work without new subcommands:

```bash
sen setting get image_filtering
sen setting set image_filtering false
sen setting set image_filtering true
```

In a source environment, the equivalent is:

```bash
.venv/bin/python -m sen.cli.sen setting set image_filtering false
```

## Apply and install

Close the Portal and stop the previous daemon first (Ctrl+C in its development
terminal, or `sudo systemctl stop sentineld.service`). Stop any manually
launched model API processes before switching to the managed services. The
obsolete manual launch script has been removed; the daemon owns this lifecycle.

```bash
cd ~/Sentinel_Project
git apply --check ~/Downloads/sentinel-image-filtering-toggle.patch
git apply ~/Downloads/sentinel-image-filtering-toggle.patch
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest sentinel/daemon/tests sentinel/portal/tests sentinel/appstore/tests -q
poetry build -f wheel
sudo /usr/lib/sentinel/venv/bin/python -m pip install --no-deps --force-reinstall dist/sen-0.1.0-py3-none-any.whl
sudo bash tools/install_image_filter_services.sh
```

The installer installs the existing API service units and the updated sentineld
unit, creates a system API account/cache directory if needed, and disables the
APIs' independent boot enablement. It does not change the saved setting or start
or stop existing services. The sentineld unit no longer unconditionally Wants
the APIs at boot. Installed model files and API Python dependencies are required;
startup failures appear as pending status and in the service journals.

Restart your development daemon:

```bash
sudo /opt/AdGuardHome/AdGuardHome -s start
cd ~/Sentinel_Project
sudo env SENTINEL_DEV_ALLOW_FIREFOX=1 \
  /usr/lib/sentinel/venv/bin/python -m sentinel.daemon.server
```

In another terminal:

```bash
/usr/lib/sentinel/venv/bin/python -m sentinel.portal.main
```

For a service deployment, use `sudo systemctl restart sentineld.service`.

## Native verification

After switching Image Filtering off, these four units should be inactive:

```bash
systemctl is-active sentinel-opennsfw.service sentinel-drawn.service sentinel-hentai.service sentinel-nudenet.service
```

Read the runtime status without credentials:

```bash
cd ~/Sentinel_Project
.venv/bin/python - <<'PY'
from pprint import pprint
from sentinel.daemon.client import get_image_filtering_status
pprint(get_image_filtering_status())
PY
```

After switching it on, the four units should become active; the monitor becomes
healthy once the models and KDE session are ready. If the managed units are
stopped but a development API still listens on a model port, the daemon reports
pending shutdown and asks you to close that manually started process. It does
not kill unrelated programs merely because they use a model port.

Already-minimized applications can be restored normally after disabling.
Sensitivity/model thresholds and scan-interval controls remain outside this patch.
