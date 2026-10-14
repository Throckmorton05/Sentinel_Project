"""Isolated settings/RPC/enforcement regression tests. No live policies or Steam."""
import itertools
import json
import stat
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from sentinel.browser_policies import chrome
from sentinel.daemon.operations import browser_ops, settings_ops
from sentinel.daemon.core.lock_state import LockMode, LockState, STATE_VERSION
from sentinel.daemon.security import policy
from sentinel.daemon.rpc import request_handler
from sentinel.daemon.supervision import steam
from sentinel.daemon.supervision.state import _stop_event
from sentinel.extension_catalog.catalog import parse_inventory
from sentinel.safe_steam import assets


@pytest.fixture(autouse=True)
def installed_extension_inventory():
    inventory = parse_inventory((Path(__file__).resolve().parents[3] / "configs/browser/approved_extensions.toml").read_text())
    with (patch.object(chrome, "catalog_extension_id", return_value="a" * 32),
          patch.object(chrome, "get_lock_state", return_value=LockState(STATE_VERSION, LockMode.UNLOCKED)),
          patch.object(chrome, "load_inventory", return_value=inventory),
          patch.object(steam, "native_steam_installed", return_value=True),
          patch.object(steam, "running_extension_path", return_value=None),
          patch.object(assets, "install_chrome_assets"),
          patch.object(assets, "native_update_needed", return_value=False)):
        yield


@pytest.mark.parametrize("name", ["safe_steam", "safe_crunchyroll"])
@pytest.mark.parametrize("mode,old,new,authenticated,allowed", [
    (LockMode.UNLOCKED, False, True, False, True),
    (LockMode.UNLOCKED, True, False, False, True),
    (LockMode.SELF_LOCK, False, True, False, True),
    (LockMode.SELF_LOCK, True, False, False, False),
    (LockMode.SELF_LOCK, True, True, False, True),
    (LockMode.GUARDIAN_LOCK, False, True, False, False),
    (LockMode.GUARDIAN_LOCK, True, False, False, False),
    (LockMode.GUARDIAN_LOCK, True, False, True, True),
    (LockMode.GUARDIAN_LOCK, False, True, True, True),
])
def test_lock_matrix_through_rpc(tmp_path, name, mode, old, new, authenticated, allowed):
    path = tmp_path / "settings.toml"
    path.write_text(f"{name} = {str(old).lower()}\n")
    state = LockState(version=STATE_VERSION, mode=mode)
    def mutate(n, v, **kwargs):
        return settings_ops.set_setting(n, v, path=path, **kwargs)
    with (
        patch.object(policy, "get_lock_state", return_value=state),
        patch.object(request_handler, "get_lock_state", return_value=state),
        patch.object(request_handler, "verify_guardian_pin", return_value=authenticated),
        patch.object(request_handler, "_set_setting", side_effect=mutate),
        patch.object(browser_ops, "apply_browser_policy") as apply,
    ):
        response = request_handler._handle_request({"request_id": "browser-test", "action": "settings.set",
            "params": {"name": name, "value": new, "guardian_pin": "test",
                       "guardian_authenticated": True}}, uid=1000, pid=22)
    assert response["ok"] is allowed
    assert settings_ops._load_settings(path)[name] is (new if allowed else old)
    assert apply.call_count == int(allowed and (new != old or name == "safe_steam"))
    if allowed:
        assert response["result"]["change"] == ("maintain" if old == new else "strengthen" if new else "weaken")
        assert "application" in response["result"]


def test_browser_defaults_and_bad_types():
    assert settings_ops.browser_snapshot({}) == dict.fromkeys(settings_ops.BROWSER_SETTINGS, True)
    for name in settings_ops.BROWSER_SETTINGS:
        for value in ("false", 0, 1, None):
            with pytest.raises(ValueError):
                settings_ops.browser_snapshot({name: value})


@pytest.mark.parametrize("values", list(itertools.product([False, True], repeat=3)))
def test_chrome_protections_stay_independent(values):
    snapshot = dict(zip(settings_ops.BROWSER_SETTINGS, values))
    result = chrome.build_chrome_policy(snapshot)
    assert len(result["ExtensionInstallForcelist"]) == 4
    # Ongoing updates must work even for old installed packages that lack
    # manifest update_url, and while filtering is switched off.
    overrides = result["ExtensionSettings"]
    assert set(overrides) == {"*"} | set(result["ExtensionInstallAllowlist"])
    assert overrides["*"] == {"installation_mode": "blocked"}
    for entry in result["ExtensionInstallForcelist"]:
        extension_id, url = entry.split(";", 1)
        expected = {
            "installation_mode": "force_installed",
            "update_url": url,
            "override_update_url": True,
        }
        if extension_id == "a" * 32:
            expected["toolbar_pin"] = "force_pinned"
        assert overrides[extension_id] == expected
    assert overrides["aghfnjkcakhmadgdomlmlhhaocbkloab"] == {"installation_mode": "allowed"}
    assert "DefaultSearchProviderNewTabURL" not in result
    assert ("ForceGoogleSafeSearch" in result) is snapshot["safe_browsing"]
    assert result["IncognitoModeAvailability"] == 1
    assert result["ExtensionInstallBlocklist"] == ["*"]
    assert result["DnsOverHttpsMode"] == "off"
    assert result["BrowserGuestModeEnabled"] is False
    assert ("ForceYouTubeRestrict" in result) is snapshot["safe_browsing"]
    if snapshot["safe_browsing"]:
        assert result["ForceYouTubeRestrict"] == 1
    assert "YouTubeRestrict" not in result
    assert result["DefaultSearchProviderSearchURL"] == "https://www.google.com/search?q={searchTerms}"
    assert result["ProxyMode"] == "direct"
    assert result["ExtensionDeveloperModeSettings"] == 1
    assert "bing.com" in result["URLBlocklist"]


def test_pending_policy_retries_persisted_snapshot():
    snapshot = {"safe_browsing": False, "safe_steam": True, "safe_crunchyroll": False}
    with patch.object(browser_ops, "get_settings", return_value=snapshot), patch.object(browser_ops, "apply_chrome_policy") as apply:
        apply.side_effect = OSError("disk unavailable")
        assert browser_ops.apply_browser_policy()["status"] == "pending"
        assert "disk unavailable" in browser_ops.application_status()["error"]
        apply.side_effect = None
        result = browser_ops.apply_browser_policy()
    assert result["status"] == "applied" and result["error"] is None
    assert result["last_applied_at"] is not None
    apply.assert_called_with(snapshot)


def test_atomic_policy_only_writes_sentinel_file(tmp_path):
    directory = tmp_path / "managed"
    directory.mkdir()
    sentinel = directory / "sentinel.json"
    unrelated = directory / "company.json"
    unrelated.write_text('{"Example":true}')
    with patch.object(chrome, "CHROME_POLICY_FILE", sentinel), patch("sentinel.dns.storage.protect_directory"):
        chrome.apply_chrome_policy(dict.fromkeys(settings_ops.BROWSER_SETTINGS, True))
    assert unrelated.read_text() == '{"Example":true}'
    assert json.loads(sentinel.read_text())["ForceGoogleSafeSearch"] is True
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o644


def test_policy_read_rejects_unprotected_or_malformed_settings():
    with patch.object(browser_ops, "read_protected", side_effect=ValueError("unprotected")):
        with pytest.raises(ValueError, match="unprotected"):
            browser_ops.get_settings()
    with patch.object(browser_ops, "read_protected", return_value='safe_steam = "false"'):
        with pytest.raises(ValueError):
            browser_ops.get_settings()


def test_browser_sync_guardian_and_status_read():
    state = LockState(version=STATE_VERSION, mode=LockMode.GUARDIAN_LOCK)
    with (patch.object(policy, "get_lock_state", return_value=state),
          patch.object(request_handler, "verify_guardian_pin", return_value=False),
          patch.object(browser_ops, "apply_browser_policy") as apply,
          patch.object(browser_ops, "get_status", return_value={"settings": {}})):
        for action, allowed in (("browser.sync", False), ("browser.status", True)):
            response = request_handler._handle_request({"request_id": "x", "action": action, "params": {}}, uid=1000, pid=2)
            assert response["ok"] is allowed
        apply.assert_not_called()


@pytest.mark.parametrize('enabled', [True, False])
def test_daemon_runs_bounded_steam_checks_as_user(enabled):
    _stop_event.clear()
    child = Mock()
    child.poll.return_value = 0
    child.returncode = 0
    with (patch.object(browser_ops, "get_settings", return_value={"safe_steam": enabled}),
          patch.object(steam, "_get_graphical_session_id", return_value="2"),
          patch.object(steam, "_user_session_command", return_value=["user-command"]) as command,
          patch.object(steam.subprocess, "Popen", return_value=child) as start):
        steam.check_steam(1000)
    assert command.call_args.args[0] == 1000
    assert command.call_args.args[-4:] == ("-I", "-m", "sentinel.safe_steam.watchdog", "--once")
    assert start.call_args.kwargs["cwd"] == "/"
    assert start.call_args.kwargs["start_new_session"] is True


def test_absent_steam_never_starts_check():
    with patch.object(steam, 'native_steam_installed', return_value=False), patch.object(steam.subprocess, "Popen") as start:
        steam.check_steam(1000)
    start.assert_not_called()


def test_policy_outage_does_not_disable_steam_supervision():
    _stop_event.clear()
    child = Mock()
    child.poll.return_value = 0
    child.returncode = 0
    with (patch.object(browser_ops, "get_settings", side_effect=ValueError("bad policy")),
          patch.object(steam, "_get_graphical_session_id", return_value="2"),
          patch.object(steam, "_user_session_command", return_value=["user-command"]),
          patch.object(steam.subprocess, "Popen", return_value=child) as start):
        steam.check_steam(1000)
    start.assert_called_once()


def test_stopped_child_is_killed_and_reaped_before_retry():
    _stop_event.clear()
    child = Mock(pid=4242)
    child.poll.return_value = None
    child.returncode = -9
    with (patch.object(browser_ops, "get_settings", return_value={"safe_steam": True}),
          patch.object(steam, "_get_graphical_session_id", return_value="2"),
          patch.object(steam, "_user_session_command", return_value=["user-command"]),
          patch.object(steam.subprocess, "Popen", return_value=child),
          patch.object(steam.time, "monotonic", side_effect=[0.0, 36.0]),
          patch.object(steam._stop_event, "wait", return_value=False),
          patch.object(steam.os, "killpg") as kill):
        steam.check_steam(1000)
    kill.assert_called_once_with(4242, steam.signal.SIGKILL)
    child.wait.assert_called_once_with(timeout=3.0)
