"""RPC credentials, desktop lifecycle, and interactive overlay refresh behavior."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
import re
import time

import pytest
from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtQml import QQmlApplicationEngine
from PyQt6.QtQuick import QQuickWindow
from sentinel.daemon import client
from sentinel.daemon.core.lock_state import LockState, LockMode
from sentinel.daemon.rpc import request_handler
from sentinel.daemon.security.caller_context import CallerContext
from sentinel.daemon.supervision import screen_time
from sentinel.daemon.core import session
from sentinel.daemon.security import tamper_protection
from sentinel.image_blocking.screen_blocker.screen_time_blocker import Controller, configure_qml_imports


def rpc(action, params=None, caller=None):
    return request_handler._handle_request({'request_id': 'screen-time', 'action': action, 'params': params or {}},
                                          uid=1000, pid=1234, caller=caller)


def test_rpc_passes_new_schedule_fields_and_filters_internal_state():
    with patch.object(request_handler.block_window_ops, 'create_block_window', return_value={}) as create:
        assert rpc('blockwindow.create', {'block_type': 'screen_time', 'recurrence': 'weekly',
                                         'screen_state': {'grace_until': 9999999999}})['ok']
    assert create.call_args.kwargs['block_type'] == 'screen_time'
    assert create.call_args.kwargs['recurrence'] == 'weekly'
    assert 'screen_state' not in create.call_args.kwargs


@pytest.mark.parametrize('pin,valid', [(None, False), ('wrong', False), ('4826', True)])
def test_rpc_bypass_verifies_pin_and_ignores_claimed_authentication(pin, valid):
    with (patch.object(request_handler, 'verify_guardian_pin', return_value=valid) as verify,
          patch.object(request_handler.block_window_ops, 'bypass_screen_time', return_value={}) as bypass):
        assert rpc('screenblock.bypass', {'id': 'window', 'guardian_pin': pin,
                                          'guardian_authenticated': True})['ok']
    assert bypass.call_args.kwargs['guardian_authenticated'] is valid
    assert verify.called is (pin is not None)


def test_status_available_during_administrative_work_but_device_registration_denied():
    with request_handler._mutation_lock, patch.object(request_handler.block_window_ops, 'get_screen_time_status', return_value={}):
        assert rpc('screenblock.status')['ok']
    caller = CallerContext('device', 'guardian', frozenset({'block_windows.write'}))
    assert not rpc('screenblock.register', caller=caller)['ok']
    caller = CallerContext('device', 'readonly', frozenset({'block_windows.read'}))
    assert not rpc('screenblock.bypass', {'id': 'window'}, caller=caller)['ok']


@pytest.fixture
def supervision():
    with (patch.object(screen_time, '_uid', 1000), patch.object(screen_time, '_pid', None),
          patch.object(screen_time, '_launcher', None), patch.object(screen_time, '_retry_at', 0),
          patch.object(screen_time, '_matches', return_value=False) as matches,
          patch.object(screen_time, 'get_screen_time_status', return_value={'blocking': True}) as status,
          patch.object(screen_time, '_user_session_command', return_value=['user-python']) as command,
          patch.object(session, '_lock_graphical_session') as lock_session,
          patch.object(session.subprocess, 'run') as run,
          patch.object(tamper_protection, '_terminate_rofi_processes') as rofi,
          patch.object(tamper_protection, '_kwin_load_effect') as load,
          patch.object(tamper_protection, '_kwin_unload_effect') as unload,
          patch.object(screen_time.subprocess, 'Popen') as launch,
          patch.object(screen_time.os, 'kill') as kill, patch.object(screen_time.os, 'killpg') as killpg,
          patch.object(screen_time.time, 'monotonic', return_value=100) as clock):
        yield SimpleNamespace(matches=matches, status=status, command=command, launch=launch,
                              kill=kill, killpg=killpg, clock=clock, lock_session=lock_session)
        lock_session.assert_not_called()
        run.assert_not_called()
        rofi.assert_not_called()
        load.assert_not_called()
        unload.assert_not_called()


def test_overlay_starts_without_monitor_and_restarts_after_termination(supervision):
    s = supervision
    s.launch.return_value.poll.return_value = 0
    screen_time.tick()
    assert s.launch.call_count == 1
    assert s.command.call_args.kwargs['wayland']
    screen_time.tick()
    assert s.launch.call_count == 1
    s.clock.return_value = 106
    screen_time.tick()
    assert s.launch.call_count == 2
    s.lock_session.assert_not_called()
    assert s.command.call_args.args[2:5] == ('-I', '-m', screen_time.MODULE)
    assert s.launch.call_args.kwargs['cwd'] == '/'


def test_registered_process_stays_alive_and_release_cleans_up(supervision):
    s = supervision
    s.matches.side_effect = lambda pid: pid == 321
    screen_time.register_overlay(1000, 321)
    screen_time.tick()
    s.launch.assert_not_called()
    s.status.return_value = {'blocking': False}
    screen_time.tick()
    assert s.kill.call_args.args[0] == 321
    assert screen_time._pid is None


def test_killed_registered_overlay_retries_without_locking_session(supervision):
    s = supervision
    screen_time._pid = 321
    screen_time._retry_at = 105
    screen_time.tick()
    s.lock_session.assert_not_called()
    s.launch.assert_called_once()


def test_registration_and_pid_reuse_cannot_target_other_processes(supervision):
    s = supervision
    with pytest.raises(ValueError):
        screen_time.register_overlay(1001, 321)
    with pytest.raises(ValueError):
        screen_time.register_overlay(1000, 321)
    screen_time._pid = 321
    screen_time.stop()
    s.kill.assert_not_called()


def test_launch_failure_keeps_session_available_and_retries(supervision):
    s = supervision
    s.command.side_effect = RuntimeError('Session not ready')
    screen_time.tick()
    s.lock_session.assert_not_called()
    s.clock.return_value = 106
    screen_time.tick()
    assert s.command.call_count == 2


def test_overlay_poll_does_not_reset_pin_model_and_errors_do_not_release():
    app = QApplication.instance() or QApplication([])
    controller = Controller(app)
    controller.timer.stop()
    windows = [{'id': 'one', 'name': 'Bedtime', 'blocking': True, 'mode': 'guardian_lock'}]
    changed = QSignalSpy(controller.windowsChanged)
    released = QSignalSpy(controller.released)
    controller.received({'blocking': True, 'windows': windows})
    assert len(changed) == 1
    controller.runner.busy_changed.emit(True)
    controller.runner.busy_changed.emit(False)
    controller.received({'blocking': True, 'windows': windows})
    assert len(changed) == 1
    controller.failed('Wrong PIN or daemon unavailable')
    assert len(released) == 0
    assert not controller.allowedToClose
    controller.received({'blocking': False, 'windows': []})
    assert len(released) == 1
    assert controller.allowedToClose
    controller.deleteLater()


def test_qml_renders_pin_entry_keeps_typed_pin_and_closes_only_after_release():
    """Exercise the actual QML content; layer-shell requires a live KDE session."""
    app = QApplication.instance() or QApplication([])
    controller = Controller(app)
    controller.timer.stop()
    status = {'blocking': True, 'windows': [dict(id='qml', name='Bedtime', mode='guardian_lock',
        blocking=True, end_at=time.time()+3600, extension_available=False)]}
    controller.received(status)
    source_path = Path(__file__).parents[2] / 'image_blocking/screen_blocker/screen_time_blocker.qml'
    source = source_path.read_text().replace('import org.kde.layershell 1.0 as LayerShell\n', '')
    source = re.sub(r'^            LayerShell\.Window\.[^\n]+\n(?:                \|[^\n]+\n)?', '', source, flags=re.M)
    source = source.replace('screen: modelData', 'screen: modelData\n            width: 1000; height: 800')
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty('screenTime', controller)
    logo = Path(__file__).parents[2] / 'portal/assets/Sentinel_Shield.svg'
    engine.rootContext().setContextProperty('sentinelLogo', QUrl.fromLocalFile(str(logo)))
    engine.loadData(source.encode())
    assert engine.rootObjects()
    app.processEvents()

    rendered = [window for window in app.allWindows() if window.title() == 'Sentinel Screen Time Block']
    assert rendered
    window = rendered[0]
    def find_pin(item):
        if item.objectName() == 'guardianPin-qml':
            return item
        for child in item.childItems():
            result = find_pin(child)
            if result is not None:
                return result
    field = find_pin(window.contentItem())
    assert field is not None and field.property('height') == 44
    field.forceActiveFocus()
    for key in (Qt.Key.Key_4, Qt.Key.Key_8, Qt.Key.Key_2, Qt.Key.Key_6):
        QTest.keyClick(window, key)
    assert field.property('text') == '4826'
    controller.runner.busy_changed.emit(True)
    controller.received(status)
    controller.runner.busy_changed.emit(False)
    app.processEvents()
    assert find_pin(window.contentItem()).property('text') == '4826'
    with patch.object(client, 'bypass_screen_time_block', return_value=status) as request:
        QTest.keyClick(window, Qt.Key.Key_Return)
        deadline = time.monotonic() + 3
        while controller.runner.busy and time.monotonic() < deadline:
            QTest.qWait(10)
        assert not controller.runner.busy
        request.assert_called_once_with('qml', guardian_pin='4826')
    assert field.property('text') == ''
    assert not window.close()
    controller.received({'blocking': False, 'windows': []})
    assert window.close()
    engine.deleteLater()
    controller.deleteLater()
    app.processEvents()


def test_system_qml_fallback_does_not_shadow_bundled_modules(tmp_path):
    app = QApplication.instance() or QApplication([])
    runtime = tmp_path / 'runtime'
    system = tmp_path / 'system'
    for root, origin in ((runtime, 'runtime'), (system, 'system')):
        marker = root / 'org/sentinel/SharedMarker'
        marker.mkdir(parents=True)
        (marker / 'qmldir').write_text('module org.sentinel.SharedMarker\nMarker 1.0 Marker.qml\n')
        (marker / 'Marker.qml').write_text(
            f'import QtQuick\nItem {{ property string origin: "{origin}" }}\n')
    fixed = QQmlApplicationEngine()
    fixed.addImportPath(str(runtime))
    original_paths = fixed.importPathList()
    configure_qml_imports(fixed, system)
    assert fixed.importPathList() == [*original_paths, str(system)]
    configure_qml_imports(fixed, system)
    assert fixed.importPathList().count(str(system)) == 1
    fixed.loadData(b'import org.sentinel.SharedMarker 1.0\nMarker {}')
    assert fixed.rootObjects() and fixed.rootObjects()[0].property('origin') == 'runtime'
    fixed.deleteLater()
    app.processEvents()


def test_kde_only_qml_modules_remain_available_via_fallback(tmp_path):
    app = QApplication.instance() or QApplication([])
    marker = tmp_path / 'org/sentinel/SystemMarker'
    marker.mkdir(parents=True)
    (marker / 'qmldir').write_text('module org.sentinel.SystemMarker\nMarker 1.0 Marker.qml\n')
    (marker / 'Marker.qml').write_text('import QtQuick\nItem { property bool fallbackLoaded: true }\n')
    engine = QQmlApplicationEngine()
    configure_qml_imports(engine, tmp_path)
    engine.loadData(b'import org.sentinel.SystemMarker 1.0\nMarker {}')
    assert engine.rootObjects() and engine.rootObjects()[0].property('fallbackLoaded')
    engine.deleteLater()
    app.processEvents()
