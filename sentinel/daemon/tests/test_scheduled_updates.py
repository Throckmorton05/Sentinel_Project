"""Scheduled updates share manual failure handling and never reboot after failure."""
import tempfile
from pathlib import Path
from unittest.mock import patch
import pytest
from sen.updates import auto_update
from sentinel.daemon.operations import system_ops


def test_failed_scheduled_update_never_records_success_or_reboots():
    config = {'enabled': True, 'cadence': 'daily', 'time': '03:00',
              'auto_restart': True, 'last_success': None}
    with patch.object(auto_update, '_load_config', return_value=config), \
         patch.object(auto_update, '_save_config') as save, \
         patch.object(system_ops, '_system_update', side_effect=TimeoutError('index failure')), \
         patch.object(auto_update.subprocess, 'run') as reboot:
        with pytest.raises(TimeoutError):
            auto_update.run_scheduled_update()
    save.assert_not_called()
    reboot.assert_not_called()
    assert config['last_success'] is None


def test_successful_scheduled_update_records_success():
    config = {'enabled': True, 'cadence': 'daily', 'time': '03:00',
              'auto_restart': False, 'last_success': None}
    with patch.object(auto_update, '_load_config', return_value=config), \
         patch.object(auto_update, '_save_config') as save, \
         patch.object(system_ops, '_system_update') as update:
        auto_update.run_scheduled_update()
    update.assert_called_once_with()
    save.assert_called_once_with(config)
    assert config['last_success'] is not None


def test_update_worker_does_not_require_initialized_daemon_state():
    from sentinel.daemon.core import lock_state
    with patch.object(lock_state, '_current_state', None), \
         patch.object(system_ops, 'run_apt') as run:
        system_ops._system_update()
    assert run.call_args_list[-1].args[0] == ['upgrade', '-y', '--no-remove']
