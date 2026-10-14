"""Check Qt package selection in fresh processes without changing pytest's Qt."""
from pathlib import Path
import subprocess
import sys

from PyQt6.QtCore import QLibraryInfo
import PyQt6

ROOT = Path(__file__).parents[3]
SELECTOR = ROOT / 'sentinel/image_blocking/screen_blocker/system_qt.py'


def run_selector(package_root, code=''):
    script = f'''
import runpy
from pathlib import Path
select = runpy.run_path({str(SELECTOR)!r})['use_system_pyqt']
select(Path({str(package_root)!r}))
{code}
'''
    return subprocess.run([sys.executable, '-I', '-c', script],
                          capture_output=True, text=True, timeout=10)


def test_complete_qt_package_loads_from_selected_runtime():
    # The CI host need not have Ubuntu's bindings; exercise the real binding
    # loader against its installed Qt in an isolated child process.
    root = Path(PyQt6.__path__[0]).parent
    result = run_selector(root, '''
from PyQt6.QtCore import QLibraryInfo
print(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
''')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)


def test_missing_system_bindings_never_fall_back_to_pip_qt(tmp_path):
    result = run_selector(tmp_path)
    assert result.returncode != 0
    assert 'System PyQt6 is unavailable' in result.stderr
    assert 'sudo apt install python3-pyqt6 python3-pyqt6.qtquick' in result.stderr


def test_incomplete_system_bindings_do_not_mix_with_virtualenv(tmp_path):
    package = tmp_path / 'PyQt6'
    package.mkdir()
    # Even if __init__ tries to append pip's package, selection pins the path.
    (package / '__init__.py').write_text(f'__path__.append({PyQt6.__path__[0]!r})\n')
    (package / 'QtCore.py').write_text('')
    result = run_selector(tmp_path)
    assert result.returncode != 0
    assert 'System Qt bindings are incomplete' in result.stderr
    assert 'PyQt6.QtGui' in result.stderr


def test_selection_supports_namespace_system_packages(tmp_path):
    package = tmp_path / 'PyQt6'
    package.mkdir()
    for name in ('QtCore', 'QtGui', 'QtQml', 'QtQuick', 'QtWidgets'):
        (package / f'{name}.py').write_text('')
    result = run_selector(tmp_path, 'import PyQt6; print(PyQt6.__path__[0])')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(package)


def test_selection_refuses_an_already_loaded_qt_runtime():
    script = f'''
import PyQt6.QtCore
import runpy
runpy.run_path({str(SELECTOR)!r})['use_system_pyqt']()
'''
    result = subprocess.run([sys.executable, '-I', '-c', script],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert 'before importing PyQt6' in result.stderr


def run_overlay(package_root):
    script = f'''
import sys, runpy
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from sentinel.image_blocking.screen_blocker import system_qt
select = system_qt.use_system_pyqt
system_qt.use_system_pyqt = lambda: select(Path({str(package_root)!r}))
sys.argv = ['screen_time_blocker', '--qt-info']
runpy.run_module('sentinel.image_blocking.screen_blocker.screen_time_blocker', run_name='__main__')
'''
    return subprocess.run([sys.executable, '-I', '-c', script],
                          capture_output=True, text=True, timeout=10)


def test_overlay_entry_point_reports_missing_runtime_without_native_crash(tmp_path):
    result = run_overlay(tmp_path)
    assert result.returncode == 1
    assert 'Sentinel screen time: System PyQt6 is unavailable' in result.stderr
    assert 'Traceback' not in result.stderr


def test_qt_diagnostics_load_selected_runtime_without_gui_or_daemon():
    result = run_overlay(Path(PyQt6.__path__[0]).parent)
    assert result.returncode == 0, result.stderr
    assert 'Screen-time Qt:' in result.stdout
    assert QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath) in result.stdout
