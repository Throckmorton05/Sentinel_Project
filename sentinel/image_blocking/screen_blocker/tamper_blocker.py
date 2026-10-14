from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __name__ == "__main__":
    # The KDE layer-shell plugin must use the distribution's matching Qt.
    from sentinel.image_blocking.screen_blocker.system_qt import use_system_pyqt
    try:
        use_system_pyqt()
    except RuntimeError as exc:
        print(f"Sentinel tamper blocker: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtQml import QQmlApplicationEngine

from sentinel.daemon.client import (
    SentineldError,
    register_tamper_blocker,
)
from sentinel.settings.paths import SENTINEL_LOGO


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--seconds",
        type=int,
        required=True,
    )

    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    if args.seconds <= 0:
        return 0

    try:
        register_tamper_blocker()

    except SentineldError as exc:
        print(
            f"Sentinel tamper blocker: {exc}",
            file=sys.stderr,
        )

        return 1

    app = QGuiApplication(sys.argv)

    app.setOverrideCursor(Qt.CursorShape.BlankCursor)

    engine = QQmlApplicationEngine()

    #
    # Allow Sentinel's virtualenv Qt runtime to find
    # Kubuntu's system KDE QML modules, including
    # org.kde.layershell.
    #
    system_qml_path = Path("/usr/lib/x86_64-linux-gnu/qt6/qml")

    if system_qml_path.is_dir():
        engine.addImportPath(str(system_qml_path))

    context = engine.rootContext()

    if context is None:
        print(
            "Sentinel tamper blocker: failed to get QML root context.",
            file=sys.stderr,
        )

        return 1

    #
    # Pass the cooldown duration to QML.
    #
    context.setContextProperty(
        "tamperDurationSeconds",
        int(args.seconds),
    )

    #
    # Pass Sentinel branding to QML.
    #
    context.setContextProperty(
        "sentinelLogo",
        QUrl.fromLocalFile(str(SENTINEL_LOGO)),
    )

    print(
        f"Sentinel tamper blocker: detected {len(app.screens())} display(s).",
        flush=True,
    )

    qml_path = Path(__file__).with_name("tamper_blocker.qml")

    engine.load(QUrl.fromLocalFile(str(qml_path)))

    if not engine.rootObjects():
        print(
            "Sentinel tamper blocker: failed to load QML.",
            file=sys.stderr,
        )

        return 1

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
