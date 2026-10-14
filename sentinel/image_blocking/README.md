# Sentinel image filtering

Local AI services analyze application-window captures on **KDE Plasma / Wayland
with KWin**. The monitor runs as the protected desktop user; the root backend
supervises it and starts/stops four model services running as the `sentinel`
system account. This is periodic detection, not guaranteed prevention of every
frame of explicit content.

## Enable and inspect

For implementation details, read the [model API guide](screen_filter/README.md),
[screen monitor guide](screen_monitor/README.md), and
[daemon architecture](../daemon/README.md). The model guide covers preprocessing,
response contracts, and decision rules; the monitor guide covers capture, hash
caching, heartbeat, shutdown, and current retry limitations.

```bash
sen setting get image_filtering
sen setting set image_filtering true
sen setting set image_filtering false
```

The Portal exposes the same Image Filtering switch. Missing settings default
to on. Enabling starts model services, then the monitor once model health and
the KDE session are ready. Disabling stops the monitor, blocker children,
registered anti-tamper UI, and model services without a tamper penalty.
Self Lock denies disabling; Guardian Lock requires the PIN for changes.
Saved intent and actual runtime readiness are separate; failures remain pending
and reconciliation retries. See [lifecycle](../daemon/IMAGE_FILTERING.md).

## Services and model files

| Service | Port | Purpose / installed model |
| --- | --- | --- |
| `sentinel-opennsfw.service` | 8000 | OpenNSFW2 gate; `/usr/share/sentinel/models/openNSFW2_ONNX_Model/open_nsfw.onnx` |
| `sentinel-drawn.service` | 8001 | Drawn/photo routing; `/usr/share/sentinel/models/anime_real_cls/mobilenetv3_v1.4_dist/model.onnx` |
| `sentinel-hentai.service` | 8002 | Illustrated content; `/usr/share/sentinel/models/hentai-vit/model.onnx` |
| `sentinel-nudenet.service` | 8003 | Photographic anatomical detection; NudeNet's model/cache |

Services bind to `127.0.0.1`, expose `GET /health` and image-analysis endpoints,
and use the protected runtime. The ONNX classifiers use CPUExecutionProvider;
a GPU is not required by these service definitions. The Hentai API reproduces
ViT preprocessing with Pillow/NumPy and needs no PyTorch/Transformers at runtime.

`models/download_models.py` is a placeholder, **not a working downloader**.
Large models are excluded from the wheel; packaging must install them separately.
The Hentai model is absent from ordinary source ZIPs; see its
[model staging guide](../../packaging/models/hentai-vit/README.md).
Model attribution/redistribution terms are in
[THIRD_PARTY.md](../../THIRD_PARTY.md), `licenses/`, and model metadata.

## Detection pipeline

1. KWin snapshots identify windows by UUID. The monitor excludes minimized,
   other-desktop, special/Sentinel, and undersized windows (below 300 × 200 at capture).
2. `ScreenShot2` captures each window; a BLAKE3 hash of a 256 × 256 RGB resize
   skips unchanged captures. The scan interval is two seconds plus work time.
3. OpenNSFW2 evaluates overlapping tiles, skipping the top grid row. Its current
   0.85 gate routes suspicious captures onward; the gate alone does not block.
4. The drawn/photo classifier selects Hentai or NudeNet. Their model-specific
   thresholds and tile/support rules produce the final allow/block decision.
5. A detected window launches its blocker and sends bounded event metadata to
   the backend Activity store.

Thresholds are constants in `screen_filter/APIs/`; they are not registered
Portal/CLI sensitivity settings. Classification, tile coverage, polling, and
capture failures limit detection coverage.

## Blocking and rechecks

The monitor owns `org.sentinel.KWinBridge`; blocker processes are D-Bus clients.
KWin quarantines the exact UUID by minimizing it and hiding it from taskbar,
switcher, and pager while retaining the original flags. The app stays running.

The block page offers a 30-second automatic recheck, Recheck Now, and Close.
Rechecks cover every display before restoring the target underneath for capture
and analysis. Safe content is released after KWin acknowledgement. Unsafe content
or analysis/capture failure returns to quarantine; overlays remain until the
minimize acknowledgement. Closing waits for the target to disappear.
See [Wayland](../wayland/README.md) and [blocker lifecycle](screen_blocker/README.md).

## Logging and other desktop blockers

Screen-block metadata goes through the authenticated monitor RPC into
`/var/lib/sentinel/activity/events.sqlite3`, with seven-day retention. No
screenshots are stored there. The old `/var/log/sentinel/blocked.jsonl` writer
is no longer active; existing legacy files are not migrated or deleted.
See [Activity](../activity/README.md) for bounds, queues, and coverage.

Anti-tamper and scheduled/daily screen-time overlays are separate backend-owned
functions sharing this UI directory. The Activity usage worker runs separately
and continues even when image filtering is off.

## Installation and verification

On an existing configured installation, update the protected wheel, install
model files and dependencies, and install service definitions:

```bash
sudo bash tools/install_image_filter_services.sh
systemctl is-active sentinel-opennsfw.service sentinel-drawn.service \
  sentinel-hentai.service sentinel-nudenet.service
```

The installer requires the runtime and control group. It disables independent
API boot enablement; `sentineld` controls lifecycle from the saved setting.
It does not supply models or bootstrap Sentinel. Stop old manual API processes
before using managed units, then restart the existing backend. See
[development runtime](../../README.md#development-runtime).

Check pending runtime health with `client.get_image_filtering_status()` and
service journals. Test capture, quarantine, multi-monitor rechecks, close,
intentional disable, and crash recovery on the target KDE session.
