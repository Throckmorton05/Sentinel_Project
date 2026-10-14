"""Image-filter setting, fixed service lifecycle, and monitor stop races."""
from copy import deepcopy
import dataclasses
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock, patch
import subprocess
import time

import pytest
from sentinel.daemon.core.lock_state import LockMode, LockState
from sentinel.daemon.operations import image_filter_ops as images, settings_ops
from sentinel.daemon.rpc import request_handler
from sentinel.daemon.rpc.protocol import encode_message
from sentinel.daemon.security import policy
from sentinel.daemon.supervision import screen_monitor, readiness
from sentinel.daemon.supervision.state import ScreenMonitorWatchdogState, _monitor_state, _stop_event
from sentinel.daemon import server, client


@pytest.fixture
def monitor():
    old = {f.name: getattr(_monitor_state, f.name) for f in dataclasses.fields(_monitor_state)}
    fresh = ScreenMonitorWatchdogState(uid=1000)
    for field in dataclasses.fields(fresh):
        setattr(_monitor_state, field.name, getattr(fresh, field.name))
    with (patch.object(screen_monitor, '_terminate_screen_monitor') as terminate,
          patch.object(screen_monitor, '_terminate_monitor_child') as child,
          patch.object(screen_monitor, '_terminate_tamper_blocker') as blocker,
          patch.object(screen_monitor, '_discover_screen_monitors', return_value=[]) as discover):
        yield SimpleNamespace(terminate=terminate, child=child, blocker=blocker, discover=discover)
    for key, value in old.items():
        setattr(_monitor_state, key, value)


@pytest.fixture
def runtime(monitor):
    events = []
    states = {unit: {'Id': unit, 'LoadState': 'loaded', 'ActiveState': 'inactive'} for unit, _ in images.API_UNITS}
    health = {port: {'reachable': False, 'ready': False} for _, port in images.API_UNITS}
    def change(operation, units):
        events.append((operation, list(units)))
        for unit in units:
            states[unit]['ActiveState'] = 'active' if operation == 'start' else 'inactive'
    original = screen_monitor.set_screen_monitor_enabled
    def configure(enabled):
        events.append(('monitor', enabled))
        original(enabled)
    with (patch.object(images, '_service_states', side_effect=lambda: deepcopy(states)) as inspect,
          patch.object(images, '_change_services', side_effect=change) as commands,
          patch.object(images, 'inference_api_health', side_effect=lambda: deepcopy(health)),
          patch.object(screen_monitor, 'set_screen_monitor_enabled', side_effect=configure),
          patch.object(images, '_application', {'status': 'pending', 'enabled': None, 'apis': [], 'error': None, 'last_applied_at': None})):
        yield SimpleNamespace(events=events, states=states, health=health, commands=commands, inspect=inspect, monitor=monitor)


def healthy(runtime):
    for state in runtime.states.values():
        state['ActiveState'] = 'active'
    for health in runtime.health.values():
        health.update(reachable=True, ready=True)
    _monitor_state.pid = 123
    _monitor_state.last_heartbeat = time.monotonic()


def test_enabled_starts_all_four_fixed_services_before_monitor(runtime):
    result = images.apply_image_filtering(settings={'image_filtering': True})
    assert runtime.events == [('start', [unit for unit, _ in images.API_UNITS]), ('monitor', True)]
    assert _monitor_state.enabled
    assert result['status'] == 'pending' and result['enabled'] is True
    assert len(result['apis']) == 4
    healthy(runtime)
    assert images.apply_image_filtering(settings={'image_filtering': True})['status'] == 'applied'
    assert runtime.commands.call_count == 1


def test_disabled_stops_monitor_before_apis_and_is_not_tampering(runtime):
    healthy(runtime)
    _monitor_state.enabled = True
    _monitor_state.failures.extend([1, 2])
    _monitor_state.tamper_cooldown_until = 900
    _monitor_state.tamper_blocker_pid = 999
    for health in runtime.health.values():
        health.update(reachable=False, ready=False)
    result = images.apply_image_filtering(settings={'image_filtering': False})
    assert runtime.events[0] == ('monitor', False)
    assert runtime.events[1] == ('stop', [unit for unit, _ in images.API_UNITS])
    runtime.monitor.terminate.assert_called_once_with(123, 1000)
    runtime.monitor.blocker.assert_called_once_with(999, 1000)
    assert not _monitor_state.enabled and _monitor_state.pid is None
    assert not _monitor_state.failures and _monitor_state.tamper_cooldown_until is None
    assert result['status'] == 'applied'
    with patch.object(screen_monitor, '_get_graphical_session_id') as session:
        screen_monitor._screen_monitor_watchdog_tick()
    session.assert_not_called()


def test_disabled_does_not_respawn_or_accept_registration(runtime):
    images.apply_image_filtering(settings={'image_filtering': False})
    with patch.object(screen_monitor, '_monitor_process_matches', return_value=True):
        with pytest.raises(ValueError, match='configured'):
            screen_monitor._register_screen_monitor(1000, 123)
    with pytest.raises(ValueError):
        screen_monitor._screen_monitor_heartbeat(1000, 123)
    images.apply_image_filtering(settings={'image_filtering': False})
    runtime.commands.assert_not_called()


def test_slow_model_loads_do_not_restart_services_or_launch_early(runtime):
    for state in runtime.states.values():
        state['ActiveState'] = 'activating'
    images.apply_image_filtering(settings={'image_filtering': True})
    runtime.commands.assert_not_called()
    with (patch.object(screen_monitor, '_get_graphical_session_id', return_value='2'),
          patch.object(screen_monitor, '_wayland_session_ready', return_value=True),
          patch.object(screen_monitor, '_inference_apis_ready', return_value=False),
          patch.object(screen_monitor, '_launch_screen_monitor') as launch):
        screen_monitor._screen_monitor_watchdog_tick()
    launch.assert_not_called()
    assert not _monitor_state.failures


def test_missing_services_return_pending_and_retry_recovers(runtime):
    runtime.commands.side_effect = RuntimeError('Unit not installed')
    result = images.apply_image_filtering(settings={'image_filtering': True})
    assert result['status'] == 'pending' and 'Unit not installed' in result['error']
    healthy(runtime)
    runtime.commands.side_effect = None
    assert images.apply_image_filtering(settings={'image_filtering': True})['status'] == 'applied'


def test_stop_failure_still_disables_supervision_and_retries(runtime):
    healthy(runtime)
    runtime.commands.side_effect = subprocess.TimeoutExpired('systemctl', 20)
    result = images.apply_image_filtering(settings={'image_filtering': False})
    assert not _monitor_state.enabled
    assert result['status'] == 'pending' and result['error']
    for state in runtime.states.values():
        state['ActiveState'] = 'inactive'
    for health in runtime.health.values():
        health.update(reachable=False, ready=False)
    runtime.commands.side_effect = None
    assert images.apply_image_filtering(settings={'image_filtering': False})['status'] == 'applied'


def test_off_detects_a_standalone_development_api(runtime):
    runtime.health[8000].update(reachable=True, ready=True)
    result = images.apply_image_filtering(settings={'image_filtering': False})
    assert result['status'] == 'pending' and 'manually' in result['error']
    assert not _monitor_state.enabled


def test_health_status_becomes_pending_after_heartbeat_expires(runtime):
    healthy(runtime)
    images.apply_image_filtering(settings={'image_filtering': True})
    _monitor_state.last_heartbeat = time.monotonic() - 100
    assert images.application_status()['status'] == 'pending'
    assert not images.application_status()['monitor']['healthy']


@pytest.mark.parametrize('mode,old,new,authenticated,allowed', [
    (LockMode.UNLOCKED, False, True, False, True),
    (LockMode.UNLOCKED, True, False, False, True),
    (LockMode.SELF_LOCK, False, True, False, True),
    (LockMode.SELF_LOCK, True, False, False, False),
    (LockMode.SELF_LOCK, True, True, False, True),
    (LockMode.GUARDIAN_LOCK, False, True, False, False),
    (LockMode.GUARDIAN_LOCK, True, False, False, False),
    (LockMode.GUARDIAN_LOCK, False, True, True, True),
    (LockMode.GUARDIAN_LOCK, True, False, True, True),
])
def test_setting_policy_through_rpc_preserves_denied_state(tmp_path, mode, old, new, authenticated, allowed):
    path = tmp_path / 'settings.toml'
    path.write_text(f'image_filtering = {str(old).lower()}\n')
    state = LockState(1, mode)
    with (patch.object(policy, 'get_lock_state', return_value=state),
          patch.object(request_handler, 'get_lock_state', return_value=state),
          patch.object(request_handler, 'verify_guardian_pin', return_value=authenticated),
          patch.object(request_handler, '_set_setting', side_effect=lambda *a, **kw: settings_ops.set_setting(*a, **kw, path=path)),
          patch.object(images, 'apply_image_filtering', return_value={'status': 'pending'}) as apply):
        response = request_handler._handle_request({'request_id': 'image', 'action': 'settings.set',
            'params': {'name': 'image_filtering', 'value': new, 'guardian_pin': '4826', 'guardian_authenticated': True}}, uid=1000, pid=22)
    assert response['ok'] is allowed
    assert settings_ops._load_settings(path)['image_filtering'] == (new if allowed else old)
    assert apply.call_count == int(allowed)
    if allowed:
        assert response['result']['change'] == ('maintain' if old == new else 'strengthen' if new else 'weaken')
        assert 'application' in response['result']
    encode_message(response)


def test_defaults_types_revision_and_noop_retry(tmp_path):
    path = tmp_path / 'settings.toml'
    path.write_text('safe_browsing = true\n')
    with (patch.object(policy, 'get_lock_state', return_value=LockState(1, LockMode.UNLOCKED)),
          patch.object(images, 'apply_image_filtering') as apply):
        assert settings_ops.get_setting('image_filtering', path=path) is True
        original = path.read_bytes()
        for value in ['true', 1, None]:
            with pytest.raises(ValueError):
                settings_ops.set_setting('image_filtering', value, path=path)
        revision = settings_ops.get_snapshot(path=path)['revision']
        settings_ops.set_setting('image_filtering', True, expected_revision=revision, path=path)
        assert path.read_bytes() == original
        apply.assert_called_once()
        settings_ops.set_setting('image_filtering', False, expected_revision=revision, path=path)
        assert settings_ops.get_setting('image_filtering', path=path) is False
        with pytest.raises(settings_ops.SettingsRevisionConflict):
            settings_ops.set_setting('image_filtering', True, expected_revision=revision, path=path)
        assert apply.call_count == 2


def test_setting_is_persisted_before_runtime_work_and_survives_failure(tmp_path):
    path = tmp_path / 'settings.toml';path.write_text('image_filtering = true\n')
    def apply(**kwargs):
        assert settings_ops._load_settings(path)['image_filtering'] is False
        return {'status': 'pending', 'error': 'stop timeout'}
    with (patch.object(policy, 'get_lock_state', return_value=LockState(1, LockMode.UNLOCKED)),
          patch.object(images, 'apply_image_filtering', side_effect=apply)):
        settings_ops.set_setting('image_filtering', False, path=path)
    assert settings_ops._load_settings(path)['image_filtering'] is False


@pytest.mark.parametrize('enabled', [False, True])
def test_daemon_restart_reconciles_the_persisted_value(runtime, tmp_path, enabled):
    path = tmp_path / 'settings.toml';path.write_text(f'image_filtering = {str(enabled).lower()}\n')
    with (patch.object(policy, 'get_lock_state', return_value=LockState(1, LockMode.UNLOCKED)),
          patch.object(settings_ops, 'get_setting', side_effect=lambda name: settings_ops._load_settings(path)[name])):
        screen_monitor.initialize_screen_monitor(1000, enabled=False)
        images.apply_image_filtering()
    assert _monitor_state.enabled is enabled
    assert runtime.commands.call_count == int(enabled)


def test_startup_socket_sees_disabled_monitor_when_saved_off(monitor):
    def applied():
        screen_monitor.set_screen_monitor_enabled(False)
    def socket_ready():
        assert _monitor_state.enabled is False
        assert _monitor_state.uid == 1000
        _stop_event.set()
        return Mock()
    with (patch.object(server.os, 'geteuid', return_value=0),
          patch.object(server, 'load_protected_uid', return_value=1000),
          patch.object(server, 'initialize_lock_state'), patch.object(server, 'initialize_guardian_auth'),
          patch.object(server, 'initialize_activity'),
          patch('sentinel.daemon.operations.daily_screen_time_ops.initialize'),
          patch.object(server.block_window_ops, 'initialize_block_windows'),
          patch.object(server.dns_ops, 'initialize_dns'),
          patch('sentinel.daemon.operations.boot_media_ops.apply_boot_media_policy'),
          patch.object(server.vm_ops, 'apply_vm_policy'),
          patch.object(server.browser_ops, 'initialize_browser_policy'),
          patch.object(server.app_blocker_ops, 'initialize_app_blocker'),
          patch.object(server.guardian_app_ops, 'initialize_guardian_apps'),
          patch.object(images, 'apply_image_filtering', side_effect=applied),
          patch.object(server, 'start_extension_server', return_value=(Mock(), Mock())),
          patch.object(server.signal, 'signal'), patch.object(server.threading, 'Thread'),
          patch.object(server, '_prepare_socket', side_effect=socket_ready),
          patch.object(server, 'SENTINELD_SOCKET')):
        server._run_daemon()
    _stop_event.clear()


def test_disable_waits_for_an_inflight_tick_then_prevents_relaunch(monitor):
    entered, release, disabled = Event(), Event(), Event()
    _monitor_state.enabled = True
    def tick():
        entered.set()
        assert release.wait(2)
        _monitor_state.pid = 123
    with patch.object(screen_monitor, '_screen_monitor_watchdog_tick_locked', side_effect=tick):
        worker = Thread(target=screen_monitor._screen_monitor_watchdog_tick)
        worker.start();assert entered.wait(1)
        stopper = Thread(target=lambda: (screen_monitor.set_screen_monitor_enabled(False), disabled.set()))
        stopper.start();assert not disabled.wait(.05)
        release.set();worker.join(2);stopper.join(2)
    assert disabled.is_set() and not _monitor_state.enabled and _monitor_state.pid is None
    monitor.terminate.assert_called_with(123, 1000)


def test_discovery_exact_identity():
    interpreter = str(Path(screen_monitor.sys.executable).resolve())
    module = screen_monitor.SCREEN_MONITOR_MODULE
    values = []
    for pid, uid, exe, args in [
        (1, 1000, interpreter, [interpreter, '-m', module]),
        (2, 1001, interpreter, [interpreter, '-m', module]),
        (3, 1000, '/usr/bin/echo', ['echo', '-m', module]),
        (4, 1000, interpreter, [interpreter, '-c', 'text', '-m', module]),
        (5, 1000, interpreter, [interpreter, '-m', 'other.module']),
    ]:
        values.append(SimpleNamespace(info={'pid': pid, 'uids': SimpleNamespace(real=uid), 'exe': exe, 'cmdline': args}))
    with patch.object(screen_monitor.psutil, 'process_iter', return_value=values), patch.object(screen_monitor, '_monitor_process_matches', return_value=True):
        assert screen_monitor._discover_screen_monitors(1000) == [1]


def test_disabled_restart_stops_discovered_previous_monitor(monitor):
    monitor.discover.return_value = [555]
    screen_monitor.initialize_screen_monitor(1000, enabled=False)
    screen_monitor.set_screen_monitor_enabled(False)
    monitor.terminate.assert_called_with(555, 1000)


def test_fixed_systemctl_arguments_and_missing_unit_parsing():
    stdout = '\n\n'.join(f'Id={unit}\nLoadState=loaded\nActiveState=active' for unit, _ in images.API_UNITS)
    with patch.object(images.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=stdout, stderr='')) as run:
        assert all(value['ActiveState'] == 'active' for value in images._service_states().values())
        images._change_services('stop', [unit for unit, _ in images.API_UNITS])
    assert run.call_args.args[0] == [images.SYSTEMCTL, 'stop', *[unit for unit, _ in images.API_UNITS]]
    assert run.call_args.kwargs['timeout'] == 20


def test_image_rpc_reads_are_pin_free_and_do_not_wait_for_mutations():
    with request_handler._mutation_lock, patch.object(images, 'get_status', return_value={'enabled': False}):
        result = request_handler._handle_request({'request_id': 'read', 'action': 'imagefilter.status', 'params': {}}, uid=1000, pid=22)
    assert result['ok'] and result['result']['enabled'] is False


def test_client_timeout_and_status_action():
    with patch.object(client, 'request', return_value={}) as request:
        client.set_setting('image_filtering', False)
        assert request.call_args.kwargs['timeout'] == 60
        client.get_image_filtering_status()
        assert request.call_args.args == ('imagefilter.status',)


def test_health_probes_skip_proxies_and_distinguish_ready_loading_offline():
    response = Mock()
    response.__enter__ = Mock(return_value=response);response.__exit__ = Mock()
    response.status = 200
    response.read.side_effect = [b'{"status":"ok","model_loaded":true}', b'{"status":"ok","model_loaded":false}']
    with patch.object(readiness._health_opener, 'open', side_effect=[response, response, OSError('offline'), OSError('offline')]):
        health = readiness.inference_api_health()
    assert health[8000] == {'reachable': True, 'ready': True}
    assert health[8001] == {'reachable': True, 'ready': False}
    assert health[8002] == {'reachable': False, 'ready': False}


def test_enabling_after_daemon_restart_retains_monitor_adoption_grace(runtime):
    screen_monitor.initialize_screen_monitor(1000, enabled=False)
    deadline = _monitor_state.awaiting_registration_until
    images.apply_image_filtering(settings={'image_filtering': True})
    assert _monitor_state.awaiting_registration_until == deadline
    assert deadline > time.monotonic()


def test_monitor_sigterm_runs_cleanup():
    # This lifecycle test does not exercise hashing or require its native
    # extension in a lightweight daemon test environment.
    with patch.dict('sys.modules', {'sentinel.image_blocking.screen_monitor.hash_cache':
                                   SimpleNamespace(WindowHashCache=Mock())}):
        from sentinel.image_blocking.screen_monitor import monitor as module
    handlers = {}
    def install(signum, handler):
        handlers[signum] = handler
        return 'previous-handler'
    bridge = Mock()
    def wait(**kwargs):
        handlers[module.signal.SIGTERM](module.signal.SIGTERM, None)
    bridge.wait_for_update.side_effect = wait
    heartbeat = Mock()
    with (patch.object(module.signal, 'signal', side_effect=install) as signal,
          patch.object(module, 'KWinBridge', return_value=bridge),
          patch.object(module, 'MonitorHeartbeat', return_value=heartbeat),
          patch.object(module.psutil, 'Process'), patch.object(module, 'WindowHashCache')):
        module.main()
    heartbeat.stop.assert_called_once()
    bridge.stop.assert_called_once()
    assert signal.call_args.args == (module.signal.SIGTERM, 'previous-handler')
