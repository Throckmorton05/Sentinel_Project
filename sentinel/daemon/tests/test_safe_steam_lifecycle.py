"""Enabling Safe Steam stages assets, repairs as the user, and exposes status."""
from unittest.mock import patch
import pytest
from sentinel.daemon.operations import browser_ops, settings_ops
from sentinel.daemon.supervision import steam
from sentinel.safe_steam import assets, status
from sentinel.daemon.core.lock_state import LockMode, LockState, STATE_VERSION
from sentinel.daemon.security import policy


@pytest.fixture(autouse=True)
def isolated_steam_runtime():
    steam._stop_event.clear()
    with (patch.object(steam, 'running_extension_path', return_value=None),
          patch.object(steam, 'native_steam_installed', return_value=True)):
        yield


def test_native_upgrade_stops_user_steam_then_installs_and_verifies():
    calls=[]
    with (patch.object(steam,'protection_enabled',return_value=True),
          patch.object(steam,'_get_graphical_session_id',return_value='3'),
          patch.object(assets,'native_update_needed',return_value=True),
          patch.object(assets,'install_native_assets',side_effect=lambda:calls.append('install')),
          patch.object(steam,'run_user_check',side_effect=lambda uid, option='--once':calls.append((uid,option)) or 0)):
        steam.check_steam(1000)
    assert calls==[(1000,'--stop-for-update'),'install',(1000,'--once')]
    assert status.get_status(True)['steam']['status']=='applied'


def test_unchanged_native_assets_only_check_registration():
    with (patch.object(steam,'protection_enabled',return_value=True),
          patch.object(steam,'_get_graphical_session_id',return_value='3'),
          patch.object(assets,'native_update_needed',return_value=False),
          patch.object(assets,'install_native_assets') as install,
          patch.object(steam,'run_user_check',return_value=0) as run):
        steam.check_steam(1000)
    install.assert_not_called();run.assert_called_once_with(1000)


def test_stop_failure_keeps_installed_assets_and_reports_pending():
    with (patch.object(steam,'protection_enabled',return_value=True),
          patch.object(steam,'_get_graphical_session_id',return_value='3'),
          patch.object(assets,'native_update_needed',return_value=True),
          patch.object(assets,'install_native_assets') as install,
          patch.object(steam,'run_user_check',return_value=1)):
        steam.check_steam(1000)
    install.assert_not_called()
    assert status.get_status(True)['steam']['status']=='pending'


def test_toggle_off_mid_upgrade_does_not_cancel_installation():
    with (patch.object(steam,'protection_enabled',return_value=False) as enabled,
          patch.object(steam,'_get_graphical_session_id',return_value='3'),
          patch.object(assets,'native_update_needed',return_value=True),
          patch.object(assets,'install_native_assets') as install,
          patch.object(steam,'run_user_check',return_value=0)):
        steam.check_steam(1000)
    install.assert_called_once()
    enabled.assert_not_called()
    assert status.get_status(False)['steam']['status']=='applied'
    assert status.get_status(False)['enabled'] is False


def test_no_session_and_uninitialized_profile_are_waiting_not_verified():
    with patch.object(steam,'protection_enabled',return_value=True), patch.object(steam,'_get_graphical_session_id',return_value=None):
        steam.check_steam(1000)
    assert status.get_status(True)['steam']['status']=='waiting'
    with (patch.object(steam,'protection_enabled',return_value=True),
          patch.object(steam,'_get_graphical_session_id',return_value='3'),
          patch.object(assets,'native_update_needed',return_value=False),
          patch.object(steam,'run_user_check',return_value=2)):
        steam.check_steam(1000)
    assert status.get_status(True)['steam']['status']=='waiting'


def test_enable_and_repeated_enable_wake_native_repair_and_apply_chrome(tmp_path):
    path=tmp_path/'settings.toml';path.write_text('safe_steam = false\n')
    state=LockState(STATE_VERSION,LockMode.UNLOCKED)
    with (patch.object(policy,'get_lock_state',return_value=state),
          patch.object(browser_ops,'apply_browser_policy') as chrome,
          patch.object(status,'request_reconcile') as wake):
        settings_ops.set_setting('safe_steam',True,path=path)
        settings_ops.set_setting('safe_steam',True,path=path)
    assert chrome.call_count==wake.call_count==2
    assert wake.call_args.args==(True,)


def test_runtime_reads_are_reported_separately_and_expire():
    with patch.object(status.time,'time',return_value=1000):
        status.observe('chrome-extension://fbgjjhaoclncakjkdebkllglkgpffkfb')
        status.observe('chrome-extension://jdfobdpedcmlbmolbdfaoaidldkjjfnh')
        assert status.get_status(True)['steam']['runtime']=='observed'
        assert status.get_status(True)['chrome']['runtime']=='observed'
    with patch.object(status.time,'time',return_value=1021):
        assert status.get_status(True)['steam']['runtime']=='waiting'
        assert status.get_status(True)['chrome']['runtime']=='waiting'
