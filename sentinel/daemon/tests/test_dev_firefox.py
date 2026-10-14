"""The explicit daemon development allowance exempts only the Firefox baseline."""
from unittest.mock import patch

import pytest

from sentinel.browser_policies import restrictions
from sentinel.daemon.operations import app_blocker_ops
from sentinel.app_blocker.process_monitor.policy import ProcessBlockPolicy
from sentinel.daemon.security.policy import PolicyDeniedError


@pytest.mark.parametrize('value,allowed', [(None,False),('0',False),('true',False),('1',True)])
def test_only_explicit_daemon_environment_enables_allowance(value, allowed):
    env = {} if value is None else {'SENTINEL_DEV_ALLOW_FIREFOX':value}
    with patch.dict(restrictions.os.environ, env, clear=True):
        assert restrictions.dev_allow_firefox() is allowed
        ids = restrictions.runtime_browser_flatpak_ids()
        assert ('org.mozilla.firefox' in ids) is not allowed
        assert 'com.brave.Browser' in ids
        assert 'com.google.Chrome' in ids


def test_native_firefox_only_is_exempt_and_full_inventory_can_identify_old_profiles(tmp_path):
    firefox = tmp_path / 'firefox'
    chromium = tmp_path / 'chromium'
    for path in (firefox, chromium):
        path.write_text('binary')
        path.chmod(0o755)
    patterns = (str(firefox), str(chromium))
    with (patch.object(restrictions,'NATIVE_BROWSER_PATTERNS',patterns),
          patch.dict(restrictions.os.environ,{'SENTINEL_DEV_ALLOW_FIREFOX':'1'})):
        entries = restrictions.installed_browser_blocks()
        assert {entry['executable'] for entry in entries.values()} == {str(chromium)}
        all_entries = restrictions.installed_browser_blocks(include_dev_exempt=True)
        assert {entry['executable'] for entry in all_entries.values()} == {str(firefox),str(chromium)}
    with patch.object(restrictions,'NATIVE_BROWSER_PATTERNS',patterns), patch.dict(restrictions.os.environ,{},clear=True):
        assert len(restrictions.installed_browser_blocks()) == 2


def test_allowance_does_not_change_browser_installer_restrictions():
    with patch.dict(restrictions.os.environ,{'SENTINEL_DEV_ALLOW_FIREFOX':'1'}):
        with pytest.raises(PolicyDeniedError):
            restrictions.require_supported_browser_package('firefox')
        with pytest.raises(PolicyDeniedError):
            restrictions.require_supported_browser_flatpak('org.mozilla.firefox')


def test_old_baseline_profile_is_explicitly_removed_and_custom_blocks_remain():
    firefox = {'name':'Firefox','type':'native','executable':'/usr/lib/firefox/firefox'}
    def inventory(*, include_dev_exempt=False):
        return {'baseline':firefox} if include_dev_exempt else {}
    custom = {'name':'Firefox','type':'flatpak','flatpak_id':'org.mozilla.firefox'}
    with (patch.dict(restrictions.os.environ,{'SENTINEL_DEV_ALLOW_FIREFOX':'1'}),
          patch.object(app_blocker_ops,'installed_browser_blocks',side_effect=inventory),
          patch.object(app_blocker_ops,'managed_profile_name',return_value='usr.lib.firefox.firefox'),
          patch.object(app_blocker_ops,'sync_blocked_apps',return_value={'errors':{},'unresolved':{}}) as sync,
          patch('sentinel.daemon.operations.block_window_ops.get_active_apps',return_value={}),
          patch.object(app_blocker_ops,'_runtime_policy',ProcessBlockPolicy.empty())):
        app_blocker_ops._apply_apps({'custom':custom})
        assert sync.call_args.kwargs['force_remove_profiles'] == {'usr.lib.firefox.firefox'}
        assert sync.call_args.args[0] == {'custom':custom}
        assert 'org.mozilla.firefox' in app_blocker_ops.get_process_policy().flatpak_ids
        app_blocker_ops._apply_apps({})
        assert 'org.mozilla.firefox' not in app_blocker_ops.get_process_policy().flatpak_ids
        assert 'com.brave.Browser' in app_blocker_ops.get_process_policy().flatpak_ids
