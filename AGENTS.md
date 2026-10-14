# Working on Sentinel

Read [LLM_README.md](LLM_README.md) for the project map, then the README nearest
the component you are changing. Read the actual implementation and its tests
before changing behavior; documentation is a navigation aid, not a substitute
for the current code.

## Development

- Use the Python version and dependency constraints in `pyproject.toml` and
  `poetry.lock`. The supported range is Python 3.14, excluding 3.14.1.
- Run focused tests for the changed component. GUI tests normally use
  `QT_QPA_PLATFORM=offscreen`; see the test commands in `LLM_README.md`.
- A checkout and the installed `/usr/lib/sentinel/venv` runtime are separate.
  Editing source does not update the installed backend or model services.
- Model API modules load large models at import time. Inspect source without
  importing those modules unless the required models and dependencies exist.
- Mocked tests cannot verify a real KDE/Wayland session, model accuracy,
  AppArmor enforcement, DNS routing, or an installed ISO. Report that distinction.

## Boundaries to preserve

- Keep privileged operations in `sentinel/daemon/`. Portal, App Store, and CLI
  clients request operations through the daemon client instead of running root.
- Enforce lock policy and Guardian authentication in the backend. Never trust a
  client-supplied authenticated flag, UID, device permission, or shell command.
- Preserve bounded inputs, fixed executable/unit names, subprocess timeouts,
  and explicit argument lists around privileged operations.
- Keep Qt widget changes on the GUI thread; follow existing RPC worker patterns
  for slow operations.
- Preserve intentional image-filter shutdown semantics and exact window UUID
  ownership. The monitor owns the KWin bridge; blockers use it as clients.
- Keep configuration, protected state, executable runtime, and enforcement
  assets protected from desktop-user writes. Updates must preserve user settings.
- Do not commit private keys, PINs, tokens, personal activity, or model weights
  as ordinary source assets. Follow the model staging and signed-release guides.

## Documentation

When interfaces, defaults, thresholds, runtime paths, or lifecycle behavior change,
update the relevant component guide in the same change. Keep this file short and
link detailed explanations from `LLM_README.md` or the component README.
