"""Guardian website security and policy integration; no live system writes."""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from sentinel.browser_policies import chrome, restrictions
from sentinel.daemon.core.lock_state import LockMode, LockState, STATE_VERSION
from sentinel.daemon.operations import guardian_website_ops as ops, browser_ops, app_blocker_ops
from sentinel.daemon.rpc import request_handler as rpc
from sentinel.daemon.security.policy import PolicyDeniedError
from sentinel.extension_catalog.catalog import parse_inventory


@pytest.fixture
def selection(tmp_path):
    path = tmp_path / 'guardian-websites.json'
    state = LockState(STATE_VERSION, LockMode.GUARDIAN_LOCK)
    real_fstat = os.fstat

    def root_owned_test_state(fd):
        # Model the production file's owner for this fixture only. Keep the
        # real file type/mode so permission and symlink rejection still work.
        info = real_fstat(fd)
        if path.exists():
            target = path.stat()
            if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
                fields = list(info)
                fields[4] = 0  # st_uid
                return os.stat_result(fields)
        return info

    with (patch.object(ops, '_config', {'version': 1, 'enabled': False, 'domains': []}),
          patch.object(ops, '_path', path),
          patch.object(ops, 'get_lock_state', return_value=state) as mode,
          patch('sentinel.dns.storage.protect_directory'),
          patch.object(os, 'fstat', side_effect=root_owned_test_state),
          patch.object(browser_ops, 'apply_browser_policy', return_value={'status': 'applied'}) as browser,
          patch.object(app_blocker_ops, 'reconcile_app_blocker', return_value={'status': 'active'}) as apps):
        ops.initialize_guardian_websites(path)
        yield path, mode, browser, apps


def request(action, params=None):
    return rpc._handle_request({'request_id': 'websites-test', 'action': action, 'params': params or {}},
                               uid=1000, pid=2)


@pytest.mark.parametrize('action,params', [
    ('enable', {'enabled': True}), ('enable', {'enabled': False}),
    ('approve', {'domain': 'school.example.org'}),
    ('revoke', {'domain': 'school.example.org'}),
    ('replace', {'enabled': True, 'domains': ['school.example.org']}),
    ('authenticate', {}),
])
@pytest.mark.parametrize('pin,valid', [(None, False), ('9999', False), ('1234', True)])
def test_every_rpc_write_requires_verified_pin(selection, action, params, pin, valid):
    path, _, browser, apps = selection
    before = path.read_bytes()
    params = {**params, 'guardian_authenticated': True}
    if pin is not None:
        params['guardian_pin'] = pin
    with patch.object(rpc, 'verify_guardian_pin', return_value=valid) as verify:
        result = request('guardian.websites.' + action, params)
    assert result['ok'] is valid
    if pin is None:
        verify.assert_not_called()
    else:
        verify.assert_called_once_with(pin)
    if not valid:
        assert path.read_bytes() == before
        browser.assert_not_called()
        apps.assert_not_called()


@pytest.mark.parametrize('mode', [LockMode.UNLOCKED, LockMode.SELF_LOCK])
def test_cannot_mutate_outside_guardian(selection, mode):
    path, state, *_ = selection
    before = path.read_bytes()
    state.return_value = LockState(STATE_VERSION, mode)
    with pytest.raises(PolicyDeniedError):
        ops.replace_selection(['example.org'], True, guardian_authenticated=True)
    assert path.read_bytes() == before


@pytest.mark.parametrize('domain', ['*', '*.example.org', '.example.org', 'https://example.org',
    'example.org/path', 'example.org:443', 'example.org@evil.test', 'com', '127.0.0.1',
    'localhost.localdomain', 'example.org\n*', 'bing.com', 'images.bing.com', '', None, 1])
def test_no_policy_syntax_or_restricted_search_injection(selection, domain):
    path, *_ = selection
    before = path.read_bytes()
    with pytest.raises(ValueError):
        ops.approve(domain, guardian_authenticated=True)
    assert path.read_bytes() == before


def test_domains_normalize_without_expanding_www(selection):
    ops.replace_selection([' WWW.SCHOOL.EXAMPLE.ORG. ', 'www.school.example.org', 'bücher.example'], True,
                          guardian_authenticated=True)
    assert ops.policy()['domains'] == ['www.school.example.org', 'xn--bcher-kva.example']
    assert 'http://school.example.org' not in ops.chrome_rules()['URLAllowlist']


def test_limit_and_invalid_enabled_type_preserve_state(selection):
    path, *_ = selection
    before = path.read_bytes()
    with pytest.raises(ValueError):
        ops.replace_selection([f's{i}.example.org' for i in range(ops.MAX_DOMAINS + 1)], True,
                              guardian_authenticated=True)
    with pytest.raises(ValueError):
        ops.set_enabled(1, guardian_authenticated=True)
    assert path.read_bytes() == before


def test_persist_restart_disable_and_reenable_preserve_choices(selection):
    path, _, browser, apps = selection
    ops.replace_selection(['youtubekids.com', 'school.example.org'], True, guardian_authenticated=True)
    ops.initialize_guardian_websites(path)
    assert ops.active()
    assert json.loads(path.read_text())['domains'] == ['school.example.org', 'youtubekids.com']
    ops.set_enabled(False, guardian_authenticated=True)
    assert ops.chrome_rules() == {}
    ops.set_enabled(True, guardian_authenticated=True)
    assert ops.active()
    assert len(ops.policy()['domains']) == 2
    assert browser.call_count == apps.call_count == 3


def test_empty_selection_blocks_all_websites(selection):
    ops.set_enabled(True, guardian_authenticated=True)
    assert ops.chrome_rules()['URLAllowlist'] == []
    assert ops.chrome_rules()['URLBlocklist'][0] == '*'


def test_corrupt_and_symlinked_state_is_not_reset(selection):
    path, *_ = selection
    path.write_text('{broken')
    with pytest.raises(ValueError):
        ops.initialize_guardian_websites(path)
    assert path.read_text() == '{broken'
    link = path.with_name('link.json')
    link.symlink_to(path)
    with pytest.raises(OSError):
        ops.initialize_guardian_websites(link)


def test_unprotected_file_rejected(selection):
    path, *_ = selection
    path.chmod(0o666)
    with pytest.raises(ValueError, match='root-owned'):
        ops.initialize_guardian_websites(path)


def test_non_root_owned_file_rejected(selection):
    path, *_ = selection
    fields = list(path.stat())
    fields[4] = 1000
    with patch.object(os, 'fstat', return_value=os.stat_result(fields)):
        with pytest.raises(ValueError, match='root-owned'):
            ops.initialize_guardian_websites(path)


def test_pending_browser_write_does_not_lose_selection(selection):
    path, _, browser, _ = selection
    browser.return_value = {'status': 'pending', 'error': 'disk unavailable'}
    with patch.object(browser_ops, 'application_status', return_value=browser.return_value):
        result = ops.replace_selection(['school.example.org'], True, guardian_authenticated=True)
    assert result['browser_application']['status'] == 'pending'
    assert json.loads(path.read_text())['enabled'] is True
    assert ops.chrome_rules()['URLAllowlist'] == ['http://school.example.org', 'https://school.example.org']


def test_full_chrome_projection_and_modes(selection):
    _, state, *_ = selection
    ops.replace_selection(['youtubekids.com', 'school.example.org'], True, guardian_authenticated=True)
    inventory = parse_inventory((Path(__file__).resolve().parents[3] /
                                 'configs/browser/approved_extensions.toml').read_text())
    with (patch.object(chrome, 'catalog_extension_id', return_value='a' * 32),
          patch.object(chrome, 'load_inventory', return_value=inventory),
          patch.object(chrome, 'load_approvals', return_value=set()),
          patch.object(chrome, 'get_lock_state', side_effect=lambda: state.return_value)):
        for safe in (True, False):
            result = chrome.build_chrome_policy({'safe_browsing': safe})
            assert result['URLBlocklist'][0] == '*'
            assert set(result['URLAllowlist']) == {'http://youtubekids.com', 'https://youtubekids.com',
                                                 'http://school.example.org', 'https://school.example.org'}
            assert all('youtube.com' not in entry for entry in result['URLAllowlist'])
            assert result['DeveloperToolsAvailability'] == 2
            assert ('ForceGoogleSafeSearch' in result) is safe
            assert len(result['ExtensionInstallForcelist']) == 4
        for mode in (LockMode.UNLOCKED, LockMode.SELF_LOCK):
            state.return_value = LockState(STATE_VERSION, mode)
            result = chrome.build_chrome_policy({'safe_browsing': True})
            assert 'URLAllowlist' not in result
            assert '*' not in result['URLBlocklist']
            assert 'DeveloperToolsAvailability' not in result


def test_firefox_dev_exception_cannot_bypass_allowlist(selection, tmp_path):
    _, mode, *_ = selection
    firefox = tmp_path / 'firefox'
    firefox.write_text('browser')
    firefox.chmod(0o755)
    with (patch.dict(os.environ, {'SENTINEL_DEV_ALLOW_FIREFOX': '1'}),
          patch.object(restrictions, 'NATIVE_BROWSER_PATTERNS', (str(firefox),))):
        assert restrictions.dev_allow_firefox()
        assert not restrictions.installed_browser_blocks()
        ops.set_enabled(True, guardian_authenticated=True)
        assert not restrictions.dev_allow_firefox()
        assert restrictions.installed_browser_blocks()
        assert 'org.mozilla.firefox' in restrictions.runtime_browser_flatpak_ids()
        mode.return_value = LockState(STATE_VERSION, LockMode.UNLOCKED)
        assert restrictions.dev_allow_firefox()


def test_status_is_read_only_without_pin(selection):
    with patch.object(rpc, 'verify_guardian_pin') as verify:
        assert request('guardian.websites.policy')['ok']
        verify.assert_not_called()


def test_initialize_before_chrome_projection(selection):
    with (patch.object(browser_ops, 'get_settings'),
          patch.object(ops, 'initialize_guardian_websites') as init,
          patch.object(browser_ops, 'apply_browser_policy', side_effect=lambda: init.assert_called_once())):
        browser_ops.initialize_browser_policy()


@pytest.mark.parametrize('action,new_mode', [
    ('state.exit_guardian_lock', LockMode.UNLOCKED),
    ('state.enter_guardian_lock', LockMode.GUARDIAN_LOCK),
    ('state.enter_self_lock', LockMode.SELF_LOCK),
])
def test_mode_changes_refresh_browser_and_browser_blocks(selection, action, new_mode):
    _, state, browser, apps = selection
    ops.set_enabled(True, guardian_authenticated=True)
    browser.reset_mock()
    apps.reset_mock()
    current = LockState(STATE_VERSION, LockMode.GUARDIAN_LOCK if new_mode is LockMode.UNLOCKED
                        else LockMode.UNLOCKED)
    new = LockState(STATE_VERSION, new_mode)
    with (patch.object(rpc, 'setup_guardian_pin'),
          patch.object(rpc, 'get_lock_state', return_value=current),
          patch.object(rpc, 'verify_guardian_pin', return_value=True),
          patch.object(rpc, 'exit_guardian_lock', return_value=new),
          patch.object(rpc, 'enter_guardian_lock', return_value=new),
          patch.object(rpc, 'enter_self_lock', return_value=new),
          patch.object(rpc.guardian_app_ops, 'reconcile')):
        params = ({'new_pin': '4826', 'pin_confirmation': '4826'}
                  if new_mode is LockMode.GUARDIAN_LOCK else {'guardian_pin': '1234'})
        assert request(action, params)['ok']
    browser.assert_called_once()
    apps.assert_called_once()


def test_failed_state_write_keeps_prior_selection(selection):
    path, _, browser, apps = selection
    before = path.read_bytes()
    with patch.object(ops, 'atomic_write', side_effect=OSError('disk full')):
        with pytest.raises(OSError):
            ops.replace_selection(['school.example.org'], True, guardian_authenticated=True)
    assert path.read_bytes() == before
    assert not ops.active()
    browser.assert_not_called()
    apps.assert_not_called()


def test_website_controls_always_available_under_app_selection():
    from sentinel.guardian_apps import desktop
    key = 'sentinel-guardian-websites.desktop'
    assert key in desktop.BUILTIN_IDS
    entry = {'desktop_id': key, 'source': '[Desktop Entry]\nType=Application\nName=Websites\nExec=evil\n'}
    rendered = desktop.render_launcher(entry, True)
    assert 'Exec=/usr/lib/sentinel/venv/bin/python -I -m sentinel.guardian_websites.manager' in rendered
    assert 'Exec=evil' not in rendered
    assert 'NoDisplay=true' in rendered


def test_portal_launcher_remains_direct_and_available_for_outage_recovery():
    from sentinel.guardian_apps import desktop
    entry = {'desktop_id': 'sentinel-portal.desktop',
             'source': '[Desktop Entry]\nType=Application\nName=Portal\nExec=/tmp/evil\n'}
    rendered = desktop.render_launcher(entry, True)
    assert 'Exec=/usr/lib/sentinel/venv/bin/python -I -B -m sentinel.portal.main\n' in rendered
    assert 'sentinel.guardian_apps.launch' not in rendered
    assert 'NoDisplay=false' in rendered


def test_cli_commands_forward_values_and_use_authenticated_invoke():
    import typer
    from typer.testing import CliRunner
    from sen.cli.guardian_websites import register_guardian_website_commands
    from sentinel.daemon import client
    calls = []
    def invoke(fn, *args, **kwargs):
        calls.append((fn, args, kwargs))
        return {'domains': ['school.example.org']}
    app = typer.Typer()
    register_guardian_website_commands(app, invoke=invoke)
    for command, fn, args in [('approve school.example.org', client.approve_guardian_website, ('school.example.org',)),
                              ('revoke school.example.org', client.revoke_guardian_website, ('school.example.org',)),
                              ('enable', client.set_guardian_websites_enabled, (True,)),
                              ('disable', client.set_guardian_websites_enabled, (False,)),
                              ('status', client.get_guardian_website_policy, ()),
                              ('list', client.get_guardian_website_policy, ())]:
        result = CliRunner().invoke(app, ['guardian-websites', *command.split()])
        assert result.exit_code == 0, result.output
        assert calls[-1] == (fn, args, {'authenticate': False} if command in {'status', 'list'} else {})
