"""Extension maintenance survives toggles, native removal and reinstallation."""
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from sentinel.daemon import client
from sentinel.daemon.supervision import steam
from sentinel.safe_steam import assets, installation, provision, runtime, status, watchdog


def test_native_discovery_ignores_stale_profiles_and_unsupported_launchers(tmp_path):
    # A deleted native launcher must not trigger repairs of a leftover profile.
    with patch.object(installation, 'NATIVE_LAUNCHERS', (tmp_path / 'missing-steam',)):
        assert not installation.native_steam_installed()
    script = tmp_path / 'steam'; script.write_text('#!/bin/sh\n'); script.chmod(0o755)
    with patch.object(installation, 'NATIVE_LAUNCHERS', (script,)):
        # /tmp and desktop-user-controlled directories fail protected-path checks.
        assert not installation.native_steam_installed()


def test_root_watchdog_absent_then_reinstalled_restarts_checks_even_with_filtering_off():
    steam._stop_event.clear()
    with (patch.object(steam, 'native_steam_installed', side_effect=[False, True]),
          patch.object(steam, 'protection_enabled', return_value=False) as filtering,
          patch.object(steam, '_get_graphical_session_id', return_value='3') as session,
          patch.object(steam, 'running_extension_path', return_value=None),
          patch.object(assets, 'native_update_needed', return_value=False) as bundle,
          patch.object(steam, 'run_user_check', return_value=0) as run):
        steam.check_steam(1000)
        assert status.get_status(False)['steam']['status'] == 'not_installed'
        run.assert_not_called(); bundle.assert_not_called(); session.assert_not_called()
        steam.check_steam(1000)
        run.assert_called_once_with(1000)
        assert status.get_status(False)['steam']['status'] == 'applied'
        filtering.assert_not_called()


def test_user_watchdog_repairs_existing_profile_after_reinstallation(tmp_path):
    prefs = tmp_path / 'Preferences'; prefs.write_text('{}')
    def repair(path, load_path):
        path.write_text(json.dumps({'extensions': {'settings': {provision.SAFE_STEAM_ID:
            {'path': str(load_path), 'state': 1, 'location': 4}}}}))
    with (patch.object(watchdog, 'native_steam_installed', side_effect=[False, True]),
          patch.object(watchdog, 'preferences_path', return_value=prefs) as profile,
          patch.object(watchdog, 'verify_source'), patch.object(watchdog, 'verify_integrity'),
          patch.object(watchdog, 'repair_safe_steam', side_effect=repair) as provisioned,
          patch.object(client, 'get_setting', side_effect=AssertionError('Installation must not depend on the toggle'))):
        assert watchdog.check_once()
        assert watchdog._last_absent
        profile.assert_not_called(); provisioned.assert_not_called()
        assert watchdog.check_once()
        assert not watchdog._last_absent
        provisioned.assert_called_once_with(prefs, runtime.CONTAINER_EXTENSION_PATH)


def test_first_steam_launch_is_allowed_to_initialize_profile(tmp_path):
    with (patch.object(watchdog, 'native_steam_installed', return_value=True),
          patch.object(watchdog, 'preferences_path', return_value=tmp_path / 'not-yet-created'),
          patch.object(watchdog, 'steam_is_running', return_value=True),
          patch.object(watchdog, 'shutdown_steam') as stop,
          patch.object(watchdog, 'run_provisioner') as write):
        assert watchdog.check_once()
        assert watchdog._last_waiting
    stop.assert_not_called(); write.assert_not_called()


def test_turning_filtering_off_never_hides_installation_errors():
    status.set_status('steam', 'pending', 'Repair failed')
    result = status.get_status(False)
    assert result['enabled'] is False
    assert result['steam']['status'] == 'pending'
    assert result['steam']['error'] == 'Repair failed'
    status.request_reconcile(False)
    assert status.get_status(False)['steam']['status'] == 'pending'


def test_bounded_child_is_not_cancelled_by_filter_switch():
    child = Mock()
    child.poll.side_effect = [None, 0, 0]
    child.returncode = 0
    stop_event = Mock(); stop_event.wait.return_value = False
    with (patch.object(steam, '_user_session_command', return_value=['protected-user-command']) as command,
          patch.object(steam.subprocess, 'Popen', return_value=child),
          patch.object(steam, '_stop_event', stop_event),
          patch.object(steam, 'protection_enabled', return_value=False) as enabled):
        assert steam.run_user_check(1000, extension_path=runtime.CONTAINER_EXTENSION_PATH) == 0
    enabled.assert_not_called()
    assert command.call_args.args[-2:] == ('--extension-path', str(runtime.CONTAINER_EXTENSION_PATH))
    child.wait.assert_called_once_with(timeout=3.0)


def test_stop_for_update_works_without_reading_filter_setting():
    with (patch.object(watchdog.os, 'geteuid', return_value=1000),
          patch('sys.argv', ['watchdog', '--stop-for-update']),
          patch.object(watchdog, 'steam_is_running', side_effect=[True, False]),
          patch.object(watchdog, 'shutdown_steam') as stop,
          patch.object(client, 'get_setting', side_effect=AssertionError('Unexpected toggle read'))):
        with pytest.raises(SystemExit) as result:
            watchdog.main()
    assert result.value.code == 0
    stop.assert_called_once()


def test_shutdown_uses_protected_usr_games_launcher_without_path_lookup():
    with (patch.object(watchdog, 'native_steam_launcher', return_value=Path('/usr/games/steam')),
          patch.object(watchdog, 'steam_is_running', return_value=False),
          patch.object(watchdog.subprocess, 'run') as command):
        watchdog.shutdown_steam()
    assert command.call_args.args[0] == ['/usr/games/steam', '-shutdown']
    assert command.call_args.kwargs['timeout'] == 5.0


@pytest.mark.parametrize('installed,exit_code', [(False, 4), (True, 2)])
def test_cli_distinguishes_absent_launcher_from_uninitialized_profile(tmp_path, installed, exit_code):
    with (patch.object(watchdog.os, 'geteuid', return_value=1000),
          patch('sys.argv', ['watchdog', '--once']),
          patch.object(watchdog, 'native_steam_installed', return_value=installed),
          patch.object(watchdog, 'preferences_path', return_value=tmp_path / 'missing')):
        with pytest.raises(SystemExit) as result:
            watchdog.main()
    assert result.value.code == exit_code
