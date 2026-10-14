"""Persisted image-filter policy controls the monitor and fixed model services."""
from copy import deepcopy
import subprocess
import threading
import time

from sentinel.daemon.core.constants import SYSTEMCTL, MONITOR_HEARTBEAT_TIMEOUT
from sentinel.daemon.security.policy import ChangeKind, require_policy
from sentinel.daemon.supervision import screen_monitor
from sentinel.daemon.supervision.readiness import inference_api_health
from sentinel.daemon.supervision.state import _monitor_state, _monitor_state_lock

API_UNITS = (
    ('sentinel-opennsfw.service', 8000), ('sentinel-drawn.service', 8001),
    ('sentinel-hentai.service', 8002), ('sentinel-nudenet.service', 8003),
)
_operation_lock = threading.RLock()
_status_lock = threading.RLock()
_application = {'status': 'pending', 'enabled': None, 'error': None,
                'apis': [], 'last_applied_at': None}


def _service_states():
    result = subprocess.run([SYSTEMCTL, 'show', *(unit for unit, _ in API_UNITS),
        '--property=Id,LoadState,ActiveState', '--no-pager'], capture_output=True,
        text=True, check=False, timeout=6)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'Could not inspect image-filter services.')
    states = {}
    for section in result.stdout.strip().split('\n\n'):
        fields = dict(line.split('=', 1) for line in section.splitlines() if '=' in line)
        if fields.get('Id'):
            states[fields['Id']] = fields
    return {unit: states.get(unit, {'LoadState': 'not-found', 'ActiveState': 'inactive'})
            for unit, _ in API_UNITS}


def _change_services(operation, units):
    if not units:
        return
    # Fixed unit names only; no client-provided paths, shell, or service names.
    result = subprocess.run([SYSTEMCTL, operation, *units], capture_output=True,
                            text=True, check=False, timeout=20)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f'Could not {operation} image-filter services.')


def application_status():
    with _status_lock:
        result = deepcopy(_application)
    with _monitor_state_lock:
        last = _monitor_state.last_heartbeat
        result['monitor'] = {'enabled': _monitor_state.enabled, 'pid': _monitor_state.pid,
            'healthy': bool(_monitor_state.enabled and _monitor_state.pid is not None
                            and last is not None and time.monotonic() - last < MONITOR_HEARTBEAT_TIMEOUT),
            'waiting_reason': _monitor_state.waiting_reason}
    if result['enabled'] and result['status'] == 'applied' and not result['monitor']['healthy']:
        result['status'] = 'pending'
    return result


def get_status():
    require_policy('imagefilter.status', ChangeKind.READ)
    from sentinel.daemon.operations import settings_ops
    return {'enabled': settings_ops.get_setting('image_filtering'), 'application': application_status()}


def apply_image_filtering(*, settings=None):
    """Reconcile saved intent; runtime failures stay visible and are retried.

    The caller serializes setting writes with the daemon's mutation lock. This
    module never changes the setting or trusts a client authentication flag.
    """
    from sentinel.daemon.operations import settings_ops
    if settings is None:
        enabled = settings_ops.get_setting('image_filtering')
    else:
        enabled = settings.get('image_filtering', True)
        if type(enabled) is not bool:
            raise ValueError('image_filtering must be true or false.')
    errors = []
    with _operation_lock:
        # Always stop the monitor first. It must neither scan nor respawn while
        # its APIs are intentionally being stopped.
        if not enabled:
            try:
                screen_monitor.set_screen_monitor_enabled(False)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                errors.append(str(exc))
        try:
            states = _service_states()
            if enabled:
                units = [unit for unit, _ in API_UNITS
                         if states[unit]['ActiveState'] not in {'active', 'activating', 'reloading'}]
            else:
                units = [unit for unit, _ in API_UNITS
                         if states[unit]['LoadState'] != 'not-found'
                         and states[unit]['ActiveState'] not in {'inactive', 'failed'}]
            if units:
                _change_services('start' if enabled else 'stop', units)
                states = _service_states()
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            errors.append(str(exc))
            states = {unit: {'ActiveState': 'unknown', 'LoadState': 'unknown'} for unit, _ in API_UNITS}
        health = inference_api_health()
        if enabled:
            try:
                # The existing watchdog waits for Wayland AND all model health
                # checks before launching, including during slow model loading.
                screen_monitor.set_screen_monitor_enabled(True)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                errors.append(str(exc))
        apis = [{'unit': unit, 'port': port, 'state': states[unit]['ActiveState'],
                 'load_state': states[unit]['LoadState'], **health[port]} for unit, port in API_UNITS]
        ready = all(api['state'] == 'active' and api['ready'] for api in apis) if enabled else all(
            api['state'] in {'inactive', 'failed'} and not api['reachable'] for api in apis)
        if not enabled and any(api['reachable'] for api in apis):
            errors.append('An API is still listening after the managed services stopped. '
                          'Close any image-filter APIs started manually in development terminals.')
        with _status_lock:
            global _application
            _application = {'status': 'applied' if ready and not errors else 'pending',
                'enabled': enabled, 'error': '; '.join(errors) or None, 'apis': apis,
                'last_applied_at': time.time() if ready and not errors else _application['last_applied_at']}
    return application_status()
