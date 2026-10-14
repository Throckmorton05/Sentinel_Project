"""Select Ubuntu's PyQt package before importing any native Qt extension.

KDE's layer-shell plugin uses QtWayland private interfaces. It must share the
distribution's Qt runtime, rather than the Qt libraries bundled in pip wheels.
Only the PyQt6 package is redirected; Sentinel and its other dependencies keep
using the protected virtualenv.
"""
from __future__ import annotations

import importlib
from importlib.machinery import PathFinder
from importlib.util import module_from_spec
from pathlib import Path
import sys

SYSTEM_PACKAGES = Path('/usr/lib/python3/dist-packages')
INSTALL_HINT = ('Install the system Qt bindings with: sudo apt install '
                'python3-pyqt6 python3-pyqt6.qtquick')


def use_system_pyqt(package_root=SYSTEM_PACKAGES):
    """Pin the complete PyQt6 package to one installation; never mix bindings."""
    if any(name == 'PyQt6' or name.startswith('PyQt6.') for name in sys.modules):
        raise RuntimeError('System Qt must be selected before importing PyQt6.')
    spec = PathFinder.find_spec('PyQt6', [str(package_root)])
    if spec is None or spec.submodule_search_locations is None:
        raise RuntimeError(f'System PyQt6 is unavailable. {INSTALL_HINT}')
    package = module_from_spec(spec)
    sys.modules['PyQt6'] = package
    if spec.loader is not None:
        spec.loader.exec_module(package)
    # Do not let namespace/package helpers extend this to the virtualenv copy.
    package.__path__ = [str(Path(package_root) / 'PyQt6')]
    try:
        for name in ('QtCore', 'QtGui', 'QtQml', 'QtQuick', 'QtWidgets'):
            importlib.import_module(f'PyQt6.{name}')
    except ImportError as exc:
        raise RuntimeError(f'System Qt bindings are incomplete: {exc}. {INSTALL_HINT}') from exc
